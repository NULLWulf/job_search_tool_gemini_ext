"""Clients for broad job aggregators (keyword+location search across many
employers), as opposed to ats_clients.py which is one board per company.

Same output shape as ats_clients.py:
    {"company": str, "title": str, "location": str, "url": str, "posted_at": str|None}

Same sandbox caveat as ats_clients.py: outbound calls to these domains were
not testable live from this cloud dev environment (network allowlist), only
via WebFetch during research. Run for real on a machine with normal internet.
"""
import os
import re

import httpx

from app import filters

USER_AGENT = "job-search-pipeline/0.1 (personal use)"
TIMEOUT = 20.0
FULL_JD_TIMEOUT = 10.0  # shorter: this is a best-effort extra request per job, don't let one slow host stall the run

# Companies discovered mid-run: fetch_full_description resolved an Adzuna
# redirect to a Greenhouse/Lever slug we don't already track in
# companies.yaml. Module-level rather than threaded through every return
# value because fetch_adzuna's return shape (list[dict] of jobs) is a
# shared contract with fetch_remotive/FETCHERS/fetch_aggregator — see
# discover_companies.py for what reads this after a run. Each process run
# starts with an empty list, so there's no cross-run leakage to worry about.
DISCOVERED_COMPANIES: list[dict] = []

