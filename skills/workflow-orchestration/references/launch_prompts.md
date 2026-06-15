# Launch Prompts

> **Audience: orchestration agent only.**
> This file is a collection of templates by which the orchestration agent generates the `prompt` argument of the `Agent` tool (or `spawn_agent`).
> **A `step agent` / `substep agent` must not `Read` this file.** The necessary template content has already been passed as the prompt argument of the `Agent` tool to the child agent after launch, and that path is blocked fail-closed by `read_manifest_read_guard` (intentional, to prevent recurrence).

## Common agent contract boilerplate

> The expansion source for the `{{COMMON_BOILERPLATE}}` placeholder. `tools/orchestration_runtime.py:_render_launch_prompt_template` replaces `{{COMMON_BOILERPLATE}}` in the step / substep templates with the `text` block body of this section. `{{ACTOR_ROLE}}` is replaced with the role of the agent being launched (`step` or `substep`). The child agent does not Read this file directly.

```text
- Immediately after launch, read `skill_ref` and execute with a contract not contradicting `skill_must_read_refs`.
- Interpret the requirement definition and judgment rules only from `docs/`, `spec/`, and the relevant trial's artifacts included in `skill_must_read_refs`. Do not extract rules from the implementation under `tools/`, verification `script`, test code, or validator code.
- Use `workspace/orchestrations/<orchestration_id>/capabilities/<agent_run_id>.json` as the canonical source for `capability_token`; immediately after launch, read that file, extract `capability_token`, and pass it to subsequent `run-gate` / `guarded-apply-patch`.
- If `capability_token` is not obtained or mismatched: do not start processing and stop with fail.
- `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` and `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` may be read directly with the `Read` tool (`run-gate` not needed).
- For an `orchestration-read` of a path other than those 2 files, run `python3 tools/orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '{"read_path":"..."}'` as the only path, and forbid calling `orchestration-read` directly.
- `orchestration-read` uses `read_manifests/<agent_run_id>.json` as the canonical source, and must not read a path outside the manifest.
- Run the child only inside the `bwrap` sandbox. Forbid non-sandbox execution.
- Branch the write path by the output path's extension. Reference `output_manifests/<agent_run_id>.json` as the canonical source, and immediately after launch confirm both the `allowed_output_paths` and `allowed_file_tool_paths` lists.
- For `.json` and `.txt` output, run `python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role {{ACTOR_ROLE}} --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` as the only path, and stop editing on rejection.
- For output other than the above such as `.yaml` / `.yml` / `.md` and source code, write directly with the `Edit` / `Write` tool only to a path enumerated in the `allowed_file_tool_paths` of `output_manifests/<agent_run_id>.json`.
- The use of `run-gate --gate apply_patch_writes` and `apply-patch-gate` as a public path, a file write via shell redirection / `tee` / `sed -i` / an arbitrary command, and a write outside `allowed_output_paths` remain forbidden.
- Both `guarded-apply-patch` and `Edit` / `Write` reference `output_manifests/<agent_run_id>.json` and reject a path outside the manifest. Do not write to a path outside the manifest.
- When a temporary file is needed, do not specify `/tmp` / `/dev/shm` directly; directly specify the literal path of `allowed_tmp_root` (`workspace/tmp/<agent_run_id>/...`). Switch the write means by extension: **(a)** for `.py` / `.yaml` / `.sh` etc. (non-`.json` / `.txt`), a Bash heredoc is OK (e.g. `cat > workspace/tmp/<agent_run_id>/work.py <<'EOF'`); **(b)** for `.json` / `.txt`, use the `Write` tool (a Bash heredoc redirect is in the NG examples (below) of this file: the hook's file_path parser may, in some quoted forms, mis-detect `'\"'` as a path and block it). `output_manifest_write_guard` judges only whether the write-target path is under `allowed_tmp_root` and does not reference the `$TMPDIR` env (`tools/hooks/common.py:_validate_write_access`). Bootstrap Bash such as `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'` is forbidden (the workflow stops on a session-sandbox approval request). Hard-coding `/tmp/` / `/dev/shm/` remains blocked by `output_manifest_write_guard`.
- The internal gate files under `gates/<agent_run_id>/` (`apply_patch_writes.json` etc.) must not be read directly even if they correspond to your own `agent_run_id`. Use the **stderr of `run-gate` / `guarded-apply-patch`** as the canonical path for obtaining the gate execution result, and save and reference it with `2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` (e.g. `python3 tools/orchestration_runtime.py run-gate --gate ... 2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt`). Literally substitute your own value for the `<agent_run_id>` part (a `${TMPDIR}` env reference is not forbidden by contract, but to reduce env dependence the literal is canonical). On failure, `violations[]` is output to stderr in JSON form. For a summary, refer to `agent.summary.txt` / `step_result.json`. Another agent's internal artifact (files under `capabilities/` / `output_manifests/` / `read_manifests/` / `access_logs/` / `agents/<other_agent_run_id>/` / `dialogs/` that do not correspond to your own `agent_run_id`) must likewise not be read directly. A cross-agent read fires `rule_source_violation` and fails the phase.
- Do not write to a file (`open(path, 'w'/'a'/'x')` / `Path.write_text` / `shutil.copy*` etc.) with `python3 -c "..."` or `python3 - <<'EOF'`. It is blocked by `forbid_python_inline_write`. Use `guarded-apply-patch` for `.json`/`.txt`, and the `Edit`/`Write` tool for others.
- Use the `Read` tool to confirm the content of a JSON file. `python3 -c "import json; ..."` is blocked by `forbid_python_inline_write`. Only when Python processing is truly necessary, write a script to `workspace/tmp/<agent_run_id>/x.py` and run it with `python3 workspace/tmp/<agent_run_id>/x.py` (`<agent_run_id>` is literally substituted).
- For UUID generation, use `python3 tools/new_agent_run_id.py`. `python3 -c 'import uuid; print(uuid.uuid4())'`, including with flags such as `-S`, is all blocked by `forbid_python_inline_write`. Do not use `cat /proc/sys/kernel/random/uuid` because it stops on a session-sandbox approval request every time.
- Your own launch prompt body is passed as input at Agent tool launch. There is no need to re-read `launches/<agent_run_id>.prompt.txt` with `Read`, and it is blocked by `read_manifest_read_guard`.
- A `Read`/`grep`/`sed`/`cat` of `tools/` / `tests/` / a validator script / a hook implementation is blocked by `forbid_tools_direct_read`. Interpret the requirements and judgment rules only from `docs/` / `spec/` / `skill_must_read_refs`. For the internal behavior of `guarded-apply-patch` (strip decision etc.), the canonical references are "Patch application contract" of `docs/ORCHESTRATION.md` and "About the strip of `guarded-apply-patch`" at the end of this file.
- When referencing an artifact you generated, use the relative path from the project root enumerated in the `allowed_output_paths` of `output_manifests/<agent_run_id>.json` (e.g. `workspace/ir/...`). Do not use an absolute path such as `/home/<user>/...` or a path without the `workspace/` prefix.
- Use `capabilities/<agent_run_id>.json` as the canonical source for orchestration metadata such as `orchestration_id` / `agent_run_id` / `node_key` / `step` / `write_roots`. `orchestration_meta.json` is blocked by `read_manifest_read_guard`.
- If `skill_name` and `skill_ref` are unspecified, stop with fail. Read only the single file `skill_ref` specified in the launch prompt, and **do not additionally Read a SKILL.md of any phase other than your own** (for the phase ↔ skill mapping, see the end of this file).
- On input shortage, do not complete by guessing; stop with fail.
- With `workflow_mode=dev`, stop with fail the moment `issue_severity=major|critical` is detected in a verify-family judgment.
- When it fails with `workflow_mode=dev`, include in the reply the basis needed to generate `failure_analysis.json` (the failure reason, related output_refs, a summary of the main logs).
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

