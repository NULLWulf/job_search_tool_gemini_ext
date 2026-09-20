"""Deterministic pre-filters: title allowlist + exclusion keywords +
location allowlist + JD stack dealbreakers.

These run BEFORE any AI call — the whole point is to keep the AI-scored
volume small and cheap. The actual keyword/pattern lists live in
`filters.yaml` at the repo root (loaded below) so they can be edited
without touching code.

Design note (2026-08-11 rewrite): an EXCLUSION-only approach doesn't scale
to a company like Affirm that posts hundreds of non-engineering roles
(Analyst, Compliance, Marketing, Customer Success, Talent Brand, Sales
Development...) — you can't blacklist your way out of that. So title
filtering is allowlist-first: the title must look like an engineering
role AT ALL before we even consider it, then a smaller exclusion list
catches engineering-adjacent titles that aren't a fit (sales engineer,
hardware/mechanical engineer, etc).
"""
import os
import re
import sys

import yaml

DEFAULT_CONFIG_PATH = "filters.yaml"
EXAMPLE_CONFIG_PATH = "filters.example.yaml"


def resolve_config_path(path: str | None = None) -> str:
    """Resolve the path to the filters configuration file.

    Priority:
    1. Explicit path parameter if passed.
    2. FILTERS_CONFIG_PATH environment variable.
    3. If running in a test environment (pytest in modules or PYTEST_CURRENT_TEST set),
       prefer filters.example.yaml if it exists.
    4. filters.yaml if it exists (local user-customized config).
    5. filters.example.yaml fallback if filters.yaml does not exist.
    6. Default to filters.yaml.
    """
    if path:
        return path
    if env_path := os.getenv("FILTERS_CONFIG_PATH"):
        return env_path
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        if os.path.exists(EXAMPLE_CONFIG_PATH):
            return EXAMPLE_CONFIG_PATH
    if os.path.exists(DEFAULT_CONFIG_PATH):
        return DEFAULT_CONFIG_PATH
    if os.path.exists(EXAMPLE_CONFIG_PATH):
        return EXAMPLE_CONFIG_PATH
    return DEFAULT_CONFIG_PATH


def _compile_keyword_alternation(keywords: list[str], config_key: str) -> re.Pattern:
    # An empty keyword list joins to "" and compiles to "()", which matches
    # the empty string at every position — .search() would then match ANY
    # title, silently turning an allowlist into "allow everything" and an
    # exclusion list into "exclude everything". Fail loudly instead.
    if not keywords:
        raise ValueError(
            f"filters config '{config_key}' is empty — this would silently "
            "match every title instead of none. Add at least one keyword."
        )
    return re.compile("(" + "|".join(re.escape(k) for k in keywords) + ")", re.IGNORECASE)


def load_config(path: str | None = None) -> dict:
    """Load configuration from YAML and compile all matching regexes.
    Can be called directly to switch configurations (e.g., during tests)."""
    global FILTERS_CONFIG_PATH
    global PRIORITY_TITLE_KEYWORDS, TITLE_ALLOW_KEYWORDS, EXCLUSION_KEYWORDS
    global LOCATION_ALLOW_PATTERNS, STACK_DEALBREAKERS, STACK_CORE
    global TRUNCATED_DESCRIPTION_MIN_CHARS
    global _priority_re, _title_allow_re, _exclusion_re
    global _location_res, _dealbreaker_res, _core_res

    resolved_path = resolve_config_path(path)
    with open(resolved_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    FILTERS_CONFIG_PATH = resolved_path
    PRIORITY_TITLE_KEYWORDS = config["priority_title_keywords"]
    TITLE_ALLOW_KEYWORDS = config["title_allow_keywords"]
    EXCLUSION_KEYWORDS = config["exclusion_keywords"]
    LOCATION_ALLOW_PATTERNS = config["location_allow_patterns"]
    STACK_DEALBREAKERS = config["stack_dealbreakers"]
    STACK_CORE = config["stack_core"]
    TRUNCATED_DESCRIPTION_MIN_CHARS = config["truncated_description_min_chars"]

    _priority_re = _compile_keyword_alternation(PRIORITY_TITLE_KEYWORDS, "priority_title_keywords")
    _title_allow_re = _compile_keyword_alternation(TITLE_ALLOW_KEYWORDS, "title_allow_keywords")
    _exclusion_re = _compile_keyword_alternation(EXCLUSION_KEYWORDS, "exclusion_keywords")
    _location_res = [re.compile(p, re.IGNORECASE) for p in LOCATION_ALLOW_PATTERNS]
    _dealbreaker_res = [re.compile(p, re.IGNORECASE) for p in STACK_DEALBREAKERS]
    _core_res = [re.compile(p, re.IGNORECASE) for p in STACK_CORE]

    return config


# Backwards-compatible alias
_load_config = load_config

# Initialize config at import time
_config = load_config()

_html_tag_re = re.compile(r"<[^>]+>")


def strip_html(html_or_text: str) -> str:
    """Cheap HTML-to-text: good enough for keyword matching, not for display."""
    if not html_or_text:
        return ""
    return _html_tag_re.sub(" ", html_or_text)


def looks_truncated(description: str) -> bool:
    """True if `description` looks like a short aggregator teaser rather
    than a full job posting — either it visibly cuts off mid-sentence, or
    it's just too short to contain real requirements/responsibilities.

    Shared by aggregator_clients.py (decide whether it's worth the extra
    request to fetch a full JD) and ai_evaluate.py (fall back to telling
    the model the description is partial, for the cases a full-JD fetch
    still couldn't recover — blocked site, dead link, genuinely short
    posting)."""
    text = strip_html(description or "").strip()
    if not text:
        return True
    if text.endswith("…") or text.endswith("...") or text.rstrip().endswith(".."):
        return True
    return len(text) < TRUNCATED_DESCRIPTION_MIN_CHARS


def title_is_relevant(title: str) -> bool:
    title = title or ""
    if _priority_re.search(title):
        # Unambiguous engineering title — e.g. "Software Engineer, Automated
        # Marketing" — skip the exclusion list entirely so a word like
        # "marketing" elsewhere in the title can't veto it.
        return True
    if not _title_allow_re.search(title):
        return False
    if _exclusion_re.search(title):
        return False
    return True


def location_is_allowed(location: str) -> bool:
    loc = location or ""
    return any(p.search(loc) for p in _location_res)


def jd_stack_mismatch(description: str) -> bool:
    """True if the JD looks like a dealbreaker-language role with no
    mention of your own core stack. Only meaningful when `description` is
    non-empty — callers should treat an empty description as "unknown,
    don't reject on stack alone"."""
    text = strip_html(description)
    if not text:
        return False
    has_dealbreaker = any(p.search(text) for p in _dealbreaker_res)
    has_core = any(p.search(text) for p in _core_res)
    return has_dealbreaker and not has_core


def passes_filters(job: dict) -> bool:
    """job must have 'title' and 'location' keys (plain strings); may
    optionally have a 'description' key (HTML or plain text)."""
    if not title_is_relevant(job.get("title", "")):
        return False
    if not location_is_allowed(job.get("location", "")):
        return False
    if jd_stack_mismatch(job.get("description", "")):
        return False
    return True
