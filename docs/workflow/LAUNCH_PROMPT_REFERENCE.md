# Launch Prompt Reference

Human-facing reference for the conductor's launch-prompt render path. The **machine-parsed
templates** (the `step agent` / `substep agent` / common-boilerplate bodies that `record-launch`
renders) live as plain-text files under [`tools/prompt_templates/`](../../tools/prompt_templates/)
(`step_agent.txt`, `substep_agent.txt`, `common_boilerplate.txt`) — this document holds only the
non-parsed reference material (the correspondence tables and the `repair_strategy` / `allowed_tmp_root`
contracts) that used to be co-located with those templates.

The render path itself: the conductor supplies the launch-request parameters; `record-launch` renders
the prompt from `tools/prompt_templates/` (substituting the `<...>` placeholders and expanding
`{{COMMON_BOILERPLATE}}` / `{{ACTOR_ROLE}}`) and returns the rendered `launch_prompt_text`, which the
conductor passes to the leaf subprocess. `tools/workflow_conductor.py:build_launch_request` reproduces
the same payload field-for-field. A `step agent` / `substep agent` reads its already-rendered prompt at
launch and must **not** `Read` the raw templates under `tools/prompt_templates/` (blocked by
`forbid_tools_direct_read` and absent from every leaf's `read_manifest`).

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
| build | — | _(conductor in-process; no skill, deterministic launch prompt)_ | — |
| execute | — | _(conductor in-process; no skill, deterministic launch prompt)_ | — |
| judge | — | workflow-validate-judge | skills/workflow-validate-judge/SKILL.md |
| promote | — | workflow-promote | skills/workflow-promote/SKILL.md |

`skills/workflow-escalate/SKILL.md` (the escalate/diagnostician persona) is intentionally ABSENT from this table: it is a conductor-consumed SKILL rendered host-side into the read-only diagnostician prompt (`_diagnosis_prompt`), never launched as a phase leaf via `skill_ref`.

**Negative constraint:** do not Read a SKILL.md of any phase other than your own (e.g. a generate substep reading `skills/workflow-compile-verify/SKILL.md` fires `rule_source_violation`). Read only the single file passed via the launch prompt's `skill_ref`.

---

#### substep ↔ allowed validator gate correspondence table

It canonicalizes, per `(step, substep)`, the `validate_pipeline_semantics --stage <X>` invocation that may appear in the rendered launch prompt body. Do not state in the launch prompt a `--stage` other than the one permitted in the "allowed_stage" column of the table below. The recurrence-prevention plan (Issue 1) is the canonical source.

| step | substep | allowed `validate_pipeline_semantics --stage` | note |
|---|---|---|---|
| compile | generate | (none) | gate calls are limited to `validate_workspace_root` / `check_artifact_syntax --expect-top object`. The authoritative `--stage compile` gate is the conductor's deterministic `Compile.static` substep. |
| compile | static | (none) | deterministic conductor substep; the conductor (not a leaf) runs `validate_workspace_root` + `check_artifact_syntax` + `--stage compile` in-process. |
| compile | verify | (none) | pure LLM semantic pass (spec-cross-reference invariants V1/V3/V5); the `--stage compile` gate moved to `Compile.static`, so verify launches no `validate_pipeline_semantics`. |
| generate | generate | (none) | `--stage post_generate` is the conductor's deterministic `Generate.static` substep responsibility (no leaf). |
| generate | lint | (none) | deterministic conductor substep (`run_linter`); no leaf, no `validate_pipeline_semantics`. |
| generate | static | (none) | deterministic conductor substep; the conductor (not a leaf) runs `validate_workspace_root` + `--stage post_generate` in-process. |
| generate | verify | (none) | pure LLM semantic pass; the static gates moved to `Generate.static`, so verify launches no `validate_pipeline_semantics`. |
| build | — | `post_build` | invoked after the MCP `compile_project` call. |
| validate | pre_judge | (none) | deterministic conductor substep (index 0); the dependency-DAG readiness check, authoring `pre_judge_meta.json`. No leaf, no `validate_pipeline_semantics` from a leaf. |
| validate | execute | `post_execute` | invoked for the judgment of the `run_program` / `run_quality_checks` result. |
| validate | judge | (none) | pure LLM semantic pass; the `--stage pre_judge` gate moved to the conductor's `pre_judge` / `post_judge` deterministic substeps, so the judge leaf launches no `validate_pipeline_semantics`. |
| validate | post_judge | (none) | deterministic conductor substep (index 3); runs `--stage pre_judge` after the judge returns and classifies violation severity into `post_judge_meta.json` (recoverable → warm-resume judge; orchestration-record/DAG integrity → fail_closed; unknown → escalate to the LLM diagnostician in prod, fail_closed in dev — G5). Naming caution: the `post_judge` substep runs the validator stage literally named `pre_judge`. |

`--stage full` is a debug stage that performs end-to-end validation, and is not explicitly included in the allow-list for any of the (step, substep) above (the steady workflow uses per-phase stages as canonical). The exhaustive list of canonical `--stage` values uses the argparse `choices` of `tools/validate_pipeline_semantics.py` (`compile` / `post_generate` / `post_build` / `post_execute` / `pre_judge` / `full`) as the primary source.

**Distinction from the recording layer:** the `validation_stage` recording rule (applied at `write-step-result` time) defines the values that **may be recorded** in `step_result.json#validation_stage` as a broader per-step set (including `full`), and is the recording-layer contract. This table is the invocation-layer contract at launch-prompt time, and imposes a stricter per-substep constraint than the recording layer. They are contracts of different layers, and a `validation_stage` value recorded as a result of being narrowed per-substep by this table is automatically included in that recording-layer allowed set (e.g. only `compile` is executable for `compile/verify` → a subset of the `compile`/`full` recording set).

**negative constraint:** do not state in the launch prompt of this `(step, substep)` a `validate_pipeline_semantics` call with a `--stage` not permitted in the table above. Example: including `validate_pipeline_semantics --stage compile` in any `Compile.*` leaf prompt is wrong — `--stage compile` is the conductor's deterministic `Compile.static` substep responsibility (no leaf), and a leaf that issues it fires `noncanonical_phase_write_attempt` / is rejected by the gate allowlist. A mere mention of an MCP tool name (`compile_project` etc.) (in explanatory text, a negative constraint, etc.) is outside the scope of this lint.

**negative constraint (MCP write tool):** do not state in the `generate/verify` launch prompt the execution of a `build-runtime` MCP write tool such as `run_linter`. lint is the conductor's deterministic `generate.lint` substep — no LLM leaf runs `run_linter` (`docs/workflow/phases/phase_02_generate.md` 2-1), and execution in verify induces a write to `command_log.jsonl` that verify's `allowed_output_paths` does not authorize and invites an `unauthorized_write_violation` → `fail_closed`. This constraint targets only the `build-runtime` MCP write tool. The `generate/verify` launch prompt also launches **no** `validate_pipeline_semantics` gate at all: `validate_workspace_root.py` and `--stage post_generate` moved to the conductor's deterministic `Generate.static` substep (table above), so verify maps to `(none)`.

`record-launch`, inside `_validate_launch_prompt_text`, reconciles the text of `launch_prompt_ref` against the per-(step, substep) allowed-stage set. It scans only actionable invocation lines (lines containing `python3` / `tools/validate_pipeline_semantics.py` / `--gate validate_pipeline_semantics`), extracts both the direct CLI form and the canonical run-gate JSON form (`--args-json '{"stage": "..."}'`), and rejects with a `ValueError` if it is outside the allowed-stage (`tools/orchestration_runtime.py::_lint_launch_prompt_gate_allowlist` and `ALLOWED_VALIDATE_PIPELINE_STAGES` are the canonical implementation). For an emergency rollback, the lint can be disabled with the env `METDSL_ENFORCE_GATE_ALLOWLIST=0` (default is enabled).

---

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

Managed JSON / `.txt` artifacts are written directly via the `Write` / `Edit` tool (see the "Artifact write — direct `Write` / `Edit` tool procedure" section of [docs/AGENT_CONTRACT.md](../AGENT_CONTRACT.md)).

`<agent_run_id>` is literally substituted in the corresponding field of the launch prompt. The `$TMPDIR` env is inherited into the subprocess by `tools/run_workflow.py`, so a `${TMPDIR}/...`-form reference also works as a result, but to minimize env dependence the literal path is canonical. `/tmp/`, `/dev/shm/`, and an argument-less `$(mktemp)` remain blocked by `output_manifest_write_guard`.
