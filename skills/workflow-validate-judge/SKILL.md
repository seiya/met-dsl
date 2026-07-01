---
name: workflow-validate-judge
description: Use this when running the judge substep of the Validate stage and judging `verdict.json`, `aggregate_verdict.json`, and `summary.json` based on `tests.md`, `diagnostics.json`, and dependency information. It applies to the work of the `blocked` judgment of the dependency `DAG`, the `self_verdict` / `aggregate_verdict` aggregation, and the `findings` recording for retry routing on a `Validate` failure.
---

# Workflow Validate Judge

## Purpose
As the judge substep of the Validate phase, reproducibly decide the physics pass/fail and the dependency-aggregated pass/fail, and record the `findings` classification needed for retry routing on failure. This substep operates in an independent LLM context, and judges based only on the primary evidence the execute substep generated.

## Scope
- the judgment processing of `workspace/pipelines/<pipeline_id>/runs/<run_id>/<node_key_safe>/`
- the generation of `verdict.json`, `aggregate_verdict.json`, `summary.json`, and `semantic_review.json`

## Requirements
- The judgment canonical source is fixed at the target `node`'s `tests.md`, `spec.ir.yaml.io_contract`, and [docs/workflow/RUNNER_OUTPUT_CONTRACT.md](../../docs/workflow/RUNNER_OUTPUT_CONTRACT.md) (the runner-output contract you recompute/judge against — your must-read: `diagnostics.json` checks/verdict → §1, `raw/` per-test evidence → §3).
- `self_verdict` is saved in `verdict.json` as the judgment result of the relevant `node` alone.
- `aggregate_verdict` aggregates the transitive dependency `node` and is saved in `aggregate_verdict.json`.
- When an immediate dependency `node` is not `pass` or `xfail`, make the relevant `node` `blocked`.
- The judgment metrics are recomputed from the execution evidence of `runs/<run_id>/<node_key_safe>/raw/` and reconciled with `diagnostics.json`. Recomputation impossible or inconsistent is a `Validate.judge fail`.
- The recomputation input is limited to the `raw` primary evidence only, and `diagnostics.json` must not be reused as recomputation input.
- The required composition of `raw` is judged using `spec.ir.yaml.io_contract.raw_requirements.required_evidence` as the canonical source, and a fixed evidence composition must not be uniformly required.
- `raw/metrics_basis.json` must be a per-test evidence index targeting all `test_id` of `io_contract.test_evidence_requirements`. When an entry of some `test_id` is missing `required_raw_variables`, or has only a whole-suite summary, it is a `Validate.judge fail`.
- `diagnostics.json` must hold a `checks.<id>` entry for every `io_contract.diagnostics_contract.checks[].id`, and — when `diagnostics_contract.verdict.required=true` — a top-level `verdict` object with the contracted `verdict.fields` (e.g. `verdict.overall` / `verdict.failed_checks`). On shortage it is a `Validate.judge fail`: record `verdict.json#failure_class=structural_violation` and `attribution=code` when the IR's `diagnostics_contract` is present but the runner's `diagnostics.json` does not satisfy it (routes retry to `Generate`); record `attribution=ir` when the IR's `diagnostics_contract` itself is absent or fails to cover `tests.md §3` (routes retry to `Compile`).
- On a physics `fail`, skip the performance evaluation.
- Require saving `self_summary` and `dependency_summary` in `summary.json`, and `dependency_summary` has `total`, `pass`, `xfail`, `fail`, and `blocked`.
- The LLM semantic-check result is saved as `semantic_review.json` under `runs/<run_id>/<node_key_safe>/`, and requires recording `review_method`, `decision`, `scope.model_ref`, `scope.runner_ref`, `scope.raw_refs`, and `findings`.
- `verdict.json` requires recording `failure_class`. The range is one of `physics_fail` / `runtime_error` / `evidence_mismatch` / `structural_violation` / `pass`.
- A `semantic_review.json#findings[*]` that detected a failure requires recording the following keys: `finding_id` (string), `attribution` (one of `code` / `ir` / `spec` / `evidence`), `evidence_refs[]` (path list), `confidence` (`high` / `medium` / `low`), `description` (text). These are the input by which the `orchestration agent` deterministically decides the retry target (Generate / Compile / Spec / Validate.execute) (canonical mapping: the "Decision criteria for retry on failure" section of `docs/workflow/phases/phase_04_validate.md`).
- The storage root for judgment artifacts allows only `workspace/`, and the workflow-root judgment targets only `workspace/`.
- The `--stage pre_judge` gate is run by the **conductor**, not this judge leaf (G3, mirroring the `Compile.static` / `Generate.static` deterministic-gate hoists). **You invoke no `validate_pipeline_semantics` gate** — you are a pure `LLM` semantic pass. Write `semantic_review.json` with your actual `decision` (`pass`/`fail`) and finalize `verdict.json` / `aggregate_verdict.json` / `summary.json`; the conductor runs `pre_judge` after you return and records the verdict in `judge_gate_meta.json`.
  - The conductor gates twice: a **pre-spawn** dependency-DAG readiness check (a not-yet-built+validated `spec.ir.yaml.dependency.all_nodes` closure fails the phase `fail_closed` before you are even spawned) and a **post-return** `--stage pre_judge` run (orchestration-record integrity + the cross-pipeline dependency DAG, scoped to this run). A `pre_judge` gate `fail` is a **non-physics integrity blocker** the conductor terminalizes `fail_closed` — even when your physics decision is `pass` — so you neither run nor react to that gate. (This is the deterministic-conductor analogue of the historic `status=blocked` termination; you no longer write the `validate` `step_result.json` yourself.)

