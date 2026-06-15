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
- The judgment canonical source is fixed at the target `node`'s `tests.md` and `spec.ir.yaml.io_contract`.
- `self_verdict` is saved in `verdict.json` as the judgment result of the relevant `node` alone.
- `aggregate_verdict` aggregates the transitive dependency `node` and is saved in `aggregate_verdict.json`.
- When an immediate dependency `node` is not `pass` or `xfail`, make the relevant `node` `blocked`.
- The judgment metrics are recomputed from the execution evidence of `runs/<run_id>/<node_key_safe>/raw/` and reconciled with `diagnostics.json`. Recomputation impossible or inconsistent is a `Validate.judge fail`.
- The recomputation input is limited to the `raw` primary evidence only, and `diagnostics.json` must not be reused as recomputation input.
- The required composition of `raw` is judged using `spec.ir.yaml.io_contract.raw_requirements.required_evidence` as the canonical source, and a fixed evidence composition must not be uniformly required.
- `raw/metrics_basis.json` must be a per-test evidence index targeting all `test_id` of `io_contract.test_evidence_requirements`. When an entry of some `test_id` is missing `required_raw_variables`, or has only a whole-suite summary, it is a `Validate.judge fail`.
- On a physics `fail`, skip the performance evaluation.
- Require saving `self_summary` and `dependency_summary` in `summary.json`, and `dependency_summary` has `total`, `pass`, `xfail`, `fail`, and `blocked`.
- The LLM semantic-check result is saved as `semantic_review.json` under `runs/<run_id>/<node_key_safe>/`, and requires recording `review_method`, `decision`, `scope.model_ref`, `scope.runner_ref`, `scope.raw_refs`, and `findings`.
- `verdict.json` requires recording `failure_class`. The range is one of `physics_fail` / `runtime_error` / `evidence_mismatch` / `structural_violation` / `pass`.
- A `semantic_review.json#findings[*]` that detected a failure requires recording the following keys: `finding_id` (string), `attribution` (one of `code` / `ir` / `spec` / `evidence`), `evidence_refs[]` (path list), `confidence` (`high` / `medium` / `low`), `description` (text). These are the input by which the `orchestration agent` deterministically decides the retry target (Generate / Compile / Spec / Validate.execute) (canonical mapping: the "Decision criteria for retry on failure" section of `docs/workflow/phases/phase_04_validate.md`).
- The storage root for judgment artifacts allows only `workspace/`, and the workflow-root judgment targets only `workspace/`.
- Before `Validate.judge` starts and before it completes, run `python3 tools/validate_pipeline_semantics.py --stage pre_judge --in-flight-agent-run-id <own agent_run_id>`, and on `fail` it is a `Validate.judge fail`. `--allow-missing-orchestration` and `--allow-missing-llm-review` must not be specified.
  - **Always attach `--in-flight-agent-run-id <own agent_run_id>` (`<...>` is the literal substitution of your own `agent_run_id`).** `record-launch` appends the own edge of `agent_graph.json` at judge launch, but because the judge's own `agent_runs.jsonl` entry and the `validate` `step_result.json` are written by the parent after the judge returns, they are not yet recorded at the point the judge runs `pre_judge` from within its own substep. `pre_judge` does **not** judge this self in-flight exception by the presence of the active marker (because it can remain/be missing due to a crash or backend difference), but trusts only the `--in-flight-agent-run-id` declared by the live caller, and, after verifying that the launch request is `step=validate, substep=judge`, permits that edge and the `validate` step_result. Without the flag, its own dangling edge and the not-yet-generated step_result become a violation and it stops fail-closed.
  - **Write (or overwrite) `semantic_review.json` with this judge's own actual `decision` before the "before completion" `pre_judge`.** Because `pre_judge` detects `semantic_review.json#decision != "pass"` as a violation, running the "before completion" `pre_judge` without overwriting the `decision=fail` left by a previous judge attempt fails on the stale value even if your own judgment is pass.
  - **Even if the node physics is pass (`semantic_review.json#decision=pass`), when it cannot be certified due to a non-physics blocker (e.g. a `pre_judge` fail by orchestration-record integrity that is unrecoverable within that run), the `validate` `step_result.json` can be written with `status=blocked`.** `write-step-result` permits the coexistence of `decision=pass` and `status=blocked` (because `status=pass` requires a finalized verdict and `status=fail` requires `decision!=pass`, `blocked` is used as an honest terminal path other than `fail_closed`). However, even on a `blocked` termination, the generation of `aggregate_verdict.json` / `summary.json` / `trial_meta.json` is required, and lacking these rejects `write-step-result`.

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
10. When the `exit code` of `python3 tools/validate_pipeline_semantics.py --stage pre_judge` is not `0`, or when `--allow-missing-orchestration` / `--allow-missing-llm-review` is specified, `verdict.json` and `aggregate_verdict.json` must not be finalized.
11. When `attribution=spec` is judged, notify the `orchestration agent` to stop with `fail_closed` and record the details (the full finding, evidence_refs, description) in `failure_analysis.json` (no automatic retry).

## Decision Criteria
- The judgment basis is traceable to `tests.md`, `spec.ir.yaml.io_contract`, and `diagnostics.json`.
- The judgment basis is recomputable from the `raw` execution evidence.
- The `blocked` judgment condition matches the dependency state.
- `aggregate_verdict.json` and `summary.json` are consistent with `spec.ir.yaml.dependency`.
- The combination of `verdict.json#failure_class` and `semantic_review.json#findings[*].attribution` is uniquely interpretable by the retry decision table of `docs/workflow/phases/phase_04_validate.md`.
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` returns `exit code 0`.