**tmp area (reference by literal path)**: `allowed_tmp_root` is fixed at `workspace/tmp/<agent_run_id>/` (already recorded in the same-named field of `output_manifests/<agent_run_id>.json`). For a temporary file, directly specify under that literal path like `cat > workspace/tmp/<agent_run_id>/...`. `output_manifest_write_guard` judges only the path and does not reference the `$TMPDIR` env. Do not call bootstrap Bash such as `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'` (the root cause of the workflow stopping on a Claude Code session-sandbox approval request). A direct write to a canonical path (`workspace/pipelines/...`, `workspace/ir/...`, `lineage.json` etc.) is limited to those registered in `allowed_file_tool_paths` via the `Edit`/`Write` tool. Otherwise `guarded-apply-patch` is required, and writing to a canonical path with a Bash heredoc is blocked by `enforce_guarded_apply_patch`.

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
- The completion reply must include, as `launch_reply`, the actions performed and the judgment result in plain text.
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

**tmp area (reference by literal path)**: `allowed_tmp_root` is fixed at `workspace/tmp/<agent_run_id>/` (already recorded in the same-named field of `output_manifests/<agent_run_id>.json`). For a temporary file, directly specify under that literal path like `cat > workspace/tmp/<agent_run_id>/...`. `output_manifest_write_guard` judges only the path and does not reference the `$TMPDIR` env. Do not call bootstrap Bash such as `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'` (the root cause of the workflow stopping on a Claude Code session-sandbox approval request). A direct write to a canonical path (`workspace/pipelines/...`, `workspace/ir/...`, `lineage.json` etc.) is limited to those registered in `allowed_file_tool_paths` via the `Edit`/`Write` tool. Otherwise `guarded-apply-patch` is required, and writing to a canonical path with a Bash heredoc is blocked by `enforce_guarded_apply_patch`.

