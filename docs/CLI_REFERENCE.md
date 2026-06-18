# CLI Reference (Tier-A frequent subcommands): `tools/orchestration_runtime.py`

## Position of this document

The **canonical CLI reference for the frequent subcommands (Tier-A)** of `tools/orchestration_runtime.py`. It covers those whose payload schema is complex, that have per-phase required-argument switching, and that cannot be determined from the `--help` output alone: `record-launch` / `record-agent-run` / `record-child-return` / `deactivate-child` / `record-reply` / `set-status` / `write-step-result` / `workflow-launch-check` / `reserve-phase-root` / `mark-dependency-readiness` / `guarded-apply-patch` / `run-gate` (12 total).

For the rare subcommands (Tier-B: `init` / `preflight` / `preflight-status` / `record-timeout` / `read-checkpoint` / `verify-checkpoint-integrity` / `check-step-completed` / `orchestration-read` / `repair-agent-runs`), only an overview is in [docs/CLI_REFERENCE_RARE.md](CLI_REFERENCE_RARE.md), and the canonical source for details is `python3 tools/orchestration_runtime.py <sub> --help`.

The information-acquisition policy per tool / subcommand (frequent vs rare, `--help` vs doc) uses the "CLI reference conventions" section of `CLAUDE.md` as the canonical source.

When the argparse definition is updated, update this file in sync (during a `tools/orchestration_runtime.py` edit review, check for a missing addition to CLI_REFERENCE. `tools/tests/test_cli_reference_sync.py` mechanically takes the diff).

Related canonical sources:
- rare subcommand overview: [docs/CLI_REFERENCE_RARE.md](CLI_REFERENCE_RARE.md)
- the startup contract of the whole workflow: `skills/workflow-orchestration/SKILL.md` and `skills/workflow-orchestration/references/startup_contract.md`
- launch prompt templates: `skills/workflow-orchestration/references/launch_prompts.md`
- workspace artifact placement: `docs/WORKSPACE_LAYOUT.md`
- hook recovery cheat sheet: `docs/RUNBOOK.md#hook-recovery`

## Common conventions