_GREENHOUSE_URL_RE = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)")
_LEVER_URL_RE = re.compile(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")

# NOTE: redirect_url is ALWAYS an adzuna.* URL (their own tracking/details
# link) whether or not it goes on to redirect somewhere else — so there's
# no way to tell "self-hosted, terminal" apart from "tracking link that
# 302s to a real ATS" by looking at redirect_url's domain alone. An
# earlier version of this file tried exactly that (skip anything on
# adzuna.*) and it was wrong: it would've skipped the Greenhouse/Lever
# redirects too, since those also start as adzuna.* links before
# following through. The only way to tell them apart is to actually
# attempt the request and see what comes back — see fetch_full_description
# below (and its KNOWN LIMITATION note for the case where it stays on
# adzuna.* and 403s).


def _collapse_whitespace(text: str) -> str:
    """Scraped page text turns every stripped block-level tag into its own
    line, so a page with a search widget/sidebar/nav (see fetch_adzuna's
    Adzuna-details-page fallback) comes back as mostly blank lines around a
    handful of real content lines. Strip each line and collapse RUNS of
    blank lines down to one — not zero, since a single blank line is often
    a legitimate paragraph break (e.g. Ashby's descriptionPlain) that's
    worth keeping, not layout noise to remove entirely."""
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _html_page_to_text(html: str) -> str:
    """Rough main-content extraction for a page we don't have a structured
    API for: drop script/style blocks (JS/CSS text isn't content), strip
    remaining tags, collapse whitespace. Will include some nav/footer
    boilerplate on sites without a recognized ATS — still far more useful
    to the AI evaluation step than a one-sentence aggregator snippet."""
    html = _SCRIPT_STYLE_RE.sub(" ", html or "")
    return _collapse_whitespace(filters.strip_html(html).strip())


def fetch_via_browser(url: str, timeout_ms: int = 20000) -> str | None:
    """Last-resort fallback for pages a plain httpx GET can't get real
    content from. UPDATED DIAGNOSIS (2026-08-12): the earlier theory here
    was a Cloudflare bot-challenge (based on this sandbox's WebFetch
    getting 403 on adzuna.ca) — but a direct httpx print from
    fetch_full_description showed a plain 200 with just an empty PAGE
    SKELETON, not a 403 or a challenge page. adzuna.ca/details/ pages are
    a client-rendered SPA: the initial HTML response is a near-empty shell
    and the actual job content gets injected by JavaScript after load —
    httpx never executes JS, so it can NEVER see that content no matter
    what headers/User-Agent it sends. This is why wait_until="networkidle"
    matters below (waits for the page's own JS/API calls to finish
    populating content) rather than "domcontentloaded" (fires as soon as
    the empty shell HTML is parsed, before any of that has happened) — an
    earlier version of this function used domcontentloaded and would have
    captured the same empty skeleton a plain httpx GET already sees,
    making the whole fallback pointless. (WebFetch's 403 might still be a
    separate, genuine bot-block layered on top for that specific tool/UA —
    unconfirmed either way; the SPA-skeleton issue is the one a real
    browser actually needs to solve, and does, by executing the JS.)

    Only called from fetch_full_description as a fallback when the plain
    request path found nothing useful — and that's only ever reached from
    fetch_adzuna's already-gated call site (title/location passed, snippet
    looks truncated), so this doesn't fire for the bulk of raw results,
    only the smallish set that already look like real candidates. Still
    meaningfully slower than an httpx call (browser launch + full page
    render), so keep that gating in place upstream — don't call this
    unguarded.

    Requires the optional `playwright` dependency (`pip install playwright
    && playwright install chromium` — see requirements.txt). Returns None
    if playwright isn't installed, the page errors, or the page loads but
    still has nothing to show for it (e.g. network never goes idle within
    the timeout, or there really is a bot-challenge underneath the SPA
    shell) — same "fail quietly, caller falls back to the short snippet"
    contract as the rest of this module. NOT verified against the real
    adzuna.ca from this sandbox (no network access here to test with) —
    try it for real and report back what you see."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                # networkidle, not domcontentloaded: the empty SPA shell
                # parses (and "DOM content loaded") almost instantly — the
                # real job content only shows up after the page's own JS
                # finishes fetching and rendering it, which is what
                # networkidle actually waits for.
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                body_text = page.inner_text("body")
            finally:
                browser.close()
    except Exception:
        return None

    return _collapse_whitespace(body_text) or None


def fetch_full_description(redirect_url: str) -> dict:
    """Best-effort: follow an aggregator's redirect_url to the real posting
    and pull the full JD text. Adzuna's search API only ever returns a
    short teaser snippet — there's no "give me the full text" parameter —
    so this is the only way to get the real description.

    KNOWN LIMITATION, PARTIALLY MITIGATED (2026-08-12): when Adzuna is the
    terminal host for a job (no external ATS — the request lands on their
    own adzuna.ca/details/... page and stays there), a plain httpx request
    gets 403'd — confirmed with two real listings, and their API doesn't
    expose a full-description field anywhere either (checked their docs).
    As of 2026-08-12 this now falls back to a real headless browser (see
    fetch_via_browser) for that case, which has a real shot at clearing
    Cloudflare's bot-challenge where httpx has none — but isn't guaranteed
    (Cloudflare fingerprints headless browsers too) and hasn't been
    verified live from this sandbox (no network access here to test with).
    If it still comes back empty, jobs fall through to the short snippet
    unchanged, and ai_evaluate.py's looks_truncated() note is what keeps
    that from tanking match_score. This DOES still recover the full JD via
    the fast path for listings whose redirect_url is a tracking link that
    goes on to a recognized ATS (Greenhouse/Lever confirmed working; see
    below) — there's no reliable way to tell the two cases apart in
    advance, so every job still gets a real attempt at the fast path first.

    Returns {"description": str|None, "ats": str|None, "slug": str|None}.
    `description` is None only if EVERY path failed (plain request, known-
    ATS API, generic scrape, AND the browser fallback); callers must still
    fall back to the short snippet rather than let one bad job kill the
    run. `ats`/`slug` are set ONLY when the redirect resolved to a
    recognized ATS's own single-job API call that actually succeeded —
    that's the signal discover_companies.py uses to suggest adding the
    company to companies.yaml, since the slug is verified by a real
    successful request, not guessed from a URL (see companies.yaml's
    Coveo/Treewalk history for why that distinction matters — a slug
    parsed off a search-result URL burned real API calls on 404s twice
    before being caught).

    Prefers the source ATS's own JSON API (single-job endpoint — cheap,
    structured, matches what ats_clients.py already parses) when the
    redirect lands on a recognized board; falls back to scraping whatever
    HTML the final page returns; falls back further to a real browser if
    that HTML was empty/useless or the initial request failed outright."""
    empty = {"description": None, "ats": None, "slug": None}
    if not redirect_url:
        return empty

    resp = None
    try:
        resp = httpx.get(
            redirect_url, headers={"User-Agent": USER_AGENT},
            timeout=FULL_JD_TIMEOUT, follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception:
        resp = None  # plain request failed outright (403/timeout/DNS/etc) -> try the browser fallback below

    if resp is not None:
        final_url = str(resp.url)

        m = _GREENHOUSE_URL_RE.search(final_url)
        if m:
            slug, job_id = m.groups()
            try:
                job_resp = httpx.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
                    params={"content": "true"}, headers={"User-Agent": USER_AGENT}, timeout=FULL_JD_TIMEOUT,
                )
                job_resp.raise_for_status()
                content = job_resp.json().get("content")
                if content:
                    return {"description": content, "ats": "greenhouse", "slug": slug}
            except Exception:
                pass  # fall through to generic HTML scrape / browser fallback below

        m = _LEVER_URL_RE.search(final_url)
        if m:
            slug, posting_id = m.groups()
            try:
                job_resp = httpx.get(
                    f"https://api.lever.co/v0/postings/{slug}/{posting_id}",
                    params={"mode": "json"}, headers={"User-Agent": USER_AGENT}, timeout=FULL_JD_TIMEOUT,
                )
                job_resp.raise_for_status()
                data = job_resp.json()
                content = data.get("descriptionPlain") or data.get("description")
                if content:
                    return {"description": content, "ats": "lever", "slug": slug}
            except Exception:
                pass

        text = _html_page_to_text(resp.text)
        if text:
            return {"description": text, "ats": None, "slug": None}

    # Either the plain request failed outright, or it succeeded but there
    # was nothing usable in the response (e.g. a bot-challenge shell page
    # with no real content) — last resort, try a real browser.
    browser_text = fetch_via_browser(redirect_url)
    return {"description": browser_text, "ats": None, "slug": None}


ADZUNA_SUPPORTED_COUNTRIES = {
    "gb", "us", "at", "au", "br", "ca", "de", "fr", "in", "it", "nl", "nz", "pl", "ru", "sg", "za"
}

_COUNTRY_INFERENCE_RULES = [
    (re.compile(r"\b(us|usa|united states|north carolina|nc|raleigh|durham|chapel hill|cary|california|new york|texas|florida|virginia|georgia|washington|seattle|austin|san francisco|chicago)\b", re.IGNORECASE), "us"),
    (re.compile(r"\b(canada|canadian|alberta|ontario|quebec|british columbia|bc|toronto|vancouver|calgary|edmonton|ottawa|montreal)\b", re.IGNORECASE), "ca"),
    (re.compile(r"\b(uk|united kingdom|great britain|england|scotland|wales|london|manchester|birmingham)\b", re.IGNORECASE), "gb"),
    (re.compile(r"\b(australia|sydney|melbourne|brisbane|perth)\b", re.IGNORECASE), "au"),
    (re.compile(r"\b(germany|deutschland|berlin|munich|münchen|frankfurt|hamburg)\b", re.IGNORECASE), "de"),
    (re.compile(r"\b(france|paris|lyon)\b", re.IGNORECASE), "fr"),
    (re.compile(r"\b(india|bangalore|bengaluru|mumbai|delhi|hyderabad|pune)\b", re.IGNORECASE), "in"),
    (re.compile(r"\b(netherlands|holland|amsterdam|rotterdam)\b", re.IGNORECASE), "nl"),
    (re.compile(r"\b(new zealand|auckland|wellington)\b", re.IGNORECASE), "nz"),
    (re.compile(r"\b(poland|polska|warsaw|warszawa|krakow|kraków)\b", re.IGNORECASE), "pl"),
    (re.compile(r"\b(singapore)\b", re.IGNORECASE), "sg"),
    (re.compile(r"\b(south africa|johannesburg|cape town)\b", re.IGNORECASE), "za"),
    (re.compile(r"\b(brazil|brasil|sao paulo|são paulo|rio)\b", re.IGNORECASE), "br"),
    (re.compile(r"\b(austria|österreich|vienna|wien)\b", re.IGNORECASE), "at"),
    (re.compile(r"\b(italy|italia|rome|roma|milan|milano)\b", re.IGNORECASE), "it"),
]


def infer_adzuna_country(params: dict) -> str:
    """Infer the 2-letter Adzuna country code without hardcoding.

    Order of precedence:
    1. Explicit 'country' in params (e.g., country: us in aggregators.yaml).
    2. Inferred from 'where' in params (city, state, or country name).
    3. Inferred from ADZUNA_COUNTRY environment variable.
    4. Inferred from filters.yaml's location_allow_patterns.
    5. Fallback to 'us'.
    """
    if "country" in params and params["country"]:
        val = str(params["country"]).strip().lower()
        if val in ADZUNA_SUPPORTED_COUNTRIES or len(val) == 2:
            return val

    where = str(params.get("where", "")).strip()
    if where:
        if where.lower() in ADZUNA_SUPPORTED_COUNTRIES:
            return where.lower()
        for pattern, code in _COUNTRY_INFERENCE_RULES:
            if pattern.search(where):
                return code

    env_country = os.environ.get("ADZUNA_COUNTRY", "").strip().lower()
    if env_country:
        return env_country

    try:
        patterns = filters._config.get("location_allow_patterns", [])
        combined = " ".join(patterns)
        for pattern, code in _COUNTRY_INFERENCE_RULES:
            if pattern.search(combined):
                return code
    except Exception:
        pass

    return "us"


def fetch_adzuna(params: dict) -> list[dict]:
    """Adzuna paginates — one page is only `results_per_page` (default 50)
    results, and the free tier's ranking means good matches can be a few
    pages deep. Fetches up to `max_pages` (default 5, set per-aggregator in
    aggregators.yaml), stopping early if a page comes back short (signals
    we've hit the end of Adzuna's result set for that query)."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError(
            "ADZUNA_APP_ID / ADZUNA_APP_KEY env vars not set — "
            "register for free at https://developer.adzuna.com"
        )

    params = dict(params)  # don't mutate the caller's dict (reused across runs)
    country = infer_adzuna_country(params)
    params.pop("country", None)
    max_pages = params.pop("max_pages", 5)
    results_per_page = params.get("results_per_page", 50)

    jobs = []
    for page in range(1, max_pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
        query = {
            "app_id": app_id,
            "app_key": app_key,
            "content-type": "application/json",
            **params,
        }
        resp = httpx.get(url, params=query, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            break

        for j in results:
            loc = (j.get("location") or {}).get("display_name", "")
            title = j.get("title", "")
            redirect_url = j.get("redirect_url", "")
            snippet = j.get("description", "")  # Adzuna gives a short snippet, not the full JD

            description = snippet
            company_name = (j.get("company") or {}).get("display_name", "Unknown")
            # Only worth the extra request for jobs that already look like
            # real candidates (title+location pass) AND whose snippet is
            # actually thin — most of Adzuna's ~2000 raw results get
            # dropped by title/location alone, so gating on that first
            # keeps this from turning into ~2000 extra HTTP requests a run.
            if (
                filters.title_is_relevant(title)
                and filters.location_is_allowed(loc)
                and filters.looks_truncated(snippet)
            ):
                full = fetch_full_description(redirect_url)
                if full["description"] and len(full["description"]) > len(snippet):
                    description = full["description"]
                if full["ats"] and full["slug"]:
                    DISCOVERED_COMPANIES.append({
                        "name": company_name, "ats": full["ats"], "slug": full["slug"],
                    })

            jobs.append({
                "company": company_name,
                "title": title,
                "location": loc,
                "url": redirect_url,
                "posted_at": j.get("created"),
                "description": description,
            })

        if len(results) < results_per_page:
            break  # short page -> no more results, stop paginating early
    return jobs


def fetch_remotive(params: dict) -> list[dict]:
    url = "https://remotive.com/api/remote-jobs"
    resp = httpx.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "company": j.get("company_name", "Unknown"),
            "title": j.get("title", ""),
            "location": j.get("candidate_required_location", ""),
            "url": j.get("url", ""),
            "posted_at": j.get("publication_date"),
            "description": j.get("description", ""),  # full HTML per Remotive's docs
        })
    return jobs


FETCHERS = {
    "adzuna": fetch_adzuna,
    "remotive": fetch_remotive,
}


def fetch_aggregator(aggregator: dict) -> list[dict]:
    fetcher = FETCHERS.get(aggregator["type"])
    if fetcher is None:
        raise ValueError(f"No fetcher for aggregator type: {aggregator['type']}")
    jobs = fetcher(aggregator.get("params", {}))
    for j in jobs:
        j["source"] = aggregator["type"]
    return jobs