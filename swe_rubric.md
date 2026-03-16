# SWE Agent ASTRO Rubric
## Evaluating Long-Horizon Software Engineering Tasks

This rubric defines how to evaluate a SWE agent's performance on long-horizon programming tasks — tasks that require multiple sequential steps, codebase understanding, iterative refinement, and error recovery to complete.

The rubric is inspired by the autoresearch framework: a fixed budget, a ground-truth metric, and a structured loop. Here, the "metric" is the **ASTRO score** (0–25), and the "ground truth" is a task-specific test suite that the agent cannot modify.

Design draws on two external benchmarks:
- **KLong / PaperBench** (arXiv 2602.17547) — hierarchical rubric trees with weighted binary leaf criteria for fine-grained partial credit
- **Terminal-Bench** — outcome-driven final-state verification and three failure-mode sub-dimensions for resilience scoring

---

## What Is a Long-Horizon SWE Task?

A long-horizon SWE task is characterized by:

- **Scope**: Requires changes to **3 or more files**
- **Depth**: Requires understanding of how components interact
- **Iteration**: Requires running tests, reading errors, and refining the solution
- **Ambiguity**: Requires interpreting a high-level goal into concrete steps
- **Length**: Typically requires **30–150+ agent actions** (tool calls) to complete

Example categories:
1. **Bug Fix** — Diagnose and fix a subtle, multi-file bug (e.g. race condition, data corruption)
2. **Feature Implementation** — Add a non-trivial feature to an existing codebase
3. **Refactoring** — Restructure code to improve quality without breaking behavior
4. **Test Suite Addition** — Write comprehensive tests for untested code paths
5. **Performance Optimization** — Profile, identify, and fix a performance bottleneck

---

## The ASTRO Rubric

Five dimensions, each scored **0–5**, for a total of **0–25 points**.

### A — Agent Autonomy (0–5)

*Does the agent work independently, without requiring hints or clarification?*

| Score | Criterion |
|-------|-----------|
| 5 | Completed the full task with zero clarification requests |
| 4 | At most one reasonable clarification; rest was autonomous |
| 3 | Completed the task but required 2–3 mid-task nudges |
| 2 | Needed repeated guidance; human intervention was substantial |
| 1 | Task incomplete even with guidance; agent lost direction |
| 0 | Agent failed to start, asked to be given the answer, or was completely off-track |

### S — Solution Completeness (0–5)

*Does the solution fulfill the task specification?*

**Hierarchical scoring** (inspired by KLong/PaperBench): each task defines a list of acceptance criteria — atomic, expert-reviewable leaf nodes, each verifiable in under 15 minutes. The S score is computed as a weighted sum rather than a coarse lookup:

```
S = 5.0 × Σ(weight_i × met_i) / Σ(weight_i)
```

Where `met_i` is 1 if criterion i is satisfied and 0 if not, and `weight_i` defaults to 1.0 unless overridden in the task spec via `criteria_weights`. The result is rounded to the nearest integer (0–5).

This means partial completion is always rewarded proportionally — an agent that meets 6 of 8 equal-weight criteria earns S=4 rather than an arbitrary S=2 or S=3.

**Criteria classification** — when writing task acceptance criteria, label each with its weight class:

| Weight | Label | Meaning |
|--------|-------|---------|
| 3 | `core` | Without this the task is not done |
| 2 | `important` | Strongly expected, hard to miss |
| 1 | `nice` | Edge case, robustness, or polish |

**Override examples in task spec:**
```json
"acceptance_criteria": [
  "Atomic writes use temp-file + rename",
  "Corrupt cache raises RuntimeError",
  "Existing valid caches still load",
  "Public API signatures unchanged"
],
"criteria_weights": [3, 3, 2, 1]
```

**Fallback scale** (when criteria weights are not defined):

| Score | Criterion |
|-------|-----------|
| 5 | All acceptance criteria met; no regressions introduced |
| 4 | Core requirement met; 1–2 minor acceptance criteria missed |
| 3 | Partial solution; main functionality works but edge cases missing |
| 2 | Skeleton or stub implementation; task largely unfinished |
| 1 | Attempted but incorrect; acceptance criteria mostly unmet |
| 0 | No meaningful implementation present |

### T — Technical Correctness (0–5)

*Is the solution correct? Does it pass the ground-truth test suite?*

