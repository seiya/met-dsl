# Workflow Orchestration Startup Contract

## Purpose
- Finalize the required decisions before `workflow orchestration` launch with minimal tokens.

## Scope
- immediately after `orchestration agent` launch
- before the first launch of a child `agent`

## How to use the tmp area (required premise)

The `allowed_tmp_root` of the orchestration agent / child agent is fixed at `workspace/tmp/<agent_run_id>/`, and is recorded in the `allowed_tmp_root` field of `output_manifests/<agent_run_id>.json` at `record-launch` time. A temporary-file write passes `output_manifest_write_guard` by specifying **directly** under that literal path.

```
# orchestration agent
workspace/tmp/<orchestration_agent_run_id>/

# child agent
workspace/tmp/<agent_run_id>/
```

- `<orchestration_agent_run_id>` is `orchestration_meta.json#orchestration_agent_run_id`, and the child agent's `<agent_run_id>` is the value issued at `record-launch` time.
- The agent uses that literal path directly with `cat > workspace/tmp/<arid>/x.patch <<EOF` etc. A reference to the `$TMPDIR` env is allowed but not required (`output_manifest_write_guard` judges only the write-target path and does not reference the env, cf. the `allowed_tmp_root` branch of `tools/hooks/common.py:_validate_write_access`).
- **bootstrap Bash forbidden**: `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'`, and `env` (other than for read-only debug) must not be called. They are a cause of the workflow stopping repeatedly on Claude Code session-sandbox approval requests. The env (`METDSL_ORCHESTRATION_ID` / `ORCHESTRATION_AGENT_RUN_ID` / `TMPDIR`) is already inherited into the subprocess by `tools/run_workflow.py`.
- A direct write outside tmp (`workspace/<canonical>/...` etc.) requires going via `guarded-apply-patch`, and the `Edit`/`Write` tool is used only for paths registered in `allowed_file_tool_paths`. Writing directly to a canonical path with a Bash heredoc is blocked by `enforce_guarded_apply_patch`.

