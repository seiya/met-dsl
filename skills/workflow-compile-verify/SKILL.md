---
name: workflow-compile-verify
description: Use this when running the verify of the Compile stage and performing the structural-invariant check of `spec.ir.yaml` including the `io_contract` section (authored by Compile.generate). It applies to the `verification_status` judgment after Compile generation.
---

# Workflow Compile Verify

## Purpose
Detect structural-invariant violations of the Compile stage output, and judge the conditions for proceeding to `Generate`. Following the "hybrid verification" principle that delegates semantic correctness to the `Validate` execution result (`docs/workflow/phases/phase_01_compile.md`), limit the scope of the self-check to structural invariants.

## Scope
- the work of checking the `spec.ir.yaml` of `workspace/ir/<node_key_safe>/<ir_id>/` (all 5 sections, including the `io_contract` section that `Compile.generate` authored)
- the work of updating the `verification_status` of `ir_meta.json`

## Requirements
- The canonical source for the judgment rules is limited to `docs/workflow/WORKFLOW_CORE.md`, `docs/workflow/phases/phase_01_compile.md`, `docs/RUNBOOK.md`, `controlled_spec.md`, `tests.md`, `deps.yaml`, and the `spec.ir.yaml` to be checked. The implementation under `tools/`, verification `script`, test code, and validator code must not be read to extract requirements or judgment rules.
- The `dependency_ref` of the launch request must always receive a value of the form `spec/<component_path>/deps.yaml`. A value of the `workspace/ir/` form is wrong, and on detection it must immediately stop with `fail`. `dependency_ref` is not a target of reading or transcription, and is used only for convention confirmation.
- Check the required items of the 5 sections `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` of `spec.ir.yaml`.
- Syntax (`check_artifact_syntax`) and the `--stage compile` structural invariants are already certified by the conductor's `Compile.static` substep (runs before this leaf; routes violations to `Compile.generate`). This leaf does NOT re-run them — it performs the spec-cross-reference semantic checks below.
- Check `execution_mode`, `steps[]`, `ordering`, `control_condition`, `iteration_contract`, `derived_field_rules`, and `invariants` of `spec.ir.yaml.algorithm`. It is a required check that `steps[].inputs` and `steps[].outputs` are a **list of non-empty strings** (e.g. `["U_L", "U_R"]`), and the object list form (`[{name: ..., source: ...}]`) is a `must be string list` violation and a `fail`. Reference form: `docs/examples/spec_ir_algorithm_section.example.yaml`.
- Check that each string token appearing in `steps[].inputs` / `steps[].outputs` is **traceable** to one of a direct input/output variable of `controlled_spec.md`, an intermediate variable of `temporaries`, or a derived quantity of `derived_field_rules`. A name derived by a slice, alias, or component decomposition is valid if it is declared in `temporaries` or `derived_field_rules`. A token that cannot be mapped to either a direct name or a derived name is an undefined binding and a `Compile fail`.
- Check that `spec.ir.yaml.algorithm.step_kind` matches the allowed vocabulary and that the `operation_ref` of `steps[]` and the resolution result of `spec.ir.yaml.dependency` do not contradict.
- Check the existence and consistency of the `spec.ir.yaml.io_contract` section against `controlled_spec.md`, `tests.md`, and `deps.yaml` (the V3 semantic invariants — recompute-sufficiency of `test_evidence_requirements`, `diagnostics_contract` coverage of `tests.md §3`, output↔algorithm consistency).
- The `spec.ir.yaml.io_contract` section is **authored by `Compile.generate`** (it produces all 5 sections so the deterministic `Compile.static` gate runs on a complete IR). This `Compile.verify substep` only **CHECKS** `io_contract`; it must NOT author or modify it (or any other `spec.ir.yaml` section). This leaf's sole write is `ir_meta.json`. On a V3 finding, `fail` and route to regenerate — do not edit `spec.ir.yaml` here.
- This substep launches **no** `validate_pipeline_semantics` gate — see the "substep ↔ allowed validator gate correspondence table" of `docs/workflow/LAUNCH_PROMPT_REFERENCE.md` (`compile/verify → (none)`). The `--stage compile` gate moved to the conductor's deterministic `Compile.static` substep; do not include any `validate_pipeline_semantics` call in this launch prompt.
- The sole write is `ir_meta.json` at `workspace/ir/<node_key_safe>/<ir_id>/ir_meta.json` (a path missing the `workspace/` prefix is forbidden); confirm it is in `allowed_output_paths` before writing. `spec.ir.yaml` is a read-only input here (authored by `Compile.generate`, validated by `Compile.static`) — do not write it.
- Check the required items (`name` / `evidence_ref` / `shape_expr`) of `io_contract.inputs` and `io_contract.outputs`.
- In `io_contract.outputs`, when `evidence_ref` references something other than `raw/state_snapshots` and declares `artifact=state_snapshots` as required, check that `raw_variables` is a non-empty array and references `schema.variables` or `schema.time_variable`.
- Check `io_contract.raw_requirements.required_evidence`, and confirm the consistency of `artifact`, `required`, `min_samples`, and `schema` (when needed).
- When `raw_requirements.required_evidence` declares `artifact=state_snapshots` with `required=true`, check the existence and validity of `schema.variables[].name`, `schema.variables[].shape_expr`, `schema.time_variable`, and `schema.time_shape_expr`.
- Check `io_contract.test_evidence_requirements`, confirm that it holds all `test_id` of `tests.md` neither more nor less, and that each `required_raw_variables` resolves to a variable declared in `schema`. Additionally, for each `test_id`, confirm `required_raw_variables` is **sufficient for independent recomputation** of that test's judgment: when the judgment recomputes from inputs (e.g. `F*=F(U_L)` for `l0_equal_state_consistency_pass`), the recompute *inputs* (`U_L`/`U_R`) must be listed (not only the outputs) and must also be declared in `raw_requirements.required_evidence[].schema.variables` so the runner snapshots them. Listing only outputs when the judgment needs the inputs is a `fail`.
- Check `io_contract.diagnostics_contract` against `tests.md §3` (Diagnostics contract) and `tests.md §4`. `diagnostics_contract.checks[].id` must cover every `checks.<id>` key required by `tests.md §3` neither more nor less. When any `tests.md §4` test's `pass_when` references `verdict.*`, `diagnostics_contract.verdict.required=true` and `verdict.fields` must cover the referenced keys (e.g. `overall` / `failed_checks`); otherwise `verdict.required=false`. This is the runner output contract that `Generate` consumes (Generate never reads `tests.md`), so the §3 checks/verdict must be encoded here.
- Check that the `io_contract` section does not hold the generation contract. The integration order, update order, `numerical_kernel_contract`, and iteration conditions must exist only in `spec.ir.yaml.algorithm`. (`diagnostics_contract` is an IO/verification contract — the structure of the runner's diagnostics output — not a generation contract, so it legitimately belongs in `io_contract`.)
- Check the fixed / knob layer boundary of `impl_defaults`. Check that all fixed sub-keys (`target.class` / `target.backend` / `target.architecture` / `toolchain.language` / `toolchain.standard` / `toolchain.build_system` / `selected.backend_key`) have a value (V6 invariant), and that the leaf values of the knob sub-keys (`abstract.*` / `backend_overrides.*`) are not a plug-hole (`null` / `<TBD>`) (V7 invariant). For details, `docs/workflow/phases/phase_01_compile.md`.
- Check the default-application rules. The targets are the language default, the `toolchain.build_system` default, and the `OpenMP` default.
- The case where the `ir_ref` and `ir_meta.json.verification_status` of an immediate dependency `node` cannot be confirmed is a `dependency compile missing` and a `fail`.
- Check the consistency of the `node_key`, the dependency set, and `topo_level` of `spec.ir.yaml.dependency`.
- Check the diff against a `spec.ir.yaml` regenerated from the same input, and detect a determinism violation.
- When the information needed to derive a structural invariant is insufficient, it is a `fail`, and `pass` must not be assigned by guessed completion.
- The workflow mode uses `METDSL_WORKFLOW_EXEC_MODE` as the canonical source, and applies `dev` when unset.
- A finding sets `verification_status=fail` (record `issue_severity`); `minor` is not tolerated. The conductor warm-repairs `minor` (re-runs `Compile.generate`) and stops(`dev`)/escalates(`prod`) on `major|critical`.
- Check that a `node` with a dependency-resolution error is treated as `blocked`.
- Check that the storage root of the checked artifact is `workspace/`, and the workflow-root judgment targets only `workspace/`.

## Operations Rules
0. Immediately after starting work, Read the `allowed_file_tool_paths` and `allowed_output_paths` of `output_manifests/<agent_run_id>.json`, and confirm that the output destination of `ir_meta.json` matches `workspace/ir/<node_key_safe>/<ir_id>/ir_meta.json`. On a mismatch, immediately stop with fail.
0.5. This substep does NOT write `spec.ir.yaml` — all 5 sections including `io_contract` are authored by `Compile.generate`, and the deterministic `Compile.static` gate already validated the IR structure before this leaf launched. `Read` `spec.ir.yaml` to check it, but the only artifact this leaf writes is `ir_meta.json`.
1. Reflect the check result into `ir_meta.json`, and update `verification_status` to `pass` or `fail`, writing it directly with the `Edit` / `Write` tool (managed JSON is direct-write eligible; no `guarded-apply-patch` and no `apply_patch_writes` gate evidence is required — under `bwrap` confinement the write is authorized by `write_roots` containment). Even when the inspection finds nothing to change, re-author `ir_meta.json` (e.g. refresh an idempotent field such as `verify_attempts`) so the substep produces its declared output. An inspect-only verify that writes nothing cannot terminate `pass`.
2. On `fail`, concretize `last_fail_reason`, and record the violated invariant ID (V1–V7), the section to fix, and the convention name.
3. Proceed to `Generate` only the `ir_id` with `verification_status=pass`.
4. Check the required keys (`attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, `context_isolated`) of `ir_meta.json`, and on omission it is a `fail`.
5. When `context_isolated=false`, a non-empty string for `constraint_reason` is a required check.
6. With `debug_mode=false`, do not save failed-attempt artifacts.
7. When the storage root for workflow artifacts is not `workspace/`, do not start a downstream phase and it is a `Compile fail`.
8. When `workspace/` does not exist before workflow execution starts, create `workspace/` directly under the repository root.
9. This leaf runs NO validator gate (`validate_workspace_root` / `check_artifact_syntax` / `--stage compile` ran in the conductor's `Compile.static` substep before launch). It only assigns `ir_meta.json.verification_status` from the semantic checks above.
10. When it `fail` in `dev` mode, record in `last_fail_reason` the basis needed to create `failure_analysis.json` (the violated convention, the target artifact, the failure reason).

## Decision Criteria
- Assign `verification_status=pass` only when there is no violation of the structural invariants V1–V7.
- Always attach a reproducible basis file to the check result.
- The judgment rules match `docs/workflow/WORKFLOW_CORE.md`, `docs/workflow/phases/phase_01_compile.md`, and `docs/RUNBOOK.md`.
