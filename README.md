# AI-Powered Job Search Automation


An automated pipeline for a targeted job search: fetch open roles directly
from companies' ATS APIs and job aggregators, filter out anything
irrelevant, deduplicate against everything already seen, optionally score
each candidate's fit against your own experience with Claude, and review
the results in a local web board.

Nothing here talks to any service other than the job sources you configure
and (for the optional AI step) the Anthropic API. All state — scraped
jobs, scores, your own notes — lives in a local SQLite database; nothing
is sent to a third party beyond fetching the postings themselves.

<img width="2532" height="1215" alt="image" src="https://github.com/user-attachments/assets/679cea89-3156-4e46-af3c-74d3e0d9b30c" />

## How it works

1. **Fetch** (`app/main.py`) — pulls open roles from:
   - `companies.yaml` — known companies queried directly via their ATS
     (Greenhouse, Ashby, Workable, Lever, SmartRecruiters) — precise, low
     noise.
   - `aggregators.yaml` — broad keyword+location search across many
     employers at once (Adzuna, Remotive) — wider reach, more noise.
2. **Filter** (`app/filters.py`) — drops anything that isn't an
   engineering-shaped title, isn't in an allowed location, or whose JD
   requires a stack you've excluded.
3. **Dedup** (`app/dedup.py`) — everything fetched is stored in a local
   SQLite database (`data/seen_jobs.sqlite3`), keyed by URL and by
   normalized company+title, so re-runs only ever surface genuinely new
   postings.
4. **AI evaluation** (`app/ai_evaluate.py`, optional, costs money) — sends
   each filtered candidate plus your `profile.yaml` to Anthropic Claude or Google Gemini, which scores
   fit (0-100) and returns concrete gaps, transferable strengths, risk
   factors, and an apply/consider/skip recommendation.
5. **Review** (`web/`) — a local Next.js app reading/writing the same
   SQLite database directly, for browsing results and tracking your own
   `applied / interview / rejected / skipped / silence` status and notes.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env with the keys you need — see the comments in that file
```

Create your experience profile from the template (this file is gitignored
— it's never committed, since it holds your personal experience):

```bash
cp profile.example.yaml profile.yaml
# then edit profile.yaml with your own background
```

`profile.example.yaml` is a blank template — it tells you the shape but
not the bar. For a fully worked (fictional) example showing the level of
specificity/quantification each evidence bullet should actually have, see
[`profile.sample.yaml`](profile.sample.yaml).

`ANTHROPIC_API_KEY` or `GEMINI_API_KEY` are only needed for the AI evaluation step. Set `AI_PROVIDER` in your `.env` to select which one to use. Adzuna
(`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`) is only needed if you keep an Adzuna
entry in `aggregators.yaml` — register a free key at
[developer.adzuna.com](https://developer.adzuna.com). Remotive needs no
auth but only covers remote roles.

### Customize for your own search

This repo ships pre-configured for the original author's search (Alberta/
Canada, Python/JS stack). **Before your first run, edit these three
files** or you'll get zero candidates, or candidates that don't match your
actual stack:

1. **`filters.yaml` → `location_allow_patterns`** — regex patterns for
   locations to keep. Ships as Canada/Alberta-only; replace with your own
   country/region/cities, or delete entries to broaden it. This is the
   #1 reason a first run returns nothing — if nothing you fetch ever
   matches these patterns, `location_is_allowed()` rejects every job.
2. **`filters.yaml` → `stack_dealbreakers` / `stack_core`** — a JD is
   rejected if it mentions a `stack_dealbreakers` language and none of
   `stack_core`. Ships assuming you want Python/JS and don't want
   Java/C#/.NET/etc. If your own stack includes one of the "dealbreaker"
   languages, move it into `stack_core` (or the filter will reject roles
   in your own stack).
3. **`companies.yaml`** — the company registry. Ships with the original
   author's real target list; add/remove companies to match who you're
   actually applying to (see the file's header comment for the format).
   `aggregators.yaml`'s `where` params are also location-specific —
   update those too if you're not targeting Alberta/Canada.

## Usage

### 1. Fetch + filter (free)

```bash
python -m app.main                    # all companies + aggregators
python -m app.main --company affirm   # just one company, for debugging a fetcher
python -m app.main --skip-aggregators # companies.yaml only
python -m app.main --skip-companies   # aggregators.yaml only

make run                              # same thing, interactive prompts instead of flags
```

Output: `data/candidates.csv`, appended to on every run.

### 2. AI evaluation (optional, costs money)

```bash
python -m app.ai_evaluate --dry-run   # see what's queued, no API calls, no cost
python -m app.ai_evaluate --limit 20  # score just 20, to sanity-check quality/cost first
python -m app.ai_evaluate             # score everything unscored

