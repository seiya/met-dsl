# CLAUDE.md

This file defines the project-specific conventions for Claude Code. For general conventions such as writing-style rules, terminology rules, document reference rules, and MCP execution rules, [AGENTS.md](AGENTS.md) is the canonical source.

## workflow execution
- The core workflow is a 5-phase structure: `Spec → Compile → Generate → Build → Validate`. The entry point to the specification is [docs/WORKFLOW.md](docs/WORKFLOW.md).
- [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) is the canonical source for the hierarchical execution contract between the `orchestration agent` and the `step agent` / `substep agent`.
- For the startup procedure of the `orchestration agent`, refer to [skills/workflow-orchestration/SKILL.md](skills/workflow-orchestration/SKILL.md).
- For the minimal pre-startup checks, refer to [skills/workflow-orchestration/references/startup_contract.md](skills/workflow-orchestration/references/startup_contract.md).
- The canonical entrypoint for starting the workflow is `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]`. `<until_phase>` specifies one of `compile` / `generate` / `build` / `validate`.
- The canonical path for resuming a workflow that failed midway is `python3 tools/run_workflow.py --resume [--orchestration-id <id>]`. With `--resume`, `spec_ref` / `until_phase` / `--llm` / `--mode` can be omitted and are restored from the target orchestration's existing artifacts (`orchestration_meta.json` / `preflight.json` / `launches/orchestration.start.prompt.txt`) (an explicit argument takes precedence; when `--orchestration-id` is omitted, the most recent one in chronological order is targeted). Internally it runs `init --resume-from-checkpoint` (`resume_enabled=true`, resetting a terminal status to `running`), and together runs `repair-agent-runs` to backfill the `parent_agent_run_id` / `agent_model` missing from pre-`caa10ab` legacy rows of `agent_runs.jsonl` from the authoritative source (if `agent_model` cannot be auto-derived it becomes `needs_manual`, and the operator runs `repair-agent-runs --agent-model <id>` manually). For details, refer to [docs/RUNBOOK.md](docs/RUNBOOK.md) §3-1.
- During workflow execution, the canonical source for `METDSL_WORKFLOW_MODE=1` and `METDSL_ORCHESTRATION_ID=<orchestration_id>` is the values set by `tools/run_workflow.py`.
- The optional flows `Tune` (implementation-discretion variant exploration) and `Promote` (promotion to the official version) are started from an entrypoint separate from the core workflow (details defined in a separate plan).

## child `agent` launch tool per execution platform

| execution platform | `--backend` argument of `preflight` | child `agent` launch tool | how to obtain `agent_session_id` |
|---|---|---|---|
| Codex | `codex` | `spawn_agent` | obtained from the actual `spawn_agent` response |
| Cursor | `cursor` | `spawn_agent` | obtained from the actual `spawn_agent` response |
| Claude Code | `claude` | `Agent` tool | use the `agent_run_id` issued before launch as the `agent_session_id` |

## hook implementation policy
- `tools/hooks/common.py` is the canonical source for backend-independent validation.
- Backend-specific invocation specifications are absorbed by the adapters under `tools/hooks/adapters/`.
- `.codex/hooks.json` is the canonical source for `Codex` hook invocation definitions.
- The `hooks` section of `.claude/settings.json` is the canonical source for `Claude Code` hook invocation definitions. It wires the 4 events `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop`.
- The `Claude Code` backend does not need a feature flag probe, and the `hooks` requirement check is limited to the Codex backend. The common policy follows `evaluate_common_policy()` in `tools/hooks/common.py`.
- The `matcher` in `.claude/settings.json` is an **exact-match string** (not a regular expression). Unlike `^Bash$` in `.codex/hooks.json`, write `"Bash"`.

## Claude Code-specific execution conventions

