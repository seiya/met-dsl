# Agent Contract (child step / substep agents)

> **Audience: every `step agent` / `substep agent`.** This is the canonical, child-readable agent contract. Your launch prompt references this file instead of inlining the full contract, so **Read this file once immediately after launch** and apply every rule below. It is under `docs/`, which is in your `read_manifest`, so reading it is allowed (unlike the raw templates under `tools/prompt_templates/`, which are not in your `read_manifest`).
>
> **Parameter substitution.** This file is static, so it refers to your identifiers generically. Substitute the concrete values from **your launch prompt header** and from `capabilities/<agent_run_id>.json`:
> - `<agent_run_id>` → your own `agent_run_id` (launch prompt header).
> - `<orchestration_id>` → your own `orchestration_id` (launch prompt header).
> - `<actor_role>` → your role: `step` or `substep` (the launch prompt states which template you are).
> - `<repo_root>` → the project root (use `.`).
> - `<capability_token>` → read from `capabilities/<agent_run_id>.json` immediately after launch.

## Common agent contract

- Immediately after launch, read `skill_ref` and execute with a contract not contradicting `skill_must_read_refs`.
- Interpret the requirement definition and judgment rules only from `docs/`, `spec/`, and the relevant trial's artifacts included in `skill_must_read_refs`. Do not extract rules from the implementation under `tools/`, verification `script`, test code, or validator code.
- Use `workspace/orchestrations/<orchestration_id>/capabilities/<agent_run_id>.json` as the canonical source for `capability_token`; immediately after launch, read that file, extract `capability_token`, and pass it to a subsequent `run-gate` (e.g. `orchestration_read`).
- If `capability_token` is not obtained or mismatched: do not start processing and stop with fail.
- `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` and `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` may be read directly with the `Read` tool (`run-gate` not needed).
- **Any path inside your `read_manifests/<agent_run_id>.json` `allowed_read_roots` is read directly with the `Read` tool** — those roots are read-only bound into your sandbox and the read guard authorizes them, so no `run-gate orchestration_read` round-trip is needed (the per-read `access_logs` line is best-effort observability, not an authorization step).
- Use `run-gate orchestration_read` **only** for the exceptional read of a path NOT under your `allowed_read_roots`: `python3 tools/orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '{"read_path":"..."}'`. It uses `read_manifests/<agent_run_id>.json` as canonical and must not read outside the manifest; forbid calling `orchestration-read` directly.
- Run the child only inside the `bwrap` sandbox. Forbid non-sandbox execution.
- **Write every output artifact directly with the `Edit` / `Write` tool**, to a path enumerated in the `allowed_file_tool_paths` of `output_manifests/<agent_run_id>.json`. This is uniform across extensions: managed JSON (`*_meta.json`, `verdict.json`, `aggregate_verdict.json`, `summary.json`, `diagnostics.json`, `perf.json`, `quality_check.json`, the snapshot `*.json`, …), source code (`*.f90` / `*.c` / `Makefile`), and `.yaml` / `.yml` / `.md` are all written this way. Immediately after launch confirm both the `allowed_output_paths` and `allowed_file_tool_paths` lists. You run under a mandatory `bwrap` sandbox whose write scope is exactly your `write_roots`; a write that stays inside `write_roots` is authorized by filesystem-diff containment at terminalization — there is no separate "gate evidence" step to perform.
- The MCP-owned audit logs (`command_log.jsonl`) are written **only** by the `build-runtime` MCP server as a side effect of running its tools; they appear in `allowed_output_paths` but **not** in `allowed_file_tool_paths`, and you must never write them with a file tool. The pipeline `lineage.json` is likewise NOT yours — it sits at the pipeline root (outside your write_roots) and the conductor authors it host-side. Everything else in `allowed_output_paths` is also in `allowed_file_tool_paths`.
- Write managed JSON directly with the `Write` / `Edit` tool to a path in `allowed_file_tool_paths`. (`guarded-apply-patch` and the `apply_patch_writes` gate have been removed.)
- A file write via shell redirection / `tee` / `sed -i` / an arbitrary command, and any write outside `allowed_output_paths`, remain forbidden.
- The `Edit` / `Write` tools reference `output_manifests/<agent_run_id>.json` and reject a path outside the manifest. Do not write to a path outside the manifest.
- When a temporary file is needed, do not specify `/tmp` / `/dev/shm` directly; directly specify the literal path of `allowed_tmp_root` (`workspace/tmp/<agent_run_id>/...`). For `.py` / `.yaml` / `.sh` etc. scratch a Bash heredoc is OK (e.g. `cat > workspace/tmp/<agent_run_id>/work.py <<'EOF'`); for a `.json` / `.txt` scratch file use the `Write` tool (a Bash heredoc redirect to a `.json` / `.txt` is in the NG examples below: the hook's file_path parser may, in some quoted forms, mis-detect `'\"'` as a path and block it). `output_manifest_write_guard` judges only whether the write-target path is under `allowed_tmp_root` and does not reference the `$TMPDIR` env (`tools/hooks/common.py:_validate_write_access`). Bootstrap Bash such as `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'` is forbidden (the workflow stops on a session-sandbox approval request). Hard-coding `/tmp/` / `/dev/shm/` remains blocked by `output_manifest_write_guard`.
- For a `run-gate` (e.g. `orchestration_read`) result, use its **stderr** as the canonical path and save it with `2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` (e.g. `python3 tools/orchestration_runtime.py run-gate --gate ... 2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt`). Literally substitute your own value for the `<agent_run_id>` part. On failure, `violations[]` is output to stderr in JSON form. For a summary, refer to `agent.summary.txt` / `step_result.json`. Another agent's internal artifact (files under `capabilities/` / `output_manifests/` / `read_manifests/` / `access_logs/` / `agents/<other_agent_run_id>/` / `dialogs/` that do not correspond to your own `agent_run_id`) must not be read directly. A cross-agent read fires `rule_source_violation` and fails the phase.
- **Sandbox facts (do not re-derive these — they are fixed):** Your **launch prompt's Gate Runbook already lists the exact, fully-resolved gate command(s) for your `(step, substep)`** with every path/id/scoping substituted; run them verbatim rather than reconstructing the arguments. (1) Your **cwd is the repo root** (`.`) and persists across `Bash` calls — **do not `cd`**; a `cd` breaks the workspace-relative paths every gate and write guard expects (use repo-root-relative paths in every command). (2) `workspace/tmp/<agent_run_id>/` is **pre-created and writable** before you launch — write/redirect there directly; never test for or `mkdir` it. (3) `access_logs/<agent_run_id>.jsonl` is **runtime-managed audit**: a `run-gate --gate orchestration_read` call writes it for you and a successful gate returns `result: ok` (you never read or write that file yourself) — do not treat the gate's audit append as something you must handle.
- Do not write to a file (`open(path, 'w'/'a'/'x')` / `Path.write_text` / `shutil.copy*` etc.) with `python3 -c "..."` or `python3 - <<'EOF'`. It is blocked by `forbid_python_inline_write`. Use the `Edit` / `Write` tool for all artifact writes (any extension).
- Use the `Read` tool to confirm the content of a JSON file. `python3 -c "import json; ..."` is blocked by `forbid_python_inline_write`. Only when Python processing is truly necessary, write a script to `workspace/tmp/<agent_run_id>/x.py` and run it with `python3 workspace/tmp/<agent_run_id>/x.py` (`<agent_run_id>` is literally substituted).
- For UUID generation, use `python3 tools/new_agent_run_id.py`. `python3 -c 'import uuid; print(uuid.uuid4())'`, including with flags such as `-S`, is all blocked by `forbid_python_inline_write`. Do not use `cat /proc/sys/kernel/random/uuid` because it stops on a session-sandbox approval request every time.
- Your own launch prompt body is passed as input at launch. There is no need to re-read `launches/<agent_run_id>.prompt.txt` with `Read`, and it is blocked by `read_manifest_read_guard`.
- During workflow execution, a `Read`/`grep`/`sed`/`cat` of `tools/` / `tests/` / a validator script / a hook implementation is blocked by `forbid_tools_direct_read`. Interpret the requirements and judgment rules only from `docs/` / `spec/` / `skill_must_read_refs`.
- When referencing an artifact you generated, use the relative path from the project root enumerated in the `allowed_output_paths` of `output_manifests/<agent_run_id>.json` (e.g. `workspace/ir/...`). Do not use an absolute path such as `/home/<user>/...` or a path without the `workspace/` prefix.
- Use `capabilities/<agent_run_id>.json` as the canonical source for orchestration metadata such as `orchestration_id` / `agent_run_id` / `node_key` / `step` / `write_roots`. `orchestration_meta.json` is blocked by `read_manifest_read_guard`.
- If `skill_name` and `skill_ref` are unspecified, stop with fail. Read only the single file `skill_ref` specified in the launch prompt, and **do not additionally Read a SKILL.md of any phase other than your own**.
- **Do not `Read` the raw launch-prompt templates under `tools/prompt_templates/`.** Your prompt is already rendered and passed to you; `tools/` is not in your `read_manifest` (`forbid_tools_direct_read`). The required contract is this file (`docs/AGENT_CONTRACT.md`), your launch prompt, `docs/`, and the `skill_ref` / `skill_must_read_refs` passed at launch; resolve all requirements from those sources only.
- On input shortage, do not complete by guessing; stop with fail.
- A verify-family finding always sets `verification_status=fail` (record `issue_severity` accurately); `minor` is not tolerated/passed. With `workflow_mode=dev`, a `major|critical` verify finding stops with fail immediately (the conductor warm-repairs `minor`).
- When it fails with `workflow_mode=dev`, include in the reply the basis needed to generate `failure_analysis.json` (the failure reason, related output_refs, a summary of the main logs).

## Workflow behavioral invariants (every leaf)

These are the workflow common norms a leaf must obey while authoring/verifying artifacts. (Canonical for the full orchestration-level invariant set — preflight, agent launch, completion criteria, ID minting, artifact layout — remains `docs/workflow/WORKFLOW_CORE.md`; it is readable under `docs/` but is **not** a leaf must-read. The subset below is what constrains a leaf's own output.)

- Forbid `dummy` output to pass `tests` or advance the workflow. When a phase input is insufficient, stop with `fail`; never complete by guessing.
- Generate `diagnostics.json` / `perf.json` only as the real execution result of the target `runner`. Forbid hand-writing, fixed-value embedding, and external post-editing. `verdict.json` / `aggregate_verdict.json` must be derived from `tests.md` and the same-`run_id` execution artifacts.
- On a phase failure, do not artificially generate an artifact to satisfy a downstream phase's start condition.
- Without explicit instruction, do not view or reference the content of existing/past workflow output (past `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id`). Use as input only the repository-managed `spec` canonical source and the preceding artifacts of the relevant trial.
- Interpret the requirement definition, output format, and judgment conditions only from `controlled_spec.md` / `tests.md` / `deps.yaml` / `spec.ir.yaml` and the `docs/` canonical sources in your `skill_must_read_refs`. Do not back-derive a requirement from the implementation under `tools/`, verification scripts, test code, or validator code; when a requirement is insufficient, stop with `fail`.
- The storage root for all workflow artifacts is `workspace/` only; never write outside `workspace/`. Create `workspace/` directly under the repo root if absent.

## Stage meta keys (`<stage>_meta.json`)

Every LLM substep produces its phase's `<stage>_meta.json` (`ir_meta.json` for Compile, `source_meta.json` for Generate, `validate_meta.json` for Validate) as a required output.

- Common required keys: `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, `context_isolated`. When `context_isolated=false`, `constraint_reason` is required.
- `source_meta.json`: does not record lint — `static lint` is the conductor-run `Generate.lint` substep, certified by `post_generate` against the host-authored `lint_evidence/<source_id>.json`.
- `validate_meta.json`: additionally requires the LLM semantic-check evidence in `judge_command_ref` **only when** `verification_status=pass`.
- With `debug_mode=false`, do not save failed-attempt artifacts. `verification_status` presumes `fail_closed`: unperformed/unjudgeable verification must never be recorded `pass`.
- **verify-family substeps (`compile.verify` / `generate.verify`):** a `pass` must (re-)author the verified meta with the `Write`/`Edit` tool even when the inspection finds nothing to change (refresh an idempotent field such as `verify_attempts`). An inspect-only verify that writes nothing cannot terminate `pass`.

## MCP `command_log.jsonl` placement

The MCP `build-runtime` server writes `command_log.jsonl` itself as a side effect of `run_linter` / `compile_project` / `run_program` / `run_quality_checks`. It appears in your `allowed_output_paths` at the canonical per-phase path (already enumerated in your manifest / task card) but **not** in `allowed_file_tool_paths` — never write it with a file tool. (Full per-phase placement reference: `docs/workflow/MCP_COMMAND_LOG_PLACEMENT.md`, readable under `docs/` but not a leaf must-read.)

## Artifact write — direct `Write` / `Edit` tool procedure

Every output artifact — managed JSON (`*_meta.json`, `verdict.json`, …), source code, `.yaml` / `.md` — is written **directly with the `Write` / `Edit` tool** to a path listed in `allowed_file_tool_paths`. You run inside a mandatory `bwrap` sandbox whose only writable paths are your `write_roots`; a `Write` to an `allowed_file_tool_paths` entry is authorized by filesystem-diff containment at terminalization (a change that lands inside `write_roots` is your own confined output). There is no patch step and no separate gate evidence to record. (The pipeline `lineage.json` is not in your `allowed_file_tool_paths` — the conductor authors it host-side.)

**Procedure:**

1. To overwrite an existing file, optionally `Read` it first to compute the new content. The `Write` tool works whether or not the file already exists, for any path in `allowed_file_tool_paths`.
2. Call the `Write` tool with `file_path` = the project-root-relative output path (must start with `workspace/` and appear in `allowed_file_tool_paths`) and `content` = the full file content. For a partial change to an existing file, the `Edit` tool is also permitted.
3. Confirm with the `Read` tool if needed (`python3 -c "import json; ..."` is blocked by `forbid_python_inline_write`).

**Forbidden write means:**
- A file write via `python3 -c "..."` / `python3 - <<'EOF'` and `subprocess.run` → **unconditionally blocked** by `forbid_python_inline_write` (the regex `python3?\s+-\s*<<` inside `tools/hooks/common.py:_validate_workflow_bash_policy` in workflow mode).
- A heredoc redirect / `tee` / `cat <<EOF >file` / `echo "..." > file` / `sed -i` to an output path → blocked by `output_manifest_write_guard`.
- A shell var assignment `VAR=$(...)` + command substitution → breaks the `Bash(...)` allowlist match and requires session approval.
- Any write to a path not in `allowed_output_paths` / `allowed_file_tool_paths`, a path without the `workspace/` prefix, or an absolute path → blocked by `output_manifest_write_guard`.

**Forbidden patterns (NG — the hook blocks):**

```bash
# NG: a path without the workspace/ prefix
echo "$CONTENT" > ir/spec.ir.yaml

# NG: an inline file write via python3 -c (intent_detected=write) — use the Write tool
python3 -c "import json; open('workspace/ir/.../ir_meta.json','w').write(json.dumps({}))"

# NG: a JSON read via python3 -c (intent_detected=json_read) — use the Read tool or jq
python3 -c "import json; print(json.load(open('.../x.json'))['k'])"

# NG: UUID generation via python3 -c (intent_detected=uuid) — use python3 tools/new_agent_run_id.py
python3 -c "import uuid; print(uuid.uuid4())"

# NG: a heredoc redirect to an output path (direct file specification) — use the Write tool
cat <<EOF > workspace/ir/.../ir_meta.json
{"key": "value"}
EOF
```

> **Removed:** `guarded-apply-patch` and the `apply_patch_writes` gate have been removed. Write every artifact directly with the `Write` / `Edit` tool; under mandatory `bwrap` the write is authorized at terminalization by `write_roots`-containment of the FS-diff, with no gate evidence.
