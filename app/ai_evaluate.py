"""Stage 2: AI evaluation via Anthropic or Gemini.

Reads every job that passed the deterministic filters (filters.py) but
hasn't been scored yet (dedup.get_unevaluated_candidates), sends it to
Claude Haiku or Gemini alongside your profile.yaml, and asks for a FUNCTIONAL FIT
judgment — not a title match. Results are stored in the ai_evaluations
table (see dedup.py) and written out to data/scored_candidates.csv, best
match first.

This is the ONLY part of the pipeline that costs money — everything
upstream (fetch, filter, dedup) is free. That's the whole point of doing
filtering deterministically first: by the time a job reaches this script,
it's already passed title/location/stack screening, so the AI-scored
volume should be small.

Setup:
    pip install anthropic google-genai pydantic
    export ANTHROPIC_API_KEY=sk-ant-...
    export GEMINI_API_KEY=AIzaSy...

Usage:
    python -m app.ai_evaluate              # evaluate everything unscored
    python -m app.ai_evaluate --limit 20   # cap this run (e.g. to control cost)
    python -m app.ai_evaluate --dry-run    # show what WOULD be sent, call nothing
"""
import argparse
import csv
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from pydantic import BaseModel, Field
from typing import Literal

from app import dedup
from app import filters

load_dotenv()

AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").lower()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
OUTPUT_CSV = Path("data/scored_candidates.csv")

REQUIRED_EVAL_FIELDS = ["match_score", "recommendation", "genuine_gaps", "transferable_strengths", "risk_factors"]

class Evaluation(BaseModel):
    match_score: int = Field(description="Overall functional fit score, 0-100.", ge=0, le=100)
    recommendation: Literal["apply", "consider", "skip"] = Field(description="apply = strong functional fit, worth the effort. consider = plausible but real gaps/risk. skip = not a genuine fit despite passing keyword filters.")
    genuine_gaps: str = Field(description="Real, specific gaps between the candidate's experience and this role's requirements. Be honest — don't invent gaps to seem balanced, and don't paper over real ones. Keep to 2-3 sentences.")
    transferable_strengths: str = Field(description="Which of the candidate's competencies/evidence genuinely transfer to this role, and why — cite specifics from their profile, not generic claims. Keep to 2-3 sentences.")
    risk_factors: str = Field(description="Non-skill risks: seniority mismatch, domain mismatch, likely comp mismatch, stack dealbreakers the deterministic filter might have missed, company-stage risk given the candidate's stated preferences, etc. Keep to 2-3 sentences.")

# Field order matters here beyond documentation: Claude tends to emit tool
# JSON in roughly declaration order, and with max_tokens capped, a run of
# long free-text fields can eat the budget before later fields get
# written — which is exactly what caused a real KeyError on 'recommendation'
# in production (2026-08-11, see evaluate_one's retry logic below for the
# other half of the fix). Putting the two short/critical fields
# (match_score, recommendation) FIRST means they're very unlikely to be the
# ones lost to truncation even if a long-text field still gets cut off.
EVALUATION_SCHEMA = {
    "name": "submit_evaluation",
    "description": "Submit a structured fit evaluation for this job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "match_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Overall functional fit score, 0-100.",
            },
            "recommendation": {
                "type": "string",
                "enum": ["apply", "consider", "skip"],
                "description": "apply = strong functional fit, worth the effort. consider = plausible but real gaps/risk. skip = not a genuine fit despite passing keyword filters.",
            },
            "genuine_gaps": {
                "type": "string",
                "description": "Real, specific gaps between the candidate's experience and this role's requirements. Be honest — don't invent gaps to seem balanced, and don't paper over real ones. Keep to 2-3 sentences.",
            },
            "transferable_strengths": {
                "type": "string",
                "description": "Which of the candidate's competencies/evidence genuinely transfer to this role, and why — cite specifics from their profile, not generic claims. Keep to 2-3 sentences.",
            },
            "risk_factors": {
                "type": "string",
                "description": "Non-skill risks: seniority mismatch, domain mismatch, likely comp mismatch, stack dealbreakers the deterministic filter might have missed, company-stage risk given the candidate's stated preferences, etc. Keep to 2-3 sentences.",
            },
        },
        "required": REQUIRED_EVAL_FIELDS,
    },
}

SYSTEM_PROMPT = """You are evaluating job postings for FUNCTIONAL FIT against a candidate's real \
experience — not title matching, not keyword matching. The candidate's profile is organized by \
competency (what they've actually done), not by job title, specifically so you judge whether their \
demonstrated capabilities transfer to this role's actual responsibilities.

Be honest and specific, not diplomatic. A generic "great candidate!" evaluation is useless — the \
candidate needs real signal on whether to spend an application on this. If the role is a stretch, \
say so and say why. If there's a real gap, name it precisely rather than softening it. Cite \
specific evidence from their profile when claiming a strength transfers; don't just assert \
seniority-level fit in the abstract."""


