# Workflow Orchestration

This document defines the `orchestration agent` that supervises the whole `workflow`, and the independent-agent execution conventions for phases / substeps. It presumes the 5-phase structure `Spec -> Compile -> Generate -> Build -> Validate`.

## Related documents

- CLI reference of all subcommands: [`docs/CLI_REFERENCE.md`](CLI_REFERENCE.md)
- workspace artifact placement: [`docs/WORKSPACE_LAYOUT.md`](WORKSPACE_LAYOUT.md)
- workflow startup contract: [`skills/workflow-orchestration/SKILL.md`](../skills/workflow-orchestration/SKILL.md) and [`skills/workflow-orchestration/references/startup_contract.md`](../skills/workflow-orchestration/references/startup_contract.md)
- launch request templates: [`skills/workflow-orchestration/references/launch_prompts.md`](../skills/workflow-orchestration/references/launch_prompts.md)

## Purpose
- Hierarchize the workflow execution and separate phase responsibilities from audit responsibilities.
- Execute each `step` / each `substep` as an independent agent, and make the execution path traceable.

## Scope
- `Compile` / `Generate` / `Build` / `Validate`
- Per-`node workflow` phase execution, and the execution of in-phase `substep` (`generate` / `verify` / `execute` / `judge`)

## term rules
- `phase` refers to the workflow's logical unit defined in the contract documents under `docs/workflow/WORKFLOW_CORE.md` and `docs/workflow/phases/`.
- `step` refers to the orchestration-level execution unit corresponding to one phase.
- `substep` refers to a lower execution unit decomposed from a `step`.
- `stage` is used only as existing field names such as `generated_by_stage`.

## phase / substep types

| phase | step type | substep |
|-------|-----------|---------|
| Compile | has substeps | `generate` / `verify` |
| Generate | has substeps | `generate` / `verify` |
| Build | single step | - |
| Validate | has substeps | `execute` / `judge` |

## Requirements

### preflight and launch control
- Workflow execution always starts by first launching exactly one `orchestration agent`.
- Before the workflow starts, the preflight of an execution platform that can launch the `step agent` and `substep agent` independently must be run. The preflight includes the `multi_agent` feature and the launchability of a child `agent` in its verification scope, and when it is not `pass` the workflow must not start.
- The preflight of `backend=codex` must simultaneously satisfy `feature_states.codex_hooks=true`, `checks.codex_hooks_enabled.pass=true`, and `checks.codex_home_writable.pass=true`.
- The preflight must include `sandbox_runtime=bwrap` and `sandbox_enforced=true` as required conditions. When at least 1 of `checks.sandbox_bwrap_available.pass=true`, `checks.sandbox_bwrap_userns.pass=true`, or `checks.sandbox_bwrap_exec.pass=true` is not satisfied, the workflow must not start.
- The `codex_hooks` feature decision at native-hook execution time runs only once per `orchestration_id`, and the result must be cached in `workspace/orchestrations/<orchestration_id>/hooks/codex_feature_check.json`.
- Making `preflight.json` `pass` by manual editing is forbidden.
- Just before launching a child `agent`, `multi_agent` and the launchability of the child `agent` must be re-checked by a live probe of the execution platform. On `fail`, `record-launch` and the child-`agent` launch are forbidden, and the workflow transitions to `fail`.
- Before starting each phase, run `workflow-launch-check`, and simultaneously check the required child-`agent`-type decision, execution-platform allowability, session-policy allowability, and dependency readiness.

### phase type and agent type
- Before starting each phase, explicitly judge by phase type whether the target phase requires a `step agent` or a `substep agent`. `Compile` / `Generate` / `Validate` require a `substep agent`, and `Build` requires a `step agent`.
- When the pre-phase judgment determines that a child `agent` is required, the parent `agent` must not start phase-artifact generation, MCP execution, or a verification-purpose provisional implementation before `spawn_agent` completes.
- The phase-artifact roots of `workspace/ir/` and `workspace/pipelines/` can be materialized only by a child `agent` that satisfies the 3 conditions of `record-launch`, a capability token, and `phase_state=child_running`. Direct generation by the `orchestration agent` is forbidden.
- When a root-path reservation is needed before launching a child `agent`, generate only the reservation artifact `workspace/orchestrations/<orchestration_id>/reservations/<node_key_safe>/<step>.json`, and the actual directories of `workspace/ir/` and `workspace/pipelines/` must not be created.
- The `orchestration agent` is responsible only for the progress control of the whole workflow, and must not directly generate the phase-body artifacts (e.g. `spec.ir.yaml`, `diagnostics.json`).
- As a substitute for workflow execution, a script that batch-automates the progress of multiple phases and artifact generation must not be newly generated or executed.
- The `Build` step is a deterministic process that calls the MCP `compile_project` and requires no LLM inference. The `step agent` limits its responsibility to the MCP call and recording the result.
- The `orchestration agent` directly launches each substep of `Compile` / `Generate` / `Validate` via `spawn_agent`.

