# Phase 4: Validate

## Overview
The phase that runs the binary produced by `Build`, obtains the primary evidence, and finalizes the pass/fail judgment. It is defined as a single phase that has the 2 substeps `execute` (execution and primary-evidence collection) and `judge` (pass/fail judgment).

## I/O contract
- execution input: `binary/<binary_id>/bin/`, `spec.ir.yaml`, `tests.md`
- verification input: `spec.ir.yaml`, `source/<source_id>/`, the `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `trial_meta.json` under the same `run_id`. The resolution path of `<source_id>` differs per substep:
  - `Validate.execute`: the `source_id` recorded as required in the launch request (runtime-enforced; its match with `binary_meta.json.source_source_id` is also verified).
  - `Validate.judge`: because the launch request requires only `run_id`, read `trial_meta.json.source_source_id` under the same `run_id` to resolve `<source_id>` (trial_meta is written by execute, and the runtime has verified its match with binary_meta). Even when multiple `source_id` coexist under a `pipeline_id` due to retries, this path uniquely pins the exact source that execute actually used.
- output: the following under `workspace/pipelines/<node_key_safe>/<pipeline_id>/runs/<run_id>/<node_key_safe>/`:
  - `diagnostics.json`, `perf.json`, `quality_check.json`, `raw/`, `stdout.log`, `stderr.log` (execute substep)
  - `semantic_review.json`, `verdict.json`, `aggregate_verdict.json`, `summary.json`, `trial_meta.json`, `validate_meta.json` (judge substep)
  - the `command_id` and `command_log_ref` of `run_program`

## substep structure
- `Validate.execute`: a **non-LLM substep** that runs the binary via MCP and generates the primary evidence (`raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `stdout.log` / `stderr.log`).
- `Validate.judge`: an **LLM substep** that recomputes the judgment metrics from the primary evidence via an independent path and finalizes the `verdict` together with an `LLM` semantic check.

## `run_id` format
- Format: `run_<YYYYMMDD>_<seq3>`, e.g. `run_20260511_001`
- `run_id` has a fixed literal `run_` prefix. The `ir_id` / `pipeline_id` form `<slug>_<YYYYMMDD>_<seq3>` (slug being hyphen-separated) must not be reused. A hyphen-slug form such as `run-rsn-p0_20260605_001` matches the generic slug grammar but is not a canonical `run_id`; the phase contract of `record-launch` rejects it, and even if it passed, the run discovery of `post_execute` recognizes only the literal `run_` layout and would silently fail with `no execution artifacts found`.