def load_profile(path: str = "profile.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_user_prompt(profile: dict, job: dict, include_tool_prompt: bool) -> str:
    raw_description = job.get("description", "")
    description = filters.strip_html(raw_description).strip() or "(no JD text available)"
    partial_note = ""
    if raw_description and filters.looks_truncated(raw_description):
        partial_note = (
            "\nNOTE: this description looks like a short snippet, not the full posting — it may "
            "have been cut off mid-sentence. Do NOT lower match_score or invent genuine_gaps just "
            "because a responsibility/requirement isn't mentioned here; judge fit on title, "
            "company, location, and whatever specifics ARE present. If the snippet is too thin to "
            "say anything meaningful about stack or seniority, say so in genuine_gaps rather than "
            "guessing.\n"
        )
    prompt = f"""CANDIDATE PROFILE:
{yaml.dump(profile, sort_keys=False, allow_unicode=True)}

---

JOB POSTING TO EVALUATE:
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
URL: {job['url']}
{partial_note}
Description:
{description}"""
    if include_tool_prompt:
        prompt += "\n\n---\n\nCall submit_evaluation with your structured assessment."
    return prompt


def _extract_tool_input(resp) -> dict | None:
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_evaluation":
            return block.input
    return None


def _missing_fields(evaluation: dict) -> list[str]:
    return [f for f in REQUIRED_EVAL_FIELDS if f not in evaluation]


def evaluate_one_gemini(client, profile: dict, job: dict, max_retries: int = 1) -> dict:
    user_content = build_user_prompt(profile, job, include_tool_prompt=False)

    for attempt in range(max_retries + 1):
        try:
            resp = client.interactions.create(
                model=GEMINI_MODEL,
                input=user_content,
                system_instruction=SYSTEM_PROMPT,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": Evaluation.model_json_schema()
                }
            )
            
            evaluation = Evaluation.model_validate_json(resp.output_text).model_dump()
            missing = _missing_fields(evaluation)
            if not missing:
                return evaluation
                
            if attempt < max_retries:
                continue
            raise RuntimeError(f"Model's response for {job['url']} is missing required field(s) "
                                f"{missing} after {max_retries + 1} attempt(s): {evaluation}")
                                
        except Exception as e:
            if attempt < max_retries:
                continue
            raise RuntimeError(f"Evaluation failed for {job['url']}: {e}")


def evaluate_one_anthropic(client, profile: dict, job: dict, max_retries: int = 1) -> dict:
    user_content = build_user_prompt(profile, job, include_tool_prompt=True)
    max_tokens = 1536

    for attempt in range(max_retries + 1):
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=[EVALUATION_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_evaluation"},
            messages=[{"role": "user", "content": user_content}],
        )
        evaluation = _extract_tool_input(resp)
        if evaluation is None:
            stop_reason = getattr(resp, "stop_reason", "unknown")
            if attempt < max_retries:
                max_tokens += 512
                continue
            raise RuntimeError(f"Model didn't call submit_evaluation for {job['url']} "
                                f"(stop_reason={stop_reason})")

        missing = _missing_fields(evaluation)
        if not missing:
            return evaluation
        if attempt < max_retries:
            max_tokens += 512
            continue
        raise RuntimeError(f"Model's response for {job['url']} is missing required field(s) "
                            f"{missing} after {max_retries + 1} attempt(s): {evaluation}")


def evaluate_one(client, profile: dict, job: dict, max_retries: int = 1) -> dict:
    if AI_PROVIDER == "gemini":
        return evaluate_one_gemini(client, profile, job, max_retries)
    else:
        return evaluate_one_anthropic(client, profile, job, max_retries)


def write_csv(conn, path: Path = OUTPUT_CSV) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(dedup.iter_scored_candidates(conn))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "match_score", "recommendation", "company", "title", "location",
            "transferable_strengths", "genuine_gaps", "risk_factors", "url", "posted_at",
        ])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max number of jobs to evaluate this run")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be evaluated, call no API")
    args = parser.parse_args()

    profile = load_profile()

    with dedup.connect() as conn:
        queue = dedup.get_unevaluated_candidates(conn)
        if args.limit:
            queue = queue[: args.limit]

        print(f"{len(queue)} candidate(s) queued for AI evaluation.")
        if not queue:
            sys.exit(0)

        if args.dry_run:
            for job in queue:
                print(f"WOULD EVALUATE | {job['company']:20s} | {job['title']}")
            sys.exit(0)

        if AI_PROVIDER == "gemini":
            try:
                from google import genai
            except ImportError:
                sys.exit("Missing dependency: pip install google-genai")
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                sys.exit("GEMINI_API_KEY env var not set.")
            client = genai.Client(api_key=api_key)
            current_model = GEMINI_MODEL
        else:
            try:
                import anthropic
            except ImportError:
                sys.exit("Missing dependency: pip install anthropic")
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                sys.exit("ANTHROPIC_API_KEY env var not set.")
            client = anthropic.Anthropic(api_key=api_key)
            current_model = ANTHROPIC_MODEL

        for i, job in enumerate(queue, 1):
            try:
                evaluation = evaluate_one(client, profile, job)
            except Exception as e:
                print(f"[WARN] {job['company']} — {job['title']}: evaluation failed — {e}", file=sys.stderr)
                continue
            dedup.save_evaluation(conn, job["url"], evaluation, current_model)
            conn.commit()  # commit per-job so a crash mid-run doesn't lose completed evaluations
            print(f"[{i}/{len(queue)}] {evaluation['match_score']:3d} {evaluation['recommendation']:9s} | "
                  f"{job['company']:20s} | {job['title']}")

        total = write_csv(conn)
        print(f"\nWrote {total} scored candidates to {OUTPUT_CSV} (sorted by match_score desc).")