### capability / write_root
- The changes to phase artifacts permitted to a child `agent` must be limited to under the `write_root` permitted by the capability token.
  - `Compile.generate` / `Compile.verify`: `workspace/ir/<node_key_safe>/<ir_id>/`
  - `Generate.generate` / `Generate.verify`: `workspace/pipelines/<node_key_safe>/<pipeline_id>/source/<source_id>/`
  - `Build`: `workspace/pipelines/<node_key_safe>/<pipeline_id>/binary/<binary_id>/`
  - `Validate.execute` / `Validate.judge`: `workspace/pipelines/<node_key_safe>/<pipeline_id>/runs/<run_id>/<node_key_safe>/`
- Changes under `ir_ref` / `pipeline_ref` are limited to: for `.json` / `.txt` output, the canonical path that passed `guarded-apply-patch`; for other extensions, a direct `Edit` / `Write` to a path enumerated in `allowed_file_tool_paths` of `output_manifests/<agent_run_id>.json`.
- `record-launch` must generate `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` per child `agent_run_id`, and finalize `allowed_output_paths`, `allowed_file_tool_paths`, and `allowed_tmp_root` (`workspace/tmp/<agent_run_id>`).
- **Required file-pin auto-inject for Make builds**: when the step is `Generate` and `spec.ir.yaml.impl_defaults.toolchain.build_system=make`, `record-launch` auto-injects the in-source `Makefile` (`<pipeline_ref>/source/<source_id>/src/Makefile`) into `allowed_output_paths` and flows it into `allowed_file_tool_paths`. With only a bare `src/` directory entry, source extensions (`.f90`/`.c`) can be written with `guarded-apply-patch`, but the extension-less `Makefile` is intentionally excluded from the source-extension set of the directory allowlist (`tools/hooks/common.py`), so it cannot be written via any path and the child fail-stops mid-run to avoid guessing. The orchestration agent usually just omits `allowed_file_tool_paths` (auto-derive).
- **launch-time provisioning verification**: when, in a Make-build Generate launch, the required `Makefile` pin is missing from the finalized `allowed_file_tool_paths` (e.g. the caller passed an explicit `allowed_file_tool_paths` and missed the pin), `record-launch` **fail-fasts with a `ValueError` before launching the child**. This converts an artifact-contaminating mid-run fail-stop into a cheap, recoverable launch-time error.
- `record-launch` must generate `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` per child `agent_run_id`, and finalize `allowed_read_roots` and `denied_read_roots`.
- `record-launch` must generate `workspace/orchestrations/<orchestration_id>/sandbox_profiles/<agent_run_id>.json` per child `agent_run_id`, and finalize the `read_roots`, `write_roots`, and runtime bind composition needed for `bwrap` execution. The child launch allows only `bwrap` execution using that profile.

### file write paths
- When a `step agent` / `substep agent` changes a phase artifact, it must branch the write path by the output path's extension. For `.json` / `.txt` output, the canonical invocation is `guarded-apply-patch` that passed the `apply_patch_writes` gate, and for extensions other than the above such as `.yaml` / `.yml` / `.md` / source code, the canonical invocation is a direct `Edit` / `Write` to a path enumerated in `output_manifests/<agent_run_id>.json.allowed_file_tool_paths`.
- `spec.ir.yaml` is in `.yaml` format, so write it with `Edit` / `Write`.
- The direct execution of normal `apply_patch`, shell redirection, `tee`, `sed -i`, `perl -0pi`, and file writes via `python` / `sh` / `bash` are forbidden.
- The connectivity check of `guarded-apply-patch` must be performed with a dry-run or no-op patch.
- A file write via shell, regardless of whether the target path is a phase artifact, must be forbidden unless it is included in the canonical invocation explicitly stated in the child-`agent` launch request.

### LLM context
- The `step agent` and `substep agent` must not share the same `LLM` context. Each `agent_run_id` has a unique `context_id` and requires recording `context_isolated=true`.
- `Compile.generate` and `Compile.verify` are launched in independent contexts. The same applies to `Generate.generate` and `Generate.verify`, and `Validate.execute` and `Validate.judge`.

