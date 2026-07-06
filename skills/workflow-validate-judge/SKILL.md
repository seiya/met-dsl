---
name: workflow-validate-judge
description: Use this when running the judge substep of the Validate stage: a pure LLM semantic pass that reviews the physics-clean run against `tests.md` / `spec.ir.yaml.io_contract` / the primary evidence and authors ONLY `semantic_review.json` (`decision` + `findings`). `verdict.json` (`per_test` + `failure_class`) is deterministically host-authored at `execute` (R2); `aggregate_verdict.json` / `summary.json` / `validate_meta.json` are conductor-authored at `post_judge` (G6).
---

# Workflow Validate Judge

## Purpose
As the judge substep of the Validate phase, run an independent LLM semantic review of a run that already passed the deterministic per-test verdict, and record a `decision` + `findings` classification for retry routing on a semantic failure. This substep operates in an independent LLM context and reasons only from the primary evidence the execute substep generated.

**You author ONLY `semantic_review.json`.** The mechanical per-test pass/fail (`verdict.json#per_test` + `failure_class`) is now computed **deterministically by the conductor at `execute`** from `io_contract.test_predicates` + `diagnostics.json` (R2). When you run, that verdict is already `pass`/`xfail` (a predicate `fail` fails the execute substep before you are spawned), so your job is to catch what the mechanical predicates cannot: fabricated/inconsistent evidence and physics that does not match the spec intent.

## Scope
- the semantic review of `workspace/pipelines/<pipeline_id>/runs/<run_id>/<node_key_safe>/`
- the generation of `semantic_review.json` only

## Requirements
- The review canonical source is the target `node`'s `tests.md`, `spec.ir.yaml.io_contract`, and [docs/workflow/RUNNER_OUTPUT_CONTRACT.md](../../docs/workflow/RUNNER_OUTPUT_CONTRACT.md) (your must-read: `diagnostics.json` checks/verdict → §1, `raw/` per-test evidence → §3).
- **You do NOT author `verdict.json`.** It is host-authored at `execute` (R2). Read it as the deterministic per-test result you are reviewing, not writing. Likewise `self_verdict`, `aggregate_verdict.json`, `summary.json`, `validate_meta.json` are conductor-derived — never authored here.
- The semantic check reconciles the runner's `diagnostics.json` against the independent `raw/` primary evidence: recompute the judged quantities from `runs/<run_id>/<node_key_safe>/raw/` and confirm they are consistent with `diagnostics.json`. Recomputation impossible, or a diagnostics/raw inconsistency (a fabricated or mismatched `diagnostics.json`), is a **semantic `fail`** — record it as a `findings[*]` with `attribution=code` (the runner emitted untrustworthy evidence) or `attribution=evidence` (the raw evidence itself is missing/malformed). The mechanical verdict trusted `diagnostics.json`; your review is the check that it deserved that trust.
- The recomputation input is limited to the `raw` primary evidence only; `diagnostics.json` must not be reused as recomputation input (else the reconciliation is circular).
- Confirm the primary evidence composition against `spec.ir.yaml.io_contract.raw_requirements.required_evidence` (do not uniformly require a fixed set), and that `raw/metrics_basis.json` is a per-test index covering every `test_id` of `io_contract.test_evidence_requirements`. A missing per-test entry / whole-suite-only summary is a semantic `fail` (`attribution=code`).
- Confirm the physics semantically satisfies `tests.md` intent beyond the mechanical predicate (e.g. the pass is not a degenerate/trivial artifact, invariants stated in prose hold). A semantic mismatch the predicate DSL cannot express is a `fail` with the appropriate `attribution`.
- `semantic_review.json` requires `review_method`, `decision`, `scope.model_ref`, `scope.runner_ref`, `scope.raw_refs`, and `findings`. `review_method` **must be the exact literal string `"llm_semantic_review"`** — the conductor's `--stage pre_judge` gate rejects any other value. This is a recoverable judge-authored conformance violation: the conductor warm-resumes you (this same context) to re-author it, terminalizing `fail_closed` only if the repair budget is exhausted. Write the exact literal the first time.
- A `semantic_review.json#findings[*]` that detected a failure requires: `finding_id` (string), `attribution` (one of `code` / `ir` / `spec` / `evidence`), `evidence_refs[]` (path list), `confidence` (`high` / `medium` / `low`), `description` (text). These are the input by which the retry target is decided (canonical mapping: the "Decision criteria for retry on failure" section of `docs/workflow/phases/phase_04_validate.md`). A `decision=="pass"` needs no findings.
- The storage root for review artifacts allows only `workspace/`.
- The `--stage pre_judge` gate is run by the **conductor** as two deterministic substeps wrapping you (G4), not by you. You are a pure LLM semantic pass: write `semantic_review.json` with your actual `decision` (`pass`/`fail`); the deterministic `post_judge` substep authors the derived `aggregate_verdict.json` / `summary.json` / `validate_meta.json` from the host-authored `verdict.json` + the dependency set (G6), then runs `--stage pre_judge` and records the verdict + a severity `disposition` in `post_judge_meta.json`.
  - The conductor gates on both sides: the `pre_judge` substep (index 0) is a dependency-DAG readiness check (a not-yet-built+validated `dependency.all_nodes` closure fails the phase `fail_closed` before you are spawned), and the `post_judge` substep (index 3) authors the derived artifacts (G6) and runs the `--stage pre_judge` validator (orchestration-record + cross-pipeline DAG integrity, scoped to this run). A **recoverable** violation in your `semantic_review.json` (e.g. a wrong `review_method`) warm-resumes you; an integrity violation (or any `pre_judge` `fail`) is terminalized `fail_closed` by the conductor. (You no longer write the `validate` `step_result.json` yourself.)

## Operations Rules
1. Limit the review input to only artifacts under the same `run_id`.
2. The review input requires the simultaneous existence of `diagnostics.json`, `perf.json`, and the `raw` execution evidence; when any is missing, `Validate.judge` does not start.
3. Record the quality-comparison result of `impl_defaults.target.class=cpu` as a `quality check`, separated from the `tests` judgment; its canonical source is `diagnostics.json` and the host-authored `verdict.json` (never `stdout` diff alone).
4. On a semantic failure, set `semantic_review.json#decision="fail"` and specify the return-target stage by `findings[*].attribution`. Do not edit `verdict.json`.
5. When `attribution=spec` is judged, notify the `orchestration agent` to stop with `fail_closed` and record the details (finding, evidence_refs, description) in `failure_analysis.json` (no automatic retry).
6. Author `semantic_review.json` only under `workspace/` (your single write target). You are a pure LLM semantic pass — run NO validator/gate yourself (neither `validate_pipeline_semantics` nor `validate_workspace_root.py`): the workspace-root layout and the `--stage pre_judge` gate are enforced by the conductor's deterministic `execute` / `pre_judge` / `post_judge` substeps around you.

## Decision Criteria
- The review basis is traceable to `tests.md`, `spec.ir.yaml.io_contract`, and `diagnostics.json`, and recomputable from the `raw` execution evidence.
- `semantic_review.json#decision` reflects the actual reconciliation + semantic judgment; a `fail` carries `findings[*].attribution` uniquely interpretable by the retry decision table of `docs/workflow/phases/phase_04_validate.md`.
- The conductor-run `pre_judge` gate (post-return) passes — but you neither run it nor gate your own completion on it.
