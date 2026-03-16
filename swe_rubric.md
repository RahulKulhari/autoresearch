# SWE Agent ASTRO Rubric
## Evaluating Long-Horizon Software Engineering Tasks

This rubric defines how to evaluate a SWE agent's performance on long-horizon programming tasks — tasks that require multiple sequential steps, codebase understanding, iterative refinement, and error recovery to complete.

The rubric is inspired by the autoresearch framework: a fixed budget, a ground-truth metric, and a structured loop. Here, the "metric" is the **ASTRO score** (0–25), and the "ground truth" is a task-specific test suite that the agent cannot modify.

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

| Score | Criterion |
|-------|-----------|
| 5 | All tests pass (unit, integration, regression); no existing tests broken |
| 4 | ≥90% tests pass; at most 1 regression |
| 3 | ≥70% tests pass; some regressions |
| 2 | ≥50% tests pass; significant regressions |
| 1 | <50% tests pass |
| 0 | Crashes, no tests pass, or the agent deleted/disabled tests |

> **Immutability rule**: The ground-truth test suite lives in `eval/tests/` and is read-only. An agent that modifies, skips, or disables tests receives a score of 0 for this dimension, regardless of apparent test results.

### R — Resilience & Recovery (0–5)

*Does the agent recover gracefully from failures, errors, and unexpected states?*

| Score | Criterion |
|-------|-----------|
| 5 | Handled all errors encountered; no repeated identical mistakes |
| 4 | Recovered from most errors; at most one unproductive loop |
| 3 | Some error recovery; got stuck ≤2 times but eventually self-corrected |
| 2 | Frequent repetition of failed approaches; needed external nudge to escape |
| 1 | Got stuck on first significant error and did not recover |
| 0 | Crashed the environment, gave up immediately, or caused irreversible damage |

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
  8. Run test_command on agent's final state → T dimension
  9. Human/LLM judge reviews transcript → A, R, O dimensions
  10. Review acceptance criteria against final state → S dimension
  11. Compute ASTRO score

LOG:
  12. Append row to results.tsv (see format below)
```

---

## Results Logging

Log results to `swe_results.tsv` (tab-separated, NOT comma-separated):

```
task_id	agent	astro_score	A	S	T	R	O	actions	wall_s	test_pass_rate	status	notes
```

Columns:
1. `task_id` — task identifier from spec
2. `agent` — agent identifier (e.g. `claude-opus-4-6`, `gpt-4o`)
3. `astro_score` — total ASTRO score (0–25)
4. `A`, `S`, `T`, `R`, `O` — individual dimension scores (0–5 each)
5. `actions` — total agent actions taken
6. `wall_s` — wall-clock seconds elapsed
7. `test_pass_rate` — fraction of tests passed (e.g. `0.923`)
8. `status` — `pass`, `partial`, `fail`, or `crash`
9. `notes` — short description of outcome

Example row:

```
task-001	claude-opus-4-6	21	5	4	5	4	3	87	412	1.000	pass	Fixed race condition; clean diff; 1 edge case missed
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
