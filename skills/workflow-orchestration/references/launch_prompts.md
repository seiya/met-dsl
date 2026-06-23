# Launch Prompts

> **Audience: the conductor render path (`record-launch`).**
> The conductor renders each leaf's launch prompt from these templates via `record-launch`; the rendered text is passed to the leaf subprocess.
> **A `step agent` / `substep agent` must not `Read` this file.** Its launch prompt is already passed to it at launch, and this path is blocked fail-closed by `read_manifest_read_guard` (intentional, to prevent recurrence).

## Common agent contract boilerplate

> The expansion source for the `{{COMMON_BOILERPLATE}}` placeholder. `tools/orchestration_runtime.py:_render_launch_prompt_template` replaces `{{COMMON_BOILERPLATE}}` in the step / substep templates with the `text` block body of this section, and `{{ACTOR_ROLE}}` with the agent's role (`step` / `substep`). Most of the common contract now lives in the child-readable canonical doc [docs/AGENT_CONTRACT.md](../../../docs/AGENT_CONTRACT.md) (under `docs/`, which is in every child's `read_manifest`), referenced by the first line below. The few **security-critical write/capability constraint lines are deliberately kept inline** because `record-launch`'s `_required_launch_prompt_constraint_lines` guard rejects any launch prompt that drops them (defense-in-depth: the prompt itself must carry these even if the child skips the doc).

```text
- **Read [docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md) immediately after launch and apply its full "Common agent contract" — it governs the remaining read/write permission guards, the direct `Write` / `Edit` artifact-write procedure, tmp-area rules, the no-`python3 -c`-write / no-cross-agent-read constraints, and the dev-mode fail rules. It is in your `read_manifest` (under `docs/`). The launch prompt header below carries your concrete `agent_run_id` / `orchestration_id` / role to substitute into the contract's generic placeholders.**
- Interpret the requirement definition and judgment rules only from `docs/`, `spec/`, and the relevant trial's artifacts in `skill_must_read_refs`. Do not extract rules from the implementation under `tools/`, verification `script`, test code, or validator code (a `Read`/`grep`/`sed`/`cat` of those is blocked by `forbid_tools_direct_read`).
- Use `workspace/orchestrations/<orchestration_id>/capabilities/<agent_run_id>.json` as the canonical source for `capability_token`; immediately after launch, read that file, extract `capability_token`, and pass it to a subsequent `run-gate` (e.g. `orchestration_read`).
- If `capability_token` is not obtained or mismatched: do not start processing and stop with fail.
- Reference `output_manifests/<agent_run_id>.json` as the canonical source, and immediately after launch confirm both the `allowed_output_paths` and `allowed_file_tool_paths` lists.
- Write every output artifact — managed JSON (`*_meta.json` / `verdict.json` / `diagnostics.json` …), source code, and `.yaml` / `.yml` / `.md` — directly with the `Edit` / `Write` tool, only to a path enumerated in the `allowed_file_tool_paths` of `output_manifests/<agent_run_id>.json`. Do not use `guarded-apply-patch`. The MCP-owned `mcp_command_log.jsonl` is written only by the build-runtime MCP server, and the pipeline `lineage.json` is authored host-side by the conductor — neither is ever written with a file tool.
- The use of `run-gate --gate apply_patch_writes` and `apply-patch-gate` as a public path, a file write via shell redirection / `tee` / `sed -i` / an arbitrary command, and a write outside `allowed_output_paths` remain forbidden.
- Another agent's internal artifact (files under `capabilities/` / `output_manifests/` / `read_manifests/` / `access_logs/` / `agents/<other_agent_run_id>/` / `dialogs/` that do not correspond to your own `agent_run_id`) must not be read directly. A cross-agent read fires `rule_source_violation` and fails the phase. (See docs/AGENT_CONTRACT.md for the full read/write guard set, including your own gate files under `gates/<agent_run_id>/`.)
```

## `step agent` launch request template