## Operations Rules
1. Limit the judgment input to only artifacts under the same `run_id`.
2. The judgment input requires the simultaneous existence of `diagnostics.json`, `perf.json`, and the `raw` execution evidence, and when any is missing, `Validate.judge` does not start.
3. Make `aggregate_verdict.json` match the dependency set of `spec.ir.yaml.dependency`.
4. Record the quality-comparison result of `impl_defaults.target.class=cpu` as a `quality check`, separated from the `tests` judgment.
5. The comparison canonical source of `quality check` is `diagnostics.json` and `verdict.json`, and pass/fail must not be finalized by `stdout` diff alone.
6. On a judgment failure, make explicit the failure classification in `summary.json`, and specify the return-target stage by `semantic_review.json#findings[*].attribution`.
7. When the output destination is not `workspace/`, it is a `Validate.judge fail`.
8. When `workspace/` does not exist before workflow execution starts, create `workspace/` directly under the repository root.
9. Before start and before completion, run `python3 tools/validate_workspace_root.py`, and on `fail` it is a `Validate.judge fail`.
10. Do not invoke `validate_pipeline_semantics --stage pre_judge` yourself — the conductor owns it (pre-spawn readiness + post-return gate; see Requirements). Finalize `verdict.json` / `aggregate_verdict.json` only from your own recomputation and semantic judgment; a `pre_judge` gate `fail` is handled by the conductor (`fail_closed`), not by you.
11. When `attribution=spec` is judged, notify the `orchestration agent` to stop with `fail_closed` and record the details (the full finding, evidence_refs, description) in `failure_analysis.json` (no automatic retry).

## Decision Criteria
- The judgment basis is traceable to `tests.md`, `spec.ir.yaml.io_contract`, and `diagnostics.json`.
- The judgment basis is recomputable from the `raw` execution evidence.
- The `blocked` judgment condition matches the dependency state.
- `aggregate_verdict.json` and `summary.json` are consistent with `spec.ir.yaml.dependency`.
- The combination of `verdict.json#failure_class` and `semantic_review.json#findings[*].attribution` is uniquely interpretable by the retry decision table of `docs/workflow/phases/phase_04_validate.md`.
- The conductor-run `pre_judge` gate (post-return) passes — but you neither run it nor gate your own completion on it (a violation is the conductor's `fail_closed`, not a leaf action).