### agent_run recording
- For a phase that has `substep`, the `orchestration agent` must launch the required `substep` group, make the completion judgment, and then finalize `step_result.json`.
- The `orchestration agent` must judge the launchability of a `step agent` or `substep agent` based on the dependencies reconstructed from `deps.yaml` and `spec_catalog.yaml` and the `dependency` section of `spec.ir.yaml`.
- Every `agent` execution has an `agent_run_id` and must record the input references, output references, and parent-child relationship.
- Each line of `agent_runs.jsonl` requires recording `started_at` and `status`, and when `status` is a terminal status (`pass` / `fail` / `blocked` / `timeout` / `cancel`), requires recording `finished_at`.
- `fail_closed` is used only as a terminal status of `orchestration_meta.status`.
- The `agent_runs.jsonl` of the `step` / `substep` roles requires recording `parent_agent_run_id`, `agent_backend`, `agent_model`, `context_id`, `context_isolated`, `agent_session_id`, `launch_request_ref`, `launch_response_ref`, `launch_prompt_ref`, `launch_reply_ref`, `agent_result_ref`, and `agent_summary_ref`.
- The `parent_agent_run_id` of a `substep agent` points to the `orchestration agent_run_id` that launched that `substep`.
- The child-`agent` identifier obtained from the `spawn_agent` response must be recorded as `agent_session_id`.
- **Claude backend — `agent_session_id` is synthetic, not a Claude Code session.** The Claude Code `Agent` tool returns no `spawn_agent`-style session id, so the convention reuses the child's `agent_run_id` (UUID) as its `agent_session_id` / `context_id` (see `CLAUDE.md`). These are **bookkeeping identifiers only**: there is no `~/.claude/projects/.../<agent_session_id>.jsonl` transcript for a child, and Claude `Agent` subagents do not emit a separate session file or sidechain transcript. The canonical record of a child's work is `workspace/orchestrations/<orchestration_id>/agents/<agent_run_id>/dialogs/` (the launch prompt/request, and the final reply/summary). Do not look for a child under `~/.claude` — it does not exist there. The **orchestration agent itself** does run inside a real Claude Code session; its id is recorded in `orchestration_meta.json#host_session_id` (pinned by `run_workflow.py` via `claude --session-id`), which resolves to `~/.claude/projects/<repo-slug>/<host_session_id>.jsonl`. On resume, a new host session is created and `host_session_id` is updated to the latest.
- `record-launch` must update `workspace/orchestrations/<orchestration_id>/session_run_index.json` immediately after launching the child, and record `agent_run_id`, `agent_session_id`, `context_id`, `agent_role`, and `status`.
- The `edge` of `agent_graph.json` uses `orchestration -> step` or `orchestration -> substep` as the canonical source.
- `agent_runs.jsonl` and `agent_graph.json` must be generated by sequentially appending in-flight events. Post-generation is forbidden.
- The `agent_runs.jsonl` row of the `orchestration agent` itself is appended **only once, immediately after launch** with `agent_role=orchestration`, `status=running`. The `orchestration agent` itself has no path to update this row via `record-agent-run` (a double invoke is rejected with `ValueError: duplicate agent_run_id`). At termination, `set-status` rewrites this row in-place to a terminal status with runtime privilege and assigns `finished_at` (it is a rewrite, not an append, so it does not hit the duplicate guard), and on resume (terminal reset) it conversely returns it to `running`. This is to prevent the agent_runs-based audit / `validate_workspace_root` from mistaking the orchestration row as permanently `running`, and the canonical terminal state of the whole orchestration continues to be expressed on the `orchestration_meta.json` side via `set-status`. For details, [docs/CLI_REFERENCE.md#record-agent-run](CLI_REFERENCE.md#record-agent-run) and [docs/CLI_REFERENCE.md#set-status](CLI_REFERENCE.md#set-status) are the canonical source.
- `record-launch` must be a process dedicated to saving the request/response immediately after `spawn_agent` succeeds.
- `record-launch` must record `sandbox_runtime=bwrap`, `sandbox_enforced=true`, and `sandbox_profile_ref` in the launch response.

### child agent launch request
- When launching a child `agent`, the `orchestration agent` must, using `docs/workflow/WORKFLOW_CORE.md` and the `docs/workflow/phases/phase_*.md` corresponding to the target `step` as the canonical source, make explicit the `execution input`, `verification input`, and `expected output` of the target `step` or `substep`.
- The `orchestration agent` must make explicit, in the child-`agent` launch request, that the canonical source for the requirement definition and judgment rules is `docs/`, `spec/`, and the relevant trial's artifacts. An instruction or implication to read the implementation under `tools/` and extract rules is forbidden.
- The validator invocation by the child `agent` defaults to `run-gate`, and the canonical invocation is `python3 tools/orchestration_runtime.py run-gate --gate <gate_name> --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '<json>'`.
- The direct execution of a validator script is permitted only as an exceptional operation. The permitted targets are limited to `validate_workspace_root.py` and `check_artifact_syntax.py`.
- When a child `agent` runs `apply_patch`, the canonical invocation is `python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role <step|substep> --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>`. The use of `guarded-apply-patch` is limited to `.json` / `.txt` output.
- `record-agent-run` must, in addition to the `output_refs` the child `agent` declared, the `apply_patch_writes` gate record, and `output_manifests/<agent_run_id>.json.allowed_file_tool_paths`, inspect the actually-changed paths by the diff against the baseline. When an actually-changed path is not under the capability token's `write_root`, or is included in neither the gate-permitted paths nor `allowed_file_tool_paths`, it is rejected as an `unauthorized write`.
- **runtime placeholder restoration (recoverability)**: before the terminal-check baseline diff, `record-agent-run` restores, as 0-byte, the runtime-owned placeholders recorded in `created_file_pin_stubs` (e.g. `lineage.json`) that have not been rewritten via a gate (= not covered by `gate_changed_paths`) and are currently absent. This prevents the deadlock in which a runtime placeholder deleted as collateral is judged an `unauthorized write` and the orchestration agent, having no means of restoration, falls into permanent `fail_closed`. It also applies to a terminal record with `status=fail`/`blocked`/`timeout`, making it possible to record a failed run in `agent_runs.jsonl` (enabling a clean restart). Because `record-agent-run` operates with runtime privilege, the canonical-path write that is forbidden to the orchestration agent itself is permitted here.
- The `orchestration agent` must generate the child-`agent` launch-request body from the corresponding template in `skills/workflow-orchestration/references/launch_prompts.md`. A free-form prompt that does not use the template is forbidden.
- `ir_ref`, `pipeline_ref`, and `dependency_ref` must be finalized to canonical paths before launching the child `agent`. A placeholder must not be recorded in the launch request.

### repair / retry
- The `orchestration agent` must evaluate the child `agent`'s returned result and judge `issue_severity` (`minor` / `major` / `critical`).
- The `orchestration agent` must judge whether re-submission is needed based on `issue_severity` and the scope of contract deviation, and when re-submission is needed, must choose `repair_strategy` (`reuse` / `restart`).
- The `orchestration agent` must not directly perform the repair of a phase artifact itself. When a repair is needed, it must re-delegate to the child `agent` of the target `step` or `substep`.
- `repair_strategy=reuse` may be chosen only when it can converge with a local fix without changing the input contract and expected output of the target `step` or `substep`.
- `repair_strategy=restart` is chosen when any of contract reinterpretation, design reconstruction, or wide-scope regeneration is needed.
- On re-submission, regardless of `repair_strategy`, a new `agent_run_id` and a new `context_id` are issued.
- With `repair_strategy=reuse`, the `agent_session_id` may be reused. With `repair_strategy=restart`, a new `agent_session_id` must be issued.
- The `launches/<agent_run_id>.request.json` on re-submission requires recording `issue_severity`, `repair_strategy`, `repair_target_agent_run_id`, and `repair_reason`.
- With `repair_strategy=reuse`, the `apply_patch_writes` gate evidence is inherited from `repair_target_agent_run_id`. A new `guarded-apply-patch` call is unnecessary, and `record-agent-run` references `gates/<repair_target>/apply_patch_writes.json` as the canonical evidence. The fact of inheritance is recorded in `<orch_root>/agents/<agent_run_id>/audit/gate_inheritance.json` and constitutes the audit path. With `repair_strategy=restart`, there is no inheritance (because of contract reinterpretation, new evidence is required).

### baseline diff contract on re-submission
- The child baseline diff in a retry after a `record-agent-run` reject walks the live workspace each time and compares it with the baseline (`_compute_changed_paths_against_baseline`). This keeps any filesystem modification after deactivate within the scope of terminal write validation.
- To prevent retry brick-cascade, only `<orch_root>/agent_runs_invalid.jsonl` (and the lock sidecar) is narrowly excluded from the live diff as a runtime-owned single file. This is the file that causes the previous `record-agent-run` failure to contaminate the next retry's diff, and `record-agent-run` itself is confirmed to be the writer. Other control-plane paths such as `<orch_root>/audit/`, `<orch_root>/violations/`, and `<orch_root>/failure_analysis.json` are **not** blanket-exempt — a hook-bypass write by the child remains as a backstop for terminal validation (`tools/orchestration_runtime.py::_should_ignore_runtime_snapshot_path` is the canonical source).
- Per-arid runtime-managed prefixes such as `<orch_root>/launches/...`, `<orch_root>/violations/...`, and `<orch_root>/agents/...` are also excluded from the diff (originally runtime-only directories). The parent scratch under `workspace/tmp/<parent_arid>/` is excluded by the `parent_tmp_root` exclusion of `_validate_actual_write_paths`.
- `deactivate-child` saves the child-authored path set in `<orch_root>/agents/<agent_run_id>/deactivate_snapshot.json` for audit. This snapshot is for manual audit and future debug use only, and is not referenced in the `record-agent-run` validation path (the live diff is always run).

- The feedback direction from a failed phase is fixed per phase:
  - `Compile` failure → only an in-Compile retry (no automatic retry because upstream is the manual Spec).
  - `Generate` failure → an in-Generate retry. When a verify failure of `source_meta.json` is judged `attribution=ir`, go back to `Compile`.
  - `Build` failure → go back to Generate. Because Build itself is a deterministic process, it does not involve an LLM, and forwards to Generate, as `repair_reason`, one of `compile_error` / `link_error` / `make_error` recorded in `build_log` (for details, the retry-trigger section of `docs/workflow/phases/phase_03_build.md`).
  - `Validate` failure → deterministically decide whether to go back to Generate / Compile / Spec by the combination of the `judge`'s `semantic_review.json#findings[*].attribution` and `verdict.json#failure_class` (the canonical decision table is the "Decision criteria for retry on failure" section of `docs/workflow/phases/phase_04_validate.md`).

## Design Policy
- Single responsibility: one `agent` has only one responsibility.
- Hierarchical delegation: control via the 2 systems `orchestration agent -> step agent` and `orchestration agent -> substep agent`.
- Contract-driven: when launching a child `agent`, fix the input contract and output contract, and forbid reads/writes outside the contract.
- Traceability: save all launch / termination events chronologically, and make the same judgment reproducible on re-execution.

## Orchestration instruction contract
### Common required items
- The `orchestration agent` must record, in the launch request to a child `agent`, `orchestration_id`, `agent_run_id`, `parent_agent_run_id`, `node_key`, `step`, `substep` (when it exists), `ir_ref`, `pipeline_ref`, and `dependency_ref` as required.
- The launch-request body to a child `agent` is based on the corresponding template in `skills/workflow-orchestration/references/launch_prompts.md`, generated by substituting the in-template placeholders with the actual values of the target `agent_run`.
- The launch request to a child `agent` records `execution input`, `verification input`, `expected output`, `write_root`, and `read_roots` as required.
- `execution input` is limited to the input that the relevant `agent` may directly reference to generate the artifact.
- `verification input` is made explicit as input that the relevant `agent` may use only for pass/fail judgment, consistency confirmation, and dependency confirmation.
- `expected output` is made explicit including the file name, storage location, and update responsibility.
- The parent `agent` must not instruct guessed completion on an input shortage. When there is an input shortage, it instructs a `fail-fast` stop.
- The launch request to a child `agent` records `skill_name`, `skill_ref`, and `skill_must_read_refs` as required.

### Conventions for `ir_ref` / `pipeline_ref` / `dependency_ref`
- `ir_ref` is only `workspace/ir/<node_key_safe>/<ir_id>`, and an additional path segment must not be appended. `<ir_id>` is in the canonical `<slug>_<YYYYMMDD>_<seq3>` form (regex `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`, canonical source: `docs/workflow/WORKFLOW_CORE.md` and `_SLUG_DATE_SEQ3_PATTERN` of `tools/orchestration_runtime.py`). Place `<node_key_safe>` as the parent directory of `<ir_id>`, and do not prepend it as a prefix to `<ir_id>` itself (because `node_key_safe` contains `__` / `_`, it violates the slug regex).
- `pipeline_ref` is only `workspace/pipelines/<node_key_safe>/<pipeline_id>`, and an additional path segment (including `source/` or `source_meta.json`) must not be appended.
- `dependency_ref` fixes the canonical path per phase. `Compile` records `spec/.../deps.yaml`, and from `Generate` onward records the phase root of `workspace/...` (`ir_ref` or `pipeline_ref`), forbidding a direct `spec` reference.
- In the launch request of `Generate verify`, `source_id` must be recorded as required.
- When a `step agent` / `substep agent` ends with `pass`, each path of `output_refs` must be included under the `ir_ref` or `pipeline_ref` directory recorded in the corresponding launch request.

### `Compile` launch request
- The `skill_must_read_refs` of `Compile.generate` includes `controlled_spec.md`, `tests.md`, `deps.yaml`, and `spec/registry/spec_catalog.yaml`.
- The `skill_must_read_refs` of `Compile.verify` includes the `spec.ir.yaml` generated by `Compile.generate`, `controlled_spec.md`, `tests.md`, and `deps.yaml`.
- `Compile.verify` has as a required responsibility the structural-invariant verification of `spec.ir.yaml` (all cases covered by algorithm.steps / closure consistency of dependency resolution / consistency of the output contract and the algorithm output). In addition, when `impl_defaults.toolchain.language` / `impl_defaults.toolchain.standard` / `impl_defaults.toolchain.build_system` / `impl_defaults.target.architecture` are undefined, it is a `fail` (canonical source: `docs/IMPL_PLAN_SPEC.md` "Required items").

### `Generate` launch request
- The `skill_must_read_refs` of `Generate.generate` includes `spec.ir.yaml`. It must not read `controlled_spec.md` directly.
- The `skill_must_read_refs` of `Generate.verify` includes `spec.ir.yaml` and, as relative paths based on `pipeline_ref`, `lineage.json` and `source/<source_id>/source_meta.json`.

### `Validate` launch request
- The `skill_must_read_refs` of `Validate.execute` includes `spec.ir.yaml` and `pipeline_ref/binary/<binary_id>/binary_meta.json`.
- The `skill_must_read_refs` of `Validate.judge` includes `spec.ir.yaml`, `tests.md`, the `raw/` / `diagnostics.json` / `perf.json` / `quality_check.json` / `trial_meta.json` under the same `run_id`, and `pipeline_ref/source/<source_id>/`. The judge's launch request does not require `source_id` / `source_binary_id` (the runtime enforces only `run_id`); instead the judge reads `trial_meta.json` under the same `run_id` and resolves `trial_meta.json.source_source_id` as `<source_id>` (because trial_meta is written by Validate.execute and the runtime has verified its match with `binary_meta.json.source_source_id`, there is no path for the judge to read the wrong source even in a pipeline where multiple sources coexist due to retries).
- `Validate.judge` must recompute the judgment metrics from `raw/` via an independent path, and confirm consistency with `diagnostics.json`. It must execute an `LLM` semantic check.

## Operations Rules
1. At workflow start, issue an `orchestration_id` and create `workspace/orchestrations/<orchestration_id>/orchestration_meta.json`.
2. Before the workflow starts, record the preflight result in `workspace/orchestrations/<orchestration_id>/preflight.json`, and when `can_launch_step_agents=true`, `can_launch_substep_agents=true`, and `sandbox_enforced=true` are not simultaneously satisfied, stop with `fail`.
3. Before starting each phase, confirm the phase type, and finalize the launch target as a `substep agent` for `Compile` / `Generate` / `Validate` and a `step agent` for `Build`.
4. The `orchestration agent` saves `launches/<agent_run_id>.request.json`, `launches/<agent_run_id>.response.json`, `launches/<agent_run_id>.prompt.txt`, and `launches/<agent_run_id>.reply.txt` per launch request of a `step agent` or `substep agent`.
5. The `response.json` and `child.response.json` saved by `record-launch` are a complete save of the actual `spawn_agent` response, and must not drop the child-`agent` identifier.
6. On completion of each `step agent` and each `substep agent`, save `agents/<agent_run_id>/dialogs/agent.result.json` and `agents/<agent_run_id>/dialogs/agent.summary.txt`.
7. `agent.summary.txt` includes at least the final `status` and the failure cause or main-artifact reference.
8. `launches/<agent_run_id>.prompt.txt` is the body that concretizes the corresponding template in `skills/workflow-orchestration/references/launch_prompts.md`.
9. The `orchestration agent` reconciles `deps.yaml`, `spec_catalog.yaml`, and the `dependency` section of `spec.ir.yaml`, and finalizes the execution queue based on the `spec` dependencies.
10. The `orchestration agent` issues a `step agent` or `substep agent` per launch target, and passes `node_key`, `step`, `ir_ref`, `pipeline_ref`, and `dependency_ref` as input.
11. Before launching the `Compile` of an upper `node`, the `orchestration agent` reconciles the `ir_ref` and `ir_meta.json.verification_status` per immediate dependency `node`, and must not launch when `direct dependency ir readiness` is not satisfied.
12. Before launching `Generate` onward of an upper `node`, the `orchestration agent` reconciles the `ir_ref`, `pipeline_ref`, and latest `aggregate_verdict` per immediate dependency `node`, and must not launch when `direct dependency execution readiness` is not satisfied.
13. When `direct dependency ir readiness` or `direct dependency execution readiness` is not satisfied, the `orchestration agent` records the relevant `node` as `blocked` or `fail`.
14. The `orchestration agent` makes explicit the `execution input`, `verification input`, and `expected output` of the target `step`.
15. For a phase that has `substep`, the `orchestration agent` launches each `substep agent` sequentially.
16. The `substep agent` generates its own artifact and the corresponding phase's metadata, and returns `agent_output_ref` to the `orchestration agent`.
17. The `orchestration agent` evaluates the child `agent`'s returned result, and finalizes `issue_severity` and whether re-submission is needed.
18. When re-submission is needed and `repair_strategy=reuse`, the `orchestration agent` may permit continuation repair of the same `agent_session_id`. It issues a new `agent_run_id` and adds a `record-launch` record with `relation_type` of `reuse`.
19. When re-submission is needed and `repair_strategy=restart`, the `orchestration agent` relaunches a `substep agent` with a new `agent_session_id` and adds a `record-launch` record with `relation_type` of `restart`.
20. For a phase that has `substep`, the `orchestration agent` verifies the required artifacts of all `substep`, and outputs `step_result.json` to `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/step_result.json`. This `agent_run_id` is the `orchestration agent_run_id`. The `substep_agent_run_ids` of `step_result.json` enumerates, without omission, the `agent_run_id` of **all** `substep` launched in that `step` and recorded in `agent_runs.jsonl`.
21. `step_result.json` holds a `retry_decisions` array when re-submission was performed, and records `issue_severity`, `repair_strategy`, `repair_target_agent_run_id`, `new_agent_run_id`, and `repair_reason` in each element. In the `retry_decisions` recorded in a `write-step-result` with `status=pass`, each `new_agent_run_id` is limited to a `pass` run finally adopted into the `effective pass substep` set.
22. A re-submission triggered by a `noncanonical_phase_write_attempt` requires `repair_strategy=restart`.
23. The `status=pass` judgment in the `step_result.json` of a phase that has `substep` is made over the `effective pass substep` set.
24. In a `step_result` with `status=pass`, each run included in the `effective pass substep` set must have terminated with `pass`.
25. The `required_outputs` coverage judgment in a `step_result` with `status=pass` is made over only the `output_refs` of the `effective pass substep` set.
26. The `step agent` verifies its own artifact in a phase that has no standard `substep` (`Build`), and outputs `step_result.json`.
27. The `orchestration agent` receives `step_result.json` and judges the launchability of the next `step`.
28. `node` execution proceeds sequentially in the dependency order reconstructed from `deps.yaml`, `spec_catalog.yaml`, and `spec.ir.yaml.dependency`. It does not perform parallel execution unless explicitly instructed.
29. When a `step agent` or `substep agent` is `fail` / `timeout` / `cancel`, the relevant `step` of the relevant `node` is `fail`, and downstream `step` launch is forbidden.
30. The `orchestration agent` appends each `agent` execution event to `workspace/orchestrations/<orchestration_id>/agent_runs.jsonl`.
31. The `orchestration agent` saves the parent-child relationship in `workspace/orchestrations/<orchestration_id>/agent_graph.json`, and requires recording `parent_agent_run_id`, `child_agent_run_id`, and `relation_type`.
32. All `agent` of the core workflow must not write outside of `workspace/`.
33. When the actual processing of a `step` / `substep` is proxied by a script during workflow execution, it is a `fail`, and the relevant trial is discarded.
34. On re-submission, issue a new `agent_run_id`, and do not overwrite existing `launch` evidence or `agent_runs` rows.
35. `status` and `can_launch_*` must not be changed by manual editing or post-editing of `preflight.json`.
36. When the live probe just before launching a child `agent` is `fail`, `record-launch` must not be run.
37. The live probe that `record-launch` runs is skipped when a probe that succeeded within the TTL set by `METDSL_PREFLIGHT_TTL_SECONDS` (default 30 minutes) exists. It is disabled when `METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT=1` is explicitly set.
38. The execution result of a native hook is appended to `workspace/orchestrations/<orchestration_id>/hooks/native_hook_events.jsonl`.
39. `tools/run_workflow.py` sets `METDSL_MISSING_ORCHESTRATION_ID_POLICY=strict` at workflow start, and forbids hook execution without an orchestration_id.
40. When about to deviate into a contract-violating shortcut in a child-`agent`-required phase, the `orchestration agent` makes explicit that the relevant phase requires a child-`agent` launch, and returns to the legitimate launch procedure.
41. After `write-step-result` completes with `status=pass`, `orchestration_checkpoint.json` is auto-updated by `tools/orchestration_runtime.py`.
42. In an orchestration with `resume_enabled=true`, the `orchestration agent` runs `check-step-completed` before launching each `step`, and permits skipping the relevant `step` only when `completed=true` and `integrity=ok`.
43. A `step` skipped by checkpoint is recorded in `agent_runs.jsonl` as `agent_role=skipped_by_checkpoint`.
44. In an orchestration with `resume_enabled=false`, a `step` must not be skipped by trusting `orchestration_checkpoint.json`.
45. When the `status` of `write-step-result` is terminal (`pass` / `fail` / `blocked` / `timeout` / `cancel`), `validation_stage` is required in the `step_result.json` of `Compile` / `Generate` / `Build` / `Validate`. The allowed values are `Compile: compile|full`, `Generate: post_generate|full`, `Build: post_build|full`, `Validate: post_execute|pre_judge|full` (runtime canonical: `STEP_REQUIRED_VALIDATION_STAGES` of `tools/orchestration_runtime.py`). The runtime rejects with `ValueError` for anything other than the per-step allowed values, or on omission.
46. A result of `codex_feature_check.json` with `status_kind=probe_error` must not be permanently fixed. A re-probe is permitted after `METDSL_HOOK_FEATURE_RETRY_TTL_SECONDS` (default 30 seconds) elapses.
47. `workspace/tmp/<agent_run_id>/` can be used as each agent's temporary working area. The agent directly specifies that literal path (`output_manifest_write_guard` judges only the write-target path and does not reference the `$TMPDIR` env). `record-agent-run` auto-deletes `workspace/tmp/<agent_run_id>/` after recording that `agent_run`. `tools/run_workflow.py` sets the environment variable `TMPDIR` to `workspace/tmp/<orchestration_agent_run_id>/` after `init` succeeds, but this is an insurance for subprocess inherit, and `export TMPDIR=...` on the agent side is unnecessary and forbidden (the Claude Code session sandbox's approval request would stop the workflow).