## Requirements
- Workflow launch must use `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` as the canonical entrypoint.
- The workflow mode is specified with `python3 tools/run_workflow.py ... --mode <dev|prod>`, and when unspecified, `dev` must be applied.
- In `dev` mode, relaxing the verify judgment is forbidden, and a fail-stop is required when `issue_severity=major|critical` is detected.
- When it fails in `dev` mode, save `workspace/orchestrations/<orchestration_id>/failure_analysis.json`, and recording the failure cause and basis reference is required. This file is written directly by the orchestration agent with the `Edit`/`Write` tool (`failure_analysis.json` is registered in `allowed_file_tool_paths`). `tools/run_workflow.py` writes to the same path as a safety-net only when the orchestration agent did not write it (it does not overwrite an existing file). When the orchestration agent already wrote it, it writes the runtime-collected data separately to `failure_analysis.runtime.<uuid12>.json` (e.g. `failure_analysis.runtime.3a7f9c2e14b0.json`). uuid12 is a 12-character hex unique per execution and prevents an overwrite collision on concurrent execution. The `analysis_ref` that `tools/run_workflow.py` returns always points to the current-run data (`failure_analysis.json` if the canonical is valid, the sidecar if stale/invalid). When going through a temporary file, always use a literal path under `allowed_tmp_root` (= `workspace/tmp/<orchestration_agent_run_id>/`), and hard-coding `/tmp/` is forbidden.
- The canonical implementation of the pre-launch confirmation is the combination of `tools/run_workflow.py` and `tools/orchestration_runtime.py`, and the backend specification of `preflight` must be done through `tools/run_workflow.py --llm`.
- The canonical source for the requirement definition and judgment rules passed to a child `agent` is limited to `docs/`, `spec/`, and the relevant trial's artifacts, and the implementation under `tools/`, verification `script`, test code, and validator code must not be referenced as a rule source.
- The validator invocation defaults to `run-gate`, and when direct execution is permitted, it is limited to a read-only check and a gate-independent check. The permitted targets are only `validate_workspace_root.py` and `check_artifact_syntax.py`, and a direct execution of any other validator is forbidden.
- `init` and `preflight` must each be run at least once.
- When `preflight.json` does not satisfy `status=pass` and `can_launch_step_agents=true` and `can_launch_substep_agents=true`, a child `agent` must not be launched.
- **How to inspect `preflight.json` / `orchestration_meta.json` status fields:** use the **`Read` tool** (or `python3 tools/orchestration_runtime.py preflight-status` for a structured summary, or `python3 tools/audit_orchestration.py`). Do **not** use `python3 -c "import json; ..."` — it is blocked fail-closed by `forbid_python_inline_write` (the regex filter cannot distinguish a read from a write). `jq`, `printenv`, `bash -c`, and other bootstrap Bash are likewise forbidden (session-sandbox approval cause; see the "bootstrap Bash forbidden" item below). A `Read` is the canonical path for inspecting these files.
- On the Claude backend, the inclusion of `claude_mcp_build_runtime_registered: pass=true` **and** `claude_mcp_build_runtime_permission_granted: pass=true` in `preflight.json#checks` is the judgment target (the AND of server registration ∧ tool-permission grant). `probe_execution_platform` has already AND-evaluated it, and there is no need for the orchestration agent side to re-run `claude mcp list`. When either is `pass=false`, it has already stopped with `status=fail`, so that branch is never reached (it is always detected before the launch of a Generate/Build/Validate child agent). The remediation when permission is not granted is to add `mcp__build-runtime` to the `permissions.allow` of `.claude/settings.json` (for details, the preflight section of [CLAUDE.md](../../../CLAUDE.md)).
- Before starting a phase, whether the target phase requires a `substep agent` or a `step agent` must be confirmed by a fixed table. `Compile` / `Generate` / `Validate` are `substep agent`, and `Build` is `step agent`.
- In the first `commentary`, the target phase, the `SKILL` to use, the kind of `agent` to launch, and the place that uses `MCP` must be declared as an execution declaration.
- Before launching the child `agent` of `Compile`, it must be confirmed that the immediate dependency `node` of the target `node` satisfies `direct dependency compile readiness`.
- Before launching the child `agent` of `Generate` onward, it must be confirmed that the immediate dependency `node` of the target `node` satisfies `direct dependency execution readiness`.
- The live check just before launching a child `agent` is required only at `record-launch` time.
- `record-agent-run` and `write-step-result` may be run when the consistency confirmation of `preflight.json` is satisfied.
- The launch-request body must be saved to `launches/<agent_run_id>.prompt.txt`, and the launch-reply body to `launches/<agent_run_id>.reply.txt`.
- Before directly editing a phase artifact or running `MCP`, preflight-done, launch-prompt-prepared, and child-`agent`-launched must be satisfied.
- Even for a provisional implementation aimed at workflow validity confirmation, verification, or a connectivity check, the parent `agent` must not proxy the body processing of a child-`agent`-required phase.

