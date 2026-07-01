---
name: workflow-validate-judge
description: Use this when running the judge substep of the Validate stage: recompute the physics pass/fail from the primary evidence and author `verdict.json` (`per_test` + `failure_class`) and `semantic_review.json` based on `tests.md`, `diagnostics.json`, and the `raw/` evidence. `aggregate_verdict.json` / `summary.json` / `validate_meta.json` are conductor-authored in `post_judge` (G6); this SKILL covers the `findings` recording for retry routing on a `Validate` failure.
---

# Workflow Validate Judge

## Purpose
As the judge substep of the Validate phase, reproducibly decide the physics pass/fail and record the `findings` classification needed for retry routing on failure. This substep operates in an independent LLM context, and judges based only on the primary evidence the execute substep generated. `aggregate_verdict.json` / `summary.json` / `validate_meta.json` are **conductor-derived** in `post_judge` from this judge's `verdict.json#per_test` + the dependency set — not authored here (G6).

## Scope
- the judgment processing of `workspace/pipelines/<pipeline_id>/runs/<run_id>/<node_key_safe>/`
- the generation of `verdict.json` (`per_test` + `failure_class`) and `semantic_review.json`

## Requirements
- The judgment canonical source is fixed at the target `node`'s `tests.md`, `spec.ir.yaml.io_contract`, and [docs/workflow/RUNNER_OUTPUT_CONTRACT.md](../../docs/workflow/RUNNER_OUTPUT_CONTRACT.md) (the runner-output contract you recompute/judge against — your must-read: `diagnostics.json` checks/verdict → §1, `raw/` per-test evidence → §3).
- `self_verdict` is saved in `verdict.json` as the judgment result of the relevant `node` alone.
- The transitive dependency aggregation (`aggregate_verdict`) and the `blocked` DAG rule (an immediate dependency `node` not built+validated in its own pipeline blocks the relevant `node`) are **conductor-derived in `post_judge`** from `verdict.json#per_test` + the dependency set — not authored here (G6).
- The judgment metrics are recomputed from the execution evidence of `runs/<run_id>/<node_key_safe>/raw/` and reconciled with `diagnostics.json`. Recomputation impossible or inconsistent is a `Validate.judge fail`.
- The recomputation input is limited to the `raw` primary evidence only, and `diagnostics.json` must not be reused as recomputation input.
- The required composition of `raw` is judged using `spec.ir.yaml.io_contract.raw_requirements.required_evidence` as the canonical source, and a fixed evidence composition must not be uniformly required.
- `raw/metrics_basis.json` must be a per-test evidence index targeting all `test_id` of `io_contract.test_evidence_requirements`. When an entry of some `test_id` is missing `required_raw_variables`, or has only a whole-suite summary, it is a `Validate.judge fail`.
- `diagnostics.json` must hold a `checks.<id>` entry for every `io_contract.diagnostics_contract.checks[].id`, and — when `diagnostics_contract.verdict.required=true` — a top-level `verdict` object with the contracted `verdict.fields` (e.g. `verdict.overall` / `verdict.failed_checks`). On shortage it is a `Validate.judge fail`: record `verdict.json#failure_class=structural_violation` and `attribution=code` when the IR's `diagnostics_contract` is present but the runner's `diagnostics.json` does not satisfy it (routes retry to `Generate`); record `attribution=ir` when the IR's `diagnostics_contract` itself is absent or fails to cover `tests.md §3` (routes retry to `Compile`).
- On a physics `fail`, skip the performance evaluation.
- `summary.json` (`self_summary` + `dependency_summary`, with counts consistent with `verdict.json#per_test`) is **conductor-authored in `post_judge`** — not by this judge (G6).
- The LLM semantic-check result is saved as `semantic_review.json` under `runs/<run_id>/<node_key_safe>/`, and requires recording `review_method`, `decision`, `scope.model_ref`, `scope.runner_ref`, `scope.raw_refs`, and `findings`. `review_method` **must be the exact literal string `"llm_semantic_review"`** — the conductor's `--stage pre_judge` gate rejects any other value (e.g. `llm_semantic_recompute`). This is a recoverable judge-authored conformance violation: the conductor warm-resumes you (this same judge context) to re-author `semantic_review.json` with the correct literal, and only terminalizes `fail_closed` if the repair budget is exhausted. Write the exact literal the first time to avoid the repair round.
- `verdict.json` requires recording `failure_class`. The range is one of `physics_fail` / `runtime_error` / `evidence_mismatch` / `structural_violation` / `pass`.
- A `semantic_review.json#findings[*]` that detected a failure requires recording the following keys: `finding_id` (string), `attribution` (one of `code` / `ir` / `spec` / `evidence`), `evidence_refs[]` (path list), `confidence` (`high` / `medium` / `low`), `description` (text). These are the input by which the `orchestration agent` deterministically decides the retry target (Generate / Compile / Spec / Validate.execute) (canonical mapping: the "Decision criteria for retry on failure" section of `docs/workflow/phases/phase_04_validate.md`).
- The storage root for judgment artifacts allows only `workspace/`, and the workflow-root judgment targets only `workspace/`.
- The `--stage pre_judge` gate is run by the **conductor** as two deterministic substeps wrapping you (G4, mirroring the `Compile.static` / `Generate.static` deterministic-gate hoists), not by you. **You invoke no `validate_pipeline_semantics` gate** — you are a pure `LLM` semantic pass. Write `semantic_review.json` with your actual `decision` (`pass`/`fail`) and finalize `verdict.json` (`per_test` + `failure_class`); the deterministic `post_judge` substep first authors the derived `aggregate_verdict.json` / `summary.json` / `validate_meta.json` from your `verdict.json` + the dependency set (G6), then runs `--stage pre_judge` and records the verdict + a severity `disposition` in `post_judge_meta.json`.
  - The conductor gates on both sides: the deterministic **`pre_judge` substep** (index 0) is a dependency-DAG readiness check (a not-yet-built+validated `spec.ir.yaml.dependency.all_nodes` closure fails the phase `fail_closed` before you are even spawned), and the deterministic **`post_judge` substep** (index 3) authors the derived artifacts (G6) and runs the `--stage pre_judge` validator (orchestration-record integrity + the cross-pipeline dependency DAG, scoped to this run) and classifies the violation severity. (Naming caution: the `post_judge` substep runs the validator stage literally named `pre_judge`.) A **recoverable** violation in one of YOUR deliverables (`semantic_review.json` / `verdict.json` — e.g. a wrong `review_method` literal) **warm-resumes you** (this same context, with a slim findings prompt) to re-author the file, then re-runs `post_judge`; write your artifacts correctly the first time to avoid the repair round. An orchestration-record / cross-pipeline-DAG **integrity** violation (or any `pre_judge` `fail`) is a **non-physics integrity blocker** the conductor terminalizes `fail_closed` — even when your physics decision is `pass` — and you neither run nor react to that gate. (You no longer write the `validate` `step_result.json` yourself.)