## Decision Criteria
- Per workflow, an `orchestration_id` is issued and `orchestration_meta.json` exists.
- Each `step` or each `substep` has an independent `agent_run_id`.
- The `context_id` of `step` and `substep` do not duplicate, and `context_isolated=true` is recorded for all.
- The `agent_runs.jsonl` of `step` and `substep` records `agent_session_id` and the various references, and the referenced entities exist.
- `launches/<agent_run_id>.response.json` and `agents/<agent_run_id>/dialogs/child.response.json` hold the same content of the actual `spawn_agent` response.
- `agent_runs.jsonl.agent_session_id` matches the child-`agent` identifier of the corresponding `launch response`.
- `preflight.json` exists and satisfies `can_launch_step_agents=true` and `can_launch_substep_agents=true`.
- `sandbox_runtime=bwrap` and `sandbox_enforced=true` are recorded in the preflight.
- From the execution record of each phase, it can be traced that `Compile` / `Generate` / `Validate` used a `substep agent` and `Build` used a `step agent`.
- The parent-child relationship `orchestration -> step` or `orchestration -> substep` can be traced in `agent_graph.json`.
- The `executor_agent_run_id` of `step_result.json` matches the relevant directory name, and `substep_agent_run_ids` is consistent with the parent-child relationship. For a phase with no standard `substep` (`Build`), `substep_agent_run_ids=[]` is allowed.
- For a `step` that has `substep`, the `agent_run_id` of all `substep` of that `step` recorded in `agent_runs.jsonl` is included in the `substep_agent_run_ids` of some `step_result.json`.
- The `required_outputs` of `step_result.json` match the phase contract of `docs/workflow/WORKFLOW_CORE.md` and the corresponding `docs/workflow/phases/phase_*.md`.
- When `step_result.json` holds `retry_decisions`, the `effective pass substep` set can be uniquely restored.
- For `Compile` / `Generate` / `Build` / `Validate` where `step_result.json` has a terminal status, `validation_stage` matches the per-phase allowed values (item 45).