## Operations Rules
1. Run `tools/run_workflow.py` to perform the initialization of `workspace/orchestrations/<orchestration_id>/` and the generation of `preflight.json`.
2. The workflow must not be started by a path other than `tools/run_workflow.py`.
3. An orchestration agent launched with `METDSL_WORKFLOW_MODE=1` must not read the `memory/` directory (`MEMORY.md` etc.) under `~/.claude/projects/`. Because workflow execution proceeds deterministically, it does not reference persistent state outside the conversation. When the following **Claude Code auto-read files** are auto-read immediately after startup, they are classified benign with `audit_detail.policy=auto_read_expected_block`, but **this is expected behavior** and does not affect the continuation of the workflow. Do not treat it as an error, and do not attempt a retry or an additional reference to these files.

   The permitted targets are divided into 2 blocks: **(A) harness-forced auto-read (applies to all agent roles)** and **(B) permitted only for the orchestration agent**. The implementation corresponds to `_HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS` / `_HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES` and `_AUTO_READ_TOLERATED_REPO_RELPATHS` of `tools/hooks/common.py`.

   **(A) harness-forced auto-read (applies to all agent roles)**

   The group of files the Claude Code harness Reads immediately after startup regardless of the agent role. It is treated as benign on any of `orchestration agent` / `step agent` / `substep agent`. It is the harness's behavior, and an agent actively Reading these is forbidden.
   - `.claude/settings.json`
   - `.cursor/mcp.json` (Claude Code's MCP discovery auto-reads it immediately after startup)
   - `mcp_servers/README.md` (same as above)
   - `mcp_servers/mcp_servers.example.json` (same as above)
   - all files under `mcp_servers/tools/` (the auto-discovery of MCP tool definitions. The implementation prefix-tolerates, and the harness reads only `*.json`)

   **(B) permitted only for the orchestration agent**

   The path by which the Claude Code harness reads project state at `orchestration agent` startup. It does not apply to a `substep agent` (because the substep's harness does not re-read project state).
   - `~/.claude/projects/.../memory/MEMORY.md`
   - `README.md` (project root)
   - `TODO.md` (project root)
   - `CLAUDE.md` (project root)
   - `MEMORY.md` directly under the project root

   **A substep agent must not Read a block (B) file** (for the substep it is a normal error and `read_manifest_read_guard` fires). Block (A) is permitted only via the harness, and actively Reading it from the agent prompt is forbidden for all roles.
4. When the `preflight` judgment is not `pass`, run `set-status --status fail` to stop.
5. In the first `commentary`, declare the target phase, the `SKILL` to use, the kind of `agent` to launch, and the place that uses `MCP`.
6. Confirm the phase type by a fixed table, and finalize the launch target as a `substep agent` for `Compile` / `Generate` / `Validate` and a `step agent` for `Build`.
7. Before launching the child `agent` of `Compile`, confirm the `ir_ref` and `ir_meta.json.verification_status` of the immediate dependency `node`.
8. Before launching the child `agent` of `Generate` onward, confirm the `ir_ref`, `pipeline_ref`, and `aggregate_verdict` of the immediate dependency `node`.
9. Do not start phase-artifact editing and `MCP` execution until preflight-done, launch-prompt-prepared, and child-`agent`-launched are satisfied.
10. When launching a child `agent`, run `record-launch`.
11. After the child `agent` completes, append `record-agent-run`.
12. After the phase completes, record `write-step-result`.
13. When about to deviate into a contract-violating shortcut, make explicit that a child-`agent` launch is required and return to the launch procedure.

## Decision Criteria
- `preflight.json` exists and satisfies the `pass` conditions.
- An execution declaration exists in the first `commentary`.
- The phase type and the kind of `agent` launched match the fixed table.
- The reference consistency of `launches/`, `agent_runs.jsonl`, and `step_result.json` holds.
- On a child-`agent` launch failure, `set-status --status fail` is recorded.

## node_key / ID format quick reference

### Composition of `node_key`

```
<spec_kind>/<spec_id>@<spec_version>
```

- `spec_kind` / `spec_id` are obtained from the same-named fields of the target `deps.yaml`.
- `spec_version` is obtained from the `spec_version` field of the target `controlled_spec.md`.
- It is distinct from a **filesystem path** (`spec/component/dynamics/...`). Always pass this form to `--node-key` of `workflow-launch-check` / `record-launch` / `reserve-phase-root` etc.

Example:
```
deps.yaml    → spec_kind: component, spec_id: dynamics_shallow_water_flux_2d_rusanov_p0
controlled_spec.md → spec_version: 0.1.0
node_key     → component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0
node_key_safe → component__dynamics_shallow_water_flux_2d_rusanov_p0__0.1.0
```

### Naming rule of `ir_id` / `pipeline_id`

Format: `<slug>_<YYYYMMDD>_<seq3>`

- `slug` is **hyphen-separated** lowercase letters and digits (underscore not allowed).
- Regex: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`
- Example: `flux-rsn-p0_20260425_001` ✓  `flux_rsn_p0_20260425_001` ✗

Pass this form to `--reserved-id` of `reserve-phase-root`.

> **`run_id` is an exception:** the above `<slug>_<YYYYMMDD>_<seq3>` form is exclusive to `ir_id` / `pipeline_id`. The `run_id` of `Validate` is the `run_<YYYYMMDD>_<seq3>` form with a **fixed literal `run_` prefix** (e.g. `run_20260605_001` ✓), and the slug form must not be reused (`run-rsn-p0_20260605_001` ✗). A hyphen-slug form happens to match the generic slug regex, but the Validate phase contract of `record-launch` rejects it with `outside phase contract`, and even if it passed, the run discovery of `post_execute` would silently fail.

### Form of `ir_ref` / `pipeline_ref`

```
workspace/ir/<node_key_safe>/<ir_id>
workspace/pipelines/<node_key_safe>/<pipeline_id>
```

- Even in a **Compile substep**, `pipeline_ref` is required. The pipeline does not exist yet, but reserve the pipeline_id in advance with `reserve-phase-root --step generate`, and specify it in the `workspace/pipelines/<node_key_safe>/<pipeline_id>` form.
- `pipeline_ref="none"` or an empty string cannot be passed to `--request-json` of `record-launch`.

### Acquisition of orchestration_agent_run_id

The orchestration agent's own `agent_run_id` uses the `orchestration_agent_run_id` field of the startup context as the canonical source.

- It is generated by `tools/run_workflow.py` via `init_orchestration()` and is already recorded in `orchestration_meta.json`.
- The orchestration agent must not generate it on its own with `uuid.uuid4()` etc.
- The `running` initial entry of `record-agent-run` (orchestration role) is auto-inserted by `init_orchestration()`, so the orchestration agent need not call it manually.
- The terminal entry (`pass` / `fail` / `fail_closed`) of `record-agent-run` (orchestration role) is called by the orchestration agent after running `set-status`.

## `record-launch` procedure (Claude Code backend)

Because Claude Code has no `spawn_agent`, run in the following order.

```
1. Generate an agent_run_id (UUID)
   - Canonical path: run `python3 tools/new_agent_run_id.py` bare, and embed the UUID printed to Bash output into subsequent commands as a literal string.
   - Do **not** use the 2-step shell var assignment form `CHILD_ARID=$(python3 tools/new_agent_run_id.py)` — the leading `CHILD_ARID=` breaks the `Bash(python3 tools/new_agent_run_id.py)` allowlist match and requires session approval.
   - Do not use `cat /proc/sys/kernel/random/uuid` / `uuidgen` because they stop on session-sandbox approval requests every time.
   - `python3 -c 'import uuid; …'` is blocked by `forbid_python_inline_write`.
2. Reserve ir_id / pipeline_id in advance with reserve-phase-root (if not yet done)
   - Compile phase only: 2 runs of `--step compile` (ir_id reservation) and `--step generate` (pipeline_id reservation) are needed. Other phases only once.
   - In both, specify the same ID (e.g. `flux-rsn-p0_20260428_001`) for `--reserved-id`.
3. Call record-launch (before launching the Agent tool)
   - request-json: a JSON including the launch parameters node_key / step / substep / ir_ref / pipeline_ref / dependency_ref /
                   skill_name / skill_ref etc.
   - response-json: {"agent_session_id": "<agent_run_id>",
                     "agent_run_id": "<agent_run_id>",
                     "started_at": "<ISO8601>",
                     "backend": "claude"}
   → capability_token / sandbox_profile / output manifest / read manifest are generated
4. Launch the Agent tool (the child agent reads the capability_token from
   capabilities/<agent_run_id>.json and runs guarded-apply-patch etc.)
5. Receive the Agent tool's return value (the final response text)
5.4. Leave the evidence of observing the Agent tool return with record-child-return (the Adv-20/Adv-30 guards require it)
     - Pass the return-token with `$(cat ...)` as an inline argument. Do not use the 2-step form
       `VAR=$(cat ...)` (the leading `VAR=` breaks the `Bash(python3 ...)` allowlist match and requires session
       approval).
     python3 tools/orchestration_runtime.py record-child-return \
       --repo-root <repo_root> \
       --orchestration-id <orchestration_id> \
       --agent-run-id <agent_run_id> \
       --return-token "$(cat <repo_root>/workspace/orchestrations/<orchestration_id>/launches/<agent_run_id>.parent_return_token)"
5.5. Run deactivate-child to return the active context to the orchestration agent
     (it is rejected with a ValueError unless the record-child-return above has completed)
     python3 tools/orchestration_runtime.py deactivate-child \
       --repo-root <repo_root> \
       --orchestration-id <orchestration_id> \
       --child-run-id <agent_run_id>
6. Save the response text to launches/<agent_run_id>.reply.txt with record-reply
   python3 tools/orchestration_runtime.py record-reply \
     --repo-root <repo_root> \
     --orchestration-id <orchestration_id> \
     --agent-run-id <agent_run_id> \
     --reply-text "<the Agent tool's return value>"
7. Run record-agent-run to append to agent_runs.jsonl
```

The reason for calling `record-launch` before the Agent tool: so that the child agent can reference the capability_token and output manifest before it starts execution.

## `record-launch` command template (Claude Code backend)

When building the command, copy the template below as-is and fill in the values. The 4 enumerated in this template are frequent subcommands, and the complete specification of the payload schema uses `docs/CLI_REFERENCE.md` (Tier-A) as the canonical source. The rare subcommands (e.g. `init` / `preflight` / `record-timeout` / `read-checkpoint` etc.) use `python3 tools/orchestration_runtime.py <sub> --help` as the canonical source, with an overview in `docs/CLI_REFERENCE_RARE.md`. For the per-tool usage, see the "CLI reference conventions" section of `CLAUDE.md`.

**Handling of started_at**: do **not** use the 2-step shell var assignment `STARTED_AT=$(date ...)` (the leading `STARTED_AT=` breaks the `Bash(python3 ...)` allowlist match and requires session approval). For the `started_at` value of `--response-json`, embed `$(date -u +"%Y-%m-%dT%H:%M:%SZ")` as an **inline** command substitution. Because the whole command starts with `python3 tools/orchestration_runtime.py …`, it matches the `Bash(python3 tools/orchestration_runtime.py *)` allowlist. `date -u *` has been added to the allowlist as reinforcement (`.claude/settings.json`).

```bash
python3 tools/orchestration_runtime.py record-launch \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --parent-agent-run-id <orchestration_agent_run_id> \
  --child-agent-run-id <agent_run_id> \
  --request-json '{
    "agent_role": "<substep|step>",
    "node_key": "<node_key>",
    "step": "<step>",
    "substep": "<substep_or_omit_for_step_agent>",
    "orchestration_id": "<orchestration_id>",
    "agent_run_id": "<agent_run_id>",
    "parent_agent_run_id": "<orchestration_agent_run_id>",
    "agent_model": "<the child agent's LLM model id, e.g. claude-opus-4-8>",
    "workflow_mode": "<dev|prod>",
    "ir_ref": "<ir_ref>",
    "pipeline_ref": "<pipeline_ref>",
    "dependency_ref": "<dependency_ref>",
    "skill_name": "<skill_name>",
    "skill_ref": "<skill_ref>",
    "allowed_output_paths": ["<list of output file paths>"]
  }' \
  --response-json "{
    \"agent_run_id\": \"<agent_run_id>\",
    \"agent_session_id\": \"<agent_run_id>\",
    \"started_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
    \"backend\": \"claude\"
  }"
