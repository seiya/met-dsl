---
name: workflow-compile-verify
description: Use this when running the verify of the Compile stage and performing the structural-invariant check of `spec.ir.yaml` and the derivation/check of the `io_contract` section. It applies to the `verification_status` judgment after Compile generation.
---

# Workflow Compile Verify

## Purpose
Detect structural-invariant violations of the Compile stage output, and judge the conditions for proceeding to `Generate`. Following the "hybrid verification" principle that delegates semantic correctness to the `Validate` execution result (`docs/workflow/phases/phase_01_compile.md`), limit the scope of the self-check to structural invariants.

## Scope
- the work of checking the `spec.ir.yaml` of `workspace/ir/<node_key_safe>/<ir_id>/`
- the work of deriving the `io_contract` section of `spec.ir.yaml` from `controlled_spec.md`, `tests.md`, and `deps.yaml`
- the work of updating the `verification_status` of `ir_meta.json`

## Requirements
- The canonical source for the judgment rules is limited to `docs/workflow/WORKFLOW_CORE.md`, `docs/workflow/phases/phase_01_compile.md`, `docs/RUNBOOK.md`, `controlled_spec.md`, `tests.md`, `deps.yaml`, and the `spec.ir.yaml` to be checked. The implementation under `tools/`, verification `script`, test code, and validator code must not be read to extract requirements or judgment rules.
- The `dependency_ref` of the launch request must always receive a value of the form `spec/<component_path>/deps.yaml`. A value of the `workspace/ir/` form is wrong, and on detection it must immediately stop with `fail`. `dependency_ref` is not a target of reading or transcription, and is used only for convention confirmation.
- Check the required items of the 5 sections `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` of `spec.ir.yaml`.
- Check that `spec.ir.yaml` and `ir_meta.json` pass `python3 tools/check_artifact_syntax.py --expect-top object`.
- Check `execution_mode`, `steps[]`, `ordering`, `control_condition`, `iteration_contract`, `derived_field_rules`, and `invariants` of `spec.ir.yaml.algorithm`. It is a required check that `steps[].inputs` and `steps[].outputs` are a **list of non-empty strings** (e.g. `["U_L", "U_R"]`), and the object list form (`[{name: ..., source: ...}]`) is a `must be string list` violation and a `fail`. Reference form: `docs/examples/spec_ir_algorithm_section.example.yaml`.
- Check that each string token appearing in `steps[].inputs` / `steps[].outputs` is **traceable** to one of a direct input/output variable of `controlled_spec.md`, an intermediate variable of `temporaries`, or a derived quantity of `derived_field_rules`. A name derived by a slice, alias, or component decomposition is valid if it is declared in `temporaries` or `derived_field_rules`. A token that cannot be mapped to either a direct name or a derived name is an undefined binding and a `Compile fail`.
- Check that `spec.ir.yaml.algorithm.step_kind` matches the allowed vocabulary and that the `operation_ref` of `steps[]` and the resolution result of `spec.ir.yaml.dependency` do not contradict.
- Check the existence and consistency of the `spec.ir.yaml.io_contract` section. The derivation source is limited to `controlled_spec.md`, `tests.md`, and `deps.yaml`.
- The `spec.ir.yaml.io_contract` section must be derived as the responsibility of the `Compile.verify substep` and written into the `spec.ir.yaml` body. The `Compile.generate substep` must not generate it.
- The validator gates this substep can launch use the "substep ↔ allowed validator gate correspondence table" of `skills/workflow-orchestration/references/launch_prompts.md` as the canonical source (`validate_pipeline_semantics --stage compile` is the responsibility of this substep and must not be included in the launch prompt of `Compile.generate`).
- The write destination of `spec.ir.yaml` must always be `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml`. A write directly under `ir/` (without the `workspace/` prefix) is forbidden. Before writing, confirm the `allowed_output_paths` of `output_manifests/<agent_run_id>.json`, and verify that it matches a path starting with `workspace/ir/`.
- Check the required items (`name` / `evidence_ref` / `shape_expr`) of `io_contract.inputs` and `io_contract.outputs`.
- In `io_contract.outputs`, when `evidence_ref` references something other than `raw/state_snapshots` and declares `artifact=state_snapshots` as required, check that `raw_variables` is a non-empty array and references `schema.variables` or `schema.time_variable`.
- Check `io_contract.raw_requirements.required_evidence`, and confirm the consistency of `artifact`, `required`, `min_samples`, and `schema` (when needed).
- When `raw_requirements.required_evidence` declares `artifact=state_snapshots` with `required=true`, check the existence and validity of `schema.variables[].name`, `schema.variables[].shape_expr`, `schema.time_variable`, and `schema.time_shape_expr`.
- Check `io_contract.test_evidence_requirements`, confirm that it holds all `test_id` of `tests.md` neither more nor less, and that each `required_raw_variables` resolves to a variable declared in `schema`.
- Check that the `io_contract` section does not hold the generation contract. The integration order, update order, `numerical_kernel_contract`, and iteration conditions must exist only in `spec.ir.yaml.algorithm`.
- Check the fixed / knob layer boundary of `impl_defaults`. Check that all fixed sub-keys (`target.class` / `target.backend` / `target.architecture` / `toolchain.language` / `toolchain.standard` / `toolchain.build_system` / `selected.backend_key`) have a value (V6 invariant), and that the leaf values of the knob sub-keys (`abstract.*` / `backend_overrides.*`) are not a plug-hole (`null` / `<TBD>`) (V7 invariant). For details, `docs/workflow/phases/phase_01_compile.md`.
- Check the default-application rules. The targets are the language default, the `toolchain.build_system` default, and the `OpenMP` default.
- The case where the `ir_ref` and `ir_meta.json.verification_status` of an immediate dependency `node` cannot be confirmed is a `dependency compile missing` and a `fail`.
- Check the consistency of the `node_key`, the dependency set, and `topo_level` of `spec.ir.yaml.dependency`.
- Check the diff against a `spec.ir.yaml` regenerated from the same input, and detect a determinism violation.
- When the information needed to derive a structural invariant is insufficient, it is a `fail`, and `pass` must not be assigned by guessed completion.
- The workflow mode uses `METDSL_WORKFLOW_EXEC_MODE` as the canonical source, and applies `dev` when unset.
- In `dev` mode, it is a `Compile fail` the moment `issue_severity=major|critical` is detected, and treating it as a minor exception is forbidden.
- Check that a `node` with a dependency-resolution error is treated as `blocked`.
- Check that the storage root of the checked artifact is `workspace/`, and the workflow-root judgment targets only `workspace/`.