## Patch application contract

The specification of the `guarded-apply-patch` subcommand (canonical source: this section. The implementation of `tools/orchestration_runtime.py` must not be referenced directly).

### CLI interface

```
python3 tools/orchestration_runtime.py guarded-apply-patch \
  --repo-root <repo_root> \
  --orchestration-id <orchestration_id> \
  --actor-role <step|substep> \
  --agent-run-id <agent_run_id> \
  --paths-json '<JSON array of changed paths>' \
  --patch-file <path_to_patch_file> \
  --capability-token <capability_token>
```

Direct embedding via `--patch-text` is also possible, but to avoid the ARG_MAX limit, going through `--patch-file` is recommended. The storage location of `--patch-file` allows only a literal path under `allowed_tmp_root` (= `workspace/tmp/<agent_run_id>/`) (a reference to the `$TMPDIR` env works, but to minimize env dependence the literal is canonical).

### automatic strip decision

The CLI argument `--strip` does not exist. Using the `changed_paths` passed via `--paths-json` as an oracle, it internally tries `git apply --check` in the order `-p1` → `-p0`, and automatically selects the first strip that can cover all `changed_paths`.

### Output contract

- On success, exit code 0; on failure, non-0.
- `violations[]` and the failure reason are output to **stderr** in JSON format.
- The gate result is written to `workspace/orchestrations/<orch_id>/gates/<agent_run_id>/apply_patch_writes.json`, but this file must not be read directly.

