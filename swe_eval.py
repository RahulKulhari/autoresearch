"""
swe_eval.py — SWE Agent ASTRO Evaluation Harness

This is the ground-truth evaluation script for long-horizon SWE agent tasks.
It is read-only from the agent's perspective: do NOT modify this file.

Usage:
    python swe_eval.py setup <task-id>       # Prepare task environment
    python swe_eval.py run   <task-id>       # Auto-score T; print scoring template
    python swe_eval.py score <task-id> ...   # Record full ASTRO scores
    python swe_eval.py report                # Print results table

Scoring:
  T  — auto-scored by running test_command against the final repo state (final
       state only; intermediate logs are not consulted)
  S  — hierarchical: each acceptance criterion is a binary leaf; score is a
       weighted fraction mapped to 0–5 (KLong/PaperBench model)
  R  — composed of three Terminal-Bench sub-dimensions:
         R-Spec (0–2): specification compliance
         R-Loop (0–2): loop avoidance
         R-Stop (0–1): termination awareness
  A, O — holistic 0–5 scores from human/LLM judge reviewing the transcript
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — do not modify
# ---------------------------------------------------------------------------

RESULTS_FILE = Path("swe_results.tsv")
TASKS_DIR = Path("tasks")
EVAL_TESTS_DIR = Path("eval/tests")

TSV_HEADER = "\t".join([
    "task_id", "agent", "astrom_score",
    "A", "S", "T", "R", "R_spec", "R_loop", "R_stop", "O", "M", "milestone_rate",
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
    criteria_weights: list[float] = field(default_factory=list)
    milestones: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, task_id: str) -> "TaskSpec":
        task_file = TASKS_DIR / f"{task_id}.json"
        if not task_file.exists():
            raise FileNotFoundError(f"Task spec not found: {task_file}")
        with open(task_file) as f:
            data = json.load(f)
        data.setdefault("criteria_weights", [])
        data.setdefault("milestones", [])
        return cls(**data)

    def effective_weights(self) -> list[float]:
        """Return per-criterion weights, defaulting to 1.0 when not specified."""
        n = len(self.acceptance_criteria)
        if not self.criteria_weights:
            return [1.0] * n
        if len(self.criteria_weights) != n:
            raise ValueError(
                f"criteria_weights has {len(self.criteria_weights)} entries "
                f"but acceptance_criteria has {n}"
            )
        return [float(w) for w in self.criteria_weights]

    def effective_milestones(self) -> list[dict]:
        """Return the task's milestone list (may be empty if not defined)."""
        return self.milestones or []

    def milestone_weights(self) -> list[float]:
        """Return milestone weights in order."""
        return [float(m.get("weight", 1)) for m in self.effective_milestones()]


@dataclass
class ResilienceScore:
    """Terminal-Bench three-axis resilience decomposition."""
    spec: int = 0   # R-Spec  (0–2): specification compliance
    loop: int = 0   # R-Loop  (0–2): loop avoidance
    stop: int = 0   # R-Stop  (0–1): termination awareness

    def total(self) -> int:
        return self.spec + self.loop + self.stop

    def validate(self) -> None:
        if not (0 <= self.spec <= 2):
            raise ValueError(f"R-Spec must be 0–2, got {self.spec}")
        if not (0 <= self.loop <= 2):
            raise ValueError(f"R-Loop must be 0–2, got {self.loop}")
        if not (0 <= self.stop <= 1):
            raise ValueError(f"R-Stop must be 0–1, got {self.stop}")