## Operations Rules
0. Immediately after starting work, Read the `allowed_file_tool_paths` and `allowed_output_paths` of `output_manifests/<agent_run_id>.json`, and confirm that the output destination of `spec.ir.yaml` matches `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml`. On a mismatch, immediately stop with fail.
0.5. The `io_contract`-section append to `spec.ir.yaml` uses `guarded-apply-patch` as the only path. Procedure: (a) confirm `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml` from `allowed_output_paths` (verify that it starts with `workspace/` as required), (b) existence check → on apply failure, retry once with the reverse patch form to absorb the race window, (c) run `python3 tools/orchestration_runtime.py guarded-apply-patch ...`. Pass the patch with `--patch-file workspace/tmp/<agent_run_id>/guarded_patch_input.txt` (`<agent_run_id>` is literally substituted) to avoid the argv ARG_MAX limit. NG: redirection such as `tee` / `cat <<EOF >file`, a file write via `python3 -c`, and a path specification missing the `workspace/` prefix.
1. Reflect the check result into `ir_meta.json`, and update `verification_status` to `pass` or `fail`.
2. On `fail`, concretize `last_fail_reason`, and record the violated invariant ID (V1–V7), the section to fix, and the convention name.
3. Proceed to `Generate` only the `ir_id` with `verification_status=pass`.
4. Check the required keys (`attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, `context_isolated`) of `ir_meta.json`, and on omission it is a `fail`.
5. When `context_isolated=false`, a non-empty string for `constraint_reason` is a required check.
6. With `debug_mode=false`, do not save failed-attempt artifacts.
7. When the storage root for workflow artifacts is not `workspace/`, do not start a downstream phase and it is a `Compile fail`.
8. When `workspace/` does not exist before workflow execution starts, create `workspace/` directly under the repository root.
9. Before start and before completion, run `python3 tools/validate_workspace_root.py`, and on `fail` it is a `Compile fail`.
10. Before verify completes, run `python3 tools/validate_pipeline_semantics.py --stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/`, and `exit code 0` is required. On `fail`, `verification_status=pass` must not be assigned to `ir_meta.json`.
11. When it `fail` in `dev` mode, record in `last_fail_reason` the basis needed to create `failure_analysis.json` (the violated convention, the target artifact, the failure reason).

## Decision Criteria
- Assign `verification_status=pass` only when there is no violation of the structural invariants V1–V7.
- Always attach a reproducible basis file to the check result.
- The judgment rules match `docs/workflow/WORKFLOW_CORE.md`, `docs/workflow/phases/phase_01_compile.md`, and `docs/RUNBOOK.md`.