## `validate_meta.json` required keys
- Common: `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, `context_isolated`
- When `context_isolated=false`, `constraint_reason` is required.
- When `verification_status=pass`, the evidence of the LLM semantic check (`command_id`, `command_log_ref`, etc.) is required in `judge_command_ref`.

## substep details

### 4-1. Validate.execute substep
- `Validate.execute` does not involve standard LLM inference, and limits its responsibility to the `run_program` MCP call and recording the result.
- Always include `spec.ir.yaml.case` in the `run_program` execution command (as the determined values of runtime input).
- Save the actual-command record of `run_program` in `JSONL` format, with the default destination `project_dir/mcp_command_log.jsonl`.
- `Validate.execute` runs individually per `node`, and must not mix in the artifacts of other `node`.
- The output targets of the `runner` are limited to `diagnostics.json`, `perf.json`, the `raw/` primary evidence, `stdout.log`, and `stderr.log`.
- The `runner` (the binary that `run_program` runs) **must not write `diagnostics.json` / `perf.json` directly** to the canonical run dir. The canonical `.json` requires the gate evidence of `guarded-apply-patch`, and a direct binary write invites an `unauthorized_write_violation` → `fail_closed` (this is handled differently from an MCP-owned audit log such as `mcp_command_log.jsonl`). Drop the binary's output to `allowed_tmp_root` (`workspace/tmp/<exec_agent_run_id>/`), and the `Validate.execute` agent reads the `.json` of the `run/` tmp tree (`workspace/tmp/<exec_agent_run_id>/run/`, = the output of the `run_program` execution) and re-authors the canonical `diagnostics.json` / `perf.json` with `guarded-apply-patch` (create-form). The runner's program output (`diagnostics.json` / `perf.json` / `raw/metrics_basis.json` / `raw/state_snapshots/*`) is always promoted from `run/`, and must not be promoted from `qc_run/` (the output of the `run_quality_checks` / make-test re-run) (if promoted, `Validate.judge` would consume evidence with a provenance mismatch). `qc_run/` is referenced only for the quality-check comparison (`quality_check.json`). `trial_meta.json` and `quality_check.json` are not runner output but metadata the agent authors (the runner must not output them directly), and are generated with `guarded-apply-patch` rather than promoted from tmp. The `.json` under `raw/` (`metrics_basis.json` / `state_snapshots/*.json` etc.) is also canonical `.json` and is re-authored with `guarded-apply-patch`. What is written with the `Write` tool is limited to non-`.json` files under `raw/`, `stdout.log`, and `stderr.log`.
- The `runner` must not write `verdict.json`, `aggregate_verdict.json`, `summary.json`, or `trial_meta.json` (these are the responsibility of `Validate.judge`).
- `diagnostics.json` and `perf.json` must be output as a UTF-8 `JSON object` restorable by a standard `JSON` parser.
- Before `Validate.execute` completes, check `diagnostics.json`, `perf.json`, and `quality_check.json` using `python3 tools/check_artifact_syntax.py --format json --expect-top object`, and on `fail` it is a `Validate.execute fail`.
- `Validate.execute` must save, in `runs/<run_id>/<node_key_safe>/raw/`, the primary evidence needed for `Validate.judge` recomputation.
- The required composition of the primary evidence uses `spec.ir.yaml.io_contract.raw_requirements.required_evidence` as the canonical source. A fixed minimal composition must not be uniformly applied to all `spec`.
- When `raw_requirements.required_evidence` declares `artifact=state_snapshots` and `required=true`, `raw/state_snapshots/` must declare `variables[].name`, `variables[].shape_expr`, `time_variable`, and `time_shape_expr` in `snapshot_schema.json`, and hold these items in at least `min_samples` state files.
- When `raw_requirements.required_evidence` does not declare `artifact=state_snapshots` as required, `raw/state_snapshots/` must not be required.
- `raw/metrics_basis.json` holds only the primary evidence, and copying `diagnostics.json` is forbidden.
- `raw/metrics_basis.json` must hold a per-test evidence index targeting all `test_id` of `io_contract.test_evidence_requirements`. The entry of each `test_id` holds `required_raw_variables` without omission.
- Within the same `metrics_basis.json`, the primary evidence of different `test_id` must not overwrite each other.
- When `Validate.execute` fails, the artificial generation of `diagnostics.json` / `perf.json` is forbidden, and the relevant `node` is `fail`.
- `quality_check.json` must simultaneously satisfy `checks.verdict_available=true`, `checks.diagnostics_match=true`, and `checks.verdict_match=true`. When any is `false` or missing, it is a `Validate.execute fail`. In addition, the literal `"pass"` must be recorded in the **top-level `status` field** (agent-authored metadata). When `status` is other than `"pass"` or missing (e.g. only `verdict:"pass"` with `status` absent), the `post_execute` gate rejects it with `quality_check.json:status must be pass`.
- `quality check` execution allows only the `preset` specification of `run_quality_checks`, and forbids arbitrary command execution such as `python3 quality_check.py`.
- `Validate.execute` must not generate `test` source, a harness, an auxiliary `script`, or a temporary `Makefile` under `runs/<run_id>/<node_key_safe>/` to establish a `quality check`. When the required artifact does not exist in the `Generate` or `Build` output, it is a `Validate.execute fail`.
- With `spec.ir.yaml.impl_defaults.toolchain.build_system=make` and `toolchain.language=fortran` / `c` / `cpp` / `mixed` families, the `quality check` runs with `make_test` or `make_check` that treats `source/<source_id>/src/` as the `project_dir`. Pass `env={OBJDIR:<abs tmp build>, BINDIR:<abs binary/<source_binary_id>/bin>, RUNDIR:<abs>/workspace/tmp/<exec_agent_run_id>/qc_run}` to `run_quality_checks`, and reference the existing binary from `binary/<source_binary_id>/bin/` (do not relink in the read-only-bound `binary/`). Because `make test` re-runs the binary and emits `diagnostics.json` / `raw/*` directly under `RUNDIR`, pointing `RUNDIR` at the canonical run node dir would have the direct binary write overwrite the gate-authored copy and invite an `unauthorized_write_violation`. Point `RUNDIR` at tmp (`workspace/tmp/<exec_agent_run_id>/qc_run`, a separate subdir from `run_program`'s `run/`), and never let the binary output be written directly to the canonical run node dir. All canonical `.json` is re-authored by the agent with `guarded-apply-patch` **after both `run_program` and `run_quality_checks` complete** (the final step). Nothing other than the cross-phase audit log is written to `src/`.
- For the specification of `perf.json`, refer to `PERFORMANCE_DIAGNOSTICS.md`.
- Before `Validate.execute` completes, run `python3 tools/validate_pipeline_semantics.py --stage post_execute --pipeline-root <pipeline_root> --run-id <run_id>`, and `exit code 0` is required. `--pipeline-root` can be specified repeatedly, and in a trial where `spec.ir.yaml.dependency.all_nodes` holds multiple `node`, specify all `pipeline_root` corresponding to `all_nodes`. For `--run-id`, specify this trial's `run_id` to scope the verification to that run. Because `pipeline_id` is `append-only` (an existing run cannot be deleted), omitting `--run-id` would leave a broken sibling run from a past retry in the same pipeline and permanently `fail` `post_execute`. Specifying `--run-id` makes only the corrected run the verification target.

### 4-2. Validate.judge substep
- The judgment canonical source is `tests.md` and `spec.ir.yaml.io_contract`.
- The judgment is performed in 2 layers: `self_verdict` (`verdict.json`) and `aggregate_verdict` (`aggregate_verdict.json`).
- The start condition of `Validate.judge` is that, under the target `run_id`, the `run_program` execution record, `diagnostics.json`, `perf.json`, and the `raw/` primary evidence exist and are traceable as artifacts of the same `run_id`.
- `Validate.judge` must recompute the judgment metrics from the `raw/` primary evidence via an independent path, and confirm consistency with `diagnostics.json`.
- The recomputation input is limited to `raw/` only. `diagnostics.json` must not be reused as recomputation input.
- `Validate.judge` verifies, as a start condition, that `raw/metrics_basis.json` holds all `test_id` of `io_contract.test_evidence_requirements` and that each entry holds the `required_raw_variables` of that `test_id` without omission. On shortage, it is a `Validate.judge fail`.
- `Validate.judge` must `fail` when recomputation is impossible or inconsistent.
- `Validate.judge`, in addition to the fixed-script check, must execute an `LLM` semantic check, and judge the consistency and fabrication suspicion of the `model` / `runner` / `raw` primary evidence.
- The result of the `LLM` semantic check is saved as `semantic_review.json` under `runs/<run_id>/<node_key_safe>/`, and requires recording `review_method`, `decision`, `scope.model_ref`, `scope.runner_ref`, `scope.raw_refs`, and `findings`.
- When the `decision` of `semantic_review.json` is `fail` or missing, the relevant `node` is a `Validate.judge fail`.
- When an immediate dependency `node` has `fail` or `blocked`, the upper `node` does not evaluate `self_verdict` and ends with `aggregate_verdict=blocked`.
- Even on a `blocked` end, `aggregate_verdict.json`, `summary.json`, and `trial_meta.json` are required outputs, and `blocked_reason` and `blocking_direct_deps` are recorded.
- `summary.json` requires holding `self_summary` and `dependency_summary`. `dependency_summary` holds `total`, `pass`, `xfail`, `fail`, and `blocked`.
- `verdict.json` requires holding `per_test`, and records all `test_id` of `tests.md` without duplication.
- The `counts` of `summary.json` must match the aggregate values of `verdict.json.per_test`.
- On judgment-input shortage, it is a `Validate.judge fail`, and the `verdict` must not be established with estimated or assumed values.
- Before `Validate.judge` starts and before it completes, run `python3 tools/validate_pipeline_semantics.py --stage pre_judge --pipeline-root <pipeline_root> --run-id <run_id>`, and on `fail` the relevant `pipeline` is `invalid`. For `--run-id`, specify the `run_id` to be judged, and exclude from the verification target the broken sibling run of past retries that remains in the `append-only` pipeline.
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` must not specify `--allow-missing-orchestration` or `--allow-missing-llm-review`.
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` must include in the verification target the `pipeline_root` of all `node` resolved by `spec.ir.yaml.dependency.all_nodes`, by specifying `--pipeline-root` repeatedly.
- `python3 tools/validate_pipeline_semantics.py --stage pre_judge` must `fail` when it detects, for `spec.ir.yaml.dependency.all_nodes`, a `node` whose `ir` or `pipeline` is not yet issued, and forbid the start of that trial's `Validate.judge`.
- The implementation-quality judgment (`impl_defaults.target.class=cpu`) is performed by comparing `threads_per_rank=1` and `threads_per_rank>1`, and the comparison targets are `diagnostics.json` and `verdict.json`.
- The comparison of with / without thread parallelism is not included in the `tests` judgment target, but is handled as a `quality check`.
- On a physics `fail`, the performance evaluation is skipped.

## Decision criteria for retry on failure
The retry target on a `Validate` failure is decided deterministically by the `orchestration agent` interpreting the `judge`'s `findings`. The judgment input is limited to the 2 of `semantic_review.json#findings[*]` and `verdict.json#failure_class`.

### Classification fields the `judge` records as required
When `Validate.judge` detects a failure, it records the following keys in `semantic_review.json#findings[*]` as required:

| field | range | meaning |
|---|---|---|
| `attribution` | `code` / `ir` / `spec` / `evidence` | the attribution category of the failure |
| `evidence_refs[]` | path list | references to the raw / diagnostics / source / IR used as the basis |
| `confidence` | `high` / `medium` / `low` | the judge's confidence |
| `description` | text | a natural-language explanation of the basis (for review) |

`verdict.json#failure_class` is one of the following values: `physics_fail` / `runtime_error` / `evidence_mismatch` / `structural_violation` / `pass`.

### Decision table
The `orchestration agent` decides the retry target by the following deterministic mapping:

| `verdict.json#failure_class` | `attribution` (judge) | retry target |
|---|---|---|
| `evidence_mismatch` | `code` | `Generate` |
| `evidence_mismatch` | `ir` | `Compile` |
| `evidence_mismatch` | `evidence` | `Validate.execute` (re-collection of primary evidence) |
| `physics_fail` | `code` | `Generate` |
| `physics_fail` | `ir` | `Compile` |
| `physics_fail` | `spec` | **`Spec` (fail_closed)**: manual intervention required |
| `runtime_error` | `code` (always) | `Generate` |
| `structural_violation` | `code` | `Generate` |
| `structural_violation` | `ir` | `Compile` |

### Launch contract for Compile retry
When launching a retry to `Compile`, the `orchestration agent` must satisfy the following:

- At least 1 finding with `semantic_review.json#findings[*].attribution=ir` exists.
- The `confidence` of the relevant finding is `high` or `medium` (when `low`, try a `Generate` retry first).
- Quote the relevant finding's `description` and `evidence_refs[]` in `launches/<new_agent_run_id>.request.json#repair_reason`.
- The re-submitted `Compile` **makes explicit the section of `spec.ir.yaml` to be fixed as the `restart` scope**, and records `validate_feedback:<finding_id>` in `ir_meta.json.last_fail_reason`.

### Handling of Spec retry
A retry to `Spec` is not automated in the core workflow (because `controlled_spec.md` needs to be updated by hand). When the `orchestration agent` judges `attribution=spec`, it stops with `fail_closed`, and records the details (the full finding, evidence_refs, the judge's `description`) in `failure_analysis.json`.

## Design trade-offs
- The reason for placing `execute` and `judge` as substeps of the same phase: "execution → pass/fail judgment" is essentially a single integrated task, and splitting them into separate phases would make the `judge` input always depend on the latest `execute` result, weakening the meaning of the phase boundary. Integrating into Validate simplifies the judgment path and makes the judgment artifacts self-contained under `run_id`.
- The reason for splitting `execute` and `judge` into substeps: `execute` is non-LLM (MCP only) and `judge` requires an LLM semantic check, so the responsibility and the need for context isolation differ. By splitting into 2 substeps within the same phase, the `judge` can make a fair judgment in an independent LLM context.
