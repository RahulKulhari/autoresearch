"""
swe_eval.py — SWE Agent ASTRO Evaluation Harness

This is the ground-truth evaluation script for long-horizon SWE agent tasks.
It is read-only from the agent's perspective: do NOT modify this file.

Usage:
    python swe_eval.py setup <task-id>       # Prepare task environment
    python swe_eval.py run   <task-id>       # Score a completed task
    python swe_eval.py score <task-id> --A 5 --S 4 --R 4 --O 4  # Record human scores
    python swe_eval.py report                # Print results table

The T (Technical Correctness) dimension is scored automatically by running
the task's test_command. All other dimensions (A, S, R, O) are scored by
a human evaluator or a separate judge LLM reviewing the agent's transcript.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants — do not modify
# ---------------------------------------------------------------------------

RESULTS_FILE = Path("swe_results.tsv")
TASKS_DIR = Path("tasks")
EVAL_TESTS_DIR = Path("eval/tests")

TSV_HEADER = "\t".join([
    "task_id", "agent", "astro_score",
    "A", "S", "T", "R", "O",
    "actions", "wall_s", "test_pass_rate",
    "status", "notes",
])

PASS_THRESHOLD = 15   # ≥15/25 is a passing score
GOOD_THRESHOLD = 20   # ≥20/25 is a good score


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    id: str
    title: str
    category: str
    difficulty: str
    description: str
    repo: str
    initial_commit: str
    acceptance_criteria: list[str]
    test_command: str
    budget: dict
    eval_weights: dict

    @classmethod
    def load(cls, task_id: str) -> "TaskSpec":
        task_file = TASKS_DIR / f"{task_id}.json"
        if not task_file.exists():
            raise FileNotFoundError(f"Task spec not found: {task_file}")
        with open(task_file) as f:
            data = json.load(f)
        return cls(**data)


@dataclass
class AstroScore:
    A: int = 0   # Autonomy
    S: int = 0   # Solution Completeness
    T: int = 0   # Technical Correctness (auto-scored)
    R: int = 0   # Resilience
    O: int = 0   # Output Quality

    def total(self) -> int:
        return self.A + self.S + self.T + self.R + self.O

    def grade(self) -> str:
        t = self.total()
        if t >= 23: return "S"
        if t >= 20: return "A"
        if t >= 15: return "B"
        if t >= 10: return "C"
        if t >= 5:  return "D"
        return "F"

    def status(self) -> str:
        t = self.total()
        if t >= GOOD_THRESHOLD:  return "pass"
        if t >= PASS_THRESHOLD:  return "partial"
        if t > 0:                return "fail"
        return "crash"

    def validate(self) -> None:
        for field in fields(self):
            val = getattr(self, field.name)
            if not (0 <= val <= 5):
                raise ValueError(
                    f"Dimension {field.name} must be 0–5, got {val}"
                )


@dataclass
class RunRecord:
    task_id: str
    agent: str
    score: AstroScore
    actions: int
    wall_s: float
    test_pass_rate: float
    notes: str

    def to_tsv_row(self) -> str:
        s = self.score
        return "\t".join([
            self.task_id,
            self.agent,
            str(s.total()),
            str(s.A), str(s.S), str(s.T), str(s.R), str(s.O),
            str(self.actions),
            f"{self.wall_s:.1f}",
            f"{self.test_pass_rate:.3f}",
            s.status(),
            self.notes,
        ])


# ---------------------------------------------------------------------------
# Core evaluation functions
# ---------------------------------------------------------------------------

def run_tests(task: TaskSpec) -> tuple[float, str]:
    """
    Run the task's test_command and return (pass_rate, raw_output).

    pass_rate is a float in [0.0, 1.0].
    If the command fails entirely, returns (0.0, stderr).
    This function is the ground truth — do not modify.
    """
    # Verify test files haven't been touched (anti-gaming)
    if EVAL_TESTS_DIR.exists():
        result = subprocess.run(
            ["git", "diff", "--name-only", str(EVAL_TESTS_DIR)],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            modified = result.stdout.strip().replace("\n", ", ")
            print(f"[VIOLATION] Eval test files modified: {modified}", file=sys.stderr)
            return 0.0, f"ANTI-GAMING VIOLATION: test files modified ({modified})"

    start = time.monotonic()
    try:
        result = subprocess.run(
            task.test_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        elapsed = time.monotonic() - start
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 0.0, "TIMEOUT: test suite exceeded 300s"
    except Exception as e:
        return 0.0, f"ERROR running tests: {e}"

    pass_rate = _parse_pass_rate(output)
    print(f"[tests] Completed in {elapsed:.1f}s — pass rate: {pass_rate:.1%}")
    return pass_rate, output


def _parse_pass_rate(output: str) -> float:
    """
    Parse test output to extract pass rate.
    Supports pytest, unittest, and generic 'X/Y passed' formats.
    Returns 0.0 if unparseable.
    """
    import re

    # pytest: "X passed, Y failed" or "X passed" or "X failed"
    m = re.search(r"(\d+) passed", output)
    passed = int(m.group(1)) if m else 0

    m = re.search(r"(\d+) failed", output)
    failed = int(m.group(1)) if m else 0

    m = re.search(r"(\d+) error", output)
    errors = int(m.group(1)) if m else 0

    total = passed + failed + errors
    if total == 0:
        # Try "X/Y" format
        m = re.search(r"(\d+)\s*/\s*(\d+)", output)
        if m:
            passed = int(m.group(1))
            total = int(m.group(2))
        else:
            return 0.0

    return passed / total if total > 0 else 0.0


def score_t_dimension(pass_rate: float, test_output: str) -> int:
    """
    Deterministically convert test pass_rate to T dimension score (0–5).
    Anti-gaming violations are caught by run_tests() before reaching here.
    """
    if "ANTI-GAMING VIOLATION" in test_output:
        return 0
    if pass_rate >= 1.0:   return 5
    if pass_rate >= 0.90:  return 4
    if pass_rate >= 0.70:  return 3
    if pass_rate >= 0.50:  return 2
    if pass_rate > 0.0:    return 1
    return 0


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_setup(task_id: str) -> None:
    """Prepare the task environment: checkout initial commit, verify tests fail."""
    task = TaskSpec.load(task_id)
    print(f"Setting up task: {task.id} — {task.title}")
    print(f"Category: {task.category}  Difficulty: {task.difficulty}")
    print(f"Initial commit: {task.initial_commit}")
    print()

    # Checkout starting state
    result = subprocess.run(
        ["git", "checkout", task.initial_commit],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] Could not checkout {task.initial_commit}:\n{result.stderr}")
        sys.exit(1)
    print(f"Checked out initial commit: {task.initial_commit}")

    # Verify tests are in a known-failing state (task is not trivially done)
    print("Verifying task is not already solved...")
    pass_rate, _ = run_tests(task)
    if pass_rate >= 1.0:
        print("[WARNING] All tests pass at initial state — task may already be solved.")
        print("          Verify that the task description introduces a real challenge.")
    else:
        print(f"Baseline test pass rate: {pass_rate:.1%} (expected < 100%)")

    print()
    print("Task description:")
    print("─" * 60)
    print(task.description)
    print("─" * 60)
    print()
    print("Acceptance criteria:")
    for i, criterion in enumerate(task.acceptance_criteria, 1):
        print(f"  {i}. {criterion}")
    print()
    print(f"Budget: {task.budget.get('max_actions', 150)} actions, "
          f"{task.budget.get('max_wall_seconds', 1800)}s wall time")
    print()
    print("Setup complete. Agent may now begin.")


def cmd_run(task_id: str, agent: str) -> None:
    """Score a completed task: run tests, compute T, print scoring template."""
    task = TaskSpec.load(task_id)
    print(f"Scoring task: {task.id} — {task.title}")
    print(f"Agent: {agent}")
    print()

    start = time.monotonic()
    pass_rate, test_output = run_tests(task)
    wall_s = time.monotonic() - start

    t_score = score_t_dimension(pass_rate, test_output)
    print(f"T dimension (auto-scored): {t_score}/5  (pass rate: {pass_rate:.1%})")
    print()

    print("─" * 60)
    print("HUMAN SCORING REQUIRED")
    print("─" * 60)
    print("Review the agent's transcript and score these dimensions:")
    print()
    print("  A — Agent Autonomy      (0–5): Did it work independently?")
    print("  S — Solution Completeness (0–5): Are acceptance criteria met?")
    print(f"  T — Technical Correctness  (auto): {t_score}")
    print("  R — Resilience           (0–5): Did it recover from errors?")
    print("  O — Output Quality       (0–5): Is the code clean?")
    print()
    print(f"Run this command to record scores:")
    print(f"  python swe_eval.py score {task_id} --agent {agent} "
          f"--A <score> --S <score> --T {t_score} --R <score> --O <score> "
          f"--actions <n> --wall-s {wall_s:.0f} --notes '<notes>'")


def cmd_score(
    task_id: str,
    agent: str,
    A: int, S: int, T: int, R: int, O: int,
    actions: int,
    wall_s: float,
    notes: str,
) -> None:
    """Record final ASTRO scores to swe_results.tsv."""
    task = TaskSpec.load(task_id)

    score = AstroScore(A=A, S=S, T=T, R=R, O=O)
    score.validate()

    # Re-run tests to get authoritative pass rate
    pass_rate, test_output = run_tests(task)
    t_auto = score_t_dimension(pass_rate, test_output)
    if T != t_auto:
        print(f"[WARNING] Provided T={T} differs from auto-scored T={t_auto}. "
              f"Using auto-scored value.")
        score.T = t_auto

    record = RunRecord(
        task_id=task_id,
        agent=agent,
        score=score,
        actions=actions,
        wall_s=wall_s,
        test_pass_rate=pass_rate,
        notes=notes,
    )

    # Initialize TSV if needed
    if not RESULTS_FILE.exists():
        RESULTS_FILE.write_text(TSV_HEADER + "\n")

    with open(RESULTS_FILE, "a") as f:
        f.write(record.to_tsv_row() + "\n")

    print(f"Recorded results to {RESULTS_FILE}")
    print()
    _print_scorecard(record)


def cmd_report() -> None:
    """Print a formatted results table from swe_results.tsv."""
    if not RESULTS_FILE.exists():
        print(f"No results file found at {RESULTS_FILE}")
        return

    rows = []
    with open(RESULTS_FILE) as f:
        lines = f.read().splitlines()

    if len(lines) < 2:
        print("No results recorded yet.")
        return

    for line in lines[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 13:
            continue
        rows.append(parts)

    if not rows:
        print("No results recorded yet.")
        return

    print(f"{'Task':<15} {'Agent':<20} {'ASTRO':>5} {'A':>2} {'S':>2} {'T':>2} "
          f"{'R':>2} {'O':>2} {'Actions':>8} {'Wall(s)':>8} "
          f"{'Tests':>6} {'Status':<8} Notes")
    print("─" * 100)

    for r in rows:
        task_id, agent, total, A, S, T, R, O, actions, wall_s, pass_rate, status, *notes = r
        notes_str = " ".join(notes)[:40]
        grade = AstroScore(
            A=int(A), S=int(S), T=int(T), R=int(R), O=int(O)
        ).grade()
        print(f"{task_id:<15} {agent:<20} {total:>4}{grade} {A:>2} {S:>2} {T:>2} "
              f"{R:>2} {O:>2} {actions:>8} {wall_s:>8} "
              f"{float(pass_rate):>5.1%} {status:<8} {notes_str}")


def _print_scorecard(record: RunRecord) -> None:
    s = record.score
    width = 45
    print("=" * width)
    print(f"  ASTRO SCORECARD — {record.task_id}")
    print("=" * width)
    print(f"  Agent:    {record.agent}")
    print(f"  Grade:    {s.grade()}  ({s.total()}/25)")
    print(f"  Status:   {s.status().upper()}")
    print("─" * width)
    print(f"  A  Autonomy            {s.A}/5")
    print(f"  S  Solution Complete   {s.S}/5")
    print(f"  T  Technical Correct   {s.T}/5  ← auto-scored")
    print(f"  R  Resilience          {s.R}/5")
    print(f"  O  Output Quality      {s.O}/5")
    print("─" * width)
    print(f"  Actions:  {record.actions}")
    print(f"  Wall:     {record.wall_s:.0f}s")
    print(f"  Tests:    {record.test_pass_rate:.1%}")
    print("=" * width)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ASTRO SWE Agent Evaluation Harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="Prepare task environment")
    p_setup.add_argument("task_id")

    # run
    p_run = sub.add_parser("run", help="Auto-score completed task (T dimension)")
    p_run.add_argument("task_id")
    p_run.add_argument("--agent", default="unknown", help="Agent identifier")

    # score
    p_score = sub.add_parser("score", help="Record full ASTRO scores")
    p_score.add_argument("task_id")
    p_score.add_argument("--agent", required=True)
    p_score.add_argument("--A", type=int, required=True)
    p_score.add_argument("--S", type=int, required=True)
    p_score.add_argument("--T", type=int, required=True)
    p_score.add_argument("--R", type=int, required=True)
    p_score.add_argument("--O", type=int, required=True)
    p_score.add_argument("--actions", type=int, default=0)
    p_score.add_argument("--wall-s", type=float, dest="wall_s", default=0.0)
    p_score.add_argument("--notes", default="")

    # report
    sub.add_parser("report", help="Print results table")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args.task_id)
    elif args.command == "run":
        cmd_run(args.task_id, args.agent)
    elif args.command == "score":
        cmd_score(
            args.task_id, args.agent,
            args.A, args.S, args.T, args.R, args.O,
            args.actions, args.wall_s, args.notes,
        )
    elif args.command == "report":
        cmd_report()


if __name__ == "__main__":
    main()
