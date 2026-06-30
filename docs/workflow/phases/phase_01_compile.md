# Phase 1: Compile

## Overview
The phase that integrates the natural-language specification (`controlled_spec.md` / `tests.md` / `deps.yaml`) into a **single structural IR (`spec.ir.yaml`)**. It is the only phase that reads `controlled_spec.md` as a **generation input**, and the subsequent `Generate` / `Build` / `Validate` use `spec.ir.yaml` as the canonical source (the lone read of `controlled_spec.md` downstream is `Generate.verify`'s requirement-fidelity cross-check, not a generation input).

## I/O contract
- execution input: `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec/registry/spec_catalog.yaml`
- verification input: `controlled_spec.md`, `tests.md`, `deps.yaml`, `spec/registry/spec_catalog.yaml`, the generated `spec.ir.yaml`
- output: `workspace/ir/<node_key_safe>/<ir_id>/spec.ir.yaml`, `ir_meta.json`

## substep structure
- `Compile.generate`: the LLM substep that generates `spec.ir.yaml`.
- `Compile.static`: a **deterministic conductor in-process substep** (no LLM leaf) run AFTER `Compile.generate` and BEFORE `Compile.verify`. The conductor (`workflow_conductor._compile_static_inproc`) runs the purely-static IR gates — `validate_workspace_root.py`, `check_artifact_syntax.py` on `spec.ir.yaml` + `ir_meta.json`, and `validate_pipeline_semantics --stage compile` — and authors `compile_static_meta.json`. A violation is a content failure routed back to `Compile.generate` via a warm-resume reopen (mirrors `Generate.static`). So `Compile.verify` is reached only on a deterministically-clean IR.
- `Compile.verify`: an independent LLM substep that self-checks the structural invariants (context-isolated from `Compile.generate`). It launches **no** `validate_pipeline_semantics` gate (that responsibility moved to `Compile.static`); it is a pure semantic pass over the spec-cross-reference invariants (V1 case substance, V3 recompute-sufficiency / `tests.md §3` diagnostics coverage, V5 impl_defaults).

## `spec.ir.yaml` schema

`spec.ir.yaml` is a YAML mapping artifact, and requires holding the following top-level keys.

```yaml
schema_version: "1.0"

meta:
  node_key: "<spec_kind>/<spec_id>@<spec_version>"
  spec_kind: "<problem|component|profile>"
  spec_id: "<spec_id>"
  spec_version: "<semver>"
  source_refs:
    controlled_spec: "spec/<spec_kind>/<domain>/<family>/<spec_id>/controlled_spec.md"
    tests: "spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md"
    deps: "spec/<spec_kind>/<domain>/<family>/<spec_id>/deps.yaml"

case:
  # determined values of runtime input (with sweep already expanded)
  test_case_set:
    - case_id: "<case_id>"
      inputs:
        grid: {...}
        time: {...}
        initial: {...}
        boundary: {...}
        profile_selection: {...}
        test_profile_id: "<test_profile_id>"
        test_profile_version: "<version>"
  sweeps: {...}        # optional. when the test profile defines a sweep
  refinements: {...}   # optional. when grid refinement is defined

algorithm:
  algorithm_id: "<id>"
  execution_mode: "<sequence|conditional|iterative|columnwise>"
  steps:
    - step_id: "<step_id>"
      step_kind: "<boundary_apply|reconstruct|flux_compute|source_term|time_integrate|column_process|pointwise_process|iterative_solve|filter|reduction|diagnostic>"
      operation_ref: "<operation_id>"
      inputs: ["<var1>", "<var2>"]
      outputs: ["<var3>"]
  ordering: [...]              # a sequence of step_id, or an array of before/after dependency objects
  control_condition: ...       # one of a string, a string array, or an object
  iteration_contract: {...}    # object. when execution_mode=iterative, an empty object is forbidden
  update_semantics: {...}
  temporaries: [...]           # a string array, or an array of name + shape_expr objects
  derived_field_rules: [...]
  invariants: ["<inv1>", ...]  # a non-empty string array
  splitting_policy:
    kind: "<kind>"

impl_defaults:
  # in the core workflow this value is used as fixed.
  # the Tune optional flow can override only the knob layer of this section as variants.
  # (for the fixed / knob boundary, see the "fixed / knob boundary of impl_defaults" section at the end of this file)
  target:
    class: "<cpu|gpu>"
    backend: "<backend>"
    architecture: "<architecture>"
  toolchain:
    language: "<fortran|c|cpp|cuda_fortran|cuda_c|mixed|python>"
    standard: "<standard>"
    build_system: "<make|cmake|setuptools|...>"
  selected:
    backend_key: "<key>"
  abstract: {...}              # the intent of parallelization, layout, fusion, tiling, etc. (knob area)
  backend_overrides: {...}     # overrides per backend (knob area)

io_contract:
  # integrates and holds the IO contract and verification contract: inputs / outputs / semantic_dependency / raw_requirements / test_evidence_requirements / diagnostics_contract
  inputs:
    - name: "<name>"
      shape_expr: "<scalar | [d1,d2,...] | (d1,d2,...)>"
  outputs:
    - name: "<name>"
      shape_expr: "<...>"
      evidence_ref: "raw/state_snapshots | raw/diagnostics | ..."
      raw_variables: ["<name1>", ...]  # when evidence_ref=raw/state_snapshots, a non-empty array is required
  raw_requirements:
    required_evidence:
      - artifact: "<state_snapshots|metrics_basis|execution_trace|...>"
        required: true|false
        min_samples: <int>
        schema:               # required when artifact=state_snapshots
          variables:
            - name: "<name>"
              shape_expr: "<...>"
          time_variable: "<name>"
          time_shape_expr: "scalar"   # MUST be "scalar": the per-snapshot time index is a scalar loop counter the runner always emits as a scalar; "[1]" (or any non-scalar) is rejected at compile and fails post_execute
  test_evidence_requirements:
    - test_id: "<test_id>"
      required_raw_variables: ["<var1>", ...]   # must be SUFFICIENT for Validate.judge to independently recompute this test's judgment, i.e. include the recompute inputs (e.g. U_L/U_R for an "F*=F(U_L)" judgment), not only the outputs. Each name resolves to a raw_requirements...schema.variables[].name
  diagnostics_contract:
    # derived from tests.md §3 (Diagnostics contract) + any tests.md §4 test whose pass_when references verdict.*
    # this is the runner output contract that Generate consumes (Generate never reads tests.md), so the §3 checks/verdict must live here
    checks:
      - id: "<check_id>"   # one id per checks.<id> key tests.md §3 requires in diagnostics.json (e.g. equal_state_consistency)
    verdict:
      required: true|false               # true when some test's pass_when references verdict.*
      fields: ["overall", "failed_checks"]   # the verdict.* keys the runner must self-emit in diagnostics.json (required when required=true)
  semantic_dependency:
    required_sources: ["<var1>", "<var2>", ...]   # a non-empty string array

dependency:
  # equivalent to the former dependency.resolved.yaml
  node_key: "<spec_kind>/<spec_id>@<spec_version>"
  direct_deps:
    - node_key: "<spec_kind>/<spec_id>@<spec_version>"
      kind: "<component|profile|problem>"
      operations: ["<operation_id>", ...]
  transitive_deps:
    - node_key: "<...>"
      via: ["<intermediate_node_key>", ...]
  all_nodes:
    - node_key: "<...>"
      topo_level: <int>
```

### `shape_expr` allowed forms
`spec/schema/plan/shape_expr.schema.json` is the canonical source. Limited to the 3 forms `scalar` (case-insensitive) / `[d1, d2, ...]` / `(d1, d2, ...)`. Function-call notation such as `vector(N)` / `matrix(M,N)` / `tensor` is forbidden and is a `Compile fail`.

### `algorithm.steps[].inputs` and `algorithm.steps[].outputs`
A list of non-empty strings (e.g. `["U_L", "U_R"]`); the object form (`[{name: ..., source: ...}]`) is forbidden.

### `algorithm.execution_mode`
Only `sequence` / `conditional` / `iterative` / `columnwise` are allowed.

### `algorithm.steps[].step_kind`
Only `boundary_apply` / `reconstruct` / `flux_compute` / `source_term` / `time_integrate` / `column_process` / `pointwise_process` / `iterative_solve` / `filter` / `reduction` / `diagnostic` are allowed.

## `ir_meta.json` required keys
- `attempt_count`, `verification_status`, `last_fail_reason`, `debug_mode`, `context_isolated`
- When `context_isolated=false`, `constraint_reason` is required.

## `ir_id` format
- Format: `<slug>_<YYYYMMDD>_<seq3>`
- `slug` is a short readable token derived from `spec_id`. Hyphen-separated alphanumeric.
- Regex: `^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$`

## substep details

### 1-1. Compile.generate substep
- Read the physics algorithm (A) of the `Controlled Spec`, deterministically expand the input conditions and `sweep` / `refinement` from `tests.md`, and generate the 5 sections `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` of `spec.ir.yaml`.
- Perform dependency resolution from `deps.yaml` and `spec/registry/spec_catalog.yaml`, and hold `direct_deps` / `transitive_deps` / `all_nodes` in the `dependency` section.
- The default values of `impl_defaults` follow the rules of `IMPL_PLAN_SPEC.md` (existing). Considering variant exploration in the Tune optional flow, express the knob set of `abstract` / `backend_overrides` in the IR.
- When the intent of `controlled_spec.md` does not fit the schema during generation, do not extend the schema; instead treat it as `Compile fail`, record "IR schema insufficiency" in `last_fail_reason`, and stop. For schema extension, separately update the `spec.ir.yaml` schema design by hand, then retry.

### 1-2. Compile.verify substep
`Compile.verify` self-checks **only the structural invariants**. Semantic correctness is delegated to the `Validate` execution result.

The required invariant set for the self-check (finalized as a **minimal set**):

#### V1. case coverage
- The `case.test_case_set[].case_id` covers the required cases of all `test_id` required by `tests.md`.
- The `sweep` / `refinement` instructions of `tests.md` are reflected in `case.sweeps` / `case.refinements`.

#### V2. algorithm completeness
- The union of each `step.outputs` set of `algorithm.steps[]` covers the state variables targeted for update by `algorithm.update_semantics`.
- `algorithm.ordering` is a valid ordering relation over `algorithm.steps[].step_id` (no cycles, no references to undefined step_id).
- `algorithm.iteration_contract` is not an empty object when `algorithm.execution_mode=iterative`.

#### V3. io_contract consistency
- Each `name` of `io_contract.outputs[]` appears in one of the `outputs` of `algorithm.steps[]`, or is derived by `algorithm.derived_field_rules`.
- When `io_contract.outputs[].evidence_ref=raw/state_snapshots`, `raw_variables` is a non-empty array, and each element references `io_contract.raw_requirements.required_evidence[].schema.variables[].name` or `time_variable`.
- `io_contract.test_evidence_requirements[].required_raw_variables` covers all `test_id` of `tests.md`, and for each `test_id` is **sufficient for independent recomputation** of that test's judgment: it includes the recompute *inputs* (e.g. `U_L`/`U_R` for a `F*=F(U_L)` judgment), and every listed variable resolves to a `raw_requirements.required_evidence[].schema.variables[].name` (so the inputs are also present in the `state_snapshots` schema). A `required_raw_variables` that lists only outputs while the judgment needs the inputs is a `fail`.
- `io_contract.diagnostics_contract.checks[].id` covers every `checks.<id>` key named in `tests.md §3` (neither more nor less).
- When any `tests.md §4` test's `pass_when` references `verdict.*`, `io_contract.diagnostics_contract.verdict.required=true` and `verdict.fields` covers the referenced keys (e.g. `overall` / `failed_checks`). When no test references `verdict.*`, `verdict.required=false` is allowed.
- `io_contract.semantic_dependency.required_sources` is a non-empty string array.

#### V4. dependency consistency
- The closure of the union of `dependency.direct_deps[]` and `dependency.transitive_deps[]` matches `dependency.all_nodes` (no node_key duplication or omission).
- The node_key set of the `expected_node_set` reconstructed from `deps.yaml` and `spec/registry/spec_catalog.yaml` matches `dependency.all_nodes`.
- Each `direct_deps[].operations` is included in the published `operation_id` set of the dependency `node` (reconcile when the dependency IR exists, and obtain from `spec_catalog.yaml` when not yet generated).

#### V5. impl_defaults consistency
- The combination of `impl_defaults.toolchain.language` and `impl_defaults.toolchain.build_system` is consistent with the default-value rules of `IMPL_PLAN_SPEC.md`.
- The combination of `impl_defaults.target.class` and `impl_defaults.target.backend` is identifiable by `impl_defaults.selected.backend_key`.

#### Verification tools
These deterministic gates run in the conductor's `Compile.static` substep (`_compile_static_inproc`), NOT inside the `Compile.verify` leaf:
- Syntax validity via `python3 tools/check_artifact_syntax.py --expect-top object <ir_ref>/spec.ir.yaml <ir_ref>/ir_meta.json`; on `fail` it is a `Compile fail` (routed to `Compile.generate`).
- `python3 tools/validate_pipeline_semantics.py --stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/`, with `exit code 0` required. The result is recorded in `compile_static_meta.json`; on `fail` the substep fails before `Compile.verify` runs, so `verification_status=pass` is never reached on a structurally-invalid IR. (The `--stage compile` validator checks the internal-consistency / shape-grammar invariants; the spec-cross-reference invariants are the `Compile.verify` LLM responsibility — see the substep structure note.)

## On-failure behavior
- When the input (`controlled_spec.md` / `tests.md` / `deps.yaml`) is insufficient, it is a `Compile fail`, and guessed completion is forbidden.
- When any self-check invariant `fail`, it is a `Compile fail`, and the violated invariant ID (V1–V5) and details are recorded in `ir_meta.json.last_fail_reason`.
- The repair_strategy defaults to `reuse`, and `restart` is chosen only when a structurally substantial reconstruction is needed.

## Acceptance of retry from Validate
When the `judge` of `Validate` produces a finding with `attribution=ir` and `confidence>=medium`, the `orchestration agent` re-submits a retry to `Compile` (the canonical source for the routing rules is the decision table of `docs/workflow/phases/phase_04_validate.md`). The acceptance contract on the `Compile` side:

- A re-submitted `Compile` presumes that the Validate finding information (`description`, `evidence_refs[]`, `finding_id`) is quoted in `launches/<agent_run_id>.request.json#repair_reason`. When there is no quote, it stops with a `Compile fail`.
- Record `validate_feedback:<finding_id>` in `ir_meta.json.last_fail_reason`, and increment `ir_meta.json.attempt_count`.
- A re-submitted `Compile` defaults to fixing **only the section of `spec.ir.yaml` that the finding points to** (e.g. `algorithm.steps[].ordering`, `io_contract.outputs[].shape_expr`), and a full renewal of all sections is permitted only when the finding has `confidence=high` and its `description` requests structural reconstruction.
- The `Compile.verify` after fixing, in addition to the self-check invariants, makes explicit in `ir_meta.json.repair_target_sections[]` that the fix to the path pointed out by `validate_feedback` is reflected.

## fixed / knob boundary of `impl_defaults`
The `impl_defaults` section is clearly separated into a **fixed layer that the core workflow treats as fixed** and a **knob layer that the Tune optional flow can override as variants**:

| sub-key | layer | handling |
|---|---|---|
| `target.class` | **fixed** | physical hardware category (`cpu` / `gpu`). Tune crossing forbidden |
| `target.backend` | **fixed** | backend type (`openmp` / `cuda` / `mpi` etc.). Tune crossing forbidden |
| `target.architecture` | **fixed** | concrete architecture (`x86_64` / `sm80` etc.). Tune crossing forbidden |
| `toolchain.language` | **fixed** | immutable because it determines the compiler premise |
| `toolchain.standard` | **fixed** | language standard (`f2008` / `c11` etc.) |
| `toolchain.build_system` | **fixed** | build tool (`make` / `cmake` etc.) |
| `selected.backend_key` | **fixed** | an identifier consistent with `target` |
| `abstract` | **knob** | the intent of parallelization granularity / layout / fusion / tiling etc. The main exploration area of Tune |
| `backend_overrides.<key>` | **knob** | backend-specific override values (thread count, block size, vector width, etc.) |

Each phase of the core workflow **treats the fixed layer as an immutable premise, and treats the values of the knob layer as read-only, respecting the IR's default values**. Only the `Tune` optional flow can specify override candidates for the knob layer via `tuning.spec` and generate a variant pipeline.

`Compile.verify`, in addition to invariants V1–V5, confirms the following:
- V6: All fixed sub-keys of `impl_defaults` have a value (no omission).
- V7: Each leaf value of `abstract` and `backend_overrides` is finalized as a "default value" (no plug-hole such as `null` / `<TBD>`).

The detailed knob schema and the Tune variant constraints use `docs/TUNING_WORKFLOW.md` as the canonical source.

## Design trade-offs
- By having the IR **self-check only the structural invariants**, the bloat of the verification contract is avoided. Semantic correctness is delegated to the `Validate` execution result (the "hybrid verification" principle).
- By holding implementation discretion in the IR as `impl_defaults`, it provides a base for variant exploration by the Tune optional flow while the core workflow can proceed with fixed values.
- By separating the inside of `impl_defaults` into the fixed / knob layers, the core workflow can mechanically distinguish "an area that Tune may change" from "an area that is never changed", and it can be guaranteed that Tune's variant generation does not break the IR's structure.