```

- `--parent-agent-run-id` and `--child-agent-run-id` are each independent positional arguments, specified separately from the inner fields of `--request-json`.
- `--response-json` is a required independent argument. It must not be included inside `--request-json`.
- `allowed_output_paths` is a required field for a step/substep agent. Omitting it fails with exit code 1.
- The `substep` field is omitted for a step agent (without a substep).

## Minimal required fields of `record-launch --request-json`

| field | description |
|---|---|
| `agent_role` | `"substep"` or `"step"` |
| `node_key` | the `<spec_kind>/<spec_id>@<spec_version>` form |
| `step` | `"plan"` / `"generate"` / `"build"` / `"execute"` / `"judge"` etc. |
| `substep` | `"generate"` / `"verify"` (for a substep agent) |
| `orchestration_id` | the orchestration ID |
| `agent_run_id` | the child agent's UUID |
| `parent_agent_run_id` | the parent (orchestration) agent's UUID |
| `agent_model` | the LLM model id that runs the child agent (e.g. `"claude-opus-4-8"`). Required at launch because it cannot be derived by the runtime. `record-agent-run` auto-copies it to the step/substep entry of `agent_runs.jsonl` (`pre_judge` requires it for both roles) |
| `workflow_mode` | `"dev"` or `"prod"` |
| `ir_ref` | the `workspace/ir/<node_key_safe>/<ir_id>` form |
| `pipeline_ref` | the `workspace/pipelines/<node_key_safe>/<pipeline_id>` form (required even in the Compile phase) |
| `dependency_ref` | the per-phase canonical path. `Compile` is `spec/.../deps.yaml`, and from `Generate` onward the phase root of `workspace/...` (`ir_ref` or `pipeline_ref`) |
| `skill_name` | naming rule: `workflow-{step}-{substep}` (e.g. `"workflow-compile-generate"`) |
| `skill_ref` | naming rule: `skills/{skill_name}/SKILL.md` (e.g. `"skills/workflow-compile-generate/SKILL.md"`) |
| `skill_must_read_refs` | must not be derived by reading the child `SKILL.md`. Use the "`Compile verify` launch request" and "`Generate verify` launch request" items of the orchestration `SKILL.md` as the canonical source |
| `allowed_output_paths` | the list of all output paths the child agent can write. Required for step/substep. `guarded-apply-patch` and the `apply_patch_writes` gate reference this list |

## Minimal required fields of `record-agent-run --agent-run-json`

| field | description | note |
|---|---|---|
| `agent_run_id` | UUID | |
| `agent_role` | `"orchestration"` / `"step"` / `"substep"` | |
| `agent_backend` | `"claude"` / `"codex"` / `"cursor"` | |
| `status` | `"running"` / `"pass"` / `"fail"` etc. | |
| `started_at` | ISO8601 | |
| `agent_session_id` | required for step/substep. In Claude Code the same value as agent_run_id | |
| `context_id` | required for step/substep | |
| `context_isolated` | required for step/substep (always `true`) | |
| `node_key` | required for step/substep | |
| `output_refs` | required at a pass termination | |
| `parent_agent_run_id` | auto-copied (no payload needed) | `record-agent-run` completes it from `launches/<arid>.request.json`. `pre_judge` requires it for a step/substep entry, but it need not be written in the payload because it already exists in the launch request |
| `agent_model` | auto-copied (no payload needed) | same as above. completed from the `agent_model` specified at record-launch time |

## `reserve-phase-root` command template (Claude Code backend)

When building the command, copy the template below as-is and fill in the values. The 4 enumerated in this template are frequent subcommands, and the complete specification of the payload schema uses `docs/CLI_REFERENCE.md` (Tier-A) as the canonical source. The rare subcommands (e.g. `init` / `preflight` / `record-timeout` / `read-checkpoint` etc.) use `python3 tools/orchestration_runtime.py <sub> --help` as the canonical source, with an overview in `docs/CLI_REFERENCE_RARE.md`. For the per-tool usage, see the "CLI reference conventions" section of `CLAUDE.md`.

```bash
python3 tools/orchestration_runtime.py reserve-phase-root \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --node-key <node_key> \
  --step <plan|generate> \
  --reserved-id <reserved_id> \
  --reserved-by-agent-run-id <agent_run_id>
