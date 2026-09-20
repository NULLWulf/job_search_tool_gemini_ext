"""Sanity test for ai_evaluate.py's non-API-calling parts: profile loading,
prompt construction, and the DB round-trip (save_evaluation -> CSV output).
Doesn't call an AI provider — that needs a real API key and costs money, so it's
excluded from this offline test. Run with: python -m app.tests.test_ai_evaluate

Loads profile.example.yaml rather than profile.yaml (which is gitignored
and holds your real, personal experience) so this test works the same for
everyone regardless of what's in their own profile.
"""
import os

from app import ai_evaluate
from app import dedup

TEST_DB = "data/test_ai_evaluate.sqlite3"


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    # --- profile loads and is well-formed enough to dump into a prompt ---
    profile = ai_evaluate.load_profile("profile.example.yaml")
    assert profile["identity"]["name"]
    assert "backend_architecture" in profile["competencies"]
    print("Profile loaded OK:", profile["identity"]["headline"])

    # --- prompt construction doesn't crash on a real-shaped job dict, and
    # strips HTML out of the description ---
    job = {
        "company": "Affirm",
        "title": "Senior Software Engineer, Backend",
        "location": "Remote Canada",
        "url": "https://job-boards.greenhouse.io/affirm/jobs/1111",
        "description": "<p>We use <b>Python</b> and FastAPI.</p> " + "Real full-length job description text, "
        "well past the truncation-detection floor so this represents a genuine complete JD, not a "
        "short aggregator teaser snippet. " * 4,
    }
    prompt = ai_evaluate.build_user_prompt(profile, job, include_tool_prompt=True)
    assert "Python" in prompt and "<b>" not in prompt
    assert "Affirm" in prompt
    assert "NOTE: this description looks like a short snippet" not in prompt  # full-length JD, no flag expected
    print("Prompt built OK, length:", len(prompt))

    # --- truncated-description detection (2026-08-11: Adzuna snippets were
    # tanking match_score for jobs that might be great fits; filters.py's
    # looks_truncated() is shared with aggregator_clients.fetch_adzuna,
    # which now tries to fetch the full JD instead of settling for this) ---
    from app import filters
    assert filters.looks_truncated("Great team, curious people, driving impact and…")
    assert filters.looks_truncated("Short snippet under the length floor.")
    assert not filters.looks_truncated(
        "A" * 500 + " full-length JD text with no trailing ellipsis, well past the floor."
    )
    assert filters.looks_truncated("")  # empty description is the extreme case of "too short"

    adzuna_job = {
        "company": "Warner Music Group",
        "title": "Software Engineer, Automated Marketing",
        "location": "Alberta, Canada",
        "url": "https://www.adzuna.ca/details/5702928490",
        "description": "Build automated marketing tooling. Curiosity is the driving force behind…",
    }
    adzuna_prompt = ai_evaluate.build_user_prompt(profile, adzuna_job, include_tool_prompt=False)
    assert "NOTE: this description looks like a short snippet" in adzuna_prompt
    print("Truncated-description flagging OK")

    # --- DB round trip: save a fake evaluation (as if the API had returned
    # it), confirm it shows up in the unevaluated-queue exclusion and in
    # the scored CSV output ---
    with dedup.connect(TEST_DB) as conn:
        dedup.save_details(conn, job, passed_filters=True)
        queue = dedup.get_unevaluated_candidates(conn)
        assert len(queue) == 1, f"expected 1 unevaluated candidate, got {len(queue)}"

        fake_evaluation = {
            "match_score": 82,
            "recommendation": "apply",
            "genuine_gaps": "No direct fintech/lending domain experience.",
            "transferable_strengths": "Backend architecture depth (Elementica, BenchSci) transfers directly.",
            "risk_factors": "None significant given Canada-remote eligibility.",
        }
        dedup.save_evaluation(conn, job["url"], fake_evaluation, model="gemini-3.8-flash")

        queue_after = dedup.get_unevaluated_candidates(conn)
        assert len(queue_after) == 0, "job should no longer be in the unevaluated queue after scoring"

        total = ai_evaluate.write_csv(conn, path=__import__("pathlib").Path("data/test_scored.csv"))
        assert total == 1

    with open("data/test_scored.csv") as f:
        content = f.read()
    assert "match_score" in content and "82" in content and "apply" in content
    print("DB round-trip + CSV output OK")

    os.remove(TEST_DB)
    os.remove("data/test_scored.csv")
    print("\nAll assertions passed.")


def test_ai_evaluate():
    main()


if __name__ == "__main__":
    main()