@dataclass
class AstroScore:
    A: int = 0                         # Autonomy
    S: int = 0                         # Solution Completeness (hierarchical)
    T: int = 0                         # Technical Correctness (auto-scored)
    resilience: ResilienceScore = field(default_factory=ResilienceScore)
    O: int = 0                         # Output Quality
    M: int = 0                         # Milestone Progress (process reward)
    milestone_rate: float = 0.0        # Raw weighted fraction of milestones reached

    @property
    def R(self) -> int:
        return self.resilience.total()

    def total(self) -> int:
        return self.A + self.S + self.T + self.R + self.O + self.M

    def grade(self) -> str:
        t = self.total()
        if t >= 27: return "S"
        if t >= 24: return "A"
        if t >= 18: return "B"
        if t >= 12: return "C"
        if t >= 6:  return "D"
        return "F"

    def status(self) -> str:
        t = self.total()
        if t >= GOOD_THRESHOLD:  return "pass"
        if t >= PASS_THRESHOLD:  return "partial"
        if t > 0:                return "fail"
        return "crash"

    def validate(self) -> None:
        for name, val, lo, hi in [
            ("A", self.A, 0, 5),
            ("S", self.S, 0, 5),
            ("T", self.T, 0, 5),
            ("O", self.O, 0, 5),
            ("M", self.M, 0, 5),
        ]:
            if not (lo <= val <= hi):
                raise ValueError(f"Dimension {name} must be {lo}–{hi}, got {val}")
        self.resilience.validate()


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
        r = s.resilience
        return "\t".join([
            self.task_id,
            self.agent,
            str(s.total()),
            str(s.A), str(s.S), str(s.T),
            str(s.R), str(r.spec), str(r.loop), str(r.stop),
            str(s.O), str(s.M), f"{s.milestone_rate:.3f}",
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

    Evaluates the FINAL STATE of the repository only — no intermediate logs
    are inspected. This is the ground truth; do not modify.

    Returns (0.0, violation_msg) immediately if eval/tests/ has been touched.
    """
    # Anti-gaming: verify eval/tests/ is unmodified
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
    Parse pytest / unittest / generic 'X/Y' output to a [0, 1] pass rate.
    Returns 0.0 if unparseable.
    """
    import re

    passed = 0
    failed = 0
    errors = 0

    m = re.search(r"(\d+) passed", output)
    if m: passed = int(m.group(1))

    m = re.search(r"(\d+) failed", output)
    if m: failed = int(m.group(1))

    m = re.search(r"(\d+) error", output)
    if m: errors = int(m.group(1))

    total = passed + failed + errors
    if total == 0:
        m = re.search(r"(\d+)\s*/\s*(\d+)", output)
        if m:
            passed = int(m.group(1))
            total = int(m.group(2))
        else:
            return 0.0

    return passed / total if total > 0 else 0.0


def score_t_dimension(pass_rate: float, test_output: str) -> int:
    """Deterministically map pass_rate → T score (0–5)."""
    if "ANTI-GAMING VIOLATION" in test_output:
        return 0
    if pass_rate >= 1.0:  return 5
    if pass_rate >= 0.90: return 4
    if pass_rate >= 0.70: return 3
    if pass_rate >= 0.50: return 2
    if pass_rate > 0.0:   return 1
    return 0


def score_m_dimension(
    milestones_reached: list[bool],
    weights: list[float],
) -> tuple[int, float]:
    """
    Milestone Progress (M) scoring — process reward model.

    Mirrors score_s_dimension but operates on intermediate milestone checkpoints
    rather than final acceptance criteria.

    Args:
        milestones_reached: parallel list of True/False per milestone
        weights:            parallel list of weights

    Returns:
        (M score 0–5, milestone_rate 0.0–1.0)
    """
    if not milestones_reached:
        return 0, 0.0
    total_weight = sum(weights)
    if total_weight == 0:
        return 0, 0.0
    weighted_reached = sum(w for w, hit in zip(weights, milestones_reached) if hit)
    rate = weighted_reached / total_weight
    return min(5, round(5.0 * rate)), rate


def score_s_dimension(
    criteria_met: list[bool],
    weights: list[float],
) -> int:
    """
    Hierarchical S scoring (KLong/PaperBench model).

    Each acceptance criterion is a binary leaf. S = 5 × weighted_fraction,
    rounded to nearest integer.

    Args:
        criteria_met: parallel list of True/False for each criterion
        weights:      parallel list of weights (same length as criteria_met)

    Returns:
        Integer score 0–5.
    """
    if not criteria_met:
        return 0
    if len(criteria_met) != len(weights):
        raise ValueError("criteria_met and weights must have the same length")

    total_weight = sum(weights)
    if total_weight == 0:
        return 0

    weighted_met = sum(w for w, met in zip(weights, criteria_met) if met)
    fraction = weighted_met / total_weight
    return min(5, round(5.0 * fraction))


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

    result = subprocess.run(
        ["git", "checkout", task.initial_commit],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[ERROR] Could not checkout {task.initial_commit}:\n{result.stderr}")
        sys.exit(1)
    print(f"Checked out initial commit: {task.initial_commit}")

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

    weights = task.effective_weights()
    print("Acceptance criteria (weight):")
    for i, (criterion, w) in enumerate(zip(task.acceptance_criteria, weights), 1):
        print(f"  {i}. [{w:g}] {criterion}")
    print()
    print(f"Budget: {task.budget.get('max_actions', 150)} actions, "
          f"{task.budget.get('max_wall_seconds', 1800)}s wall time")
    print()
    print("Setup complete. Agent may now begin.")


def cmd_run(task_id: str, agent: str) -> None:
    """Auto-score T; print scoring template for human dimensions."""
    task = TaskSpec.load(task_id)
    print(f"Scoring task: {task.id} — {task.title}")
    print(f"Agent: {agent}")
    print()

    pass_rate, _ = run_tests(task)
    t_score = score_t_dimension(pass_rate, _)
    print(f"T dimension (auto-scored): {t_score}/5  (pass rate: {pass_rate:.1%})")
    print()

    weights = task.effective_weights()
    print("─" * 60)
    print("HUMAN SCORING REQUIRED")
    print("─" * 60)
    print()
    print("S — Solution Completeness (hierarchical)")
    print("  Mark each acceptance criterion as met (1) or not (0):")
    for i, (criterion, w) in enumerate(zip(task.acceptance_criteria, weights), 1):
        print(f"    [{i}] weight={w:g}  {criterion}")
    print("  Then pass --S-criteria '1,1,0,...' to swe_eval.py score")
    print("  (or use --S <0-5> to override directly)")
    print()
    print("A — Agent Autonomy (0–5): Did it work independently?")
    print()
    print("R — Resilience sub-scores:")
    print("  --R-spec (0–2): Did it respect explicit constraints?")
    print("    2 = no violations, 1 = minor deviation, 0 = major violation")
    print("  --R-loop (0–2): Did it avoid repeating failed approaches?")
    print("    2 = no loops, 1 = escaped one loop, 0 = stuck or needed nudge")
    print("  --R-stop (0–1): Did it stop at the right time?")
    print("    1 = clean stop at success/futility, 0 = continued past done or quit early")
    print()
    print("O — Output Quality (0–5): Is the code clean and idiomatic?")
    print()

    milestones = task.effective_milestones()
    if milestones:
        print("M — Milestone Progress (process reward, 0–5)")
        print("  Review the transcript and mark each milestone reached (1) or not (0):")
        for m in milestones:
            print(f"    [{m['id']}] weight={m['weight']}  {m['description']}")
        print("  Then pass --M-milestones '1,0,1,...' (or --M <0-5> to override)")
    else:
        print("M — Milestone Progress (0–5): no milestones defined for this task; use --M 0")
    print()
    print("Run this to record scores:")
    print(f"  python swe_eval.py score {task_id} --agent {agent} \\")
    print(f"    --A <0-5> --S-criteria '1,1,...' --T {t_score} \\")
    print(f"    --R-spec <0-2> --R-loop <0-2> --R-stop <0-1> \\")
    print(f"    --O <0-5> --M-milestones '1,0,...' \\")
    print(f"    --actions <n> --wall-s <s> --notes '<notes>'")


def cmd_score(
    task_id: str,
    agent: str,
    A: int,
    S_override: int | None,
    S_criteria_str: str | None,
    T: int,
    R_spec: int,
    R_loop: int,
    R_stop: int,
    O: int,
    M_override: int | None,
    M_milestones_str: str | None,
    actions: int,
    wall_s: float,
    notes: str,
) -> None:
    """Record full ASTRO scores to swe_results.tsv."""
    task = TaskSpec.load(task_id)

    # ── Compute S ───────────────────────────────────────────────────────────
    if S_criteria_str is not None:
        criteria_met = [bool(int(x.strip())) for x in S_criteria_str.split(",")]
        if len(criteria_met) != len(task.acceptance_criteria):
            print(
                f"[ERROR] --S-criteria has {len(criteria_met)} values but "
                f"task has {len(task.acceptance_criteria)} criteria."
            )
            sys.exit(1)
        S = score_s_dimension(criteria_met, task.effective_weights())
        print(f"S dimension (hierarchical): {S}/5")
    elif S_override is not None:
        S = S_override
        print(f"S dimension (manual override): {S}/5")
    else:
        print("[ERROR] Provide either --S-criteria or --S")
        sys.exit(1)

    # ── Re-run tests for authoritative T ────────────────────────────────────
    pass_rate, test_output = run_tests(task)
    t_auto = score_t_dimension(pass_rate, test_output)
    if T != t_auto:
        print(f"[WARNING] Provided T={T} differs from auto-scored T={t_auto}. "
              f"Using auto-scored value.")
    T = t_auto

    # ── Compute M ───────────────────────────────────────────────────────────
    if M_milestones_str is not None:
        m_reached = [bool(int(x.strip())) for x in M_milestones_str.split(",")]
        m_weights = task.milestone_weights()
        if not m_weights:
            m_weights = [1.0] * len(m_reached)
        if len(m_reached) != len(m_weights):
            print(f"[ERROR] --M-milestones has {len(m_reached)} values but task has "
                  f"{len(m_weights)} milestones.")
            sys.exit(1)
        M, milestone_rate = score_m_dimension(m_reached, m_weights)
        print(f"M dimension (process reward): {M}/5  (milestone rate: {milestone_rate:.1%})")
    elif M_override is not None:
        M = M_override
        milestone_rate = M / 5.0
        print(f"M dimension (manual override): {M}/5")
    else:
        print("[ERROR] Provide either --M-milestones or --M")
        sys.exit(1)

    resilience = ResilienceScore(spec=R_spec, loop=R_loop, stop=R_stop)
    score = AstroScore(A=A, S=S, T=T, resilience=resilience, O=O, M=M,
                       milestone_rate=milestone_rate)
    score.validate()

    record = RunRecord(
        task_id=task_id,
        agent=agent,
        score=score,
        actions=actions,
        wall_s=wall_s,
        test_pass_rate=pass_rate,
        notes=notes,
    )

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

    with open(RESULTS_FILE) as f:
        lines = f.read().splitlines()

    if len(lines) < 2:
        print("No results recorded yet.")
        return

    rows = [line.split("\t") for line in lines[1:] if line.strip()]
    rows = [r for r in rows if len(r) >= 18]

    if not rows:
        print("No results recorded yet.")
        return

    print(f"{'Task':<15} {'Agent':<20} {'ASTROM':>6} {'A':>2} {'S':>2} {'T':>2} "
          f"{'R':>2} {'Rsp':>3} {'Rlp':>3} {'Rst':>3} {'O':>2} {'M':>2} {'Mile%':>6} "
          f"{'Actions':>7} {'Wall':>6} {'Tests':>6} {'Status':<8} Notes")
    print("─" * 128)

    for r in rows:
        (task_id, agent, total, A, S, T, R, R_spec, R_loop, R_stop, O, M, mile_rate,
         actions, wall_s, pass_rate, status, *notes) = r
        notes_str = " ".join(notes)[:30]
        grade = AstroScore(
            A=int(A), S=int(S), T=int(T),
            resilience=ResilienceScore(int(R_spec), int(R_loop), int(R_stop)),
            O=int(O), M=int(M),
        ).grade()
        print(f"{task_id:<15} {agent:<20} {total:>5}{grade} {A:>2} {S:>2} {T:>2} "
              f"{R:>2} {R_spec:>3} {R_loop:>3} {R_stop:>3} {O:>2} {M:>2} "
              f"{float(mile_rate):>5.1%} "
              f"{actions:>7} {wall_s:>6} {float(pass_rate):>5.1%} "
              f"{status:<8} {notes_str}")


def _print_scorecard(record: RunRecord) -> None:
    s = record.score
    r = s.resilience
    width = 50
    print("=" * width)
    print(f"  ASTRO SCORECARD — {record.task_id}")
    print("=" * width)
    print(f"  Agent:    {record.agent}")
    print(f"  Grade:    {s.grade()}  ({s.total()}/30)")
    print(f"  Status:   {s.status().upper()}")
    print("─" * width)
    print(f"  A  Autonomy                    {s.A}/5")
    print(f"  S  Solution Completeness       {s.S}/5  (hierarchical, outcome)")
    print(f"  T  Technical Correctness       {s.T}/5  ← auto-scored (final state)")
    print(f"  R  Resilience                  {s.R}/5")
    print(f"       R-Spec  spec compliance   {r.spec}/2")
    print(f"       R-Loop  loop avoidance    {r.loop}/2")
    print(f"       R-Stop  termination       {r.stop}/1")
    print(f"  O  Output Quality              {s.O}/5")
    print(f"  M  Milestone Progress          {s.M}/5  (process, {s.milestone_rate:.1%} reached)")
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
    p = sub.add_parser("setup", help="Prepare task environment")
    p.add_argument("task_id")

    # run
    p = sub.add_parser("run", help="Auto-score T; print scoring template")
    p.add_argument("task_id")
    p.add_argument("--agent", default="unknown")

    # score
    p = sub.add_parser("score", help="Record full ASTRO scores")
    p.add_argument("task_id")
    p.add_argument("--agent", required=True)
    p.add_argument("--A", type=int, required=True)
    # S: either hierarchical criteria or direct override
    s_grp = p.add_mutually_exclusive_group(required=True)
    s_grp.add_argument(
        "--S-criteria",
        dest="S_criteria",
        metavar="'1,0,1,...'",
        help="Comma-separated 0/1 per acceptance criterion (computes S via weighted sum)",
    )
    s_grp.add_argument("--S", type=int, dest="S_override", help="Direct S score 0–5")
    p.add_argument("--T", type=int, required=True,
                   help="Expected T (will be overridden by auto-scored value)")
    # R sub-dimensions
    p.add_argument("--R-spec", type=int, required=True, dest="R_spec",
                   help="Spec compliance (0–2)")
    p.add_argument("--R-loop", type=int, required=True, dest="R_loop",
                   help="Loop avoidance (0–2)")
    p.add_argument("--R-stop", type=int, required=True, dest="R_stop",
                   help="Termination awareness (0–1)")
    p.add_argument("--O", type=int, required=True)
    # M: either milestone list or direct override
    m_grp = p.add_mutually_exclusive_group(required=True)
    m_grp.add_argument(
        "--M-milestones",
        dest="M_milestones",
        metavar="'1,0,1,...'",
        help="Comma-separated 0/1 per milestone (computes M via weighted sum)",
    )
    m_grp.add_argument("--M", type=int, dest="M_override", help="Direct M score 0–5")
    p.add_argument("--actions", type=int, default=0)
    p.add_argument("--wall-s", type=float, dest="wall_s", default=0.0)
    p.add_argument("--notes", default="")

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
            args.A,
            args.S_override,
            args.S_criteria,
            args.T,
            args.R_spec, args.R_loop, args.R_stop,
            args.O,
            args.M_override,
            args.M_milestones,
            args.actions, args.wall_s, args.notes,
        )
    elif args.command == "report":
        cmd_report()


if __name__ == "__main__":
    main()
