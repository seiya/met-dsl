# Agent Contract (child step / substep agents)

> **Audience: every `step agent` / `substep agent`.** This is the canonical, child-readable agent contract. Your launch prompt references this file instead of inlining the full contract, so **Read this file once immediately after launch** and apply every rule below. It is under `docs/`, which is in your `read_manifest`, so reading it is allowed (unlike `skills/workflow-orchestration/references/*`, which remains blocked by `read_manifest_read_guard`).
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
- Use `workspace/orchestrations/<orchestration_id>/capabilities/<agent_run_id>.json` as the canonical source for `capability_token`; immediately after launch, read that file, extract `capability_token`, and pass it to subsequent `run-gate` / `guarded-apply-patch`.
- If `capability_token` is not obtained or mismatched: do not start processing and stop with fail.
- `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` and `workspace/orchestrations/<orchestration_id>/read_manifests/<agent_run_id>.json` may be read directly with the `Read` tool (`run-gate` not needed).
- For an `orchestration-read` of a path other than those 2 files, run `python3 tools/orchestration_runtime.py run-gate --gate orchestration_read --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '{"read_path":"..."}'` as the only path, and forbid calling `orchestration-read` directly.
- `orchestration-read` uses `read_manifests/<agent_run_id>.json` as the canonical source, and must not read a path outside the manifest.
- Run the child only inside the `bwrap` sandbox. Forbid non-sandbox execution.
- Branch the write path by the output path's extension. Reference `output_manifests/<agent_run_id>.json` as the canonical source, and immediately after launch confirm both the `allowed_output_paths` and `allowed_file_tool_paths` lists.
- For `.json` and `.txt` output, run `python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root <repo_root> --orchestration-id <orchestration_id> --actor-role <actor_role> --agent-run-id <agent_run_id> --paths-json '["..."]' --patch-text '<patch_text>' --capability-token <capability_token>` as the only path, and stop editing on rejection.
- For output other than the above such as `.yaml` / `.yml` / `.md` and source code, write directly with the `Edit` / `Write` tool only to a path enumerated in the `allowed_file_tool_paths` of `output_manifests/<agent_run_id>.json`.
- **Caveat for `generate.generate` canonical source (`<pipeline_ref>/source/<source_id>/src/*.f90` `*.c` etc.):** a bare `src/` directory entry in `allowed_output_paths` auto-derives authorization for **`guarded-apply-patch` only**, not for the `Edit` / `Write` tool. Unless the concrete source path is **explicitly enumerated in `allowed_file_tool_paths`**, an `Edit` / `Write` to it is blocked by `enforce_guarded_apply_patch`. Therefore write generated source files via `guarded-apply-patch` (use a `--patch-text` create-form for a new file). The extension-less `Makefile` is the exception that `record-launch` auto-injects into both lists. Confirm `allowed_file_tool_paths` immediately after launch before choosing the write means.
- The use of `run-gate --gate apply_patch_writes` and `apply-patch-gate` as a public path, a file write via shell redirection / `tee` / `sed -i` / an arbitrary command, and a write outside `allowed_output_paths` remain forbidden.
- Both `guarded-apply-patch` and `Edit` / `Write` reference `output_manifests/<agent_run_id>.json` and reject a path outside the manifest. Do not write to a path outside the manifest.
- When a temporary file is needed, do not specify `/tmp` / `/dev/shm` directly; directly specify the literal path of `allowed_tmp_root` (`workspace/tmp/<agent_run_id>/...`). Switch the write means by extension: **(a)** for `.py` / `.yaml` / `.sh` etc. (non-`.json` / `.txt`), a Bash heredoc is OK (e.g. `cat > workspace/tmp/<agent_run_id>/work.py <<'EOF'`); **(b)** for `.json` / `.txt`, use the `Write` tool (a Bash heredoc redirect is in the NG examples below: the hook's file_path parser may, in some quoted forms, mis-detect `'\"'` as a path and block it). `output_manifest_write_guard` judges only whether the write-target path is under `allowed_tmp_root` and does not reference the `$TMPDIR` env (`tools/hooks/common.py:_validate_write_access`). Bootstrap Bash such as `export TMPDIR=...`, `jq -er ...`, `printenv`, `bash -c '...'` is forbidden (the workflow stops on a session-sandbox approval request). Hard-coding `/tmp/` / `/dev/shm/` remains blocked by `output_manifest_write_guard`.
- The internal gate files under `gates/<agent_run_id>/` (`apply_patch_writes.json` etc.) must not be read directly even if they correspond to your own `agent_run_id`. Use the **stderr of `run-gate` / `guarded-apply-patch`** as the canonical path for obtaining the gate execution result, and save and reference it with `2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` (e.g. `python3 tools/orchestration_runtime.py run-gate --gate ... 2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt`). Literally substitute your own value for the `<agent_run_id>` part. On failure, `violations[]` is output to stderr in JSON form. For a summary, refer to `agent.summary.txt` / `step_result.json`. Another agent's internal artifact (files under `capabilities/` / `output_manifests/` / `read_manifests/` / `access_logs/` / `agents/<other_agent_run_id>/` / `dialogs/` that do not correspond to your own `agent_run_id`) must likewise not be read directly. A cross-agent read fires `rule_source_violation` and fails the phase.
- Do not write to a file (`open(path, 'w'/'a'/'x')` / `Path.write_text` / `shutil.copy*` etc.) with `python3 -c "..."` or `python3 - <<'EOF'`. It is blocked by `forbid_python_inline_write`. Use `guarded-apply-patch` for `.json`/`.txt`, and the `Edit`/`Write` tool for others.
- Use the `Read` tool to confirm the content of a JSON file. `python3 -c "import json; ..."` is blocked by `forbid_python_inline_write`. Only when Python processing is truly necessary, write a script to `workspace/tmp/<agent_run_id>/x.py` and run it with `python3 workspace/tmp/<agent_run_id>/x.py` (`<agent_run_id>` is literally substituted).
- For UUID generation, use `python3 tools/new_agent_run_id.py`. `python3 -c 'import uuid; print(uuid.uuid4())'`, including with flags such as `-S`, is all blocked by `forbid_python_inline_write`. Do not use `cat /proc/sys/kernel/random/uuid` because it stops on a session-sandbox approval request every time.
- Your own launch prompt body is passed as input at Agent tool launch. There is no need to re-read `launches/<agent_run_id>.prompt.txt` with `Read`, and it is blocked by `read_manifest_read_guard`.
- During workflow execution, a `Read`/`grep`/`sed`/`cat` of `tools/` / `tests/` / a validator script / a hook implementation is blocked by `forbid_tools_direct_read`. Interpret the requirements and judgment rules only from `docs/` / `spec/` / `skill_must_read_refs`. For the internal behavior of `guarded-apply-patch` (strip decision etc.), the canonical references are "Patch application contract" of `docs/ORCHESTRATION.md` and the strip note below.
- When referencing an artifact you generated, use the relative path from the project root enumerated in the `allowed_output_paths` of `output_manifests/<agent_run_id>.json` (e.g. `workspace/ir/...`). Do not use an absolute path such as `/home/<user>/...` or a path without the `workspace/` prefix.
- Use `capabilities/<agent_run_id>.json` as the canonical source for orchestration metadata such as `orchestration_id` / `agent_run_id` / `node_key` / `step` / `write_roots`. `orchestration_meta.json` is blocked by `read_manifest_read_guard`.
- If `skill_name` and `skill_ref` are unspecified, stop with fail. Read only the single file `skill_ref` specified in the launch prompt, and **do not additionally Read a SKILL.md of any phase other than your own**.
- **Do not `Read` anything under `skills/workflow-orchestration/references/` (including `startup_contract.md` and `launch_prompts.md`).** These are orchestration-agent-only documents; they are not in the `read_manifest` and are blocked fail-closed by `read_manifest_read_guard`. The required contract is this file (`docs/AGENT_CONTRACT.md`), your launch prompt, `docs/`, and the `skill_ref` / `skill_must_read_refs` passed at launch; resolve all requirements from those sources only.
- On input shortage, do not complete by guessing; stop with fail.
- With `workflow_mode=dev`, stop with fail the moment `issue_severity=major|critical` is detected in a verify-family judgment.
- When it fails with `workflow_mode=dev`, include in the reply the basis needed to generate `failure_analysis.json` (the failure reason, related output_refs, a summary of the main logs).

## `.json` / `.txt` artifact write — `guarded-apply-patch` usage procedure

`.json` / `.txt` output must always be done by the following procedure. All means that involve a file write (heredoc redirect / `tee` / a file write via `python3 -c` / `echo > file` etc.) are forbidden. Assembling the patch text into a variable is permitted.

**Procedure (the correct pattern — create-or-overwrite with retry):**

Because `guarded-apply-patch` internally uses `git apply`, the patch form differs depending on whether the file exists. Because there is a race window between `os.path.exists()` and the patch application, on a patch failure retry once with the reverse form. Concurrent writes to the same output path do not occur by orchestration design, but absorb the case where an empty file from a previous attempt remains during retry/repair.

**Procedure (Bash + Write tool based; `python3 -c` / `python3 - <<EOF` is blocked by `forbid_python_inline_write` so it is not usable):**

1. **Confirm the target file's state**: read the target (e.g. `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml`) with the `Read` tool (if it does not exist, `Read` returns an error, in which case assemble a `/dev/null` create-form patch). If it exists, control its line count (`len(old_lines)`).
2. **Assemble the patch text**: there are 3 forms — existing / new / empty file — and all can be built by simple string concatenation. Template (`<old_lines>` / `<new_lines>` are substituted by the agent with literal values):

   ```text
   # update / replace form (existing file, len(old_lines)=N>0, len(new_lines)=M)
   --- a/<target>
   +++ b/<target>
   @@ -1,N +1,M @@
   -<old line 1>
   ...
   +<new line 1>
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

3. **Write the patch text to `workspace/tmp/<agent_run_id>/guarded_patch_input.txt`**: use the `Write` tool (because the literal path is under `allowed_tmp_root`, it passes `output_manifest_write_guard`. A Bash heredoc redirect to a `.json` / `.txt` is forbidden in the NG examples below, so the `Write` tool is the canonical path).
4. **Run guarded-apply-patch**:

   ```bash
   python3 tools/orchestration_runtime.py guarded-apply-patch \
     --repo-root . \
     --orchestration-id <orchestration_id> \
     --actor-role <actor_role> \
     --agent-run-id <agent_run_id> \
     --paths-json '["workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml"]' \
     --patch-file workspace/tmp/<agent_run_id>/guarded_patch_input.txt \
     --capability-token <capability_token>
   ```

5. **race window retry**: if the command fails, to absorb the race between `os.path.exists()` and `git apply`, rebuild the reverse patch form (update ↔ create) with the `Write` tool and run `guarded-apply-patch` again. Judge failure by the Bash exit code (`echo $?` or an error string contained in the output). Retry at most once.

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
python3 -c "import json; print(json.load(open('.../x.json'))['k'])"

# NG: UUID generation via python3 -c (intent_detected=uuid) — use python3 tools/new_agent_run_id.py
python3 -c "import uuid; print(uuid.uuid4())"

# NG: a heredoc redirect (direct file specification)
cat <<EOF > workspace/ir/.../spec.ir.yaml
{"key": "value"}
EOF

# NG: even under workspace/tmp/<agent_run_id>/, a heredoc redirect to a .json/.txt output
# path is forbidden (the hook cannot interpret the file_path and mis-detects '\"' as a path and blocks).
cat > workspace/tmp/<agent_run_id>/work.json << 'EOF'
{"key": "value"}
EOF
```

**Important:** for `.json` / `.txt` output, **forbid all means other than** this `guarded-apply-patch` procedure. After writing the patch text to `workspace/tmp/<agent_run_id>/guarded_patch_input.txt` with the `Write` tool, apply it via `guarded-apply-patch --patch-file`. The `+++ b/` path of `--paths-json` and `--patch-text` must both be a project-root-relative path starting with `workspace/`; `plans/...` (without the `workspace/` prefix) or an absolute path is blocked by `output_manifest_write_guard`.

### About the strip of `guarded-apply-patch`

`guarded-apply-patch` has no CLI argument `--strip`. Using the `changed_paths` passed via `--paths-json` as an oracle, it internally tries `git apply --check` in the order `-p1` → `-p0`, and automatically selects a strip that can cover all `changed_paths`. The agent need not specify the strip. On `cannot determine patch strip level`: reconcile the `--paths-json` path with the patch header (`+++ b/...`) prefix, ensure no extra `/` or `./` is mixed in, and for a new file use the `--- /dev/null` / `+++ b/<path>` form. The canonical references are this paragraph and `docs/ORCHESTRATION.md#patch-application-contract`.