**Auto-scored** by running `test_command` from the task spec against the agent's final repo state. T is never entered manually.

| Score | Criterion |
|-------|-----------|
| 5 | All tests pass (unit, integration, regression); no existing tests broken |
| 4 | ≥90% tests pass; at most 1 regression |
| 3 | ≥70% tests pass; some regressions |
| 2 | ≥50% tests pass; significant regressions |
| 1 | <50% tests pass |
| 0 | Crashes, no tests pass, or the agent deleted/disabled tests |

> **Final-state only** (Terminal-Bench lesson): tests verify the *final repository state* — file contents, exit codes, produced outputs — not the agent's intermediate commands or logs. A test must be a deterministic assertion about the artifact the agent left behind. Tests that inspect tool call traces or check "whether the agent ran command X" are not valid ground-truth tests.

> **Immutability rule**: The ground-truth test suite lives in `eval/tests/` and is read-only. An agent that modifies, skips, or disables any file under `eval/tests/` receives T = 0, regardless of apparent test results. `swe_eval.py` enforces this with a `git diff` check before running the suite.

### R — Resilience & Recovery (0–5)

*Does the agent recover gracefully from failures, errors, and unexpected states?*

**Composed of three sub-dimensions** (from Terminal-Bench failure mode taxonomy). R = R-Spec + R-Loop + R-Stop.

#### R-Spec — Specification Compliance (0–2)

*Did the agent respect the explicit constraints in the task description?*

| Score | Criterion |
|-------|-----------|
| 2 | No explicit constraint violated (required method, output path, API shape, etc.) |
| 1 | Minor violation: deviated from a non-critical constraint with a plausible reason |
| 0 | Major violation: directly contradicted a core task directive |

#### R-Loop — Loop Avoidance (0–2)

*Did the agent escape repeated-failure loops without external help?*

| Score | Criterion |
|-------|-----------|
| 2 | No unproductive loops; each retry used a meaningfully different strategy |
| 1 | Got stuck once (ran same failing approach ≥2 times) but self-corrected without a nudge |
| 0 | Repeated the same failing approach ≥3 times, OR required external intervention to escape |

#### R-Stop — Termination Awareness (0–1)

*Did the agent know when to stop?*

| Score | Criterion |
|-------|-----------|
| 1 | Stopped at the right moment: after confirming success, or after recognising clear futility |
| 0 | Continued past a clear success ("done but kept going"), gave up prematurely, or declared completion while tests still fail |

**R scoring summary:**

| R-Spec | R-Loop | R-Stop | Total R | Interpretation |
|--------|--------|--------|---------|----------------|
| 2 | 2 | 1 | **5** | Ideal — no violations, no loops, clean stop |
| 2 | 1 | 1 | **4** | Recovered from one loop; otherwise clean |
| 1 | 2 | 1 | **4** | Minor spec deviation; loop-free |
| 2 | 0 | 1 | **3** | Couldn't escape a loop without help |
| 0 | 2 | 1 | **3** | Broke a core constraint but executed cleanly |
| 0 | 0 | 0 | **0** | Complete failure across all resilience axes |

### O — Output Quality (0–5)

*Is the code clean, idiomatic, and maintainable?*

| Score | Criterion |
|-------|-----------|
| 5 | Idiomatic, well-structured, minimal added complexity; simplicity wins |
| 4 | Clean code with at most minor style issues |
| 3 | Functional but has redundant/ugly sections; noticeable tech debt introduced |
| 2 | Works but code is hard to follow; significant complexity added unnecessarily |
| 1 | Poor code that barely functions; would require a full rewrite |
| 0 | No meaningful output or code is harmful/dangerous |

---

## Scoring Summary

```
ASTRO Score = A + S + T + R + O

Range:    0 – 25
Passing:  ≥ 15  (60%)
Good:     ≥ 20  (80%)
Excellent: 23+  (92%+)
```

### Grade Bands

| Score | Grade | Interpretation |
|-------|-------|----------------|
| 23–25 | S | Production-ready; exceeds expectations |
| 20–22 | A | Strong performance; minor gaps only |
| 15–19 | B | Passing; core task done with issues |
| 10–14 | C | Partial completion; significant gaps |
| 5–9   | D | Mostly failed; marginal attempt |
| 0–4   | F | Complete failure |

---

## Task Specification Format

Each long-horizon task is defined in a JSON file with the following schema:

