"""Sanity test for discover_companies.py — runs against a throwaway COPY of
companies.example.yaml, never the real file. Verifies: known companies are never
re-suggested, duplicates within one run are collapsed, appending preserves
the existing file's comments/formatting, and the appended block is valid
YAML that round-trips correctly. Run with: python -m app.tests.test_discover_companies or python -m pytest app/
"""
import os
import shutil

import yaml

from app import discover_companies

TEST_YAML = "data/test_companies.yaml"
SOURCE_YAML = "companies.example.yaml" if os.path.exists("companies.example.yaml") else "companies.yaml"


def main():
    os.makedirs("data", exist_ok=True)
    shutil.copy(SOURCE_YAML, TEST_YAML)

    with open(TEST_YAML) as f:
        original_text = f.read()
    original_companies = yaml.safe_load(original_text)["companies"]

    # --- already-tracked companies (Affirm/greenhouse) should never be
    # re-suggested, even with a different display-name casing from Adzuna ---
    known = discover_companies.load_known_keys(TEST_YAML)
    assert ("greenhouse", "affirm") in known
    assert ("workable", "treewalk") in known
    print(f"load_known_keys OK: {len(known)} pairs loaded")

    discovered = [
        {"name": "Affirm Inc.", "ats": "greenhouse", "slug": "affirm"},  # already known -> skip
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"},  # new
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"},  # dup within this run -> collapse
        {"name": "Some New Co", "ats": "lever", "slug": "somenewco"},  # new, different ATS
    ]
    added = discover_companies.append_new_companies(discovered, path=TEST_YAML)

    assert len(added) == 2, f"expected 2 new companies appended, got {len(added)}: {added}"
    assert {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"} in added
    assert {"name": "Some New Co", "ats": "lever", "slug": "somenewco"} in added
    print("append_new_companies OK: correctly deduped known + within-call dupes, added 2")

    # --- existing file content (comments included) must be untouched, only
    # appended to ---
    with open(TEST_YAML) as f:
        new_text = f.read()
    assert new_text.startswith(original_text), "existing file content was modified, not just appended to"
    assert "# Company registry for the job discovery pipeline." in new_text  # header comment survived
    assert "coveodeven" in new_text or "corrected 2026-08-10" in new_text  # provenance comment survived
    print("File comments/formatting preserved OK")

    # --- the whole file (original + appended block) must still be valid,
    # loadable YAML with the right final company count ---
    with open(TEST_YAML) as f:
        reloaded = yaml.safe_load(f)
    assert len(reloaded["companies"]) == len(original_companies) + 2
    names = {c["name"] for c in reloaded["companies"]}
    assert "Warner Music Group" in names and "Some New Co" in names
    print(f"Round-trip YAML valid OK: {len(reloaded['companies'])} companies total")

    # --- running again with the same discovered list should add nothing
    # (now-known from the previous append) ---
    added_again = discover_companies.append_new_companies(discovered, path=TEST_YAML)
    assert added_again == [], f"expected no new companies on second run, got {added_again}"
    print("Idempotency OK: re-running with the same discoveries adds nothing")

    os.remove(TEST_YAML)
    print("\nAll assertions passed.")


def test_discover_companies():
    main()


if __name__ == "__main__":
    main()