make evaluate                         # interactive prompts instead of flags
```

Results are stored in SQLite (so re-running never re-pays for a job
already scored) and written to `data/scored_candidates.csv`, sorted
best-match-first.

### 3. Review results

```bash
cd web && npm install    # first time only
cd ..
make web                              # starts the board at http://localhost:3000
```

Reads/writes `data/seen_jobs.sqlite3` directly — no export/import step.
See [`web/README.md`](web/README.md) for details (custom `DB_PATH`,
production build, etc).

## Makefile commands

Thin wrappers over the commands above — run from the repo root:

| Command        | Equivalent to                    | What it does |
|-----------------|----------------------------------|--------------|
| `make run`      | `python -m app.main`             | Fetch + filter (step 1). Interactively asks whether to skip companies/aggregators/discovery and whether to limit to one company slug, instead of you remembering the flags. |
| `make evaluate` | `python -m app.ai_evaluate`       | AI evaluation (step 2). Interactively asks for `--dry-run` and an optional `--limit`. |
| `make web`      | `npm --prefix web run dev`       | Starts the Next.js review board, with `DB_PATH` already pointed at `data/seen_jobs.sqlite3`. |
| `make test`     | `pytest app/` + the standalone sanity-check scripts | Runs the full test suite (see the Tests section below). |

`make` with no target runs `make run` (the default goal).

## Configuration

- **`companies.yaml`** — the company registry (name, ATS type, slug). Add
  a company here once you've identified its ATS.
- **`aggregators.yaml`** — aggregator search config (keywords, location,
  pagination limits).
- **`filters.yaml`** — title allowlist/exclusion keywords, location
  allowlist patterns, and JD stack-dealbreaker/core-stack keywords. Edit
  this directly as you refine what counts as in-scope for you — no code
  changes needed (matching logic lives in `app/filters.py`).
- **`profile.yaml`** — your experience profile fed to the AI evaluation
  step (see `profile.example.yaml` for the blank template and
  `profile.sample.yaml` for a fully worked example).

## Project layout

```
app/
  main.py                 orchestrates fetch -> filter -> dedup -> candidates.csv
  ats_clients.py           one fetch function per ATS
  aggregator_clients.py    one fetch function per aggregator
  filters.py               loads and applies filters.yaml's rules
  dedup.py                 SQLite store (seen_jobs, job_details)
  discover_companies.py    auto-appends newly-resolved companies to companies.yaml
  ai_evaluate.py           stage 2: AI-based fit scoring (Anthropic or Gemini)
  inspect_job.py           CLI to look up a stored job or list recent rejections
  refilter.py              re-runs current filters.py against already-fetched jobs
  scripts/                 one-off diagnostic/maintenance scripts, not part of the pipeline
  tests/                   pytest + standalone sanity-check scripts
web/                       Next.js review board (see web/README.md)
companies.yaml             company -> ATS registry
aggregators.yaml           aggregator search config
filters.yaml               title/location/stack filter rules
profile.example.yaml       blank template for profile.yaml (your real profile, gitignored)
profile.sample.yaml        fully worked (fictional) example of a filled-in profile
```

## Tests

```bash
make test
```

Runs the pytest suite plus the standalone sanity-check scripts
(`test_pipeline.py`, `test_discover_companies.py`, `test_ai_evaluate.py`).
None of them call live external APIs.

## Notes on scope

- ATS fetchers for Greenhouse, Ashby, Workable, and Lever were each
  validated against real live responses. SmartRecruiters support is built
  from documentation and third-party corroboration only — verify a new
  SmartRecruiters company with `python -m app.main --company <slug>`
  before trusting it in a real run.
- If a particular company's fetch fails, `main.py` logs a warning and
  continues with the rest rather than crashing the whole run.

## Possible next steps

- Workday support (`clio.wd3.myworkdayjobs.com`-style boards) — its
  job-search API is POST-based with a different pagination shape than the
  ATSes currently supported.
- Company-name normalization for aggregator-sourced dedup (the same
  posting sometimes comes back under slightly different company name
  strings, e.g. "Acme Corp" vs. "Acme").
- A digest output (email/Sheet) beyond the current CSV/SQLite/web-board
  review flow.
- Scheduling: once you're happy with a full local run, a daily cron entry
  like `0 7 * * * cd /path/to/job_search_pipeline && python -m app.main && python -m app.ai_evaluate`.

## License

[MIT](LICENSE)