### Permitted extensions

Only `.json` / `.txt` output. For `.yaml`, `.yml`, `.md`, and source code, use the `Edit`/`Write` tool (via `allowed_file_tool_paths`). `spec.ir.yaml` is written via `Edit`/`Write`.

### Protection of runtime-generated placeholders

`record-launch` pre-generates a file pin (e.g. Generate's `lineage.json`) as a 0-byte placeholder so that bwrap can bind it at file granularity, and records it in `created_file_pin_stubs` of `sandbox_profiles/<agent_run_id>.json`. After the apply completes, `guarded-apply-patch` **restores, as 0-byte, a placeholder of `created_file_pin_stubs` not included in `changed_paths` if it has disappeared** (defense-in-depth). That `git apply` deletes a path outside the `changed_paths` coverage is already rejected by the coverage check after the strip decision, but even in case of an out-of-band deletion, it guarantees that the runtime-owned placeholder is not lost, preventing the downstream `record-agent-run` terminal check from making it permanent `fail_closed` as "a non-gate change to a runtime artifact = `unauthorized_write`". A path covered by `changed_paths` (a path the agent intentionally rewrote/deleted via a gate) is excluded from restoration.

## Capability / Manifest contract

The required fields and invariants of the 3 manifests that `record-launch` issues (canonical source: this section).

### Required fields of `capabilities/<agent_run_id>.json`

| field | type | description |
|---|---|---|
| `agent_run_id` | string | the child agent's UUID |
| `capability_token` | string | a 32-byte hex token |
| `orchestration_id` | string | the orchestration ID |
| `agent_role` | `"orchestration"\|"step"\|"substep"` | the agent role |
| `node_key` | string | `<spec_kind>/<spec_id>@<spec_version>` |
| `step` | string | `"compile"\|"generate"\|"build"\|"validate"` |
| `write_roots` | array of strings | the list of write roots the capability permits |
| `mcp_permissions` | object | the MCP permission scope |
| `expires_at` | ISO8601 | the capability expiry |

**Invariant:** a capability whose `agent_role` is `"step"` or `"substep"` must not have an empty array for `write_roots`.

### Required fields of `output_manifests/<agent_run_id>.json`

| field | type | description |
|---|---|---|
| `allowed_output_paths` | array of strings | the permitted path set for `.json`/`.txt` output |
| `allowed_file_tool_paths` | array of strings | the permitted path set for direct `Edit`/`Write` |
| `allowed_tmp_root` | string | the temporary-file permission root (`workspace/tmp/<agent_run_id>`) |

**Usage:** the agent writes by directly specifying the literal path of `allowed_tmp_root` (`workspace/tmp/<agent_run_id>/...`). Because `output_manifest_write_guard` judges only the write-target path and does not reference the `$TMPDIR` env, bootstrap Bash such as `export TMPDIR=...` / `jq -er ...` is unnecessary and forbidden (the Claude Code session sandbox's approval request would stop the workflow; for details see the tmp-area usage contract of `skills/workflow-orchestration/references/startup_contract.md`).

### Required fields of `read_manifests/<agent_run_id>.json`

| field | type | description |
|---|---|---|
| `allowed_read_roots` | array of strings | the list of root paths that are permitted to read |
| `denied_read_roots` | array of strings | the list of root paths that are explicitly denied |

`output_manifests/<agent_run_id>.json` and `read_manifests/<agent_run_id>.json` may be read directly with the `Read` tool (`run-gate` not needed).
