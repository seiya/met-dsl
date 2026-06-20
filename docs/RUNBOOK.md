# Runbook (minimal procedure for running trials)

This document defines the "minimal operational procedure for running trials". The core workflow is a 5-phase structure `Spec → Compile → Generate → Build → Validate`. Update it as operational knowledge accrues.

## 0. Purpose
- From a `spec`'s `Controlled Spec` (physics definition) and `tests` (verification profile), perform execution and judgment, and evaluate physical validity and performance.
- Isolate where a failure's cause lies among **Spec / Compile / Generate / Build / Validate**. The optional flows `Tune` / `Promote` are handled outside the core workflow.

## 0-1. Required CLI tools

Workflow execution and the repair procedures of this RUNBOOK presume the following CLI.

| tool | purpose |
|---|---|
| `python3` | the workflow runtime (`tools/orchestration_runtime.py` etc.) |
| `jq` | extracting shell variables from JSON such as output_manifest (`python3 -c` is blocked by `forbid_python_inline_write`, so it cannot be substituted) |
| `git` | `write_scope_baseline` / `git apply` (used inside `guarded-apply-patch`) / status check |

When absent, it fail-fasts at the point `tools/run_workflow.py` starts.

## 1. Input and artifacts (minimal)
- Input: `controlled_spec.md` (physics / algorithm definition) / `tests.md` (case expansion / execution conditions / judgment thresholds) / `deps.yaml` (dependency declaration)
- Generated (Compile): `spec.ir.yaml` (**a single structural IR**: integrating the case / algorithm / impl_defaults / io_contract / dependency sections)
- Generated (Generate): the source of `model` (physics computation) and `runner` (execution / judgment coordination)
- Generated (Build): the binary (`binary/<binary_id>/bin/`)
- Output (Validate): `diagnostics.json` / `perf.json` / `verdict.json` / `aggregate_verdict.json` / `summary.json` / `semantic_review.json`
- Forbidden: `dummy` output, `dummy` data, `dummy` computation, artificial artifact generation for the purpose of advancing the workflow

## 1-1. artifact layout (operationally required)
- `Compile` saves `spec.ir.yaml` and `ir_meta.json` in `workspace/ir/<node_key_safe>/<ir_id>/`.
- `Generate` / `Build` / `Validate` save in `workspace/pipelines/<node_key_safe>/<pipeline_id>/`.
- Each `pipeline` requires placing `lineage.json`.
- The `source` artifact is saved in `workspace/pipelines/<node_key_safe>/<pipeline_id>/source/<source_id>/`.
- The `binary` artifact is saved in `workspace/pipelines/<node_key_safe>/<pipeline_id>/binary/<binary_id>/`.
- The `Validate` artifact is saved in `workspace/pipelines/<node_key_safe>/<pipeline_id>/runs/<run_id>/<node_key_safe>/`.
- For judgment, load per `run_id`. Mixing files across `run_id` is forbidden.
- For judgment, load `verdict` / `aggregate_verdict` / `summary` separately per `node_key`.
- The official-version artifact of the optional flow `Promote` is saved in `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` (outside the core workflow). `workspace` is limited to trial use.

## 1-2. Deviation-prevention gates (operationally required)
- `docs/workflow/WORKFLOW_CORE.md` is the canonical source for the workflow common invariants (anti-fraud, the ban on referencing past artifacts, verification-contract derivation, the `workspace/` root constraint, the `quality check` judgment axis).
- `SPEC.md` is the canonical source for the overall policy and `spec` management requirements (`spec_kind` / registry / naming rules).
- Workflow execution runs each phase (`Compile` / `Generate` / `Validate`) with the `LLM`. `Build` is a deterministic process and is run by an MCP `compile_project` call.
- As a substitute for workflow execution, a script that batch-proxies the processing of multiple phases and artifact generation must not be newly generated or executed.
- Before each phase starts, capture `write_scope_baseline`, and before each phase completes, mandatorily run the `write_scope` check that detects diffs outside of `workspace/`.
- When `python` execution is used in the workflow path, limit `__pycache__` to under `workspace/`. Mandatorily apply `PYTHONDONTWRITEBYTECODE=1` or `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/`.
- When the `write_scope` check detects a diff outside of `workspace/`, the relevant phase is `fail`, and `write_scope_violation.json` is recorded under `workspace/`.
- `spec.ir.yaml.io_contract.semantic_dependency.required_sources` is the canonical source for the data-dependency judgment of `Generate.verify`.
- `spec.ir.yaml.io_contract.outputs` is the canonical source for the output-contract judgment of `Generate.verify`, and the consistency of `evidence_ref` and `shape_expr` is mandatorily checked.
- The requirement definition for the output format, input/output contract, and judgment conditions is obtained from `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec.ir.yaml`, and the `docs/` canonical source, and the verification scripts under `tools/` must not be used as input for the requirement definition.
- The canonical implementation of the procedure that finalizes a mechanical pass/fail is the procedure of running the `validate_pipeline_semantics.py`-equivalent invocation via `python3 tools/orchestration_runtime.py run-gate --gate validate_pipeline_semantics --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '<json>'`. The agent completes that `run-gate` execution to `exit code 0`.
- The validator invocation defaults to `run-gate`. When direct execution is permitted, it is limited to a read-only check and a gate-independent check, and the permitted targets are only `validate_workspace_root.py` and `check_artifact_syntax.py`.
- A shortage of the requirement definition must not be back-derived from the verification implementation. On shortage, the relevant phase is `fail`.
- `Validate.judge`, in addition to the fixed-script check, mandatorily executes an `LLM` semantic check, and includes `decision=pass` of `semantic_review.json` as a start condition.
- Before `Validate.judge` starts, verify that the `run_program` execution record, `diagnostics.json`, `perf.json`, and the `raw` execution evidence are present under the same `run_id` of the target `node_key`. When not met, it is a `Validate.judge fail`.
- Before `Compile.verify` completes, run `validate_pipeline_semantics` via `run-gate` with arguments equivalent to `--stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/`. On `fail`, `verification_status=pass` must not be assigned to `ir_meta.json`.
- Before `Generate.verify` completes, run `validate_pipeline_semantics` via `run-gate` with arguments equivalent to `--stage post_generate --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/`. When fixing the `source_id` to verify, add an argument equivalent to `--source-id <source_id>`.
- Before `Build` completes, run `validate_pipeline_semantics` via `run-gate` with arguments equivalent to `--stage post_build --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/`.
- Before `Validate.execute` completes, run `validate_pipeline_semantics` via `run-gate` with arguments equivalent to `--stage post_execute`. `--pipeline-root` can be specified repeatedly, and in a trial where `spec.ir.yaml.dependency.all_nodes` holds multiple `node`, expand all `pipeline_root` corresponding to `all_nodes` into `--pipeline-root` to run. Pass this trial's `run_id` into the `run_id` of `args_json` (→ `--run-id`) to scope the verification to that run (to avoid permanent fail on a broken sibling run of a past retry that remains in the `append-only` pipeline). On `fail`, `Validate.execute` is `fail`, and `Validate.judge` must not start.
- Before `Validate.judge` starts and before it completes, run `validate_pipeline_semantics` via `run-gate` with arguments equivalent to `--stage pre_judge`, and on `fail` the relevant `pipeline` is `invalid`. Pass the `run_id` to be judged into the `run_id` of `args_json` (→ `--run-id`) to scope the verification to that run.
- `validate_pipeline_semantics --stage pre_judge` must not be combined with `--allow-missing-orchestration` and `--allow-missing-llm-review`.
- The `pre_judge`-equivalent arguments before `Validate.judge` starts are run by specifying all `pipeline_root` corresponding to the target `spec.ir.yaml.dependency.all_nodes` repeatedly into `--pipeline-root`.
- `trial_meta.json` requires recording `generated_by_stage`, `source_source_id`, `source_binary_id`, `source_command_ref`, and `source_artifact_hash`, and on omission or inconsistency it is a `fail` (because `run_id` is encoded by the `runs/<run_id>/` directory path itself where the trial_meta is placed, a separate `source_run_id` field is not recorded).
- A trial that violates the verification of this section stops at the relevant phase, and artificial artifact generation for the purpose of satisfying a downstream phase's start condition is forbidden.