### preflight
- When running `preflight`, specify `--backend claude`.
- Command example: `python3 tools/run_workflow.py <spec_ref> <until_phase> --llm claude`
- The `preflight` of the Claude backend requires that the **`.claude/settings.json` committed to the repository** include `build-runtime` in `enabledMcpjsonServers` (or that `enableAllProjectMcpServers=true` and `.mcp.json` define `build-runtime`) (providing `run_linter` / `compile_project` / `run_program` / `run_quality_checks` / `detect_build_system`). The decision is made over the set `(.claude/settings.json: enabledMcpjsonServers ∪ enableAll expansion) − (.claude/settings.json: disabledMcpjsonServers) − (.claude/settings.local.json: disabledMcpjsonServers)`. **`~/.claude.json` (per-user / per-machine trust history) is intentionally not referenced** — because it would cause preflight results to vary per machine, enablement is declared via repo commit to ensure reproducibility (only the disable in `.claude/settings.local.json` is subtracted as a personal opt-out; since disables via `~/.claude.json` are not seen, this is a deliberate trade-off that can false-pass). Because `claude mcp list` skips the workspace trust dialog and spawns the stdio server, even a `✓ Connected` does not guarantee that the session exposes the tool (a false-positive source), so it is not used for the preflight gate and is kept as advisory display only. When not enabled, `claude_mcp_build_runtime_registered` in `preflight.json#checks` becomes `pass=false` and stops with `status=fail`. Remediation is to add `"enabledMcpjsonServers": ["build-runtime"]` at the top level of the committed `.claude/settings.json`, and confirm there is no disable of `build-runtime` in `.claude/settings.local.json`.
- The `preflight` of the Claude backend also requires that, **in addition to server registration, the MCP tool be permission-granted to the child `Agent` session**. Even if registered (enabled), without tool-invocation permission the child agent cannot call `run_linter` etc. and gets blocked with `Claude requested permissions … but you haven't granted it yet.` (stopping Generate/Build/Validate entirely). The decision is made by `claude_mcp_build_runtime_permission_granted` in `preflight.json#checks`, and its **AND** with `claude_mcp_build_runtime_registered` becomes the launch gate (`can_launch_*` / `status`). The granted condition is one of: (a) the committed `.claude/settings.json` `permissions.allow` has the server-level grant `mcp__build-runtime` (and not in deny), (b) the required 4 tools `mcp__build-runtime__run_linter` / `__compile_project` / `__run_program` / `__run_quality_checks` are individually allowed (and none in deny), (c) `permissions.defaultMode == "bypassPermissions"`. The `permissions.allow` of `.claude/settings.local.json` is also combined, and `permissions.deny` is subtracted. **Claude Code's permission rule does not interpret a wildcard in the MCP tool name part (`mcp__build-runtime__*`)**, so to allow all tools use the server-level `mcp__build-runtime`. Remediation is to add `"mcp__build-runtime"` to the `permissions.allow` of the committed `.claude/settings.json`. A restart of the Claude Code session may be required for the permission to take effect.

### child `agent` launch
- In Claude Code, use the `Agent` tool instead of `spawn_agent` to launch a child `agent`.
- For the `prompt` argument of the `Agent` tool, apply the corresponding template in [skills/workflow-orchestration/references/launch_prompts.md](skills/workflow-orchestration/references/launch_prompts.md).
- The `subagent_type` of the `Agent` tool defaults to `general-purpose`; select an appropriate value according to the phase being launched.
- `context_isolated=true` indicates that the Claude Code `Agent` tool runs in an isolated context, and is always recorded as `true`.

### Execution order of `record-launch` in Claude Code

In Claude Code, call `record-launch` **before the Agent tool**. Unlike Codex's `spawn_agent`, in order to call the `Agent` tool synchronously, the `capability_token` and `output_manifest` that the child agent references during execution must be generated in advance.

```
Steps:
1. Issue an agent_run_id (UUID)
   - Canonical path: run `python3 tools/new_agent_run_id.py` bare, and embed the UUID printed to Bash stdout into subsequent commands as a literal string.
   - Do **not** use the 2-step shell var assignment form `CHILD_ARID=$(python3 tools/new_agent_run_id.py)` — the leading `CHILD_ARID=` breaks the `Bash(python3 tools/new_agent_run_id.py)` allowlist match and causes the session sandbox to request approval every time.
   - Do not use `cat /proc/sys/kernel/random/uuid` / `uuidgen` because they stop on session-sandbox approval requests every time. `python3 -c 'import uuid; …'` is blocked by `forbid_python_inline_write`.
2. Reserve ir_id / pipeline_id with reserve-phase-root (if not yet reserved)
3. Run record-launch (before launching the Agent tool)
   → capability_token / sandbox_profile / output manifest / read manifest are generated
   → launches/<agent_run_id>.reply.txt is written with provisional content
4. Launch the Agent tool (the child agent reads the capability_token from
   capabilities/<agent_run_id>.json and runs guarded-apply-patch etc.)
5. Receive the Agent tool's return value (the final response text)
6. Run record-child-return to leave evidence of observing the Agent tool return (child_returns/<agent_run_id>.txt)
   → required argument: `--return-token "<literal token>"` (Adv-30: a parent-bound token to prevent forgery by an arbitrary caller; auto-generated by record-launch)
   → pass the token via the **two-step method** (steps 6a → 6b; see "`parent_return_token` reference convention" below). Do not use the `$(cat ...)` command-substitution form because it does not pass the Bash tool's static analysis.
   → if the ack is absent or the token mismatches, deactivate-child in step 7 is rejected with a ValueError (Adv-20/Adv-30 guard)
7. Run deactivate-child to switch the active context back to the orchestration agent
8. Run record-reply to overwrite launches/<agent_run_id>.reply.txt with the response text
9. Run record-agent-run to append to agent_runs.jsonl
```