```json
{
  "id": "task-001",
  "title": "Short human-readable title",
  "category": "bug_fix | feature | refactor | tests | performance",
  "difficulty": "medium | hard | very_hard",
  "description": "Full prose description of what the agent must accomplish.",
  "repo": "path/or/url/to/starting/codebase",
  "initial_commit": "git SHA of the starting state",
  "acceptance_criteria": [
    "Criterion 1 written as a verifiable statement",
    "Criterion 2 ...",
    "..."
  ],
  "criteria_weights": [3, 2, 1],
  "test_command": "command to run to execute the ground-truth test suite",
  "budget": {
    "max_actions": 150,
    "max_wall_seconds": 1800
  },
  "eval_weights": {
    "note": "Optional per-task overrides to ASTRO dimension weights (default 1.0 each)"
  }
}
```

`criteria_weights` is optional. When omitted, all criteria are weighted equally. Weights do not need to sum to any particular value — only the ratios matter. A weight of `3` means that criterion counts three times as much as a weight-`1` criterion.

---

## The Evaluation Loop

The evaluation protocol mirrors the autoresearch loop: set up the environment once, run the agent, score the output, log results.

```
SETUP:
  1. Load task spec from tasks/<task-id>.json
  2. Restore repo to initial_commit state (clean git checkout)
  3. Confirm test suite baseline: run test_command, expect known-failing state
  4. Start action counter and wall clock

RUN AGENT:
  5. Give the agent the task description and repo access
  6. Agent works autonomously until: task complete, budget exhausted, or agent stops
  7. Record total actions taken, wall time, and final git diff

SCORE:
  8.  Run test_command on agent's final state → T dimension (auto, final state only)
  9.  For each acceptance criterion: mark met (1) or not (0) → compute weighted S
  10. Review transcript for autonomy → A dimension
  11. Review transcript for R-Spec, R-Loop, R-Stop → sum for R dimension
  12. Review final diff for code quality → O dimension
  13. Compute ASTRO score = A + S + T + R + O

LOG:
  12. Append row to results.tsv (see format below)
```

---

## Results Logging

Log results to `swe_results.tsv` (tab-separated, NOT comma-separated):

```
task_id	agent	astro_score	A	S	T	R	R_spec	R_loop	R_stop	O	actions	wall_s	test_pass_rate	status	notes
```

Columns:
1. `task_id` — task identifier from spec
2. `agent` — agent identifier (e.g. `claude-opus-4-6`, `gpt-4o`)
3. `astro_score` — total ASTRO score (0–25)
4. `A` — autonomy score (0–5)
5. `S` — solution completeness, computed via hierarchical criteria weights (0–5)
6. `T` — technical correctness, auto-scored from test suite (0–5)
7. `R` — total resilience = R_spec + R_loop + R_stop (0–5)
8. `R_spec` — specification compliance sub-score (0–2)
9. `R_loop` — loop avoidance sub-score (0–2)
10. `R_stop` — termination awareness sub-score (0–1)
11. `O` — output quality (0–5)
12. `actions` — total agent actions taken
13. `wall_s` — wall-clock seconds elapsed
14. `test_pass_rate` — fraction of tests passed (e.g. `0.923`)
15. `status` — `pass`, `partial`, `fail`, or `crash`
16. `notes` — short description of outcome

Example row:

```
task-001	claude-opus-4-6	21	5	4	5	4	2	1	1	3	87	412	1.000	pass	Fixed race condition; clean diff; 1 edge case missed
```

---

## Simplicity Criterion

Identical to the autoresearch simplicity criterion:

> All else being equal, simpler is better. A small improvement that adds ugly complexity is not worth it. Conversely, a solution that achieves the goal with *less* code than expected is excellent.

In scoring terms: two solutions with equal T (test pass rate) and S (acceptance criteria) should have the one with lower O (code complexity) score **higher** on the O dimension.

---

## Anti-Gaming Rules

The following behaviors result in automatic score penalties:

| Behavior | Penalty |
|----------|---------|
| Modifying or deleting test files in `eval/tests/` | T = 0 |
| Hardcoding expected test outputs | T = 0, O = 0 |
| Exceeding the action budget | Cap A at max 3 |
| Asking the evaluator for the answer | A = 0 |
| Introducing security vulnerabilities | O = 0 |
| Committing secrets or credentials | O = 0, immediate disqualification |