### 1-2-1. Supplementary static rules of `validate_pipeline_semantics.py` (around Generate)
- **Target notation of `Makefile` object rules**: for a `src/` consisting of `spec.ir.yaml.impl_defaults.toolchain.language=fortran` and multiple `module`, the object-dependency check mechanically derived from the `use` dependencies runs. The check adopts as a rule only the **literal** base name (e.g. `foo.o`) that remains after removing `$(NAME)` / `${NAME}` from the target token. The `.mod` / `.o` required for each `.o`'s prerequisite is enumerated as a **literal target line** (e.g. `foo.o: bar.o baz.mod`).
- **Scope of the substring check for forbidden output names of the `runner`**: detect it as a **substring** of a forbidden name after lowercasing the full text of `*_runner.f90`. **Comment lines are not excluded.** `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json` must not be contained in a comment or string literal.
- **Each `pipeline`'s `lineage.json`**: `workspace/pipelines/<node_key_safe>/<pipeline_id>/lineage.json` is required for each `pipeline` to be checked.

## 1-3. Agent launch conventions (operationally required)
- Workflow execution starts from the `orchestration agent` and requires issuing an `orchestration_id`.
- Before the workflow starts, run the `preflight` that verifies the independent launchability of a `step agent` and a `substep agent`, and when it is not `pass` do not start.
- The preflight of `backend=codex` must simultaneously satisfy `checks.hooks_enabled.pass=true` and `checks.codex_home_writable.pass=true`.
- The `preflight` includes `sandbox_runtime=bwrap` and `sandbox_enforced=true` as required conditions.
- The canonical entrypoint for starting the workflow is `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]`. `<until_phase>` specifies one of `compile` / `generate` / `build` / `validate`.
- The `Build` step, which has no standard `substep`, runs by launching a `step agent` independently.
- Each phase of `Compile` / `Generate` / `Validate` runs with the `orchestration agent` launching each `substep`'s `substep agent` independently.
- The actual processing of each `step` / `substep` must not be proxied by a script.
- The `step agent` and `substep agent` have a unique `context_id` per `agent_run_id` and require recording `context_isolated=true`.
- `record-launch` generates `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` / `read_manifests/<agent_run_id>.json` / `sandbox_profiles/<agent_run_id>.json`.
- On completion of each `step` / `substep`, save `agent.result.json` and `agent.summary.txt`.
- The `orchestration agent` sequentially decides the launch order based on the `topo_level` of `spec.ir.yaml.dependency` and the dependency-satisfaction state.
- The `orchestration` execution record is saved in `workspace/orchestrations/<orchestration_id>/`, and `orchestration_meta.json`, `agent_graph.json`, and `agent_runs.jsonl` are required.
- `step_result.json` requires recording `executor_agent_run_id` and `substep_agent_run_ids`. The `substep_agent_run_ids` of `Build` (a phase with no standard substep) may be an empty array.

## 2. Minimal loop
1. **Spec update**: fix `controlled_spec.md` / `tests.md` / `deps.yaml`, and resolve ambiguity and omissions.
2. **Compile**: take `controlled_spec.md` + `tests.md` + `deps.yaml` + `spec/registry/spec_catalog.yaml` as input and generate `spec.ir.yaml`.
   - The `Compile.generate` substep generates a single IR that integrates and holds the 5 sections `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency`.
   - The `Compile.verify` substep self-checks the structural invariants (case coverage / algorithm completeness / io_contract consistency / dependency consistency / impl_defaults consistency).
   - Because it is an `LLM`-using phase, apply the "Handling of the `LLM`" of `SPEC.md`.
3. **Fix the hierarchical execution order**: fix the execution order in ascending `spec.ir.yaml.dependency.topo_level`. The `Compile` of a parent `node` must not start until the immediate dependency `node` satisfies `direct dependency ir readiness`. `Generate` onward of a parent `node` must not start until the immediate dependency `node` satisfies `direct dependency execution readiness`. Independent `node` of the same `topo_level` are also executed sequentially one at a time.
4. **Per-`node` workflow issuance**: the `orchestration agent` issues an individual `ir_id` and an individual `pipeline_id` per `node_key`.
5. **Generate**: per target `node`, generate `model` and `runner` separately with the `LLM`.
   - `Generate` must not take `controlled_spec.md` as direct input, and uses `spec.ir.yaml` as the canonical source.
   - `Generate.verify` performs the G1–G7 verification items (see `phase_02_generate.md`) against each section of `spec.ir.yaml`.
   - A `node` that has dependencies requires an implementation that calls the published `operation` of the dependency `node` resolved by `spec.ir.yaml.dependency.direct_deps`, and forbids re-implementing an equivalent function.