```

- Reserve ir_id with `--step compile` and pipeline_id with `--step generate`. The Compile phase runs both once each, and specifies the **same ID** (e.g. `flux-rsn-p0_20260509_001`) for `--reserved-id`. Other phases run only `--step generate` once.
- `--reserved-id` is the `<slug>_<YYYYMMDD>_<seq3>` form (slug is hyphen-separated lowercase alphanumeric. An underscore must not be included in the slug).
- `--reserved-by-agent-run-id` is the UUID of the child agent that actually uses that ID.

## `record-agent-run` command template (Claude Code backend)

When building the command, copy the template below as-is and fill in the values. The 4 enumerated in this template are frequent subcommands, and the complete specification of the payload schema uses `docs/CLI_REFERENCE.md` (Tier-A) as the canonical source. The rare subcommands (e.g. `init` / `preflight` / `record-timeout` / `read-checkpoint` etc.) use `python3 tools/orchestration_runtime.py <sub> --help` as the canonical source, with an overview in `docs/CLI_REFERENCE_RARE.md`. For the per-tool usage, see the "CLI reference conventions" section of `CLAUDE.md`.

```bash
python3 tools/orchestration_runtime.py record-agent-run \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --agent-run-json '{
    "agent_run_id": "<agent_run_id>",
    "agent_role": "<orchestration|step|substep>",
    "agent_backend": "claude",
    "status": "<running|pass|fail|fail_closed|blocked|timeout|cancel>",
    "started_at": "<ISO8601>",
    "finished_at": "<ISO8601>",
    "agent_session_id": "<agent_run_id>",
    "context_id": "<agent_run_id>",
    "context_isolated": true,
    "node_key": "<node_key>",
    "step": "<step>",
    "substep": "<substep_or_omit_for_step_agent>",
    "output_refs": ["<path>", ...]
  }'