Required requirements:
- **Do not `Read` your own launch prompt body (`launches/<agent_run_id>.prompt.txt`).** Because the prompt is already passed as the input of the `Agent` tool, re-reading is unnecessary, and that path is blocked fail-closed by `read_manifest_read_guard`. `launches/<agent_run_id>.prompt.txt` is the canonical artifact that stores, 1-to-1, the original text passed to the `Agent` tool for audit / replay use.
- Read only the contracted input.
- Write only the contracted artifacts.
- Observe the expected output and storage location.
{{COMMON_BOILERPLATE}}
- A `Compile` substep must not start unless the immediate dependency `node` satisfies `direct dependency compile readiness`.
- When updating `ir_meta.json` of `Compile`, record `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, and `context_isolated` as required, and when `context_isolated=false`, record `constraint_reason` as required.
- A `Generate` / `Build` / `Validate` substep must not start unless the immediate dependency `node` satisfies `direct dependency execution readiness`.
- Even if the immediate dependency `node` is incomplete, do not substitute by embedding the dependency's code into the target `node`'s `src/`.
- **Always include the MCP side output `mcp_command_log.jsonl` in `allowed_output_paths`.** The canonical placement per phase is the following:
  - Generate substep: `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl` (run_linter). **This auto-inject and the `run_linter` side output correspond only to the `generate.generate` substep**. Because the `allowed_output_paths` of the `generate.verify` substep does not authorize the `run_linter` side output (`mcp_command_log.jsonl`), do not write `run_linter` execution in the verify launch prompt (the `validate_pipeline_semantics --stage` the verify may write is only `post_generate` as in the "substep ↔ allowed validator gate correspondence table" below; the read-only `validate_workspace_root.py` is separately permitted).
  - Build step (in-phase, CMake/Meson out-of-source): `<pipeline_ref>/binary/<binary_id>/mcp_command_log.jsonl` (compile_project, project_dir=<binary_id>/)
  - Build step (cross-phase, Make in-source for Fortran/C-family): `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl` (compile_project, project_dir=<gen>/src/). Bound by the `source_id` of the launch request, and `record-launch` verifies `verification_status=pass` of `source_meta.json`
  - Validate.execute substep (in-phase): `<pipeline_ref>/runs/<run_id>/<node_key_safe>/mcp_command_log.jsonl` (run_program etc.)
  - Validate.execute substep (cross-phase quality_check): `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl` (`skills/workflow-validate-execute/SKILL.md` L20 — with `toolchain.build_system=make` + Fortran/C-family, `run_quality_checks` runs with `project_dir=source/<source_id>/src/`, so the log is side-output into the generate tree). When `source_id` is included in the launch request of the Validate.execute substep, the runtime auto-injects the cross-phase canonical placement and bypasses the phase contract and write_roots check. `source_id` is verified by the existence of `<pipeline_ref>/source/<source_id>/source_meta.json`, and passing an unknown source_id (a value not corresponding to an actual generate execution) makes `record-launch` reject with a `ValueError` (prevention of cross-phase write authorization injection by an arbitrary caller).

  In addition, `validate_pipeline_semantics.py` restricts all `command_log_ref` it trusts as MCP tool execution evidence to only the canonical placement:
  - `lint_command_ref.run_linter[].command_log_ref`: `<gen_dir>/src/mcp_command_log.jsonl`
  - `source_command_ref.<run_program-key>.command_log_ref`: `<execute node_dir>/mcp_command_log.jsonl` (sibling of trial_meta)
  - `source_command_ref.run_quality_checks.command_log_ref`: `<pipeline_ref>/generate/<source_source_id>/src/mcp_command_log.jsonl` — bound to **only the single gen_id** by `trial_meta.source_source_id`. The canonical placement of a sibling/older generation of the same pipeline is not accepted.

  A placement to a non-canonical path (e.g. `<execute>/raw/forged.jsonl`) is rejected by the post_generate / post_execute gate (prevention of forged MCP execution evidence).

  Furthermore, `_validate_trial_meta` requires that the log record each entry of `source_command_ref` points to include a **recognized MCP `tool_name`** (`run_program` / `run_quality_checks`). `compile_project` is a build-phase tool and is not accepted in the execute trial_meta. A forge record with a missing / unknown `tool_name` is rejected (blocking the path where a tool-specific validator silently skips).

  The cross-phase canonical placement of the Validate.execute substep is bound only to the `source_id` field of the launch request. Including `<pipeline_ref>/generate/<other_gen>/src/...` (a generation different from the request's `source_id`) in `allowed_output_paths` is rejected by the phase contract. On the trial_meta side too, `source_source_id` is a required record, fixing the bind between execute and generate.

  **Single-namespace enforcement:** the `allowed_output_paths` of a generate / build / validate step (Validate.execute substep) must target only a single `<source_id>` / `<binary_id>` / `<run_id>`. Mixing and listing multiple ids under the same pipeline makes `record-launch` reject with a `ValueError` (prevention of granting write authority to the audit log of a sibling/older run). For Generate / Validate.execute, it additionally requires that the request's `source_id` / `run_id` matches the ids of the listed paths (a mismatch is rejected with `does not match request ...id`).

  **Canonical form of `run_id` (Validate execute/judge):** `run_id` is the only one in the id family with a **fixed literal `run_` prefix**, in the form `run_<YYYYMMDD>_<seq3>` (e.g. `run_20260605_001`). The `<slug>_<YYYYMMDD>_<seq3>` form of `ir_id` / `pipeline_id` (slug being hyphen-separated) **must not be reused** — for example `run-rsn-p0_20260605_001` matches the slug form (slug=`run-rsn-p0`) but is not a canonical `run_id`, and the phase contract of `record-launch` rejects it with `outside phase contract`. Even if it passed, the run discovery of Validate `post_execute` recognizes only the literal `run_` layout and silently fails with `no execution artifacts found`. Good example `run_20260605_001` ✓ / bad example `run-rsn-p0_20260605_001` ✗.

  **Quality_check stale-generation countermeasure:** the `source_meta.json` that `trial_meta.source_source_id` points to must have `verification_status=pass`. Referencing a failed / old generation as quality_check evidence is rejected by the post_execute validator. Furthermore, **`record-launch` also** checks `verification_status` and rejects, at launch time, granting write authority to the MCP audit log under a failed generation (prevention of provenance contamination of a failed gen tree).

  **Build lineage bind (specific build):** the launch request of the Validate.execute substep requires recording `source_binary_id`, and the `source_source_id` of `<pipeline>/binary/<source_binary_id>/binary_meta.json` must match the request's `source_id`. A missing `source_binary_id`, an absent binary_meta.json, an unrecorded `source_source_id`, or a value mismatch is rejected by record_launch. This prevents a mixed-build forge (reusing the quality_check evidence of build B while running the binary of build A). The Build step records `source_source_id` in `binary_meta.json` as required (see `skills/workflow-build/SKILL.md`).

  **Make-only gate of cross-phase auto-inject:** the cross-phase write authority to `<pipeline>/source/<source_id>/src/mcp_command_log.jsonl` is auto-injected only when `toolchain.build_system=make` (a Fortran/C-family in-source build). For an out-of-source toolchain such as CMake/Meson/Ninja, cross-phase is not injected, and the build/execute log allows only the in-phase canonical (`<binary_id>/mcp_command_log.jsonl` or `<exec_id>/<node_key_safe>/mcp_command_log.jsonl`). `record-launch` reads `toolchain.build_system` of `spec.ir.yaml.impl_defaults` and judges automatically.

  **Out-of-source dir override of a Make build (`build_system=make`):** the in-source Make's `compile_project`/`run_quality_checks` runs with `project_dir=<pipeline>/source/<source_id>/src/`, but emitting build artifacts to `src/` is outside the Build/Validate capability write_root (Build=`binary/` only, Validate.execute=`runs/` only) and becomes an `unauthorized_write_violation` → `fail_closed`. Because the generated `src/Makefile` is already parameterized with `OBJDIR ?= .` / `BINDIR ?= .` / `RUNDIR ?= .` ([workflow-generate-generate](../../workflow-generate-generate/SKILL.md)), pass an absolute-path override at launch to route artifacts into the write_root:
  - **Build step:** `compile_project extra_args=["OBJDIR=<repo_abs>/workspace/tmp/<build_agent_run_id>/build", "BINDIR=<repo_abs>/<pipeline_ref>/binary/<binary_id>/bin"]`. object/`.mod` land in a per-run tmp (`workspace/tmp/<agent_run_id>/` — auto-authorize + auto-clean on success), and the execution binary lands in `binary/<binary_id>/bin/`. Include the execution binary `<pipeline_ref>/binary/<binary_id>/bin/<exe>` in `allowed_output_paths` in **file form** (because it contains `/bin/`, it passes the build phase contract, and by auto-derive enters `allowed_file_tool_paths`→`_exact_declared_set` and is authorized in terminal validation). Do not make `allowed_file_tool_paths` explicit and leave it to auto-derive (if made explicit, always include that exe path). `binary_meta.json#binary_artifact_ref` points to `binary/<binary_id>/bin/<exe>`.
  - **Validate.execute substep:** `run_quality_checks env={"OBJDIR":"<repo_abs>/workspace/tmp/<exec_agent_run_id>/build", "BINDIR":"<repo_abs>/<pipeline_ref>/binary/<source_binary_id>/bin", "RUNDIR":"<repo_abs>/workspace/tmp/<exec_agent_run_id>/qc_run"}` (`run_quality_checks` is a fixed command but propagates `env` to make). Because `binary/` and `source/` are read-only-bound, `make test` does not relink (it references the existing binary via the Makefile guard), and the `diagnostics.json` / `raw/*` emitted by the `make test` binary re-run are confined under tmp (`workspace/tmp/<exec_agent_run_id>/qc_run`, a separate subdir from `run_program`'s `run/`). Pointing `RUNDIR` at the canonical run node dir would have the direct binary write overwrite the gate-authored copy and invite an `unauthorized_write_violation` → `fail_closed`, so always point it at tmp (for details see "Validate.execute program output routing — direct binary write forbidden" below). All canonical `.json` is re-authored by the agent with `guarded-apply-patch` after both `run_program` and `run_quality_checks` complete (the final step). The only write to `src/` is the cross-phase audit log.

  **`ok=true` requirement for execute evidence:** the post_execute validator requires `ok=true` in the `run_program` / `run_quality_checks` record. A record with `ok=false` or a missing `ok` is regarded as a failed execution and is not accepted as tool-execution evidence (the same policy as the lint validator).

  **Role binding for source_command_ref:** each entry of `source_command_ref` of the Validate.execute trial_meta declares the `tool_name` field (= `run_program` or `run_quality_checks`), and it must match the `tool_name` of the log record. `compile_project` is build-phase-only and is not accepted in the validate trial_meta. The trial_meta must include at least 1 `tool_name='run_program'` entry (the actual program-execution evidence). A role mismatch (e.g. a compile_project record in the run_program slot) is regarded as a forge and rejected.

  **Run_program log canonical placement (MCP gate enforcement):** `validate_mcp_build_tool_invocation` (the MCP server pre-call gate) enforces the log placement to only the canonical (`<pipeline_ref>/runs/<run_id>/<node_key_safe>/mcp_command_log.jsonl`) for a call with `tool_name=run_program` and `step=execute`. Set `project_dir` to the execute node_dir, or make the canonical absolute/relative path explicit with the `command_log_path` argument. A non-canonical placement is rejected with a `RuntimeError` at MCP call time, not delayed to the later post_execute validator.

  **Validate.execute program output (`diagnostics.json`/`perf.json`) routing — direct binary write forbidden:** `mcp_command_log.jsonl` is an **MCP-owned audit log** for which a canonical direct write is permitted (the trust model above). On the other hand, `diagnostics.json` / `perf.json` are the **program output of the binary (runner)** that `run_program` runs, and as canonical `.json` they **require the gate evidence of `guarded-apply-patch`**. Because the binary cannot go through a gate, having it write directly to the canonical run dir (`<pipeline_ref>/runs/<run_id>/<node_key_safe>/`) makes the terminal `record-agent-run`'s baseline-diff detect an `unauthorized_write_violation` and become `fail_closed` (a `.json` not registered in `mcp_owned_audit_logs` is not tolerated as MCP-owned). The launch specifies the following routing:
  - **Point the binary output destination at tmp:** make `run_program`'s `project_dir` (= the binary's cwd) or the case's output destination `allowed_tmp_root` (`workspace/tmp/<exec_agent_run_id>/run/`), and have the binary drop `diagnostics.json` / `perf.json` / raw / logs to **tmp (auto-authorize)**. **`run_quality_checks` (`make test`) likewise**: point `env.RUNDIR` at `workspace/tmp/<exec_agent_run_id>/qc_run` (a separate subdir from run_program's `run/`), and confine the `diagnostics.json` / `raw/*` emitted by the `make test` binary re-run to tmp too. Pointing `RUNDIR` at the canonical run node dir would have the `make test` direct binary write overwrite the gate-authored copy already re-authored after `run_program` and invite an `unauthorized_write_violation` (a typical ordering defect).
  - **Make command_log_path canonical explicit:** because making `project_dir` tmp would drop the MCP log into tmp too, make `<repo_abs>/<pipeline_ref>/runs/<run_id>/<node_key_safe>/mcp_command_log.jsonl` explicit with the `command_log_path` argument to satisfy the "Run_program log canonical placement" gate above.
  - **The agent re-authors the canonical `.json` (final step):** the execute agent `Read`s the `diagnostics.json` / `perf.json` / `raw/*` of the **`run/` tmp tree** (`workspace/tmp/<exec_agent_run_id>/run/`, = the output of the `run_program` execution with `spec.ir.yaml.case` as arguments), and re-authors the canonical `runs/<run_id>/<node_key_safe>/diagnostics.json` / `perf.json` / `raw/metrics_basis.json` / `raw/state_snapshots/*` with **`guarded-apply-patch` (create-form)**. **The runner's program output (`diagnostics.json` / `perf.json` / `raw/metrics_basis.json` / `raw/state_snapshots/*`) is always promoted from `run/`, and must not be promoted from `qc_run/` (the output of the make-test re-run)** — if `qc_run/` is promoted, the canonical evidence does not correspond to the required `run_program` invocation (the `spec.ir.yaml.case` and its `command_log`), and `Validate.judge` consumes evidence with a provenance mismatch. The output of `qc_run/` is referenced only for the quality-check comparison (the verdict computation of `quality_check.json`). `trial_meta.json` and `quality_check.json` are **not runner output but metadata the agent authors** (`trial_meta.json` is composed from the MCP command refs / `source_source_id` / `source_command_ref`, and `quality_check.json` from the comparison result. The runner must not output either file directly), and are generated with `guarded-apply-patch` rather than promoted from tmp. **The re-author is the final step after both `run_program` and `run_quality_checks` complete**, ensuring an order in which a subsequent binary re-run does not overwrite the gate-authored copy (the ordering of authoring after run_program then running make test in the canonical RUNDIR is an overwrite defect).
  - **raw `.json` via guarded-apply-patch / raw non-`.json` and log via the Write tool:** the `.json` under `raw/` (`metrics_basis.json` / `state_snapshots/*.json` etc.) **is also canonical `.json` and is re-authored with `guarded-apply-patch`** (included in the target of "the agent re-authors the canonical `.json`" above). What is written with the `Write` tool is limited to **non-`.json`** files under `raw/`, `stdout.log`, and `stderr.log`, written to a path within `allowed_file_tool_paths` (it is auto-derived if that path is included in `allowed_output_paths` in file form). Writing raw `.json` with the `Write` tool is rejected by `enforce_guarded_apply_patch`, and even if it could be written, `record-agent-run` detects an `unauthorized_write_violation` from the missing gate evidence.
  Make this distinction (an MCP-owned log may be written canonically directly / a program output `.json` must go via tmp + re-author) explicit in the Validate.execute launch prompt.

  **MCP audit log trust model (defense-in-depth, not cryptographic proof):** a canonical log path registered in the manifest via the `mcp_owned_audit_logs` field is approved as an "authorized MCP-owned write" at terminalization. This relies on the write from the MCP server to the path having all other paths blocked by the following 3 defense layers: (a) the hook layer rejects `Edit`/`Write` by excluding it from `allowed_file_tool_paths`, (b) `guarded-apply-patch` rejects a mutation to a path within `mcp_owned_audit_logs` with a `RuntimeError`, (c) a Bash heredoc/redirect is also rejected by the same hook. This makes a write to the canonical path effectively possible only via the MCP server. **However, because this has no out-of-band MCP-side signature or invocation cross-reference**, if a new write path (other than MCP) leaks into the hook layer in the future, it could allow a forge again. A complete defense needs a design extension where the MCP server keeps its own audit ledger and the validator cross-references the path with that ledger (recognized as future work at this point).

  **Make build's `src/Makefile` auto-inject:** when the step is `Generate` and `spec.ir.yaml.impl_defaults.toolchain.build_system=make`, `record-launch` auto-injects `<pipeline_ref>/source/<source_id>/src/Makefile` into `allowed_output_paths` and `allowed_file_tool_paths`. With only a bare `src/` directory entry, source extensions (`.f90`/`.c`) can be written with `guarded-apply-patch` but the extension-less `Makefile` is intentionally excluded from the source-extension set of the directory allowlist and cannot be written via any path (a cause of the child fail-stopping mid-run because the `test`/`check` target required for make+Fortran/C cannot be written). **The orchestration agent usually just does not make `allowed_file_tool_paths` explicit in a Generate launch and leaves it to auto-derive.** If made explicit, always include `src/Makefile` — an omission makes `record-launch` fail-fast with a `ValueError` before launching the child (prevention of a mid-run fail-stop).

  `_allowed_output_paths_for_launch()` of `tools/orchestration_runtime.py` does a defensive auto-inject in any of the generate/build/execute phases, but making it explicit in `record-launch`'s `--request-json` fixes it without depending on the auto-inject. On omission, an `unauthorized_write_violation` occurs at `record-agent-run` and the orchestration stops with `fail_closed`. This log is integrity-protected and all of the following 3 paths are rejected (because `validate_pipeline_semantics.py` trusts the log's content, its generation is limited to via the MCP server):
  - direct `Edit` / `Write` tool write (auto-excluded from `allowed_file_tool_paths`)
  - patch application via `guarded-apply-patch` (`RuntimeError` if it matches any of `changed_paths` / `numstat_targets` / a rename source/destination)
  - any Bash redirect that bypasses the above (already blocked by the existing `enforce_guarded_apply_patch` and `output_manifest_write_guard`)
