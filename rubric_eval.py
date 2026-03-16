"""
rubric_eval.py — Immutable Rubric Meta-Evaluator

This is the ground-truth scoring script for the rubric research loop.
Do NOT modify this file. It is the equivalent of prepare.py in autoresearch.

What it does:
  1. Loads the current swe_rubric.md (the mutable file the agent iterates on)
  2. Loads the 8 fixed agent transcripts from gold/transcripts/
  3. Loads the human expert gold rankings from gold/rankings.json
  4. Uses a judge LLM to apply the rubric to each transcript → ASTRO score
  5. Computes Kendall's τ between rubric scores and gold rankings
  6. Prints a summary (rubric_kendall_tau, rubric_spearman, cost, per-transcript)

The primary metric is rubric_kendall_tau (higher is better, max = 1.0).

Usage:
    python rubric_eval.py              # evaluate current swe_rubric.md
    python rubric_eval.py --dry-run    # mock judge (no API calls, no cost)
    python rubric_eval.py --verbose    # print full judge responses

Requires: ANTHROPIC_API_KEY environment variable (unless --dry-run)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants — do not modify
# ---------------------------------------------------------------------------

RUBRIC_FILE     = Path("swe_rubric.md")
GOLD_DIR        = Path("gold")
TRANSCRIPTS_DIR = GOLD_DIR / "transcripts"
RANKINGS_FILE   = GOLD_DIR / "rankings.json"

JUDGE_MODEL     = "claude-haiku-4-5-20251001"   # fixed — do not change
MAX_TOKENS      = 1024
TRANSCRIPT_IDS  = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"]

# Input token cost (USD per 1M tokens) for the judge model — for cost tracking
INPUT_COST_PER_1M  = 0.80
OUTPUT_COST_PER_1M = 4.00


# ---------------------------------------------------------------------------
# Kendall's τ and Spearman ρ
# ---------------------------------------------------------------------------

def kendall_tau(ranked_scores: list[float], gold_ranks: list[int]) -> float:
    """
    Compute Kendall's τ-b between rubric score ordering and gold rank ordering.

    ranked_scores: ASTRO scores from rubric (higher = better agent)
    gold_ranks:    gold rank numbers (1 = best, 8 = worst)

    Converts gold_ranks to a descending score (9 - rank) so both lists are
    "higher is better", then computes τ-b.
    """
    gold_scores = [9 - r for r in gold_ranks]
    n = len(ranked_scores)
    assert n == len(gold_scores), "lengths must match"

    concordant = 0
    discordant = 0
    ties_x = 0
    ties_y = 0

    for i in range(n):
        for j in range(i + 1, n):
            dx = ranked_scores[i] - ranked_scores[j]
            dy = gold_scores[i] - gold_scores[j]
            if dx == 0 and dy == 0:
                pass
            elif dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0) == (dy > 0):
                concordant += 1
            else:
                discordant += 1

    denom = math.sqrt((concordant + discordant + ties_x) *
                      (concordant + discordant + ties_y))
    if denom == 0:
        return 0.0
    return (concordant - discordant) / denom


def spearman_rho(ranked_scores: list[float], gold_ranks: list[int]) -> float:
    """Spearman ρ (secondary metric, easier to interpret as a sanity check)."""
    n = len(ranked_scores)
    # Rank rubric scores (descending: highest score = rank 1)
    order = sorted(range(n), key=lambda i: -ranked_scores[i])
    rubric_ranks = [0] * n
    for rank, idx in enumerate(order, 1):
        rubric_ranks[idx] = rank

    d2 = sum((rubric_ranks[i] - gold_ranks[i]) ** 2 for i in range(n))
    return 1 - (6 * d2) / (n * (n * n - 1))


# ---------------------------------------------------------------------------
# Judge LLM
# ---------------------------------------------------------------------------

JUDGE_PROMPT_TEMPLATE = """\
You are an expert evaluator of AI software engineering agents. You will score \
an agent's performance on a long-horizon SWE task using the provided rubric.

## Rubric
{rubric_text}

## Agent Transcript
Task: {task_title}

{transcript_summary}

Key events:
{key_events}

Errors encountered: {errors}
Spec violations: {spec_violations}
Repeated failures: {repeated_failures}
Termination behaviour: {termination}
Final test result: {test_result}
Code quality notes: {code_quality}

## Instructions
Score the agent on each ASTRO dimension using the rubric above.

For the R dimension, provide sub-scores:
  R-Spec (0-2): specification compliance
  R-Loop (0-2): loop avoidance
  R-Stop (0-1): termination awareness
  R = R-Spec + R-Loop + R-Stop