6. **Build**: per target `node`, run the standard build tool that can handle dependencies via the `MCP` server's `compile_project`.
   - A `Build` failure **always becomes a retry feedback to `Generate`** (because it is a deterministic process, there is no room for fixing other than the code).
   - `Build` must not internally retry itself.
7. **Validate**: per target `node`, the `Validate.execute` substep runs the binary and generates primary evidence, and the `Validate.judge` substep recomputes the judgment metrics and finalizes the `verdict`.
   - `Validate.execute` runs the `runner` with `MCP run_program`, and always includes `spec.ir.yaml.case` in the `run_program` execution command.
   - `Validate.execute` saves the primary evidence for judgment recomputation into `runs/<run_id>/<node_key_safe>/raw/`. The required condition of the `raw` composition uses `spec.ir.yaml.io_contract.raw_requirements.required_evidence` as the canonical source.
   - `Validate.judge` recomputes the judgment metrics with only the `raw` primary evidence as input, and when it does not match `diagnostics`, it is a `Validate.judge fail`. In addition to the fixed-script check, it performs an `LLM` semantic check, and makes `decision=pass` of `semantic_review.json` a required condition.
   - The judgment including dependencies is output to `aggregate_verdict.json`. When an immediate dependency `node` is `fail` or `blocked`, the upper `node` ends as `blocked`.
8. **Forced stop**: when the relevant phase cannot proceed due to an input shortage or a preceding-stage artifact shortage, stop the relevant phase with `fail`. It must not proceed with estimated completion or artificial file generation.
9. **Recording**: save `spec_version` / `test_profile_version` / `case_hash` / `git_sha`.
   - Save `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id`.
   - Save `node_key` / `topo_level` / `dependency_ref`.
   - `dependency_ref` saves the per-phase canonical path. `Compile` records `spec/.../deps.yaml`, and from `Generate` onward records the phase root of `workspace/...` (`ir_ref` or `pipeline_ref`).
   - An `LLM`-using phase saves `attempt_count` / `verification_status` / `last_fail_reason` / `debug_mode` in each phase's `<stage>_meta.json`.
   - The `agent_runs.jsonl` of `step` / `substep` records `agent_backend` / `agent_model` / `context_id` / `context_isolated=true`.
10. **Next action**: decide where to go back according to the failure classification (next section).