- With `repair_strategy=reuse`, limit it to a diff fix against the output of `repair_target_agent_run_id`.
- With `repair_strategy=restart`, regenerate from the contract input without reusing past output.
- On completion, return the artifact references and status to the `orchestration agent`.
- The completion reply must include, as `launch_reply`, the actions performed and the judgment result in plain text.

#### `.json` artifact write — `guarded-apply-patch` usage procedure

`.json` / `.txt` output must always be done by the following procedure. All means that involve a file write (heredoc redirect / `tee` / a file write via `python3 -c` / `echo > file` etc.) are forbidden. Assembling the patch text into a variable is permitted.

**Procedure (the correct pattern — create-or-overwrite with retry):**

Because `guarded-apply-patch` internally uses `git apply`, the patch form differs depending on whether the file exists. Because there is a race window between `os.path.exists()` and the patch application, on a patch failure retry once with the reverse form. Concurrent writes to the same output path do not occur by orchestration design, but absorb the case where an empty file from a previous attempt remains during retry/repair.

**Procedure (Bash + Write tool based; `python3 -c` / `python3 - <<EOF` is blocked by `forbid_python_inline_write` so it is not usable):**

1. **Confirm the target file's state**: read `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml` with the `Read` tool (if it does not exist, `Read` returns an error, in which case assemble a `/dev/null` create-form patch). If it exists, control its line count (`len(old_lines)`).
2. **Assemble the patch text**: there are 3 forms — existing / new / empty file — and all can be built by simple string concatenation. Template (`<old_lines>` / `<new_lines>` are substituted by the agent with literal values):

   ```text
   # update / replace form (existing file, len(old_lines)=N>0, len(new_lines)=M)
   --- a/<target>
   +++ b/<target>
   @@ -1,N +1,M @@
   -<old line 1>
   -<old line 2>
   ...
   +<new line 1>
   +<new line 2>
   ...

   # 0-byte existing file (len(old_lines)=0)
   --- a/<target>
   +++ b/<target>
   @@ -0,0 +1,M @@
   +<new line 1>
   ...

   # file absent: /dev/null create hunk
   --- /dev/null
   +++ b/<target>
   @@ -0,0 +1,M @@
   +<new line 1>
   ...
   ```