Respond with ONLY a JSON object, no commentary:
{{
  "A": <0-5>,
  "S": <0-5>,
  "T": <0-5>,
  "R_spec": <0-2>,
  "R_loop": <0-2>,
  "R_stop": <0-1>,
  "O": <0-5>,
  "reasoning": "<one sentence per dimension>"
}}
"""


def format_transcript(t: dict) -> tuple[str, str, str, str, str, str, str]:
    """Extract fields needed for the judge prompt."""
    events = "\n".join(
        f"  [{e['action']:3d}] {e['type'].upper()}: {e['detail']}"
        for e in t.get("key_events", [])
    )
    errors = json.dumps(t.get("errors_encountered", [])) if t.get("errors_encountered") else "none"
    violations = json.dumps(t.get("spec_violations", [])) if t.get("spec_violations") else "none"
    loops = json.dumps(t.get("repeated_failures", [])) if t.get("repeated_failures") else "none"
    termination = t.get("termination", "unknown")
    test_result = json.dumps(t.get("final_test_result", {}))
    code_quality = t.get("code_quality_notes", "")
    return events, errors, violations, loops, termination, test_result, code_quality


def call_judge(
    rubric_text: str,
    transcript: dict,
    verbose: bool = False,
) -> tuple[dict, int, int]:
    """
    Call the judge LLM to score one transcript.
    Returns (scores_dict, input_tokens, output_tokens).
    This function is immutable — the judge model, prompt structure, and
    parsing logic are all fixed.
    """
    try:
        import anthropic
    except ImportError:
        print("[ERROR] anthropic package not found. Install with: pip install anthropic",
              file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    events, errors, violations, loops, termination, test_result, code_quality = \
        format_transcript(transcript)

    prompt = JUDGE_PROMPT_TEMPLATE.format(
        rubric_text=rubric_text,
        task_title=transcript.get("task_title", ""),
        transcript_summary=transcript.get("summary", ""),
        key_events=events,
        errors=errors,
        spec_violations=violations,
        repeated_failures=loops,
        termination=termination,
        test_result=test_result,
        code_quality=code_quality,
    )

    if verbose:
        print(f"\n[judge prompt for {transcript['transcript_id']}]")
        print(prompt[:500] + "...")

    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    if verbose:
        print(f"\n[judge response]\n{raw}\n")

    # Parse JSON — strip any markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    scores = json.loads(raw.strip())
    return scores, input_tokens, output_tokens


def call_judge_dry_run(transcript: dict) -> tuple[dict, int, int]:
    """
    Deterministic mock judge for --dry-run mode. Uses transcript metadata
    to produce plausible scores without API calls. Fixed logic — do not modify.
    """
    pass_rate = transcript.get("final_test_result", {}).get("pass_rate", 0.0)
    has_violations = bool(transcript.get("spec_violations"))
    repeated = len(transcript.get("repeated_failures", []))
    loops = len(transcript.get("errors_encountered", []))
    termination = transcript.get("termination", "")
    actions = transcript.get("total_actions", 100)

    # T from pass rate
    T = (5 if pass_rate >= 1.0 else
         4 if pass_rate >= 0.9 else
         3 if pass_rate >= 0.7 else
         2 if pass_rate >= 0.5 else
         1 if pass_rate > 0 else 0)

    # A from clarification behaviour (proxy: actions count and guidance mentions)
    summary = transcript.get("summary", "").lower()
    pause_count = sum(1 for e in transcript.get("key_events", []) if e["type"] == "pause")
    A = max(0, 5 - pause_count * 2)

    # S proxy from T and pass_rate
    S = max(0, T - (1 if pass_rate < 0.9 else 0))

    # R sub-scores
    R_spec = 0 if has_violations else 2
    R_loop = max(0, 2 - repeated)
    R_stop = 0 if "fail" in termination or "success" in termination.replace("confirming_success", "") else 1
    if "confirming_success" in termination:
        R_stop = 1

    # O proxy from code quality notes
    notes = transcript.get("code_quality_notes", "").lower()
    O = (4 if "clean" in notes and "unnecessary" not in notes else
         3 if "functional" in notes else
         2 if "redundant" in notes or "overengineered" in notes else
         1 if "rewrite" in notes else 0)

    scores = {
        "A": A, "S": S, "T": T,
        "R_spec": R_spec, "R_loop": R_loop, "R_stop": R_stop,
        "O": O,
        "reasoning": "dry-run mock scores",
    }
    # mock token counts
    return scores, 800, 120


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def load_gold() -> dict:
    with open(RANKINGS_FILE) as f:
        return json.load(f)


def load_transcript(tid: str) -> dict:
    path = TRANSCRIPTS_DIR / f"{tid}.json"
    with open(path) as f:
        return json.load(f)


def evaluate_rubric(dry_run: bool = False, verbose: bool = False) -> dict:
    """
    Core evaluation function — do not modify.

    Applies the current swe_rubric.md to all 8 gold transcripts,
    computes Kendall's τ vs gold rankings, returns the result dict.
    """
    if not RUBRIC_FILE.exists():
        print(f"[ERROR] {RUBRIC_FILE} not found.", file=sys.stderr)
        sys.exit(1)

    rubric_text = RUBRIC_FILE.read_text()
    gold = load_gold()
    gold_by_id = {r["transcript_id"]: r for r in gold["rankings"]}

    results = []
    total_input_tokens = 0
    total_output_tokens = 0

    print(f"Evaluating rubric against {len(TRANSCRIPT_IDS)} transcripts...")
    if dry_run:
        print("(dry-run mode: mock judge, no API calls)")
    print()

    for tid in TRANSCRIPT_IDS:
        transcript = load_transcript(tid)
        gold_entry = gold_by_id[tid]

        if dry_run:
            scores, in_tok, out_tok = call_judge_dry_run(transcript)
        else:
            scores, in_tok, out_tok = call_judge(rubric_text, transcript, verbose)

        total_input_tokens  += in_tok
        total_output_tokens += out_tok

        R = scores["R_spec"] + scores["R_loop"] + scores["R_stop"]
        astro = scores["A"] + scores["S"] + scores["T"] + R + scores["O"]

        results.append({
            "transcript_id": tid,
            "gold_rank":  gold_entry["gold_rank"],
            "gold_astro": gold_entry["gold_astro"],
            "astro":      astro,
            "A": scores["A"],
            "S": scores["S"],
            "T": scores["T"],
            "R": R,
            "R_spec": scores["R_spec"],
            "R_loop": scores["R_loop"],
            "R_stop": scores["R_stop"],
            "O": scores["O"],
            "reasoning": scores.get("reasoning", ""),
        })

    # Sort by gold rank for display
    results.sort(key=lambda r: r["gold_rank"])

    rubric_scores = [r["astro"]    for r in results]
    gold_ranks    = [r["gold_rank"] for r in results]

    tau = kendall_tau(rubric_scores, gold_ranks)
    rho = spearman_rho(rubric_scores, gold_ranks)

    cost = (total_input_tokens  / 1_000_000 * INPUT_COST_PER_1M +
            total_output_tokens / 1_000_000 * OUTPUT_COST_PER_1M)

    return {
        "rubric_kendall_tau":  tau,
        "rubric_spearman":     rho,
        "judge_cost_usd":      cost,
        "judge_tokens_total":  total_input_tokens + total_output_tokens,
        "per_transcript":      results,
    }


def print_results(result: dict) -> None:
    """Print in the autoresearch-style summary format."""
    print("---")
    print(f"rubric_kendall_tau:   {result['rubric_kendall_tau']:.6f}")
    print(f"rubric_spearman:      {result['rubric_spearman']:.6f}")
    print(f"judge_cost_usd:       {result['judge_cost_usd']:.4f}")
    print(f"judge_tokens_total:   {result['judge_tokens_total']}")
    print("per_transcript_scores:")
    for r in result["per_transcript"]:
        print(f"  {r['transcript_id']} (gold_rank={r['gold_rank']}): "
              f"astro={r['astro']:2d}  "
              f"A={r['A']} S={r['S']} T={r['T']} "
              f"R={r['R']}(spec={r['R_spec']},loop={r['R_loop']},stop={r['R_stop']}) "
              f"O={r['O']}")
    print()

    # Rank agreement summary
    rubric_order = sorted(result["per_transcript"], key=lambda r: -r["astro"])
    rubric_ranking = [r["transcript_id"] for r in rubric_order]
    gold_order     = sorted(result["per_transcript"], key=lambda r:  r["gold_rank"])
    gold_ranking   = [r["transcript_id"] for r in gold_order]
    print(f"rubric_order: {' > '.join(rubric_ranking)}")
    print(f"gold_order:   {' > '.join(gold_ranking)}")

    tau = result["rubric_kendall_tau"]
    if tau >= 0.9:
        verdict = "EXCELLENT — rubric ordering nearly matches human expert judgment"
    elif tau >= 0.7:
        verdict = "GOOD — strong agreement with human expert judgment"
    elif tau >= 0.5:
        verdict = "FAIR — moderate agreement, room for improvement"
    elif tau >= 0.0:
        verdict = "POOR — weak agreement, rubric needs significant revision"
    else:
        verdict = "INVERTED — rubric ranks agents opposite to human experts"
    print(f"verdict:      {verdict}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rubric meta-evaluator — measures how well swe_rubric.md "
                    "agrees with human expert judgment on 8 fixed transcripts."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Use mock judge (no API calls, instant, free)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print judge prompts and responses")
    args = parser.parse_args()

    start = time.monotonic()
    result = evaluate_rubric(dry_run=args.dry_run, verbose=args.verbose)
    elapsed = time.monotonic() - start

    print_results(result)
    print(f"eval_seconds:         {elapsed:.1f}")


if __name__ == "__main__":
    main()