- `--repo-root` / `--orchestration-id` are **required** in (almost) all subcommands.
- agent_run_id is a UUID. The canonical path for issuing a new one is `python3 tools/new_agent_run_id.py` (`python3 -c 'import uuid; …'` is rejected by hook policy).
- The form of `node_key` is `<spec_kind>/<spec_id>@<spec_version>` (e.g. `component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0`). It is not a filesystem path.
- The form of `ir_id` / `pipeline_id` is `<slug>_<YYYYMMDD>_<seq3>` (slug being hyphen-separated lowercase alphanumeric). E.g. `flux-rsn-p0_20260425_001`. An underscore in the slug is invalid.
- ISO 8601 timestamps are canonically UTC (`Z` suffix).
- For JSON arguments (`--*-json`), be careful with shell quoting. For a complex payload, use a file specification like `--patch-file`.
- **Terse stdout by default.** The high-frequency bookkeeping subcommands (`record-launch` / `record-agent-run` / `record-child-return` / `deactivate-child` / `record-reply` / `write-step-result` / `run-gate`) print **only the result fields the orchestration agent consumes downstream** to stdout, not the full payload. This keeps the orchestration's resident context small (its cache-read cost scales with context size × turn count). The full payload is always persisted to the canonical artifact files regardless (`launches/<arid>.*`, `agent_runs.jsonl`, `steps/.../step_result.json`, `gates/<arid>/<gate>.json`, etc.); pass `--verbose` to also emit the full JSON to stdout for debugging/audit. Soft-failure signals (`violations` / `error[s]` / `warning[s]`) are retained in terse output when present, and hard failures still exit non-zero via stderr.
  - `record-launch` terse fields: `capability_token`, `capability_ref`, `read_access_manifest_ref`, `allowed_output_manifest_ref`, `sandbox_profile_ref`, `launch_prompt_ref`, and **`launch_prompt_text`** (the exact rendered prompt the orchestration passes verbatim to the Agent tool — it cannot read the template or the written prompt file). The remaining `launch_*_ref` / `child_launch_*_ref` paths are deterministic from `<orchestration_id>`+`<arid>` and are dropped from terse stdout.
  - `run-gate` terse keeps `result` (the `orchestration_read` content — child agents' only allowed path for those reads) in addition to `violations` / `gate_result_ref`.

---

## Tier-A frequent subcommand list

The 12 subcommands whose details are covered in this file.

| subcommand | purpose | section |
|---|---|---|
| `record-launch` | child-agent launch evidence + capability_token + manifest generation | [record-launch](#record-launch) |
| `record-child-return` | Adv-20: record the Agent tool return ack | [record-child-return](#record-child-return) |
| `deactivate-child` | release the active_children marker | [deactivate-child](#deactivate-child) |
| `record-reply` | overwrite launches/<arid>.reply.txt with the Agent tool response | [record-reply](#record-reply) |
| `record-agent-run` | append 1 line to agent_runs.jsonl + save agent.result.json/agent.summary.txt | [record-agent-run](#record-agent-run) |
| `set-status` | update orchestration_meta.json status + terminate the orchestration row of agent_runs.jsonl in-place | [set-status](#set-status) |
| `mark-dependency-readiness` | update the dependency_readiness detail flags and derive the top-level | [mark-dependency-readiness](#mark-dependency-readiness) |
| `write-step-result` | generate and verify step_result.json | [write-step-result](#write-step-result) |
| `reserve-phase-root` | reserve ir_id / pipeline_id (does not materialize the path) | [reserve-phase-root](#reserve-phase-root) |
| `workflow-launch-check` | the pre-phase gate (dependency readiness, agent type) | [workflow-launch-check](#workflow-launch-check) |
| `run-gate` | run a validator gate (validate_pipeline_semantics etc.) across the capability | [run-gate](#run-gate) |
| `guarded-apply-patch` | the canonical write to `.json` / `.txt` | [guarded-apply-patch](#guarded-apply-patch) |

## Tier-B rare subcommand list (overview only)

For details `python3 tools/orchestration_runtime.py <sub> --help`, for the overview [docs/CLI_REFERENCE_RARE.md](CLI_REFERENCE_RARE.md).

| subcommand | purpose |
|---|---|
| `init` | start an orchestration / generate orchestration_meta.json (usually via `tools/run_workflow.py`) |
| `preflight` | judge the launchability of the execution platform |
| `preflight-status` | read back an existing preflight.json |
| `record-timeout` | the canonical recovery for an API stream idle timeout |
| `orchestration-read` | the gate-mediated read of a path outside the manifest |
| `read-checkpoint` | obtain orchestration_checkpoint.json |
| `verify-checkpoint-integrity` | reconcile the checkpoint with the artifact hash |
| `check-step-completed` | with resume_enabled, confirm the completion of the relevant step |

---

## record-launch

The most important entry point of a child-agent launch. It generates the capability_token, sandbox_profile, output_manifest, and read_manifest, and writes `launches/<child_agent_run_id>.{request,response,prompt,reply}.txt`.

In Claude Code, call it **before launching the `Agent` tool** (because the child agent needs to Read capabilities/<arid>.json immediately after launch).

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--parent-agent-run-id` | yes | the UUID of the orchestration agent (parent) |
| `--child-agent-run-id` | yes | the pre-issued child UUID. In Claude Code, `agent_session_id` is also the same value |
| `--request-json` | yes | the child-launch request payload (schema below) |
| `--response-json` | yes | the spawn response payload (schema below) |
| `--relation-type` | no | default `launch` |

### `--request-json` payload (main fields)

| field | required | content |
|---|---|---|
| `agent_role` | yes | `step` or `substep` |
| `node_key` | yes | `<spec_kind>/<spec_id>@<spec_version>` |
| `step` | yes | `compile` / `generate` / `build` / `validate` (core 5-phase). Tune / Promote are optional flows with a separate entrypoint |
| `substep` | yes for a substep agent | Compile / Generate: `generate` / `verify`. Validate: `execute` / `judge` |
| `orchestration_id` | yes | |
| `agent_run_id` | yes | matches child_agent_run_id |
| `parent_agent_run_id` | yes | |
| `agent_model` | yes | the LLM model id that runs the child agent (e.g. `claude-opus-4-8`). It is provenance information that cannot be derived by the runtime, so it is required at launch. record-launch persists it in the request, and `record-agent-run` auto-copies it to the relevant step/substep entry of `agent_runs.jsonl` (see below). When unspecified, it fail-fasts with `ValueError: launch request must include non-empty agent_model` |
| `workflow_mode` | yes | `dev` / `prod` |
| `ir_ref` | yes | `workspace/ir/<node_key_safe>/<ir_id>` (required in all phases including the Compile phase) |
| `pipeline_ref` | yes | `workspace/pipelines/<node_key_safe>/<pipeline_id>` (required even in the Compile phase. If not yet generated, reserve it first with `reserve-phase-root --step generate`) |
| `dependency_ref` | yes | Compile: `spec/.../deps.yaml`, from Generate onward: the phase root in workspace |
| `skill_name` | yes | `workflow-<step>` or `workflow-<step>-<substep>` |
| `skill_ref` | yes | `skills/<skill_name>/SKILL.md` |
| `allowed_output_paths` or `required_outputs` or `output_refs` | one required for step/substep | the list of write-permitted paths |
| `allowed_file_tool_paths` | optional | the path for direct `Edit` / `Write`. A subset of `allowed_output_paths` |
| `run_id` | yes for a Validate step | the execution ID (1 pinned per launch) |
| `source_id` | yes for a Generate substep / Validate / Build (cross-phase Make) | identifies the Generate output |
| `source_binary_id` | yes for a Validate step | the `binary_id` to use |

### `--response-json` payload (Claude Code)

```json
{
  "agent_run_id": "<child_agent_run_id>",
  "agent_session_id": "<child_agent_run_id>",
  "started_at": "<ISO8601>",
  "backend": "claude"
}
```

`sandbox_runtime` / `sandbox_enforced` / `sandbox_profile_ref` are auto-added by record-launch.

**Make build's `src/Makefile` auto-inject + provisioning verification:** when `step=generate` and `spec.ir.yaml.impl_defaults.toolchain.build_system=make`, record-launch auto-injects `<pipeline_ref>/source/<source_id>/src/Makefile` into `allowed_output_paths` / `allowed_file_tool_paths` (because with only a bare `src/` directory entry the extension-less Makefile cannot be written via any path). When an explicit `allowed_file_tool_paths` is passed and the Makefile pin is missed, it **fail-fasts with a `ValueError` before launching the child**. For the canonical contract, refer to [docs/ORCHESTRATION.md](ORCHESTRATION.md).

---

## record-child-return

Adv-20: record the evidence (`child_returns/<arid>.txt`) that the orchestration agent observed the `Agent` tool return. A premise of `deactivate-child`.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-id` | yes | the child agent's UUID |
| `--return-token` | yes | Adv-30: the value of `workspace/orchestrations/<orch>/launches/<arid>.parent_return_token`. Pass it with `$(cat <path>)`. **Do not read that file in advance with the `Read` tool etc.** (a Read during the active_child window is evaluated against the child arid's `read_manifest` and blocked by `read_manifest_read_guard`) |
| `--reply-excerpt` | no | an optional short text (truncated to 200 chars). For audit |

---

## deactivate-child

Switch the active context back to the orchestration agent. Without the ack of `record-child-return`, it is rejected with a `ValueError`.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--child-run-id` | yes | the child agent's UUID |

---

## record-reply

Overwrite `launches/<arid>.reply.txt` with the final response text of the `Agent` tool.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-id` | yes | the child agent's UUID |
| `--reply-text` | one required | direct text |
| `--reply-from-stdin` | one required | flag. read from stdin (for a large reply) |

---

## record-agent-run

Append 1 line to `agent_runs.jsonl`. For a step/substep role, also save `agent.result.json` and `agent.summary.txt`. When an `unauthorized write` not included in the capability's write_root is detected, reject.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--agent-run-json` | yes | schema below |

### `--agent-run-json` payload

| field | required | content |
|---|---|---|
| `agent_run_id` | yes | UUID |
| `agent_role` | yes | `orchestration` / `step` / `substep` |
| `agent_backend` | yes | `claude` / `codex` / `cursor` |
| `status` | yes | `running` / `pass` / `fail` / `blocked` / `timeout` / `cancel` |
| `started_at` | yes | ISO 8601 |
| `agent_session_id` | yes for step/substep | in Claude Code, the same value as `agent_run_id` |
| `context_id` | yes for step/substep | unique UUID |
| `context_isolated` | yes for step/substep | `true` (Claude Code) |
| `node_key` | yes for step/substep | |
| `finished_at` | yes for a terminal status | ISO 8601 |
| `output_refs` | yes for `pass` | the list of written artifact paths. **Concrete file paths only — a directory entry is rejected** (e.g. `.../src/` fails terminal-payload validation with `allowed_output_paths manifest violation`; enumerate each file: `.../src/<name>.f90`, `.../src/Makefile`, `.../src/mcp_command_log.jsonl`). For Validate.execute likewise enumerate each `raw/state_snapshots/<case_id>.json` rather than `raw/`. |
| `parent_agent_run_id` | automatic | required for a step/substep entry but **need not be written in the payload**. `record-agent-run` auto-copies it from `launches/<arid>.request.json` (record-launch already persisted it from `--parent-agent-run-id`). An explicitly specified value takes precedence |
| `agent_model` | automatic | same as above. auto-copied from the launch request's `agent_model` (required at record-launch time). An explicitly specified value takes precedence |
| `issue_severity` | optional | `minor` / `major` / `critical` |

> **Auto-copy of `parent_agent_run_id` / `agent_model`:** `validate_pipeline_semantics --stage pre_judge` requires both fields in every step/substep entry. Because these already exist in the launch request, `record-agent-run` completes them from the launch request with `setdefault` when missing from the payload. The orchestration agent need not make these explicit in the record-agent-run payload (`agent_model` is specified only once at record-launch time).

**Calling conventions**

- **Double registration of the same `agent_run_id` is not possible.** Re-invoking `record-agent-run` with the same `agent_run_id` as an existing row raises `ValueError: duplicate agent_run_id: <id>`. Because it is not idempotent, to retry, re-number a new `agent_run_id` with `python3 tools/new_agent_run_id.py` and redo the sequence from `record-launch`. For the detailed recovery procedure, [docs/RUNBOOK.md#duplicate-agent_run_id-recovery](RUNBOOK.md#duplicate-agent_run_id-recovery).
- **The orchestration agent's own entry is appended once immediately after orchestration launch**. Append with `agent_role=orchestration`, `status=running`. The orchestration agent itself must not update this row with `record-agent-run` (a second `record-agent-run` is rejected with `duplicate agent_run_id`). Instead, at termination, [set-status](#set-status) rewrites this row in-place to a terminal status with runtime privilege and assigns `finished_at` (on resume, it conversely returns it to `running`). The orchestration's canonical terminal state continues to be expressed on the `orchestration_meta.json` side.
- **runtime placeholder restoration**: before the terminal-check baseline diff, `record-agent-run` restores, as 0-byte, the runtime-owned placeholders of `created_file_pin_stubs` (e.g. `lineage.json`) that were collaterally deleted without going through a gate. This prevents a deleted runtime placeholder from being judged an `unauthorized_write` and the orchestration, with no means of restoration, falling into a permanent `fail_closed` deadlock (it also applies to `status=fail`/`blocked`/`timeout` to make it possible to record a failed run). For the canonical contract, refer to [docs/ORCHESTRATION.md](ORCHESTRATION.md).

---

## set-status

Update `status` / `reason_code` / `reason_detail` / `blocking_policy_scope` of `orchestration_meta.json`. It is the **legitimate entrypoint for orchestration finalize / finalization**, and there is no separate subcommand such as `finalize_orchestration`. A transition to `pass` / `fail` / `fail_closed` is the terminal operation of the whole orchestration. On a terminal transition, it also **rewrites the orchestration's own row (`agent_role=orchestration`) of `agent_runs.jsonl` in-place to that terminal status** and assigns `finished_at` (it is a rewrite, not an append, so it does not hit the `duplicate agent_run_id` guard). In addition to terminating a `running` row, it also follows the row for the permitted `fail` → `fail_closed` promotion (updating an already-`fail` row to `fail_closed`). When the row already matches the target status, it is a no-op (a replay of the same terminal preserves `finished_at`). This is to prevent the agent_runs-based audit / `validate_workspace_root` from mistaking the orchestration row as permanently `running`, and `orchestration_meta.json` remains the canonical terminal state. There is still no path to update the row by calling `record-agent-run` a second time (that path is rejected with `duplicate agent_run_id`). On resume (the terminal reset of `init --resume-from-checkpoint`), as the inverse operation, it returns the relevant row to `running` and removes `finished_at`.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--status` | yes | `pass` / `fail` / `fail_closed` / `blocked` / etc. |
| `--reason-code` | no | a snake_case identifier (e.g. `compile_verify_shape_expr_invalid`) |
| `--reason-detail` | no | free text |
| `--blocking-policy-scope` | no | `sandbox` / `verify` / `dependency` etc. |

**Calling conventions**

- A re-call with the same terminal value is an **idempotent operation** that tolerates at-least-once retry. A replay does not re-run the status-specific precondition (the preflight/completion verification of `pass`, the reason_code requirement of `fail_closed`) (the terminal-replay detection after lock acquisition happens before the status-specific verification). **On replay, when there is no canonical `set_status` event in phase_state_log.jsonl (the original forward call failed at the log append after committing meta + marker), it is backfilled from the persisted meta's reason_code / reason_detail / blocking_policy_scope before the replay completes.** The backfilled event is flagged `backfilled: true`:
  - `cleanup_committed/<orch_arid>.json` marker not written → cleanup retry (re-run only `_cleanup_agent_tmp_root` + marker write, narrative fields unchanged). `event=set_status_cleanup_retry` in `phase_state_log.jsonl`.
  - marker written (fully committed) → no-op replay (return the existing meta as-is). `event=set_status_noop_replay` in `phase_state_log.jsonl`. Guarantees that a defensive retry or a reissue after response loss does not become a `ValueError`.
- The narrative fields (`reason_code` / `reason_detail` / `blocking_policy_scope`) are fixed at the first `set-status`. A re-call does not overwrite the narrative. To append a narrative, directly edit `workspace/orchestrations/<orchestration_id>/failure_analysis.json` (already registered in the orchestration agent's `allowed_file_tool_paths`).
- The only permitted terminal-to-terminal transition is `fail` → `fail_closed` (the flow of finalizing fail_closed after a live preflight gate fail). Any other terminal-to-terminal transition is rejected with a `ValueError`.
- Assuming it is called from a concurrent terminalizer, the read-check-write-cleanup-marker critical section is serialized by an fcntl `LOCK_EX` on `orchestration_meta.json.lock` (POSIX environment). Because the `orchestration_meta.json` update of `write_preflight` and the update of `mark-dependency-readiness` share the same lock, the race in which a flag verified by `mark-dependency-readiness` is overwritten by a concurrent preflight does not occur.
- The canonical `set_status` event of `phase_state_log.jsonl` records the value **read back from `orchestration_meta.json` after commit** (after `.strip()` normalization, after the `fail → fail_closed` promotion, etc.). Because it audits the persisted state, not the raw call arguments, the forward write and the replay backfill have the same shape, and no divergence occurs in recovery / postmortem tooling.

---

## write-step-result

Generate `workspace/orchestrations/<orch>/steps/<node_key_safe>/<step>/<arid>/step_result.json` and run validation.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--node-key` | yes | |
| `--step` | yes | |
| `--agent-run-id` | yes | the primary agent that executed the step (the orchestration agent for substep-aware phases) |
| `--result-json` | yes | schema below |
| `--backfill` | no | recovery-only. Write a step_result for an already-terminal step agent that lacks one, bypassing the `child_finished` phase gate and **without** advancing the phase state. See below. |

### `--backfill` (recovery)

The normal write path requires the node/step phase to be exactly `child_finished` (a freshly-recorded terminal child) and advances it to `step_result_written`. That makes one step_result writable per live child. After a checkpoint resume resets the build phase out of `child_finished` (`resume_reset_stale_child_running`), a step agent whose step_result was never written becomes **stranded**: launching a new child to reach `child_finished` is itself a step agent that needs its own step_result, so `_validate_orchestration_completion_for_pass` can never be satisfied (each `run child → write one step_result` is net-zero). `--backfill` breaks this deadlock by writing the missing step_result directly.

Guards (all enforced; otherwise `RuntimeError`):
- the target `step_result.json` must **not** already exist (gap-fill only, never overwrites);
- `--agent-run-id` must be present and **terminal** in `agent_runs.jsonl`;
- the recorded run must be a `step` agent whose `node_key` / `step` match `--node-key` / `--step` (the result path is built from the supplied values, so an identity mismatch would write to the wrong directory and leave the real stranded step uncovered);
- the `--result-json` `status` must **equal** the recorded run status. This is the anti-fabrication guard: backfill can only mirror the authoritative recorded terminal status, never invent a better one. A recorded `pass` is backfillable too (a build child can record `pass` — with its outputs validated by `record-agent-run` — yet lose its `child_finished` before the result was written), so the only path that would otherwise be wedged is recoverable.

`validation_stage` is still required (e.g. `post_build` for a build fail) and `substep_agent_run_ids=[]` for build. Backfill does not advance the phase state and does not update the checkpoint. The recurrence root (a build agent leaving its phase without a step_result) is itself blocked at `record-launch` time: a build cannot relaunch while a prior terminal build step agent for the node still lacks its `step_result.json` (checked against the actual result files, so an interrupted phase transition does not wedge recovery).

### `--result-json` payload

| field | required | content |
|---|---|---|
| `status` | yes | `pass` / `fail` / `blocked` / `timeout` / `cancel` |
| `required_outputs` | yes | list[str] |
| `executor_agent_run_id` | yes | UUID |
| `substep_agent_run_ids` | yes | list[str]. A phase that has substeps includes the UUID of all substeps without omission |
| `failed_substeps` | optional | list[str] |
| `retry_decisions` | optional | list[object]. Each item: `{issue_severity, repair_strategy, repair_target_agent_run_id, new_agent_run_id, repair_reason}` |
| `validation_stage` | yes for a terminal status of compile/generate/build/validate | one of `compile` / `post_generate` / `post_build` / `post_execute` / `pre_judge` / `full` (with per-step allowed values) |

---

## reserve-phase-root

Reserve an `ir_id` or `pipeline_id`. It does not create the actual directory (the child agent creates it). Before the Compile phase launch, both `--step compile` (ir_id reservation) and `--step generate` (pipeline_id reservation) are needed.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--node-key` | yes | |
| `--step` | yes | reserve ir_id with `compile`, reserve pipeline_id with `generate` |
| `--reserved-id` | yes | `<slug>_<YYYYMMDD>_<seq3>` (an underscore in the slug is invalid; use a hyphen) |
| `--reserved-by-agent-run-id` | yes | the UUID of the agent that uses the reserved ID |

---

## workflow-launch-check

The pre-phase gate. Checks execution-platform availability, session policy, dependency readiness, and the required child-agent type. Run once before the first phase. When it returns `status=fail_closed`, stop with `set-status`.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--node-key` | yes | |
| `--step` | yes | `compile` / `generate` / `build` / `validate` (core 5-phase) |
| `--backend` | no | default `codex` |
| `--require-child-agent` | yes | `step` or `substep`. Compile / Generate / Validate are `substep`, Build is `step` |
| `--launch-request-json` | no | the launch request payload for the downstream artifact check |

Return value: `{"status": "pass"|"fail_closed", "next_action": "...", ...}`.

### Canonical fields of dependency_readiness (orchestration_meta.json)

Below are the canonical keys of the dependency readiness that `workflow-launch-check` references. The `preflight` subcommand writes them at orchestration_meta initialization, and `mark-dependency-readiness` (below) updates the flags after verification completes.

| key | step | content |
|---|---|---|
| `dependency_readiness.direct_dependency_compile_readiness` | `compile` | whether the immediate dependency node's `ir_ref` and `ir_meta.json.verification_status` are satisfied |
| `dependency_readiness.direct_dependency_execution_readiness` | `generate` / `build` / `validate` | whether the immediate dependency node's `ir_ref` / `pipeline_ref` / latest `aggregate_verdict` are satisfied |
| `dependency_readiness.detail.ir_ref_verified` | `compile` onward | |
| `dependency_readiness.detail.pipeline_ref_verified` | `generate` onward | |
| `dependency_readiness.detail.aggregate_verdict_verified` | `validate` | |

When not recorded or recorded with a value other than `true`, `workflow-launch-check` returns `fail_closed` (`reason_detail=direct_dependency_<step>_readiness_not_pass` or `dependency_readiness_detail_not_pass:<key>`). When the stored `dep_set_fingerprint` does not match the currently computed fingerprint, it is rejected with `reason_detail=dep_set_fingerprint_stale`. **Furthermore, when deps.yaml exists at launch time, the gate performs a live recompute and judges by the recomputed booleans, not the persisted booleans, as authoritative**: even if you directly edit `orchestration_meta.json` to forge the flag, if the live recompute returns false the gate rejects. When the live recompute fails, production returns a concrete reason **fail-closed**: `deps_yaml_missing_or_unparseable` (deps.yaml absent or YAML parse failure) and `deps_yaml_malformed_schema` (the schema is malformed) are distinguished (Codex round 25 F2). When test scaffolding needs the legacy fallback to persisted booleans, opt in with the environment variable `METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK=1` (do not set it in a production environment). PyYAML is lazily resolved only in the paths that parse deps.yaml / spec_catalog.yaml (`_read_deps_yaml`, `_load_spec_catalog_from_bytes`) (Codex round 27 F1). When not installed, a `RuntimeError` (with an install hint) propagates to the caller at the first YAML touch of these functions, preventing a silent fail-closed, while recovery / non-dep commands such as `set-status` / `record-timeout` / `workflow-launch-check` (leaf paths) can run even without PyYAML, avoiding a control-plane outage. The meta read + fingerprint recompute of `_dependency_ready` is serialized by an fcntl `LOCK_EX` on the same `orchestration_meta.json.lock` as `mark-dependency-readiness` / `write_preflight` / `update_orchestration_status` (Codex round 27 F2), so even if a writer and reader run simultaneously, a torn read will not falsely detect `dep_set_fingerprint_stale`. Reference: `skills/workflow-orchestration/SKILL.md` items 60-62.

**Initial-value computation rules** (`_compute_initial_dependency_readiness`):
- `deps.yaml` exists under `orchestration_meta.spec_ref` and both `dependencies.components` and `dependencies.profiles` are empty → all flags `true` (vacuous truth; for a leaf node)
- `spec_ref` unset, `deps.yaml` absent, or a non-empty dependency is listed → all flags **`false` (fail-closed)**. `workflow-launch-check` blocks the phase launch.
- For a node with a non-trivial dependency, after the orchestration agent verifies by the procedure of SKILL.md items 60-61, it explicitly raises the flags with `mark-dependency-readiness`.

**dep_set_fingerprint and invalidation conventions**:
- Each `dependency_readiness` records a `dep_set_fingerprint`. The fingerprint is a SHA-256 of the following:
  - `spec_ref` (normalized)
  - the bytes of `<spec_ref>/deps.yaml`
  - **a deps-relevant catalog subset**: only for the `(spec_kind, spec_id)` pairs appearing in deps.yaml, the bytes representing the catalog's sorted version list in deterministic JSON (not the full catalog bytes. Codex round 19 F1 — does not invalidate all orchestrations on an unrelated spec publication)
  - for each certified direct dep, the `(spec_kind, spec_id, spec_version, stage)` identifier + the latest workspace artifact bytes of that stage (`ir_meta.json` / `binary_meta.json` / `aggregate_verdict.json`). The identifier prefix is Codex round 19 F2 — it detects certified version drift even if the artifact bytes happen to match
- Check timing:
  - On `write_preflight` re-run: on fingerprint mismatch detection, reset `dependency_readiness` to the initial value (leaf=trivial-true / non-leaf=fail-closed) (recorded in `phase_state_log.jsonl` as `event=dependency_readiness_invalidated`).
  - `_dependency_ready` (launch-time gate): reject a fingerprint mismatch with `reason=dep_set_fingerprint_stale`. Detected immediately, without waiting for a preflight re-run.
- This prevents the gate from passing in all of the following drift scenarios: spec_ref replacement / deps.yaml edit / spec_catalog.yaml drift (adding a new matching version / removing an existing version / a constraint resolution becoming ambiguous) / **a post-mark dep artifact regression** (a new ir_meta.json / binary_meta.json / aggregate_verdict.json changing the verification_status or verdict).
- When the fingerprint matches:
  - leaf node (computed is trivial-true) → overwrite recompute (idempotent).
  - non-leaf node → preserve the existing value (guarantees that a flag raised by `mark-dependency-readiness` is not lost on a preflight re-run).
- When existing is unset → initialize by the initial-value rules above.

---

## mark-dependency-readiness

Recompute `orchestration_meta.dependency_readiness` from **artifact verification by the runtime**, and **overwrite all detail flags every time**. The CLI acts as a verification request, not a caller assertion. It parses `<spec_ref>/deps.yaml`, resolves each dep to `(spec_kind, spec_id, spec_version)` with `spec/registry/spec_catalog.yaml`, then actually inspects the workspace artifacts of all stages.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |

**Full-recompute every time (partial per-stage update is not allowed)**: overwrite all detail flags with the artifact-verification result every time. If partial updates were allowed, a `true` flag raised in some stage in the past would survive a subsequent dependency regression (a new artifact turning to fail, etc.), and `workflow-launch-check` would trust the stale persisted boolean and pass the gate.

**path-safety validation**: because each token of `spec_kind` / `spec_id` / `spec_version` is interpolated into the workspace path, a value containing characters outside the `[A-Za-z0-9._+-]` range, the `..` substring, or a path separator (`/`, `\\`) is not accepted. If an unsafe token appears in deps.yaml, all stages are fail-closed with well_formed=False, and an unsafe entry in spec_catalog.yaml is skipped at indexing time (resolve returns None for that id).

**within-mark consistency**: `mark-dependency-readiness` reads all artifact bytes **only once** in a single `_compute_dep_readiness_and_fingerprint` pass, and derives both the readiness booleans and the `dep_set_fingerprint` from that same snapshot. This closes the within-mark TOCTOU window in which the verification read and the fingerprint read observe a different byte state at different times. Because `_dependency_set_fingerprint` (for the gate) also uses the same walker, the same on-disk state always produces the same hash.

**build-variant ambiguity rejection**: when a range / inequality constraint (e.g. `>=1.0.0`) matches multiple catalog entries differing only in build metadata (`1.0.0+cpu`, `1.0.0+gpu`), `_matching_dep_versions` returns an empty tuple and is fail-closed. Because the workspace artifact root is keyed by the full version string, delegating the choice between `+cpu` and `+gpu` to a range constraint is an ambiguity. To pin a specific variant, use an exact-string constraint like `==1.0.0+cpu`.

**version resolution rules**: evaluate each dep's `(spec_kind, spec_id, version_constraint)` against all catalog versions of `spec/registry/spec_catalog.yaml`. The constraint supports AND-combined `>=`/`>`/`<=`/`<`/`==`/`!=` operators, and the version value accepts semver-style (`X.Y.Z[-prerelease][+build]`). Operator semantics:
- The ordering operators (`>`, `>=`, `<`, `<=`) use SemVer-numeric precedence (ignoring §11 prerelease and §10 build metadata).
- The equality operators (`==`, `!=`) are evaluated by an **exact match of the normalized string including build metadata**. Because the workspace artifact root is keyed by the full version string, this prevents `==1.0.0+cpu` from silently matching `1.0.0+gpu`.

**per-stage verification (same-version coherence + certified version pinning)**: rather than judging each stage independently, it requires a **cumulative chain against the same catalog version**. Furthermore, it selects **only one certified version** per dep:
- For each dep's matched catalog versions, compute the cumulative level (ir=1, ir+pipeline=2, ir+pipeline+verdict=3).
- Among the versions that achieved the maximum level, select the **highest version** as the certified version.
- The readiness flags of all stages are derived from the certified version's level.
- `dep_set_fingerprint` hashes only the certified version's artifact bytes.

This:
- prevents cross-version mixing (ir in version A, pipeline in version B)
- persists the per-dep canonical version (`certified_deps` field) in `meta.dependency_readiness`, so a downstream consumer can resolve with the same version
- prevents readiness from being erroneously invalidated by artifact churn of a non-certified version (e.g. a new version partially published / an old version cleaned up)
- a certified-version artifact regression is invalidated by a fingerprint mismatch

If no version matches the constraint, it is fail-closed. A range like `>=0.1.0 <1.0.0` remains launchable with the chain completed in the old version (0.1.0) even after a new version is published (e.g. 0.2.0) with its artifacts not yet prepared (0.1.0 is certified).

**Per-stage verification conditions** (evaluate **only the latest (by canonical id order) artifact** of each dep, stage true with ALL deps pass):

| stage | condition |
|---|---|
| `ir_ref` | the `verification_status == "pass"` of the **latest file** of `workspace/ir/<kind>__<id>__<version>/*/ir_meta.json` |
| `pipeline_ref` | under `workspace/pipelines/<kind>__<id>__<version>/`, select the "latest pipeline_id directory" (`_latest_pipeline_dir`), and the `verification_status == "pass"` of the latest `binary/*/binary_meta.json` in that pipeline |
| `aggregate_verdict` | within the same latest pipeline directory, take as candidates only the verdicts whose `trial_meta.json.source_binary_id` matches the **binary selected by pipeline_ref**, and that latest `aggregate_verdict ∈ {"pass", "xfail"}` (docs/GLOSSARY.md). If there is no verdict corresponding to that binary, fail-closed (Codex round 24: do not let the old binary's passing verdict be reused for the new binary) |

`pipeline_ref` and `aggregate_verdict` are bound to the **same pipeline_id** (Codex round 11 F2 + round 24). This prevents execution_readiness from erroneously passing on cross-pipeline mixing of "the new pipeline's binary is pass / the old pipeline's verdict is pass".

"Latest" is determined by the order of the **`(date, seq)` parsed from the canonical id suffix (`<slug>_<YYYYMMDD>_<seq3>`)** (not mtime, not raw path lex, not slug). A directory without a canonical suffix (e.g. a stray `zzz/`, a short form like `dep_a_001`, a version-mixed form like `dep_a_0.1.0_002`) is **filtered out by the selector** (Codex round 23 F2 / round 31 F2: align the reader's and writer's grammar in lock-step and prevent a gate bypass by a non-canonical name). When multiple canonical ids with the same `(date, seq)` (differing slugs) exist under the same parent directory, it is **explicitly fail-closed as a collision** (Codex round 35 F1: it does not allow a silent choice by slug-tiebreaker, emits `freshness_id_collision at (date=…, seq=…): …` to stderr, and `_select_max_by_id_extracted` returns None). The reason for not trusting mtime is that it is easily forged by touch / copy / restore / clock skew. Even if a historical past run's passing artifact remains, if the new-id run is fail the gate does not pass. If even one unresolvable dep (not registered in the catalog, or constraint-unresolvable) exists, all stages are fail-closed. The `dependencies` block of `deps.yaml` **strictly requires the canonical 2 keys `{components, profiles}`**: both keys exist explicitly in list form, and the absence of any unknown key (e.g. the singular typo `component:`, an extra section `extras:`) is a premise for leaf trivial-true. Each list item accepts **only dict form (`{component_id|profile_id, version_constraint?}`)**, and a bare string (`"dep_a"`, `"profile/foo"`, `"../dep_a"` etc.) is uniformly fail-closed as schema malformed (to prevent wrong-dep certification by silent normalization).

Derivation rules:
- `direct_dependency_compile_readiness = detail.ir_ref_verified`
- `direct_dependency_execution_readiness = detail.ir_ref_verified AND detail.pipeline_ref_verified AND detail.aggregate_verdict_verified`

`dep_set_fingerprint` (the SHA-256 of `spec_ref + deps.yaml`) is also refreshed every time. The write is serialized by an fcntl `LOCK_EX` on `orchestration_meta.json.lock`. `event=mark_dependency_readiness` with `verified` / `detail` is recorded in `phase_state_log.jsonl`.

**Behavior on verification failure**: when any of the following is detected, the runtime **overwrites `dependency_readiness` with a fail-closed payload before raising**, and records it in `phase_state_log.jsonl` as `event=mark_dependency_readiness_failed` with a `reason`. Via the CLI (`tools/orchestration_runtime.py mark-dependency-readiness`), it does not spit out a traceback but outputs the reason to stderr and returns exit 1 (Codex round 26 F2).
- `reason=deps_yaml_missing_or_unparseable`: `deps.yaml` absent, or YAML parse failure.
- `reason=deps_yaml_malformed_schema`: `deps.yaml` can be parsed but the schema is malformed (the level directly under `dependencies` is not an exact match of `{components, profiles}`, not a list type, a missing `*_id` in each entry, a non-string `version_constraint`, a path-traversal token, etc.).
- `reason=spec_catalog_corrupt` (Codex round 33 F2 + round 34 F2 + round 35 F2): `spec/registry/spec_catalog.yaml` is absent, unreadable, zero-byte, YAML parse failure, or its top-level schema is malformed (`specs:` list missing, not a `dict`, etc.). This is a repository-wide outage and is distinguished from an ordinary dependency miss. `mark-dependency-readiness`, even via the CLI, `print`s the `ValueError` and returns exit 1.

Additional reasons returned by the `_dependency_ready` path of `workflow-launch-check`:
- `reason=pyyaml_unavailable` (Codex round 28 F1): PyYAML not installed, so a live recompute is impossible. Only a leaf orchestration (`certified_deps == []` and the persisted byte-only fingerprint matches) is permitted to launch; otherwise fail-closed.
- `freshness_id_collision` (stderr output only, Codex round 35 F1): a canonical id collision with the same `(date, seq)`. Because `_select_max_by_id_extracted` returns `None`, on the gate it appears as `direct_dependency_<step>_readiness_not_pass` / `dependency_readiness_detail_not_pass:<key>`. To identify the cause, refer to the runtime's stderr (`freshness_id_collision at (date=…, seq=…): <colliding paths>`).

With the distinct reason design, observability tooling can distinguish a "spec-definition defect" from an "ordinary negative verification". It prevents an orchestration that was in a passing state before the error from remaining launchable in a subsequent `workflow-launch-check`.

**Design trust boundary**: merely calling the CLI cannot raise a flag. Rather than the caller passing a boolean, the runtime resolves the version_constraint and inspects the **workspace artifact selected by canonical id order (`(date, seq)` of `<slug>_<YYYYMMDD>_<seq3>`)** of the identified catalog version (Codex round 26 F1: because the catalog cache is also content-keyed, it is not affected by mtime forgery). If any of stale artifact / version mismatch / verdict=fail / constraint ambiguous is detected, the flag stays false. Furthermore, with full-overwrite every time, `dep_set_fingerprint` match confirmation (also performed at launch time), content-keyed invalidation of the catalog cache, immediate fail-closed persist on verification failure, and incorporating per-dep artifact bytes into the fingerprint, it prevents all of: (a) gate bypass by a CLI call, (b) unblocking a new launch with an old passing artifact, (c) adopting an artifact of a version different from the constraint, (d) a stale `true` remaining from a partial update, (e) a stale state remaining after spec_ref replacement / deps.yaml edit, (f) gate bypass by an out-of-band edit in the interval until a preflight re-run, (g) a passing state surviving a verification failure, (h) a stale `true` passing the gate due to a post-mark dep artifact regression, and (i) resolution drift in a long-lived process where the catalog cache does not reflect an in-process edit.

---

## run-gate

Run a validator gate across the capability_token. The canonical path in a context that forbids a direct validator call.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--gate` | yes | `validate_pipeline_semantics` / `check_artifact_syntax` / `validate_workspace_root` / `orchestration_read` |
| `--agent-run-id` | yes | the child agent's UUID |
| `--args-json` | yes | per-gate schema (below) |
| `--capability-token` | yes | `capabilities/<agent_run_id>.json#capability_token` |

### `--args-json` schema (per gate)

| gate | schema |
|---|---|
| `orchestration_read` | `{"read_path": "docs/..."}` |
| `validate_workspace_root` | `{"paths": ["workspace"]}` (optional, defaults to repo workspace) |
| `check_artifact_syntax` | `{"expect_top": "object", "paths": ["workspace/.../file.yaml", ...]}` |
| `validate_pipeline_semantics` | `{"stage": "compile|post_generate|post_build|post_execute|pre_judge|full", "ir_ref": "workspace/ir/..." (compile stage), "pipeline_root": "workspace/pipelines/..." or a list, "source_id": "<id>" (optional)}` |

The keys are converted into CLI flags (`pipeline_root` → `--pipeline-root`).

The gate result JSON (`status`, `violations`, ...) is output on the last line of stderr. Save and reference it with `2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` (`<agent_run_id>` is literally substituted).

---

## guarded-apply-patch

The only canonical write path for `.json` / `.txt` output. Applies a unified diff to a path enumerated in allowed_output_paths.

| arg | required | description |
|---|---|---|
| `--repo-root` | yes | |
| `--orchestration-id` | yes | |
| `--actor-role` | yes | `step` / `substep` / `orchestration` |
| `--agent-run-id` | yes | UUID |
| `--paths-json` | yes | JSON list of path strings (e.g. `'["workspace/ir/.../ir_meta.json"]'`) |
| `--patch-text` | one required | the unified diff body (inline) |
| `--patch-file` | one required | a path to a file containing the unified diff (required for a large patch to avoid the OS ARG_MAX) |
| `--capability-token` | yes | |

---