```text
You are a step agent.
Target node_key: <node_key>
Target step: <step>
orchestration_id: <orchestration_id>
agent_run_id: <agent_run_id>
parent_agent_run_id: <parent_agent_run_id>
workflow_mode: <workflow_mode>
ir_ref: <ir_ref>
pipeline_ref: <pipeline_ref>
dependency_ref: <dependency_ref>
skill_name: <skill_name>
skill_ref: <skill_ref>
skill_must_read_refs: <skill_must_read_refs>
issue_severity: <issue_severity>
repair_strategy: <repair_strategy>
repair_target_agent_run_id: <repair_target_agent_run_id>
repair_reason: <repair_reason>

**tmp area (reference by literal path)**: `allowed_tmp_root` is fixed at `workspace/tmp/<agent_run_id>/` (already recorded in the same-named field of `output_manifests/<agent_run_id>.json`). For a temporary file, directly specify under that literal path like `cat > workspace/tmp/<agent_run_id>/...`. `output_manifest_write_guard` judges only the path and does not reference the `$TMPDIR` env. Do not call bootstrap Bash such as `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'` (the root cause of the workflow stopping on a Claude Code session-sandbox approval request). A direct write to a canonical path (`workspace/pipelines/...`, `workspace/ir/...`, `*_meta.json` etc.) is done with the `Edit`/`Write` tool to a path registered in `allowed_file_tool_paths`; writing to a canonical path with a Bash heredoc is blocked by `enforce_guarded_apply_patch`. (The pipeline `lineage.json` is authored host-side by the conductor, not a leaf write.)

Required requirements:
- **Do not `Read` your own launch prompt body (`launches/<agent_run_id>.prompt.txt`).** Because the prompt is already passed as the input of the `Agent` tool, re-reading is unnecessary, and that path is blocked fail-closed by `read_manifest_read_guard`. `launches/<agent_run_id>.prompt.txt` is the canonical artifact that stores, 1-to-1, the original text passed to the `Agent` tool for audit / replay use.
- You are responsible for directly generating phase artifacts.
- This step is a phase with no standard substep. Complete the step contract yourself.
{{COMMON_BOILERPLATE}}
- For `Compile`, do not start unless the immediate dependency `node` satisfies `direct dependency compile readiness`.
- When updating `ir_meta.json` of `Compile`, record `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, and `context_isolated` as required, and when `context_isolated=false`, record `constraint_reason` as required.
- For `Generate` / `Build` / `Validate`, do not start unless the immediate dependency `node` satisfies `direct dependency execution readiness`.
- Even if the immediate dependency `node` is incomplete, do not substitute by embedding the dependency's code into your own `src/`.
- After completion, return required_outputs, failed_substeps, and substep_agent_run_ids to the parent.
- Keep your final message (the `launch_reply`) **terse and bounded**: a `status:` line, an `output_refs:` list, and at most ~8 lines of rationale — nothing more. Put all detail (diffs, full logs, per-check output) in your artifacts (`step_result.json` / `*_meta.json`), which the orchestration reads on demand via the gated read path; do **not** restate that detail in the reply. An over-budget reply is flagged by `record-agent-run` (and rejected when `METDSL_ENFORCE_REPLY_BUDGET=1`).
```

## `substep agent` launch request template

```text
You are a substep agent.
Target node_key: <node_key>
Target step: <step>
Target substep: <substep>
orchestration_id: <orchestration_id>
agent_run_id: <agent_run_id>
parent_agent_run_id: <parent_agent_run_id>
workflow_mode: <workflow_mode>
ir_ref: <ir_ref>
pipeline_ref: <pipeline_ref>
dependency_ref: <dependency_ref>
skill_name: <skill_name>
skill_ref: <skill_ref>
skill_must_read_refs: <skill_must_read_refs>
issue_severity: <issue_severity>
repair_strategy: <repair_strategy>
repair_target_agent_run_id: <repair_target_agent_run_id>
repair_reason: <repair_reason>

**tmp area (reference by literal path)**: `allowed_tmp_root` is fixed at `workspace/tmp/<agent_run_id>/` (already recorded in the same-named field of `output_manifests/<agent_run_id>.json`). For a temporary file, directly specify under that literal path like `cat > workspace/tmp/<agent_run_id>/...`. `output_manifest_write_guard` judges only the path and does not reference the `$TMPDIR` env. Do not call bootstrap Bash such as `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'` (the root cause of the workflow stopping on a Claude Code session-sandbox approval request). A direct write to a canonical path (`workspace/pipelines/...`, `workspace/ir/...`, `*_meta.json` etc.) is done with the `Edit`/`Write` tool to a path registered in `allowed_file_tool_paths`; writing to a canonical path with a Bash heredoc is blocked by `enforce_guarded_apply_patch`. (The pipeline `lineage.json` is authored host-side by the conductor, not a leaf write.)