## 3. Where to go back on failure (guidance)
| failure kind | go back to |
|---|---|
| `LLM` stage cannot run | the input contract or the `MCP` connection definition |
| Spec deficiency (ambiguity / omission / unit inconsistency) | `Spec` |
| Test deficiency (contradiction in case expansion / threshold / execution conditions) | `tests` |
| Dependency resolution fail (unregistered / unimplemented / compatibility violation) | `deps.yaml` / `spec_catalog.yaml` |
| Dependency block (`fail` of a lower `node`) | the lower `node` |
| Compile verification fail (IR structural invariant violation) | `Compile` (and `Spec` as needed) |
| Generate verification fail (implementation inconsistent with the IR) | `Generate` (and `Compile` if the IR is wrong) |
| Build failure (compile error) | `Generate` (deterministically go back to Generate) |
| Physics fail (the execution result is a judgment failure) | one of `Generate` / `Compile` / `Spec` — specify the detail in `judge.findings` |
| Validate judgment fail (divergence of the primary evidence and diagnostics) | `Generate` (code-quality problem) |
| `semantic_review.decision=fail` (implementation differs from the IR's intent) | `Generate` |
| `semantic_review.decision=fail` (the IR itself differs from the spec's intent) | `Compile` |
| Dependency integration fail (missing dependency `operation` call) | `Generate` or `Build` |
| Dependency Compile incomplete | `Orchestration` or the lower `node` |
| Dependency workflow not run | `Orchestration` or the lower `node` |
| Improper-generation fail (`dummy` output, artificial data creation) | discard the relevant phase and go to `Spec` / the phase input definition |
| Reproducibility collapse (determinism breakage) | `Compile` / the execution environment |

An automatic retry to `Spec` is not performed in the core workflow. When the `orchestration agent` judges that a return to `Spec` is needed, it stops with `fail_closed` and records the details in `failure_analysis.json`.

Optional flows:
- Performance shortfall (insufficient exploration of B) → launch the `impl_defaults` variant exploration of the optional flow `Tune`.
- Official-version promotion → go to `releases/` with the optional flow `Promote`.

## 3-0. Auto-running dependencies (`--with-deps`)

When a `node` is fail-closed with `dependency_not_ready` (`direct_dependency_compile_readiness_not_pass` / `..._execution_readiness_not_pass`) because its dependency nodes have not been compiled/built/validated yet, there are two ways forward:

1. Run the core workflow for each dependency node manually (bottom-up: components → profile → problem), then run the target.
2. Run the target with `--with-deps`, which resolves the target's transitive dependency closure (`deps.yaml` + `spec_catalog.yaml`) and runs each not-yet-ready dependency node bottom-up (one orchestration per node) before the target.

```bash
# auto-run the dependency closure, then the target
python3 tools/run_workflow.py spec/problem/dynamics/advection_diffusion/advdiff1d_linear validate --llm claude --with-deps
```

Behavior:
- Dependency nodes run to **Compile** when `<until_phase>=compile` (compile readiness), else to **Validate** (execution readiness).
- Dependency nodes that already satisfy the required readiness are **skipped** (idempotent).
- Execution is **sequential** in dependency order; on the first dependency-node failure the run **stops** before the dependent/target node, and the JSON output records `failed_dependency_node` + its `orchestration_id` + the `dependency_runs` summary.
- The target node's final JSON result carries a `dependency_runs` summary of which nodes ran / were skipped.
- `--with-deps` is **ignored with `--resume`** (resume re-enters a single existing orchestration — the target — only).

## 3-1. Resuming a failed workflow (`--resume`)

The canonical path to resume a workflow that failed midway, from the failure point while reusing completed `step` (e.g. already-compiled), is `python3 tools/run_workflow.py --resume`.

```bash
# resume the most recent orchestration with the previous spec_ref / until_phase / llm
python3 tools/run_workflow.py --resume

# to resume a specific orchestration
python3 tools/run_workflow.py --resume --orchestration-id <orchestration_id>

# to resume with an extended until_phase (if the lone positional is a phase name, it overrides until_phase)
python3 tools/run_workflow.py --resume build
```

- When `spec_ref` / `until_phase` / `--llm` / `--mode` are omitted, they are restored from the target orchestration's existing artifacts (`orchestration_meta.json` / `preflight.json` / `launches/orchestration.start.prompt.txt`). An explicitly specified value takes precedence.
- When `--orchestration-id` is omitted, the most recent (by `orchestration_meta.json#started_at` order) orchestration in `workspace/orchestrations/` is targeted. However, when the latest is in a non-terminal status (`running` etc.), to avoid the accident of erroneously connecting to a concurrent running run and destroying the shared `workspace/tmp/<arid>`, it stops with `latest_orchestration_not_resumable` (to resume that run, specify `--orchestration-id` explicitly).
- When `spec_ref` is explicitly overridden at resume time, the overridden `spec_ref` / `source_dependency_ref` is reflected into `orchestration_meta.json` (so that the next implicit resume does not revert to the stale old value).
- Internal behavior: `--resume` runs `orchestration_runtime.py init --resume-from-checkpoint` (= set `resume_enabled=true`, retain `orchestration_agent_run_id`, merge `phase_state`) and then starts. When the target orchestration is already terminated with a terminal status (`fail` / `fail_closed` / `pass` etc.), it returns the live status to `running` (because the runtime rejects a transition from terminal to another status except `fail` → `fail_closed`, without a reset the resumed agent could not record `pass` even if it completed). On reset, the terminal-time `reason_code` / `reason_detail` / `blocking_policy_scope` are saved to `resumed_from_*`, and `finished_at` / `detected_at` are removed (the history remains in `failure_analysis.json` and `phase_state_log.jsonl`). The skip judgment of a completed `step` is made by the orchestration agent via `check-step-completed` (SKILL.md Operations Rule 19). A `step` detected as `stale` by `verify-checkpoint-integrity` is not skipped and is re-run.
- **Prohibition of a concurrent `claude` session (during workflow execution)**: do not launch another `claude` session in the same project dir during workflow execution. Another session's startup cleanup deletes the running workflow's `workspace/tmp/<orchestration_arid>/.../tasks/*.output`, and the Bash tool output becomes unobtainable with `output file ... could not be read (ENOENT) ... another Claude Code process ... deleted it during startup cleanup` (observed). When concurrent work is needed, isolate and run it in a separate checkout with `git worktree`. When the symptom occurs, re-running the relevant Bash recovers it (the artifact itself is not corrupted).
- Automatic repair of legacy records: `init --resume-from-checkpoint` also runs `repair-agent-runs`, completing the `parent_agent_run_id` / `agent_model` missing from the step/substep rows of `agent_runs.jsonl` recorded **before** the introduction of mandatory `agent_model` + auto-backfill (commit `caa10ab`). Because these are append-only and a duplicate `record-agent-run` is also rejected, they cannot be restored going forward, and they permanently failed the `pre_judge` gate of `Validate.judge` and made resume impossible. The repair is authoritatively derived from existing artifacts (`parent_agent_run_id`: substep from `step_result.json#executor_agent_run_id`, step from `orchestration_meta.json#orchestration_agent_run_id`, cross-checked with the child→parent edges of `agent_graph.json`; `agent_model`: adopting the uniform non-empty value of the same orchestration). The orchestration row itself is also covered for `agent_model` only (it is the graph root and legitimately has no `parent_agent_run_id`); on a fresh run `run_workflow.py` already records it via `init --agent-model` (default `claude-opus-4-8` for the claude backend), so this backfill mainly fixes orchestrations created before that wiring. It does not overwrite an existing non-empty value, attaches provenance to the repaired row, and leaves an audit log in `record_repairs.jsonl`. It is idempotent. When `agent_model` cannot be auto-derived (no non-empty value in siblings / multiple values mixed), the repair result becomes `needs_manual` and resume continues as-is (a later gate fails), so the operator explicitly runs `python3 tools/orchestration_runtime.py repair-agent-runs --repo-root . --orchestration-id <id> --agent-model <model_id>` and then resumes again.
- Automatic repair of a wrong-executor `step_result.json`: `init --resume-from-checkpoint` also runs `repair-step-result-executor` for every corrupt node/step. The failure it heals is `validate_pre_judge_step_result_executor_integrity` — a substep-aware (`compile` / `generate` / `validate`) `step_result.json` whose `executor_agent_run_id` is a verify-substep arid instead of the orchestration arid (both the directory key and the field). The `pre_judge` gate of `Validate.judge` requires the executor to be the orchestration agent, so the node locks at `step_result_written` with no forward path (the `write-step-result` guard only blocks a **new** bad write; `--backfill` is keyed to a terminal step agent; `repair-agent-runs` only touches `agent_runs.jsonl` rows). The repair relocates the file to the orchestration-arid directory, rewrites the field, and appends the old substep arid to `substep_agent_run_ids` to keep the substep→step_result linkage. It refuses (leaving the file untouched) unless the wrong arid is a recorded `substep` for that exact node/step and no legitimate `step_result.json` already exists at the orchestration dir, validates the corrected payload exactly as the write path would, leaves an audit log in `record_repairs.jsonl`, and is idempotent. For a manual one-off run: `python3 tools/orchestration_runtime.py repair-step-result-executor --repo-root . --orchestration-id <id> --node-key <node_key> --step <compile|generate|validate>`.
- Cross-phase `Compile` retry of an already-passed phase (`reopen-phase`): when `Validate.judge` returns `structural_violation` / `attribution=ir` (or `Generate.verify` records `ir_inconsistency`), the decision table (§3) routes the retry to `Compile`. After `Compile` / `Generate` / `Build` are checkpointed `pass`, that re-point cannot be expressed in place: `check-step-completed` reads the stale IR as `integrity=ok` (artifact hashes intact, semantic inadequacy invisible), the phase sits at `step_result_written` rather than the `child_finished` the `write-step-result` path requires, and `retry_decisions` only models a within-step substep retry. `reopen-phase --from-phase compile --node-key <node_key> --trigger-agent-run-id <failed_downstream_arid> --reason <reason_code>` resolves this: it snapshots the prior attempt's step/substep runs for the reopened phases as **superseded** (durably in `reopen/superseded_runs.json`, exempt from the `_validate_orchestration_completion_for_pass` vouch so they do not block the eventual `pass`), archives their `step_result.json` to `step_result.superseded.<seq>.json` (freeing the deterministic executor path), drops the affected `completed_steps` checkpoint entries (so `check-step-completed` re-runs them), and resets the affected `phase_state` to `not_started`. The orchestration agent then re-runs `Compile` → `Generate` → `Build` → `Validate` against the corrected IR. `--trigger-agent-run-id` must be a terminal non-pass step/substep strictly downstream of `--from-phase` (the anti-abuse gate). On `--resume` of an `attribution=ir` failure, `init --resume-from-checkpoint` records `orchestration_meta.resume_directive` (`reopen_from` / `node_key` / `trigger_agent_run_id` / `finding_id`, derived from `failure_analysis.json#original_finding`); the resumed agent honors it by calling `reopen-phase` first, then re-running, so the resume proceeds deterministically instead of re-running only `Validate` and reproducing the same fail. A manual one-off run: `python3 tools/orchestration_runtime.py reopen-phase --repo-root . --orchestration-id <id> --node-key <node_key> --from-phase compile --reason <reason_code> --trigger-agent-run-id <arid> [--finding-id <id>]`.
- Recovery from an **unauthorized-write `fail_closed`** (`reopen-phase` with an invalid-log trigger): when a downstream phase fails *because* it performed an unauthorized write (e.g. a Generate Makefile that names the build binary inconsistently, so `make test` relinks a stray binary into the read-only binary dir), `record-agent-run` rejects the run and the orchestration stops `fail_closed`. The rejected run's terminal `fail` payload is **not** appended to `agent_runs.jsonl` — it is diverted to `agent_runs_invalid.jsonl` (so a same-arid retry stays possible) with an authoritative `violations/<arid>.unauthorized_write_violation.json` marker. Historically this dead-locked resume (`resume_reopen_no_valid_trigger`): `reopen-phase` read only `agent_runs.jsonl`, so the actual failing run could not be used as a trigger, and when the only previously-valid trigger had already been consumed by an earlier `reopen_seq`, `reopen-phase` returned `noop` and the operator was forced into a fresh full workflow run. Now `reopen-phase` accepts the diverted entry as `--trigger-agent-run-id` **when** a matching `unauthorized_write_violation.json` exists (the violation file is the anti-abuse gate — a sandbox/identity reject without one is refused, since reopening an upstream phase would not help). The invalid-log trigger is recorded into `reopen/superseded_runs.json` so a redundant re-invocation is `noop` (idempotent), and the result/audit log carries `trigger_source=agent_runs_invalid`. On `--resume`, when the prior failure is such an unauthorized write attributed to a single upstream phase (read from `failure_analysis.json` attribution, mapped to `compile`/`generate`/`build`), `init --resume-from-checkpoint` records `orchestration_meta.resume_directive` (`reopen_from` / `node_key` / `trigger_agent_run_id`, `source=agent_runs_invalid.unauthorized_write`) so the resumed agent calls `reopen-phase` deterministically instead of re-deriving the dead end; the surface defect (the inconsistent Makefile etc.) is still corrected by the re-run Generate phase. A manual one-off run uses the same `reopen-phase` invocation as above with `--from-phase <attributed_phase>` and the diverted arid as `--trigger-agent-run-id`.
- Because a `Build` failure is a deterministic process, do not internally retry Build but go back to `Generate` (this table §3). The Build step remains "incomplete" on the checkpoint and is re-run from Generate on resume.

## 4. Minimal operational checklist
- The `Controlled Spec` has no undefined items.
- `spec.ir.yaml` holds the 5 sections `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` and satisfies the V1–V5 invariants of `Compile.verify`.
- The `evidence_ref` of `spec.ir.yaml.io_contract.outputs` resolves to a `raw` entity.
- `spec.ir.yaml.io_contract.test_evidence_requirements` holds all `test_id` of `tests.md` neither more nor less.
- When `spec.ir.yaml.io_contract.raw_requirements.required_evidence` declares `artifact=state_snapshots` as required, `schema.variables[].name`, `schema.variables[].shape_expr`, `schema.time_variable`, and `schema.time_shape_expr` are defined.
- `write_scope_baseline` is captured in each phase, and a diff comparison is performed before completion.
- The `write_scope` check has not detected any diff outside of `workspace/`.
- The `__pycache__` output destination at `python` execution time is limited to under `workspace/`.
- `Generate.verify` performs each of the G1–G7 verification items.
- `Generate.verify` reconciles the `runner`'s raw-evidence output design with `spec.ir.yaml.io_contract.raw_requirements.required_evidence` / `test_evidence_requirements`, and statically confirms the per-test evidence needed for `Validate.judge` recomputation.
- The required composition of `raw` matches `spec.ir.yaml.io_contract.raw_requirements.required_evidence`.
- In the metadata of the `LLM`-using phase, `verification_status` is `pass`.
- In a trial with `debug_mode=false`, no failed-attempt artifacts are saved.
- `diagnostics` / `perf` / `verdict` all come out.
- `aggregate_verdict` and `summary.dependency_summary` are consistent with `spec.ir.yaml.dependency`.
- The `node_key` set of `spec.ir.yaml.dependency.all_nodes` matches the `node` set of `workspace/ir` / `workspace/pipelines`.
- `orchestration_meta.json` / `agent_graph.json` / `agent_runs.jsonl` exist in `workspace/orchestrations/<orchestration_id>/`.
- `step_result.json` exists in `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/`.
- Each `step` and each `substep` has an independent `agent_run_id`, and the parent-child relationship can be traced by `parent_agent_run_id`.
- The `context_id` of each `step` and each `substep` does not duplicate, and `context_isolated=true` is recorded for all.
- `workspace/orchestrations/<orchestration_id>/preflight.json` satisfies `can_launch_step_agents=true`, `can_launch_substep_agents=true`, and `sandbox_enforced=true`.
- The individual `ir_id` and individual `pipeline_id` of each `node_key` are issued.
- From the execution evidence, it can be confirmed that it is an independent `agent` execution of `orchestration -> step` or `orchestration -> substep`, not a batch `script` execution.
- In a trial without an explicit specification, no reference or viewing of existing workflow output has been performed.
- `lineage.json` is separated per `node`, and a single `lineage` does not mix multiple `node_key`.
- The `Validate.judge` input is limited to the `run_program` execution record and `diagnostics` / `perf` of the same `run_id`.
- A `node` that has dependencies calls the dependency `operation` resolved by `spec.ir.yaml.dependency`.
- A function equivalent to a dependency `operation` is not re-implemented in the depending `node`.
- The implementation body of a dependency `node` is not copied, relocated, or redefined in the upper `node`'s `source/<source_id>/src/`.
- In a `node` that has a dependency `component` with `spec.ir.yaml.impl_defaults.toolchain.language=fortran`, `use <spec_id>_model` and `call <spec_id>__*` are implemented.
- `trial_meta.json`'s `generated_by_stage` / `source_source_id` / `source_binary_id` / `source_command_ref` / `source_artifact_hash` are not missing (`run_id` is not made a separate field because the `runs/<run_id>/` directory path itself encodes it).
- The `run_program` execution command referenced by `trial_meta.json`'s `source_command_ref` includes `spec.ir.yaml.case`.
- A `node` that ended with `blocked` has `aggregate_verdict.json` / `summary.json` / `trial_meta.json`, and `blocked_reason` is recorded.
- The `runner` does not launch an external interpreter such as `python` / `bash` / `sh` / `node`.
- The `runner` does not write `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json`.
- `runs/<run_id>/<node_key_safe>/raw/` exists, and the files needed for `Validate.judge` recomputation are present.
- `raw/metrics_basis.json` is composed from primary evidence, not a copy of `diagnostics.json`.
- The `validate_workspace_root` execution via `run-gate` returns `PASS`.
- The `validate_pipeline_semantics --stage pre_judge`-equivalent execution via `run-gate` returns `PASS`.
- `semantic_review.json` exists and `decision=pass`.
- The `source/<source_id>/src` of different `node_key` do not improperly match exactly.
- `copy_based_artifact_reuse` is not detected.
- `write_scope_violation.json` is not generated.

## Repair cheat sheet on a hook block {#hook-recovery}

When a hook blocks during workflow execution, identify the cause from `reason` and `audit_detail.policy`, and take the next action according to the table below.

| policy | example of the blocked operation | the one next action to take |
|---|---|---|
| `auto_read_expected_block` | the Claude Code harness auto-read a file under `.claude/settings.json` / `.cursor/mcp.json` / `mcp_servers/README.md` / `mcp_servers/mcp_servers.example.json` / `mcp_servers/tools/` (the harness actually reads `*.json`) immediately after startup (for the orchestration agent, additionally `MEMORY.md` / `README.md` / `TODO.md` / `CLAUDE.md` / `~/.claude/projects/.../memory/MEMORY.md`) | **may be ignored**. It is a deterministic startup behavior of the harness and benign noise. Do not retry or attempt an additional Read. For details of the acceptable range, see blocks (A)/(B) of Operations Rule 3 of `skills/workflow-orchestration/references/startup_contract.md` |
| `read_manifest_read_guard` | `Read` a file outside the permitted root during workflow execution | check `allowed_read_roots` of `read_manifests/<agent_run_id>.json`, and read via `run-gate --gate orchestration_read` if needed. For `launches/<arid>.parent_return_token`, **do not read with the `Read` tool**, but pass it to `record-child-return --return-token` in the `"$(cat <path>)"` form (a Read during the active_child window is evaluated against the child arid's manifest and blocked). For CLI specification confirmation during workflow execution, refer to `docs/CLI_REFERENCE.md`, and do not read `tools/orchestration_runtime.py` directly |
| `output_manifest_write_guard` | a write to `/tmp` / `/dev/shm` / a path outside the manifest | directly specify under `allowed_tmp_root` (= `workspace/tmp/<agent_run_id>/`) of `output_manifests/<agent_run_id>.json` as a **literal path** (e.g. `cat > workspace/tmp/<agent_run_id>/x.patch <<EOF`). Bootstrap Bash such as `export TMPDIR=...` / `jq -er ...` / `printenv` is forbidden because the Claude Code session sandbox's approval request would stop the workflow (see the tmp-area usage contract of `skills/workflow-orchestration/references/startup_contract.md`). The hook judges only the write-target path and does not reference the `$TMPDIR` env |
| `enforce_guarded_apply_patch` | tried to write `.json`/`.txt` with `Edit`/`Write`/`apply_patch` | switch to `python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root . --orchestration-id <oid> --actor-role <role> --agent-run-id <id> --paths-json '["<path>"]' --patch-file workspace/tmp/<agent_run_id>/x.patch --capability-token <token>` (`<agent_run_id>` is literally substituted). For `.yaml` such as `spec.ir.yaml`, use `Edit`/`Write` directly (guarded-apply-patch is `.json`/`.txt` only) |
| `forbid_python_inline_write` | ran `python3 -c` / `python3 - <<EOF` | **write intent**: use `guarded-apply-patch` for `.json`/`.txt`, and the `Edit`/`Write` tool for others. **UUID-generation intent**: use `python3 tools/new_agent_run_id.py`. **JSON-read intent**: read directly with the `Read` tool |
| `forbid_tools_direct_read` | tried to read under `tools/` with `grep` / `cat` / `sed` during workflow execution | the implementation under `tools/` is forbidden to reference as a workflow rule source. For the specification, refer to `docs/` / `spec/` / `skill_must_read_refs`. During repository improvement, maintenance, testing, or refactoring, `tools/*.py` is ordinary source code and may be inspected directly |
| `rule_source_violation` | read another agent's capability / gate result / another phase's SKILL.md | obtain the gate-failure content by capturing stderr with `2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` (`<agent_run_id>` is literally substituted) |
| `forbid_git_reset_hard` | tried to run `git reset --hard` | return individual files with `git restore <file>` or `git checkout <file>` |
| `capability_invalid_empty_write_roots` | tried to write with a capability of `write_roots=[]` | check whether `allowed_output_paths` is correctly set in `record-launch`'s `--request-json` |

## dismiss of unauthorized_write_violation {#dismiss-violation-recovery}

When `record-agent-run` fails with `terminal run has unauthorized write paths: ...`, the operator can approve (dismiss) a benign violation and retry by the following procedure.

**Typical causes (benign)**

- `tools/__pycache__/*.pyc` — git-ignored Python bytecode (mostly removed by the snapshot ignore of Fix 1b, but insurance for when existing pyc remains)
- an audit log generated by the MCP server was not included in `manifest_integrity_protected_logs`

**Recovery procedure**

1. Check the violation's unauthorized_paths.
   ```
   cat workspace/orchestrations/<orch_id>/violations/<arid>.unauthorized_write_violation.json
   ```
2. Dismiss only the benign paths (`--paths` is matched as a subset of the violation's `unauthorized_paths`).
   ```bash
   python3 tools/orchestration_runtime.py dismiss-violation \
     --repo-root . \
     --orchestration-id <orch_id> \
     --agent-run-id <arid> \
     --dismiss-reason "tools/__pycache__ is gitignored Python bytecode and harmless" \
     --operator-token "$(cat ~/.met-dsl/operator_tokens/<orch_id>.txt)" \
     --paths tools/__pycache__/orchestration_runtime.cpython-313.pyc
   ```
   The operator token is auto-generated into `~/.met-dsl/operator_tokens/<orch_id>.txt` at orchestration init. Because it is not under `workspace/`, the agent cannot read it, and only the operator can reference it.
3. Re-run `record-agent-run` with the same `agent_run_id`. If the detected unauthorized_paths are a subset of `dismissed_paths` (= dismissed_paths contains unauthorized_paths), the terminal validation passes. When only some of the violation paths have been dismissed, the re-run fails again, so check whether any non-dismissed violation paths remain.

**Notes**

- dismiss-violation is a safety gate for recording the operator's explicit approval, and must not be called by an automation script.
- To additionally dismiss a new path later, re-run the same command (`dismissed_paths` is overwritten).
- When, for a dismissed violation, a subsequent re-detection includes a **new unauthorized path not covered by the dismissal**, the violation file is regenerated and the terminal validation fails again. At this time, the previous operator approval is not lost but is preserved in the history as `prior_dismissals[]` (`dismissed_at` / `dismiss_reason` / `dismissed_paths` / `superseded_at`) (ensuring the continuity of the audit trail).
- If Fix 1a (the `PYTHONDONTWRITEBYTECODE=1` environment variable) is applied, the `.pyc` violation itself does not occur, so this procedure is usually unnecessary.

## duplicate agent_run_id recovery {#duplicate-agent_run_id-recovery}

Invoking `record-agent-run` twice with the **same `agent_run_id`** raises `ValueError: duplicate agent_run_id: <id>`. It is designed as a non-idempotent hard error, and there is no path to later update/upsert the same `agent_run_id`.

**Typical causes**

- attempted a retry for a child agent_run already appended to `agent_runs.jsonl`
- tried to re-append the orchestration agent's own entry at terminal (the orchestration's termination is canonically via `set-status`, and there is no path to call `record-agent-run` a second time). **When `set-status` writes a terminal status, it automatically terminates the orchestration row (`status:running`) of `agent_runs.jsonl` in-place**, so there is no need to manually append/update the orchestration row (re-appending via `record-agent-run` causes this error).

**Recovery procedure**

1. Number a new `agent_run_id` with `python3 tools/new_agent_run_id.py`.
2. Newly reserve `ir_id` / `pipeline_id` with `python3 tools/orchestration_runtime.py reserve-phase-root --orchestration-id <oid> --agent-run-id <new_arid> --node-key <node_key> --step <step>` (when the old `agent_run_id` already reserved, confirm with the operator whether the reservation can be reused).
3. Re-run the legitimate sequence `record-launch` → `Agent` tool launch → `finalize-child` (the single call that performs `record-child-return` → `deactivate-child` → `record-reply` → `record-agent-run`) with the new `agent_run_id` (see steps 1–6 of CLAUDE.md).
4. To terminate the orchestration itself, call `set-status --status fail_closed --reason-code <code> --reason-detail <detail>`. Because `set-status` automatically terminates the orchestration row of `agent_runs.jsonl` in-place, do not update it manually.

The detailed CLI conventions use [docs/CLI_REFERENCE.md#record-agent-run](CLI_REFERENCE.md#record-agent-run) as the canonical source.

## Substep timeout recovery {#substep-timeout-recovery}

When a child Agent tool is cut off midway by an API stream idle timeout, the orchestration agent calls `record-timeout` to finalize the terminal entry. **An ad-hoc script must not be written to `workspace/tmp/`.**

**Premise**: before calling `record-timeout`, always run the following in order.

1. `record-child-return --agent-run-id <arid> --return-token <token>`: record the evidence that the orchestration agent actually observed the Agent tool return.
2. `deactivate-child --child-run-id <arid>`: release the active marker after confirming the ack and re-verifying the token match.
3. `record-timeout --agent-run-id <arid> --reason ...`: record the terminal entry.

```bash
# pass the return-token via the two-step method (same as CLAUDE.md steps 6a→6b).
# step 6a: print the token with a single cat (it matches the allowlist
#          `Bash(cat workspace/orchestrations/*)` and requires no approval). Do not use the
#          $(cat ...) command-substitution form because the Bash tool's static analysis
#          rejects it with `Contains shell syntax ... cannot be statically analyzed`.
#          Do not use the VAR=$(cat ...) 2-step shell-var form either, as it breaks
#          the allowlist match.
cat workspace/orchestrations/<orchestration_id>/launches/<child_agent_run_id>.parent_return_token

# step 6b: embed the token printed above as a literal string.
python3 tools/orchestration_runtime.py record-child-return \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --agent-run-id <child_agent_run_id> \
  --return-token "<literal token>"

python3 tools/orchestration_runtime.py deactivate-child \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --child-run-id <child_agent_run_id>

python3 tools/orchestration_runtime.py record-timeout \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --agent-run-id <child_agent_run_id> \
  --reason "API stream idle timeout after 600s"
```

After the calls, the orchestration agent subsequently calls `set-status --status fail_closed --reason-code <code> --reason-detail <detail>` to terminate the orchestration itself.

### Escape hatch for a wedged child

Only when `record-child-return` cannot be written because the Agent tool process is in an abnormal state where it can observe no return at all, the marker check can be bypassed with `record-timeout --force-reason "<operator override content>"`. Prioritize the normal flow, and use it as a last resort.

## Incomplete launch recovery (dangling active_child window) {#launch-incomplete-recovery}

Distinct from the substep-timeout case above (where the orchestration agent is still alive and finalizes the child itself): here the **host LLM process itself exits** while a child launch is mid-flight. `record-launch` opened the active_child window — the backend-neutral per-arid marker `active_children/<arid>.txt` is written for **all** backends (Claude additionally writes the sequential pointer `active_child_agent_run_id.txt`) — but the child hung or was interrupted before returning, so there is no `child_returns/<arid>.txt` and no terminal `agent_runs.jsonl` row for `<arid>`. The host can even exit cleanly (returncode 0 — e.g. the Claude orchestration agent ends its turn with an "I've paused" message), which the returncode-based failure path would otherwise miss, leaving the orchestration silently `running`. Detection keys off the backend-neutral marker, so it covers Codex/Cursor as well as Claude (the `~/.claude` transcript correlation below is Claude-specific; for other backends the in-repo dangling-child facts are still captured).

`tools/run_workflow.py` now detects this when the synchronous launch subprocess returns and:

- terminalizes the orchestration with `set-status --status fail --reason-code launch_incomplete_active_child` (so `--resume` can recover it; a non-terminal `running` is otherwise refused by implicit resume — see §3-1), and
- writes a one-shot diagnostics snapshot **`workspace/orchestrations/<id>/launch_incident.runtime.<uuid12>.json`** capturing the dangling child (arid / step / substep / `launch_recorded_at` / elapsed) and the **decisive, otherwise-ephemeral** child subagent transcript tail correlated from `~/.claude` (`last_activity_ts`, last `tool_use`, `dead_air_seconds`, and the interrupt/abort marker). `failure_analysis.json#launch_incident_refs` links to it.

To diagnose (now or later, even after `~/.claude` cleanup removes the live transcript):

```bash
python3 tools/audit_orchestration.py --orchestration-id <orchestration_id>
# → "## Dangling launch (active_child window)" section: which child, when launched,
#   last activity, dead-air seconds, and the abort marker.
```

`audit_orchestration.py` reproduces the same correlation on demand from in-repo artifacts plus the `~/.claude` transcript when still present; when the transcript has been cleaned it degrades to the in-repo facts and points at the captured `launch_incident.runtime.*.json`.

Recovery is the normal `python3 tools/run_workflow.py --resume --orchestration-id <orchestration_id>` (§3-1): the completed substeps are skipped and the dangling substep is re-launched. As part of the terminal→`running` reset (`enable_checkpoint_resume`), `--resume` reconciles the artifacts the dead host left behind for the abandoned launch:

1. **Stale active_child markers** (`active_child_agent_run_id.txt` / `active_children/<arid>.txt`, with no `deactivate-child` having run) are cleared (logged as `resume_cleared_stale_active_child`) — without this the resumed agent's `record-launch` would be rejected by the Claude-backend sequential-child check while the stale pointer persists.
2. **Orphan `agent_graph.json` edges** — `record_launch` writes the parent→child edge before the marker, so the abandoned child has a graph edge but never a terminal `agent_runs.jsonl` row. Such edges are pruned (logged as `resume_pruned_orphan_graph_edges`) — without this the resumed run's eventual `set-status pass` is rejected by `_validate_orchestration_completion_for_pass` with `agent_graph edge child_agent_run_id missing from agent_runs.jsonl`. Pruning is scoped to genuine abandonment on both sides. An edge is removed only when its child (a) **was** genuinely launched — proven by a durable `launches/<arid>.request.json` (the same `is_owner_via_launch` signal) — **and** (b) has **no** evidence of reaching/returning from a run: it is in none of `agent_runs.jsonl`, any `step_result.json` reference, `agent_runs_invalid.jsonl`, or a `child_returns/<arid>.txt` ack. So an arbitrarily-corrupted edge whose child was never launched (no request artifact) is **kept** and still rejected by validation; and a child that completed (step_result-referenced) but lost its run row, one diverted to `agent_runs_invalid.jsonl` (sandbox / session-id failure), or one that already returned (`child_returns` ack) all keep their edge too. Only a launch interrupted **before** the Agent tool returned — launched, but no ack and no run/invalid record — is pruned, which is exactly the dangling-launch case. The launch artifacts (`launches/<arid>.*`, the incident snapshot) are kept for forensics; only the spurious edge is removed.

3. **Stale `child_running` phase state** — `record_launch` sets the node/step to `child_running`, which the phase gates (`apply-patch`, the MCP phase gate, `run-gate`) treat as authorization for child work. The abandoned launch leaves it there even after the marker is cleared, so any such node/step is reset to `not_started` (logged as `resume_reset_stale_child_running`) to drop the dead child's lingering phase authority; the re-launch transitions it back to `child_running` for the real new child.

All three reconciliations are scoped to the terminal-reset path, so a genuinely-running orchestration's live markers / edges / phase state are never touched.

**Stranded build `step_result` after a resume reset — backfill, do not relaunch.** Reconciliation 3 resets the build node/step out of `child_finished` back to `not_started`. If a prior terminal **build** step agent had reached `child_finished` but its `step_result.json` was never written (e.g. the original run captured the failure only inside a later retry's `retry_decisions`, or the host died between `child_finished` and `write-step-result`), the reset removes the `child_finished` state that the normal `write-step-result` path requires. Do **not** launch a fresh build child just to re-reach `child_finished` and write the missing result: each new build step agent is itself something `_validate_orchestration_completion_for_pass` then requires a `step_result.json` for, so the count of missing results never decreases (net-zero) and the orchestration deadlocks (it cannot reach `pass`). Instead, fill the gap with `write-step-result --backfill`, which writes the `step_result.json` for the already-terminal build agent **without** consuming a `child_finished` transition or launching a new agent (gap-fill only, recorded run must be a terminal step agent for the same node/step, and `--result-json` `status` must equal the recorded run status — see `docs/CLI_REFERENCE.md` §write-step-result `--backfill`). Each backfill is net-negative on the missing-result count, so the completion invariant can reach zero. The recurrence root is itself blocked at `record-launch` time: a build cannot relaunch while a prior terminal build step agent for the node still lacks its `step_result.json`.