## Operations Rules
1. Limit the judgment input to only artifacts under the same `run_id`.
2. The judgment input requires the simultaneous existence of `diagnostics.json`, `perf.json`, and the `raw` execution evidence, and when any is missing, `Validate.judge` does not start.
3. The conductor-authored `aggregate_verdict.json` matches the dependency set of `spec.ir.yaml.dependency` by construction (derived in `post_judge`, G6) — you do not author it.
4. Record the quality-comparison result of `impl_defaults.target.class=cpu` as a `quality check`, separated from the `tests` judgment.
5. The comparison canonical source of `quality check` is `diagnostics.json` and `verdict.json`, and pass/fail must not be finalized by `stdout` diff alone.
6. On a judgment failure, make explicit the failure classification in `verdict.json#failure_class`, and specify the return-target stage by `semantic_review.json#findings[*].attribution` (the conductor projects the class into the derived `summary.json`).
7. When the output destination is not `workspace/`, it is a `Validate.judge fail`.
8. When `workspace/` does not exist before workflow execution starts, create `workspace/` directly under the repository root.
9. Before start and before completion, run `python3 tools/validate_workspace_root.py`, and on `fail` it is a `Validate.judge fail`.
10. Do not invoke `validate_pipeline_semantics --stage pre_judge` yourself — the conductor owns it (pre-spawn readiness + post-return gate; see Requirements). Finalize `verdict.json` only from your own recomputation and semantic judgment (the conductor derives `aggregate_verdict.json` from it in `post_judge`, G6); a `pre_judge` gate `fail` is handled by the conductor (`fail_closed`), not by you.
11. When `attribution=spec` is judged, notify the `orchestration agent` to stop with `fail_closed` and record the details (the full finding, evidence_refs, description) in `failure_analysis.json` (no automatic retry).

## Decision Criteria
- The judgment basis is traceable to `tests.md`, `spec.ir.yaml.io_contract`, and `diagnostics.json`.
- The judgment basis is recomputable from the `raw` execution evidence.
- The combination of `verdict.json#failure_class` and `semantic_review.json#findings[*].attribution` is uniquely interpretable by the retry decision table of `docs/workflow/phases/phase_04_validate.md`.
- The conductor-run `pre_judge` gate (post-return) passes — but you neither run it nor gate your own completion on it (a violation is the conductor's `fail_closed`, not a leaf action).