3. **Write the patch text to `workspace/tmp/<agent_run_id>/guarded_patch_input.txt`**: use the `Write` tool (because the literal path is under `allowed_tmp_root`, it passes `output_manifest_write_guard`. A Bash heredoc redirect to a `.json` / `.txt` is forbidden in this file's NG examples (see below), so the `Write` tool is the canonical path).
4. **Run guarded-apply-patch**:

   ```bash
   python3 tools/orchestration_runtime.py guarded-apply-patch \
     --repo-root . \
     --orchestration-id <orchestration_id> \
     --actor-role <substep|step> \
     --agent-run-id <agent_run_id> \
     --paths-json '["workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml"]' \
     --patch-file workspace/tmp/<agent_run_id>/guarded_patch_input.txt \
     --capability-token <capability_token>
   ```

5. **race window retry**: if the command fails, to absorb the race between `os.path.exists()` and `git apply`, rebuild the reverse patch form (update ↔ create) with the `Write` tool and run `guarded-apply-patch` again. Judge failure by the Bash exit code (`echo $?` or an error string contained in the output). Retry at most once. Concurrent writes to the same output path do not occur by orchestration design, but absorb the case where an empty file from a previous attempt remains during retry/repair.

**Forbidden alternative patterns:**
- The form of building a patch with `python3 -c "..."` / `python3 - <<'EOF'` and running subprocess.run → **unconditionally blocked** by `forbid_python_inline_write` (the regex `python3?\s+-\s*<<` inside `tools/hooks/common.py:_validate_workflow_bash_policy` in workflow mode).
- A shell var assignment `VAR=$(...)` + command substitution → breaks the `Bash(python3 ...)` allowlist match and requires session approval.
- Writing out a patch with `tee` / `cat <<EOF >file` etc. → blocked by `output_manifest_write_guard` or `enforce_guarded_apply_patch`.

**Forbidden patterns (NG — the hook blocks):**

```bash
# NG: a path without the workspace/ prefix
echo "$CONTENT" > ir/spec.ir.yaml

# NG: an inline file write via python3 -c (intent_detected=write)
python3 -c "import json; open('workspace/ir/.../spec.ir.yaml','w').write(json.dumps({}))"

# NG: a JSON read via python3 -c (intent_detected=json_read) — use the Read tool or jq
python3 -c "import json; print(json.load(open('workspace/orchestrations/<oid>/output_manifests/<id>.json'))['allowed_tmp_root'])"

# NG: UUID generation via python3 -c (intent_detected=uuid) — use python3 tools/new_agent_run_id.py
python3 -c "import uuid; print(uuid.uuid4())"

# NG: a heredoc redirect (direct file specification)
cat <<EOF > workspace/ir/.../spec.ir.yaml
{"key": "value"}
EOF

# NG: even under workspace/tmp/<agent_run_id>/, a heredoc redirect to a .json/.txt output
# path is forbidden (the hook cannot interpret the file_path and mis-detects '\"' as a path and blocks).
# In addition, a shell var assignment like TMPFILE=$(mktemp ...) breaks the allowlist match
# and requires session approval, so do not use it.
cat > workspace/tmp/<agent_run_id>/work.json << 'EOF'
{"key": "value"}
EOF
# → write the patch text to workspace/tmp/<arid>/guarded_patch_input.txt with the Write tool, then
# pass it to guarded-apply-patch --patch-file (see the "`.json` artifact write — `guarded-apply-patch` usage procedure" section of this file)
```

**Important:** for `.json` / `.txt` output, **forbid all means other than** the `guarded-apply-patch` procedure of the "`.json` artifact write — `guarded-apply-patch` usage procedure" section of this file. After writing the patch text to `workspace/tmp/<agent_run_id>/guarded_patch_input.txt` with the `Write` tool, apply it via `guarded-apply-patch --patch-file`.

**Important:** the `+++ b/` path of `--paths-json` and `--patch-text` must both be a project-root-relative path starting with `workspace/`. `plans/...` (without the `workspace/` prefix) or an absolute path is blocked by `output_manifest_write_guard`.

---

#### About the strip of `guarded-apply-patch`

`guarded-apply-patch` has no CLI argument `--strip`. Using the `changed_paths` passed via `--paths-json` as an oracle, it internally tries `git apply --check` in the order `-p1` → `-p0`, and automatically selects a strip that can cover all `changed_paths`. The agent need not specify the strip.

**Response when the error `cannot determine patch strip level` appears:**

1. Reconcile the path of `--paths-json` with the prefix of the patch header (`+++ b/...`).
   - When applied with strip=0 (-p0): `--- workspace/foo/bar.json` + `+++ workspace/foo/bar.json` → the changed_path is `workspace/foo/bar.json`
   - When applied with strip=1 (-p1): `--- a/workspace/foo/bar.json` + `+++ b/workspace/foo/bar.json` → the changed_path is `workspace/foo/bar.json`
2. Confirm that no extra `/` or relative-path symbol (`./`) is mixed into the path before or after.
3. For a new-file creation, use the `--- /dev/null` / `+++ b/<path>` form.

Do not attempt to confirm this logic by grepping `tools/orchestration_runtime.py` (it is blocked by `forbid_tools_direct_read`). The legitimate references are this paragraph and `docs/ORCHESTRATION.md#patch-application-contract`.

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

It canonicalizes, per `(step, substep)`, the `validate_pipeline_semantics --stage <X>` invocation the `orchestration agent` may state/enumerate in the launch prompt body. Do not state in the launch prompt a `--stage` other than the one permitted in the "allowed_stage" column of the table below. The recurrence-prevention plan (Issue 1) is the canonical source.

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

**Distinction from the recording layer:** line 116 of `skills/workflow-orchestration/SKILL.md` defines the values that **may be recorded** in `step_result.json#validation_stage` as a broader per-step set (including `full`), and this is the recording-layer contract at write-step-result time. This table is the invocation-layer contract at launch-prompt time, and imposes a stricter per-substep constraint than the recording layer. They are contracts of different layers, and a `validation_stage` value recorded as a result of being narrowed per-substep by this table is automatically included in the allowed set of SKILL.md line 116 (e.g. only `compile` is executable for `compile/verify` → a subset of the SKILL.md `compile`/`full` set).

**negative constraint:** do not state in the launch prompt of this `(step, substep)` a `validate_pipeline_semantics` call with a `--stage` not permitted in the table above. Example: including `validate_pipeline_semantics --stage compile` in the `Compile.generate` prompt invades the `Compile.verify` responsibility and fires `noncanonical_phase_write_attempt`. A mere mention of an MCP tool name (`compile_project` etc.) (in explanatory text, a negative constraint, etc.) is outside the scope of this lint.

**negative constraint (MCP write tool):** do not state in the `generate/verify` launch prompt the execution of a `build-runtime` MCP write tool such as `run_linter`. lint is the `generate.generate` responsibility (`docs/workflow/phases/phase_02_generate.md` 2-1), and execution in verify induces a write to `mcp_command_log.jsonl` that verify's `allowed_output_paths` does not authorize and invites an `unauthorized_write_violation` → `fail_closed`. This constraint targets only the `build-runtime` MCP write tool, and the read-only `validate_workspace_root.py` and `validate_pipeline_semantics --stage post_generate` (table above) remain permitted.

`record-launch`, inside `_validate_launch_prompt_text`, reconciles the text of `launch_prompt_ref` against the per-(step, substep) allowed-stage set. It scans only actionable invocation lines (lines containing `python3` / `tools/validate_pipeline_semantics.py` / `--gate validate_pipeline_semantics`), extracts both the direct CLI form and the canonical run-gate JSON form (`--args-json '{"stage": "..."}'`), and rejects with a `ValueError` if it is outside the allowed-stage (`tools/orchestration_runtime.py::_lint_launch_prompt_gate_allowlist` and `ALLOWED_VALIDATE_PIPELINE_STAGES` are the canonical implementation). For an emergency rollback, the lint can be disabled with the env `METDSL_ENFORCE_GATE_ALLOWLIST=0` (default is enabled).

#### Additional contract on `repair_strategy=reuse`

A re-submission with `repair_strategy=reuse` inherits the `apply_patch_writes` evidence of `record-agent-run` from `repair_target_agent_run_id` (the repair / retry section of `docs/ORCHESTRATION.md` is the canonical source). The `guarded-apply-patch`-related constraint lines of the launch prompt body may be kept as-is — if the child actually re-writes the same path, it goes through the gate as usual, and if it writes nothing, the inherited evidence satisfies the coverage. The reliability of the inheritance is ensured by the runtime-side same-identity verification (`(node_key, step, substep)` match).

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

The write of a `.json` / `.txt` patch file is canonically via the `Write` tool (see the "`.json` artifact write — `guarded-apply-patch` usage procedure" section of this file).

`<agent_run_id>` is literally substituted in the corresponding field of the launch prompt. The `$TMPDIR` env is inherited into the subprocess by `tools/run_workflow.py`, so a `${TMPDIR}/...`-form reference also works as a result, but to minimize env dependence the literal path is canonical. `/tmp/`, `/dev/shm/`, and an argument-less `$(mktemp)` remain blocked by `output_manifest_write_guard`.
