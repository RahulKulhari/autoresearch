# rubric_program — Autonomous Rubric Research

This is the autoresearch loop applied to rubric design.

**autoresearch** discovers the best model architecture by having an agent iterate
on `train.py` and measuring `val_bpb`.

**rubric_program** discovers the best evaluation rubric for long-horizon SWE tasks
by having an agent iterate on `swe_rubric.md` and measuring `rubric_kendall_tau` —
how well the rubric ranks agents in agreement with human expert judgment.

```
autoresearch           rubric_program
─────────────────────  ─────────────────────────────────────────
train.py     (mutable) swe_rubric.md             (mutable)
prepare.py (immutable) rubric_eval.py          (immutable)
val_bpb       (metric) rubric_kendall_tau         (metric)
5-min budget           judge token budget
```

---

## Setup

1. **Agree on a run tag** based on today's date (e.g. `rubric-mar16`). Branch
   `autoresearch/<tag>` must not already exist.
2. **Create the branch**: `git checkout -b autoresearch/<tag>`
3. **Read all in-scope files**:
   - This file
   - `swe_rubric.md` — the file you modify
   - `rubric_eval.py` — the immutable meta-evaluator. Do not modify.
   - `gold/rankings.json` — human expert gold rankings. Do not modify.
   - `gold/transcripts/*.json` — fixed agent transcripts. Do not modify.
4. **Verify the API key**: `rubric_eval.py` uses `ANTHROPIC_API_KEY`. Check it
   is set. If not, tell the human.
5. **Initialize rubric_results.tsv**: create it with just the header row.
6. **Establish baseline**: run `python rubric_eval.py` on the unmodified
   `swe_rubric.md`. Record the baseline `rubric_kendall_tau`.

---

## The Metric

**`rubric_kendall_tau`** (Kendall's τ, range −1 to +1, higher is better):

- `rubric_eval.py` applies the current `swe_rubric.md` to each of the 8 fixed
  agent transcripts in `gold/transcripts/` using a judge LLM.
- It produces an ASTRO score (0–25) for each transcript.
- It computes Kendall's τ between those 8 scores and the human gold rankings
  in `gold/rankings.json`.
- τ = +1.0 means the rubric ranks agents in perfect agreement with human experts.
- τ = 0.0 means the rubric's rankings are uncorrelated with human judgment.
- τ < 0.0 means the rubric actively inverts the human ordering.

**The goal: maximize `rubric_kendall_tau`.**

Secondary constraint: `judge_cost_usd` should not increase dramatically. A small
τ gain that triples judging cost is not worth it.

---

## What You CAN Do

Modify `swe_rubric.md` — this is the only file you edit. Everything is in scope:

- **Dimension definitions**: rewrite what A, S, T, R, O measure
- **Scoring bands**: change the 0–5 criteria for any dimension
- **Weights**: change relative importance of dimensions (via the `eval_weights`
  field or by restructuring the rubric)
- **R sub-dimensions**: add, remove, or rebalance R-Spec / R-Loop / R-Stop
- **S scoring formula**: change how criteria weights translate to a 0–5 score
- **New dimensions**: add a 6th dimension (changes max score; account for this)
- **Task spec schema**: extend the JSON schema if the rubric needs more fields
- **Anti-gaming rules**: add or refine rules
- **Simplicity criterion**: clarify or sharpen

---

## What You CANNOT Do

- Modify `rubric_eval.py`. It is the ground truth. Its judge prompt, the gold
  transcripts, the gold rankings, and the Kendall τ computation are all fixed.
- Modify anything under `gold/`. The transcripts and rankings are the fixed
  validation set — the equivalent of the pinned validation shard in autoresearch.
- Add new Python dependencies.
- Change the judge model (set in `rubric_eval.py`).

---

## Output Format

Running `python rubric_eval.py` prints:

```
---
rubric_kendall_tau:   0.857143
rubric_spearman:      0.916667
judge_cost_usd:       0.0341
judge_tokens_total:   12480
per_transcript_scores:
  T1 (gold_rank=1): astro=24  A=5 S=5 T=5 R=5 O=4
  T2 (gold_rank=2): astro=21  A=4 S=4 T=5 R=4 O=4
  T3 (gold_rank=3): astro=20  A=5 S=4 T=4 R=4 O=3
  T4 (gold_rank=4): astro=17  A=2 S=4 T=4 R=4 O=3
  T5 (gold_rank=5): astro=15  A=3 S=3 T=3 R=3 O=3
  T6 (gold_rank=6): astro=14  A=4 S=3 T=3 R=1 O=3
  T7 (gold_rank=7): astro=11  A=3 S=2 T=2 R=2 O=2
  T8 (gold_rank=8): astro=5   A=1 S=1 T=1 R=1 O=1
```

Extract the key metric:

```
grep "^rubric_kendall_tau:" run.log
```

---

## Logging Results

Log to `rubric_results.tsv` (tab-separated):

```
commit  rubric_kendall_tau  rubric_spearman  judge_cost_usd  status  description
```

1. git commit hash (7 chars)
2. `rubric_kendall_tau` (e.g. `0.857143`)
3. `rubric_spearman` (secondary, e.g. `0.916667`)
4. `judge_cost_usd` (e.g. `0.034`)
5. status: `keep`, `discard`, or `crash`
6. description: what rubric change was tried

Example:

```
commit  rubric_kendall_tau  rubric_spearman  judge_cost_usd  status  description
a1b2c3d 0.714286            0.833333         0.031          keep    baseline
b2c3d4e 0.857143            0.916667         0.034          keep    split R into R-Spec/R-Loop/R-Stop
c3d4e5f 0.714286            0.833333         0.033          discard add 6th dimension E (efficiency)
d4e5f6g 0.000000            0.000000         0.000          crash   removed T dimension entirely
```

---

## The Experiment Loop

LOOP FOREVER:

1. Check current git state.
2. Modify `swe_rubric.md` with an experimental change.
3. `git commit`
4. Run: `python rubric_eval.py > run.log 2>&1`
5. Extract result: `grep "^rubric_kendall_tau:" run.log`
6. If empty → crashed. Run `tail -50 run.log` to diagnose. Fix and retry or skip.
7. Log to `rubric_results.tsv`.
8. If `rubric_kendall_tau` improved → keep the commit (advance branch).
9. If equal or worse → `git reset --hard` (discard).

**Timeout**: each eval run should complete in under 5 minutes. If it exceeds
10 minutes, kill it, treat as crash.

**NEVER STOP**: once the loop begins, do not pause to ask the human. Run
indefinitely until manually interrupted. If you run out of obvious ideas,
think harder:

- Read the per-transcript scores — which transcripts is the rubric ranking
  wrongly? What dimension is causing the disagreement?
- Look at which gold transcript the rubric most mis-scores and ask why.
- Try restructuring how a dimension is defined vs how the judge interprets it.
- Try adding concrete examples to scoring bands (rubric-as-few-shot-prompt).
- Try decomposing a dimension into sub-scores (like R → R-Spec/R-Loop/R-Stop).
- Try collapsing dimensions that the judge consistently conflates.

**Simplicity criterion**: all else equal, a simpler rubric that achieves the
same τ is better. A rubric that gets τ=0.857 with 3 dimensions is better
than one that gets τ=0.857 with 7 dimensions. Fewer words, clearer criteria,
same predictive power = keep.