**Necessity of running steps 4–9 contiguously (active_child window):** Launching the `Agent` tool in step 4 switches the active context to the child, and it remains the child until `deactivate-child` in step 7. **During this period (from the Agent tool return in step 5 until step 7 completes), the parent orchestration agent must not issue any file write via `Write` / `Edit` / `Bash`.** If it does, even a write to the parent's own `workspace/tmp/<self_arid>/` is evaluated against the child's `output_manifest` and rejected by `output_manifest_write_guard` (`agent_run_id=<child>`) (this hook block has been observed in past audits). Run steps 4 through 9 contiguously without interruption, and batch the parent's own tmp scripts or arbitrary-path writes after step 9 completes or just before step 3 (`record-launch`).

**`parent_return_token` reference convention in step 6 (two-step method):** Obtain and pass the token in the following 2 steps. Do **not** use the `$(cat ...)` Bash command-substitution form — Claude Code's Bash tool static analysis rejects it with `Contains shell syntax (string) that cannot be statically analyzed` (4 consecutive failures observed in a past workflow).

- **Step 6a (obtain):** run `cat workspace/orchestrations/<orchestration_id>/launches/<agent_run_id>.parent_return_token` as a **single Bash command** to print the token to stdout (it matches the allowlist `Bash(cat workspace/orchestrations/*)` and requires no approval). Substitute `<orchestration_id>` / `<agent_run_id>` literally.
- **Step 6b (pass):** embed the token printed in 6a as a **literal string** into `record-child-return --return-token "<literal token>"` and run it.

**Do not read the file with the `Read` tool.** A Read during the active_child window is evaluated against the child arid's `read_manifest`, and even if it is in the parent's own broader manifest it is blocked by `read_manifest_read_guard` (a Read tool block on `launches/<arid>.parent_return_token` has been observed in past audits). Therefore obtain it not with `Read` but with the allowlist-matching single `cat` (step 6a).

### CLI reference conventions (per-location total-cost optimization)

Choose the path for obtaining CLI argument information based on the target subcommand's frequency, payload schema complexity, and doc synchronization cost. The choice of canonical source follows the table below.

| target | canonical source | reason |
|---|---|---|
| Frequent subcommands of `tools/orchestration_runtime.py` (`record-launch` / `record-agent-run` / `record-child-return` / `deactivate-child` / `record-reply` / `set-status` / `write-step-result` / `workflow-launch-check` / `reserve-phase-root` / `mark-dependency-readiness` / `guarded-apply-patch` / `run-gate`) | [docs/CLI_REFERENCE.md](docs/CLI_REFERENCE.md) (Tier-A) + templates in `references/startup_contract.md` | complex payload schema, per-phase required-argument switching, `--help` alone is insufficient |
| Rare subcommands of `tools/orchestration_runtime.py` (`init` / `preflight` / `preflight-status` / `record-timeout` / `read-checkpoint` / `verify-checkpoint-integrity` / `check-step-completed` / `orchestration-read` / `repair-agent-runs`) | `python3 tools/orchestration_runtime.py <sub> --help`. Overview is [docs/CLI_REFERENCE_RARE.md](docs/CLI_REFERENCE_RARE.md) | doc maintenance cost does not match usage frequency |
| `tools/run_workflow.py` / `tools/validate_pipeline_semantics.py` / `tools/audit_orchestration.py` | `<tool> --help` | no dedicated doc is created |
| `tools/new_agent_run_id.py` | literal (`python3 tools/new_agent_run_id.py`) | no arguments |
| Calls to `guarded-apply-patch` / `run-gate` etc. from a step / substep agent | the literal embedded in the parent's launch prompt (`references/launch_prompts.md`) | saves child context; the parent pins it via template |

During workflow execution, reading the implementations under `tools/` directly (the path of reading `.py` implementations via the `Read` tool / `grep` / `sed` / `cat` etc.) remains forbidden and subject to `forbid_tools_direct_read` and `read_manifest_read_guard`. `<tool> --help` is an information-acquisition path limited to argparse output and is allowed outside the scope of `forbid_tools_direct_read` (the hook only records an audit log).

During repository improvement, maintenance, testing, or refactoring, `tools/*.py` is ordinary source code and may be inspected directly. The workflow-execution restriction does not apply to that work.

### `response.json` of `record-launch`
- The Agent tool's launch response has no structured JSON like Codex's `spawn_agent`.
- Pass the following minimal JSON to `record-launch --response-json`. `sandbox_runtime`, `sandbox_enforced`, and `sandbox_profile_ref` are auto-added by `record-launch`.

```json
{
  "agent_run_id": "<agent_run_id>",
  "agent_session_id": "<agent_run_id>",
  "started_at": "<ISO8601>",
  "backend": "claude"
}
```

- Use the same value for `agent_session_id` as the issued `agent_run_id` (because Claude Code has no dedicated session ID like Codex).
- `launches/<agent_run_id>.response.json` and `agents/<agent_run_id>/dialogs/child.response.json` store content including the above plus the fields added by `record-launch`.
- `launches/<agent_run_id>.reply.txt` is provisionally written by record-launch in step 3, and overwritten with the actual Agent tool response text by record-reply in step 8.