```

- For a terminal status (`pass` / `fail` / `fail_closed` / `blocked` / `timeout` / `cancel`), `finished_at` is required.
- For a `pass` termination, `output_refs` is required. **List concrete file paths only — a directory entry (e.g. `.../src/`, `raw/`, `raw/state_snapshots/`) is rejected with `allowed_output_paths manifest violation`. Enumerate each file** (`.../src/<name>.f90`, `.../src/Makefile`, `.../src/mcp_command_log.jsonl`, `raw/state_snapshots/<case_id>.json`, ...).
- For a step/substep role, `agent_session_id` / `context_id` / `context_isolated` / `node_key` are required. In Claude Code, `agent_session_id` and `context_id` are recorded with the same value as `agent_run_id`.
- The `running` initial entry of the orchestration role is auto-inserted by `init_orchestration()`, so it is not called from the orchestration agent. Only the terminal entry is appended by the orchestration agent after running `set-status`.

## `set-status` command template (Claude Code backend)

When building the command, copy the template below as-is and fill in the values. The 4 enumerated in this template are frequent subcommands, and the complete specification of the payload schema uses `docs/CLI_REFERENCE.md` (Tier-A) as the canonical source. The rare subcommands (e.g. `init` / `preflight` / `record-timeout` / `read-checkpoint` etc.) use `python3 tools/orchestration_runtime.py <sub> --help` as the canonical source, with an overview in `docs/CLI_REFERENCE_RARE.md`. For the per-tool usage, see the "CLI reference conventions" section of `CLAUDE.md`.

```bash
python3 tools/orchestration_runtime.py set-status \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --status <running|pass|fail|fail_closed> \
  --reason-code <reason_code_or_omit> \
  --reason-detail <reason_detail_or_omit> \
  --blocking-policy-scope <scope_or_omit>
```

- `--reason-code` / `--reason-detail` / `--blocking-policy-scope` are needed for `fail` / `fail_closed`. They can be omitted for `pass`.
- For an optional flag to omit, **drop the flag entirely** (do not pass an empty string or the string `omit` as a value). Example: for `pass`, delete the entire `--reason-code` line.
- The orchestration agent appends the orchestration-role terminal entry with `record-agent-run` after running this command.

## Supplement per execution platform

For the correspondence between the child-`agent` launch tool and the `preflight` arguments per execution platform, refer to "child `agent` launch tool per execution platform" of `CLAUDE.md`.