Required requirements:
- **Do not `Read` your own launch prompt body (`launches/<agent_run_id>.prompt.txt`).** Because the prompt is already passed as the input of the `Agent` tool, re-reading is unnecessary, and that path is blocked fail-closed by `read_manifest_read_guard`. `launches/<agent_run_id>.prompt.txt` is the canonical artifact that stores, 1-to-1, the original text passed to the `Agent` tool for audit / replay use.
- Read only the contracted input.
- Write only the contracted artifacts.
- Observe the expected output and storage location.
- **verify-family substep (`compile.verify` / `generate.verify`): a `pass` must produce its declared meta output.** You MUST re-author the verified meta (`ir_meta.json` for compile, `source_meta.json` for generate) with the `Edit` / `Write` tool even when the inspection finds nothing to change (refresh an idempotent field such as `verify_attempts`). An inspect-only verify that writes nothing cannot terminate `pass`.
{{COMMON_BOILERPLATE}}
- A `Compile` substep must not start unless the immediate dependency `node` satisfies `direct dependency compile readiness`.
- When updating `ir_meta.json` of `Compile`, record `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, and `context_isolated` as required, and when `context_isolated=false`, record `constraint_reason` as required.
- A `Generate` / `Build` / `Validate` substep must not start unless the immediate dependency `node` satisfies `direct dependency execution readiness`.
- Even if the immediate dependency `node` is incomplete, do not substitute by embedding the dependency's code into the target `node`'s `src/`.
- **MCP command-log & program-output placement (Generate / Build / Validate.execute):** the canonical `allowed_output_paths` placement of the MCP side output `mcp_command_log.jsonl` per phase, the `src/Makefile` auto-inject, the cross-phase Make rules, the `run_id` literal form, the trusted `command_log_ref` set, and the Validate.execute program-output (`diagnostics.json` / `perf.json` / `raw/*`) routing with the direct-binary-write prohibition are canonical in [docs/workflow/MCP_COMMAND_LOG_PLACEMENT.md](docs/workflow/MCP_COMMAND_LOG_PLACEMENT.md) (in your `read_manifest` under `docs/`). When your phase is Generate / Build / Validate.execute, read that doc and follow it. The actionable minimum: always include `mcp_command_log.jsonl` in `allowed_output_paths` at the canonical per-phase path; `record-launch` also defensively auto-injects it, but the explicit enumeration is canonical. (The pipeline `lineage.json` is NOT a leaf output — the conductor authors it host-side.)
- With `repair_strategy=reuse`, limit it to a diff fix against the output of `repair_target_agent_run_id`.
- With `repair_strategy=restart`, regenerate from the contract input without reusing past output.
- On completion, return the artifact references and status to the conductor.
- Keep your final message (the `launch_reply`) **terse and bounded**: a `status:` line, an `output_refs:` list, and at most ~8 lines of rationale — nothing more. Put all detail (diffs, full logs, per-check output) in your artifacts (`step_result.json` / `*_meta.json`), which the orchestration reads on demand via the gated read path; do **not** restate that detail in the reply. An over-budget reply is flagged by `record-agent-run` (and rejected when `METDSL_ENFORCE_REPLY_BUDGET=1`).

```

> The direct `Write` / `Edit` artifact-write procedure and its forbidden-pattern (NG) examples now live in the child-readable canonical doc [docs/AGENT_CONTRACT.md](../../../docs/AGENT_CONTRACT.md) (§"Artifact write — direct `Write` / `Edit` tool procedure"). The child reaches it via the AGENT_CONTRACT pointer expanded into the template body, so it is no longer inlined here.

---

#### phase ↔ skill correspondence table

| step | substep | skill_name | skill_ref |
|---|---|---|---|
| plan | generate | workflow-compile-generate | skills/workflow-compile-generate/SKILL.md |
| plan | verify | workflow-compile-verify | skills/workflow-compile-verify/SKILL.md |
| generate | generate | workflow-generate-generate | skills/workflow-generate-generate/SKILL.md |
| generate | verify | workflow-generate-verify | skills/workflow-generate-verify/SKILL.md |
| tune | generate | workflow-tune-generate | skills/workflow-tune-generate/SKILL.md |
| tune | verify | workflow-tune-verify | skills/workflow-tune-verify/SKILL.md |
| build | — | workflow-build | skills/workflow-build/SKILL.md |
| execute | — | workflow-validate-execute | skills/workflow-validate-execute/SKILL.md |
| judge | — | workflow-validate-judge | skills/workflow-validate-judge/SKILL.md |
| promote | — | workflow-promote | skills/workflow-promote/SKILL.md |

**Negative constraint:** do not Read a SKILL.md of any phase other than your own (e.g. a generate substep reading `skills/workflow-compile-verify/SKILL.md` fires `rule_source_violation`). Read only the single file passed via the launch prompt's `skill_ref`.

---

#### substep ↔ allowed validator gate correspondence table

It canonicalizes, per `(step, substep)`, the `validate_pipeline_semantics --stage <X>` invocation that may appear in the rendered launch prompt body. Do not state in the launch prompt a `--stage` other than the one permitted in the "allowed_stage" column of the table below. The recurrence-prevention plan (Issue 1) is the canonical source.

| step | substep | allowed `validate_pipeline_semantics --stage` | note |
|---|---|---|---|
| compile | generate | (none) | gate calls are limited to `validate_workspace_root` / `check_artifact_syntax --expect-top object`. `io_contract`-related is the `Compile.verify` responsibility. |
| compile | verify | `compile` | required before verify completes after `io_contract` derivation. |
| generate | generate | (none) | `--stage post_generate` is the `Generate.verify` responsibility. |
| generate | verify | `post_generate` | |
| build | — | `post_build` | invoked after the MCP `compile_project` call. |
| validate | execute | `post_execute` | invoked for the judgment of the `run_program` / `run_quality_checks` result. |
| validate | judge | `pre_judge` | the final validation before `aggregate_verdict` finalization. |

`--stage full` is a debug stage that performs end-to-end validation, and is not explicitly included in the allow-list for any of the (step, substep) above (the steady workflow uses per-phase stages as canonical). The exhaustive list of canonical `--stage` values uses the argparse `choices` of `tools/validate_pipeline_semantics.py` (`compile` / `post_generate` / `post_build` / `post_execute` / `pre_judge` / `full`) as the primary source.

**Distinction from the recording layer:** the `validation_stage` recording rule (applied at `write-step-result` time) defines the values that **may be recorded** in `step_result.json#validation_stage` as a broader per-step set (including `full`), and is the recording-layer contract. This table is the invocation-layer contract at launch-prompt time, and imposes a stricter per-substep constraint than the recording layer. They are contracts of different layers, and a `validation_stage` value recorded as a result of being narrowed per-substep by this table is automatically included in that recording-layer allowed set (e.g. only `compile` is executable for `compile/verify` → a subset of the `compile`/`full` recording set).

**negative constraint:** do not state in the launch prompt of this `(step, substep)` a `validate_pipeline_semantics` call with a `--stage` not permitted in the table above. Example: including `validate_pipeline_semantics --stage compile` in the `Compile.generate` prompt invades the `Compile.verify` responsibility and fires `noncanonical_phase_write_attempt`. A mere mention of an MCP tool name (`compile_project` etc.) (in explanatory text, a negative constraint, etc.) is outside the scope of this lint.

**negative constraint (MCP write tool):** do not state in the `generate/verify` launch prompt the execution of a `build-runtime` MCP write tool such as `run_linter`. lint is the `generate.generate` responsibility (`docs/workflow/phases/phase_02_generate.md` 2-1), and execution in verify induces a write to `mcp_command_log.jsonl` that verify's `allowed_output_paths` does not authorize and invites an `unauthorized_write_violation` → `fail_closed`. This constraint targets only the `build-runtime` MCP write tool, and the read-only `validate_workspace_root.py` and `validate_pipeline_semantics --stage post_generate` (table above) remain permitted.

`record-launch`, inside `_validate_launch_prompt_text`, reconciles the text of `launch_prompt_ref` against the per-(step, substep) allowed-stage set. It scans only actionable invocation lines (lines containing `python3` / `tools/validate_pipeline_semantics.py` / `--gate validate_pipeline_semantics`), extracts both the direct CLI form and the canonical run-gate JSON form (`--args-json '{"stage": "..."}'`), and rejects with a `ValueError` if it is outside the allowed-stage (`tools/orchestration_runtime.py::_lint_launch_prompt_gate_allowlist` and `ALLOWED_VALIDATE_PIPELINE_STAGES` are the canonical implementation). For an emergency rollback, the lint can be disabled with the env `METDSL_ENFORCE_GATE_ALLOWLIST=0` (default is enabled).

#### Additional contract on `repair_strategy=reuse`

A re-submission with `repair_strategy=reuse` is limited to a diff fix against the output of `repair_target_agent_run_id` (the repair / retry section of `docs/ORCHESTRATION.md` is the canonical source). Under `bwrap` + FS-diff attribution a step/substep `pass` no longer requires `apply_patch_writes` gate evidence (`_validate_apply_patch_gate_coverage` early-returns for step/substep), so a reuse retry that writes nothing needs no inherited gate evidence; if it re-writes a path, it does so directly with the `Edit` / `Write` tool. Same-identity (`(node_key, step, substep)`) is still verified runtime-side.

---

#### Usage contract of `allowed_tmp_root`

`record-launch` creates `workspace/tmp/<agent_run_id>/` and records it in the `allowed_tmp_root` field of `output_manifests/<agent_run_id>.json`. **The agent uses this literal path directly** to pass `output_manifest_write_guard` (it judges only the write-target path and does not reference the `$TMPDIR` env, `tools/hooks/common.py:_validate_write_access`).

**Forbidden bootstrap Bash:**

- `export TMPDIR=$(jq -er ...)`, `export TMPDIR=...` — the root cause of the workflow stopping on a Claude Code session-sandbox approval request.
- `jq -er ...` / `printenv` / `bash -c '...'` — same as above.
- `python3 -c "import json; ..."` — blocked by `forbid_python_inline_write` (intent_detected=`json_read`).

**Correct temporary-file write:**

- **`.json` / `.txt` files**: write `workspace/tmp/<agent_run_id>/<name>.{json,txt}` directly with the `Write` tool. Avoid a Bash heredoc redirect because the quoted form `cat > "path" <<EOF` has the known risk that the hook's file_path parser mis-detects `'\"'` as a path.
- **`.py` / `.yaml` / `.sh` etc.**: a Bash heredoc is OK.

```bash
# saving gate stderr (a redirect to a non-.json/.txt path is safe)
python3 tools/orchestration_runtime.py run-gate --gate ... 2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt

# a temporary python script
cat > workspace/tmp/<agent_run_id>/build_patch.py <<'EOF'
# script body ...
EOF
python3 workspace/tmp/<agent_run_id>/build_patch.py
```

Managed JSON / `.txt` artifacts are written directly via the `Write` / `Edit` tool (see the "Artifact write — direct `Write` / `Edit` tool procedure" section of [docs/AGENT_CONTRACT.md](../../../docs/AGENT_CONTRACT.md)).

`<agent_run_id>` is literally substituted in the corresponding field of the launch prompt. The `$TMPDIR` env is inherited into the subprocess by `tools/run_workflow.py`, so a `${TMPDIR}/...`-form reference also works as a result, but to minimize env dependence the literal path is canonical. `/tmp/`, `/dev/shm/`, and an argument-less `$(mktemp)` remain blocked by `output_manifest_write_guard`.
