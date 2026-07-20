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
  spec_kind: "<problem|component|profile|infrastructure>"
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
      evidence_ref: "raw/state_snapshots | raw/diagnostics | ..."  # required non-empty string, same form as outputs[].evidence_ref
  outputs:
    - name: "<name>"
      shape_expr: "<...>"
      evidence_ref: "raw/state_snapshots | raw/diagnostics | ..."
      raw_variables: ["<name1>", ...]  # when evidence_ref=raw/state_snapshots, a non-empty array is required
  raw_requirements:
    required_evidence:
      - artifact: "<state_snapshots | metrics_basis.json | execution_trace.json>"  # normalizes to this enum (optional raw/ prefix, case-insensitive); author the canonical bare form, with the .json suffix on metrics_basis.json / execution_trace.json
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
  test_predicates:
    # R2 deterministic per-test verdict DSL: one entry per tests.md test_id (set equality).
    # Validate.execute evaluates these against diagnostics.json to author verdict.json in-process
    # (the judge no longer authors per_test). Formalize each tests.md §4 pass_when / §7 rule.
    - test_id: "<test_id>"                 # exactly the tests.md test_ids, no more/less
      expected_outcome: "pass"             # pass | xfail (the certifying outcome when pass_when holds)
      target_cases: ["<case_id>", ...]     # case.test_case_set case_ids this predicate ranges over
      pass_when:
        all:                               # `all` is the only combinator (conjunction)
          - ref: "verdict.overall"         # dotted path into diagnostics.json (see ref vocabulary below)
            op: "eq"                        # eq | ne | le | ge | lt | gt | includes
            value: "pass"                   # literal, OR {per_case: {<case_id>: v}} for nx-dependent thresholds
            # an ordered op (le/ge/lt/gt) REQUIRES a numeric threshold written as a YAML float
            # WITH a decimal point: `1.0e-10` (NOT `1e-10`, which YAML 1.1 parses as a string and
            # the --stage compile gate rejects). `includes` takes a list-member literal.
            per_case: false                 # optional: resolve `ref` inside each target case's diagnostics slice
            case: "<case_id>"               # optional: resolve `ref` inside ONE target case's slice (excludes per_case)
            na_allowed: false               # optional: a null/absent lhs counts as satisfied (a "not applied" metric)
  # NOT DEGENERATE (--stage compile, degenerate_predicate_violations): a pass set whose EVERY
  #   expected_outcome=pass predicate asserts only verdict.* is rejected (it collapses the per-test
  #   judgment back to the runner's verdict.overall). Each pass test carries its concrete conditions
  #   (checks.<id> or a pinned metrics address). An xfail predicate is exempt.
  # test_predicates ref vocabulary (all resolvable at --stage compile):
  #   verdict.<field>   -> a diagnostics_contract.verdict.fields entry (overall/failed_checks)
  #   checks.<id>...    -> an id in diagnostics_contract.checks (the runner emits checks.<id>.pass|status)
  #   <metric address>  -> any other head (metrics.*/errors.*/cfl.*/convergence.*) MUST be pinned in
  #                        diagnostics_contract.metrics (below). The runner emits every numeric judgment
  #                        already reduced to a field, so predicates do no arithmetic.
  # condition scope — the three ways a multi-target test compares its evidence:
  #   (none)            -> resolve `ref` against the suite-level (top-level) diagnostics object.
  #   per_case: true    -> the condition must hold in EVERY target case. Combine with a
  #                        {per_case: {<case_id>: v}} value map when the threshold varies per case
  #                        (e.g. an nx-dependent error bound). The map must cover every target case.
  #   case: <case_id>   -> the condition is resolved in exactly ONE target case's slice; the case must
  #                        be a member of this predicate's own target_cases, and it excludes per_case.
  #                        This is how a CROSS-CASE reduction is read: the checks module accumulates
  #                        across cases and emits the derived metric (convergence order, symmetry
  #                        residual) as a per-case metric of the case where it first becomes computable
  #                        (CHECKS_MODULE_CONTRACT.md §2/§3), and the predicate compares it there.
  # diagnostics_contract.metrics (OPTIONAL): per-case metric addresses the predicates reference; the
  # intermediate per-case addressing pin for problem specs (until R1 fixes the harness output shape). e.g.
  #   metrics: ["cfl.max", "metrics.mass_drift_rel", "errors.analytic_h.l2_rel_tend", "convergence.n32_to_n64.analytic_h_order"]

dependency:
  # LLM-authored: ONLY node_key + direct_deps (the directly-read edge + semantic `operations`).
  # Do NOT author all_nodes / transitive_deps — the conductor derives them (sidecar, below).
  node_key: "<spec_kind>/<spec_id>@<spec_version>"
  direct_deps:
    - node_key: "<spec_kind>/<spec_id>@<spec_version>"
      kind: "<component|profile|problem|infrastructure>"
      operations: ["<operation_id>", ...]

public_api:
  # infrastructure nodes ONLY (spec_kind: infrastructure); OMIT for component/profile/problem.
  # The COMPLETE published surface controlled_spec §5 declares ("operation_ids are exactly: ...") —
  # every operation incl. helper emitters/writers no test exercises directly — so Generate
  # publishes all of them and the runner CALLS them, not reimplements. The --stage compile gate
  # (_validate_infrastructure_public_api) pins these sets == §5 (V8).
  published_operations:
    - operation_id: "<spec_id>__<name>"   # every §5 operation_id (set equality with §5)
      exercised_by: ["<test_id>", ...]    # optional: tests exercising it ([] for a helper)
  published_types: ["<spec_id>__<type>", ...]   # every §5 published derived type (set equality)
  signatures:                             # transcribe controlled_spec §5.1 VERBATIM, one per symbol
    - symbol: "<spec_id>__<name>"         # every §5.1 op AND type (set equality with §5.1)
      interface: |                        # the exact Fortran interface stanza (block scalar):
        subroutine <spec_id>__<name>(a, b)   #   header + dummy-arg decls (+ `result` for a function);
          <type>, intent(in) :: a            #   a type stanza is `type :: name` .. `end type name`.
          <type>, intent(in) :: b            # The Generate leaf publishes these verbatim (it cannot
        end subroutine <spec_id>__<name>     # read controlled_spec — phase_02 §2-1); the gate pins
                                             # signatures == §5.1 (normalized) at Compile (V8).
```

### `<ir_ref>/dependency_graph.json` sidecar (conductor-authored)
The conductor writes this at Compile phase start (`workflow_conductor._write_dependency_graph`)
from `deps.yaml` + `spec_catalog.yaml` — a leaf-non-writable managed path (like
`compile_static_meta.json`; no `operations`):
```yaml
{ "node_key": "<kind>/<id>@<v>",
  "all_nodes":       [{"node_key": "...", "topo_level": <int>}, ...],   # incl. self; topo_level = height
  "transitive_deps": [{"node_key": "...", "via": ["<intermediate>", ...]}, ...],
  "generated_by": "conductor" }
```
The host directly-required set is `{all_nodes} − {self} − {transitive_deps}`; the `--stage compile`
gate cross-checks it against the IR's `direct_deps`.

### `shape_expr` allowed forms
`spec/schema/ir/shape_expr.schema.json` is the canonical source. Limited to the 3 forms `scalar` (case-insensitive) / `[d1, d2, ...]` / `(d1, d2, ...)`. Function-call notation such as `vector(N)` / `matrix(M,N)` / `tensor` is forbidden and is a `Compile fail`.

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
- Read the physics algorithm (A) of the `Controlled Spec`, deterministically expand the input conditions and `sweep` / `refinement` from `tests.md`, and generate the 5 sections `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` of `spec.ir.yaml`. For an `infrastructure` node additionally author the `public_api` section — the COMPLETE published surface controlled_spec §5 declares (every `operation_id`, incl. helper emitters/writers no test exercises directly, plus every published derived type) AND `public_api.signatures` (each `{symbol, signature}`) copying the §5.1 structured signatures; the gate pins names == §5 and `signatures` == §5.1 (V8).
- Author `dependency.node_key` and `dependency.direct_deps[]` (each with `kind` + `operations`) — the directly-read dependencies from `deps.yaml`. Do NOT author `transitive_deps` / `all_nodes`: the conductor derives that closure/topo graph host-side into `<ir_ref>/dependency_graph.json` (a pure function of `deps.yaml` + `spec_catalog.yaml`).
- The default values of `impl_defaults` follow the rules of `IMPL_PLAN_SPEC.md` (existing). Considering variant exploration in the Tune optional flow, express the knob set of `abstract` / `backend_overrides` in the IR.
- When the intent of `controlled_spec.md` does not fit the schema during generation, do not extend the schema; instead treat it as `Compile fail`, record "IR schema insufficiency" in `last_fail_reason`, and stop. For schema extension, separately update the `spec.ir.yaml` schema design by hand, then retry.

### 1-2. Compile.verify substep
`Compile.verify` self-checks **only the structural invariants**. Semantic correctness is delegated to the `Validate` execution result.

The required invariant set for the self-check (finalized as a **minimal set**):

#### V1. case coverage
- The `case.test_case_set[].case_id` covers the required cases of all `test_id` required by `tests.md`.
- The `sweep` / `refinement` instructions of `tests.md` are reflected in `case.sweeps` / `case.refinements`.

#### V2. algorithm completeness
- The union of each `step.outputs` set of `algorithm.steps[]` covers the state variables targeted for update by `algorithm.update_semantics` (whose own vocabulary is `target_variables` / `update_order` / …).
- **IR self-sufficiency (the IR is `Generate`'s sole algorithm carrier).** Every `algorithm.steps[].operation_ref` NOT resolved by a `dependency.direct_deps` call — a LOCAL operation the node implements — must be lowered WITH the math a `spec/`-blind leaf needs (defining/update expression + pinning constraints, incl. any form `controlled_spec.md` forbids), in existing fields (`derived_field_rules`, step/operation descriptions, `invariants`); no new schema field. A local op lowered as name + I/O shape + invariants only is a `Compile.verify` **major** (a free-form-math completeness judgment, so semantic at V2, not deterministically gated). This is the **removal trigger** for the interim `controlled_spec.md` carve-out on the `pure` leaf (phase_02 §Generate-executor).
- `algorithm.ordering` is a valid ordering relation over `algorithm.steps[].step_id` (no cycles, no references to undefined step_id).
- `algorithm.iteration_contract` is not an empty object when `algorithm.execution_mode=iterative`.
- **Multi-dimensional `problem` contract.** For a `problem` `node` whose `spec_id` contains `2d` / `3d`, `algorithm` additionally holds the 4 contract keys `state_variables[]` (each with `name` + `shape_expr`), `required_update_paths`, `diagnostics_from_state=true`, and `fallback_policy=fail_closed`, as **direct children of `algorithm`**. `required_update_paths` is a **list of non-empty strings**, each one a `state_variables[].name` (`["h", "hu", "hv"]`); the object form (`[{target: ..., path: [...]}]`) is a `fail`, because `ordering` / `steps[].step_id` already carry the update order.
- **Placement of the multi-dimensional contract (resolution order).** `--stage compile` resolves the contract from the FIRST of these that exists, and validates only that one: (1) `algorithm.state_contract` — **any** mapping wins, even an empty one; (2) `algorithm.update_semantics` — wins if it holds **any** of the 4 contract keys; (3) the direct children of `algorithm`. Author (3) and nothing else: an `algorithm.state_contract` key, or one contract key strayed into `algorithm.update_semantics` (whose own vocabulary is `target_variables` / `update_order` / …), **shadows** the direct children so that they are never read and fail as if absent. Every finding is named `state_contract.<field>` after the RESOLVED contract, not after where the field was authored. Reference form: `docs/examples/spec_ir_algorithm_2d_problem_contract.example.yaml`.

#### V3. io_contract consistency
- Every `io_contract.inputs[]` entry holds a non-empty `evidence_ref` string (same form as `outputs[].evidence_ref`, e.g. `raw/state_snapshots`); an input with a missing or empty `evidence_ref` is a `fail`.
- Each `io_contract.raw_requirements.required_evidence[].artifact` normalizes (case-insensitively, with an optional `raw/` prefix) to one of `state_snapshots` / `metrics_basis.json` / `execution_trace.json`; author the canonical bare form (`metrics_basis.json`, not `raw/metrics_basis.json`). A token that does not normalize to the enum — e.g. `metrics_basis` without the `.json` suffix — is a `fail`.
- Each `name` of `io_contract.outputs[]` appears in one of the `outputs` of `algorithm.steps[]`, or is derived by `algorithm.derived_field_rules`.
- When `io_contract.outputs[].evidence_ref=raw/state_snapshots`, `raw_variables` is a non-empty array, and each element references `io_contract.raw_requirements.required_evidence[].schema.variables[].name` or `time_variable`.
- `io_contract.test_evidence_requirements[]` holds the `test_id` of `tests.md` **neither more nor less** (no duplicates), read through `meta.source_refs.tests` — the IR's only route to `tests.md`, so a ref that does not resolve is itself a `fail`. For each `test_id`, `required_raw_variables` is **sufficient for independent recomputation** of that test's judgment: it includes the recompute *inputs* (e.g. `U_L`/`U_R` for a `F*=F(U_L)` judgment), and every listed variable resolves to a `raw_requirements.required_evidence[].schema.variables[].name` (so the inputs are also present in the `state_snapshots` schema). A `required_raw_variables` that lists only outputs while the judgment needs the inputs is a `fail`.
- `io_contract.diagnostics_contract.checks[].id` covers every `checks.<id>` key named in `tests.md §3` (neither more nor less).
- When any `tests.md §4` test's `pass_when` references `verdict.*`, `io_contract.diagnostics_contract.verdict.required=true` and `verdict.fields` covers the referenced keys (e.g. `overall` / `failed_checks`). When no test references `verdict.*`, `verdict.required=false` is allowed.
- `io_contract.semantic_dependency.required_sources` is a non-empty string array.
- **`io_contract.test_predicates` (R2) faithfully encodes every `tests.md §6/§7` pass rule.** The `--stage compile` gate (`_validate_test_predicates` → `verdict_evaluator.validate_predicate_schema`) enforces the schema mechanically (op/outcome enums, non-empty `pass_when.all`, `target_cases ⊆ case.test_case_set`, a `case:` selector that is one of the predicate's own `target_cases` and is not combined with `per_case`, `ref` resolves against the declared `verdict.fields` / `checks[].id` / `diagnostics_contract.metrics` vocabulary, `test_id` set == `tests.md`); this V3 invariant is the SEMANTIC check `Compile.verify` owns: each predicate's conjunction is a truthful translation of that test's prose judgment (correct `op`/threshold direction, the right check/metric `ref`, per-case thresholds matching the nx map, `na_allowed` only where `tests.md` marks the metric "not applied"), **and each condition carries the scope the prose asks for** — `per_case` for "in every case", `case:` for a cross-case reduction read at the case that completes it, neither for a suite-level fact. A schema-valid but semantically-wrong predicate (e.g. `ge` where the prose says `le`, or a per-case bound written suite-level) is a V3 `fail` — this is where the judge-time nondeterminism R2 removed becomes a reviewable compile-time artifact.
- **`target_cases` is also the evidence contract.** The runner records one `raw/metrics_basis.json` entry per (`test_id`, target `case_id`) pair, and `post_execute` pins that matrix in both directions. So a test's `target_cases` must list every case its judgment actually reads — a convergence sweep names all its resolutions, an equivariance test both members of the pair — and no case it does not.

#### V4. dependency consistency
The derived-closure invariants (former V4a `direct∪transitive == all_nodes`, V4b
`expected_node_set == all_nodes`, and topo ordering) are now **correct-by-construction** — the
conductor authors the closure/topo graph into the sidecar and the `--stage compile` gate
cross-checks the IR's `direct_deps` against it (see below); the LLM no longer verifies them. The
one remaining LLM-verified invariant:
- **V4c (operations ⊆ published)**: each `direct_deps[].operations` is in the published
  `operation_id` set of the dependency `node` (from the dependency IR if generated, else
  `spec_catalog.yaml`). `operations` is semantic with no host data source, so it stays LLM-verified.

#### V5. impl_defaults consistency
- The combination of `impl_defaults.toolchain.language` and `impl_defaults.toolchain.build_system` is consistent with the default-value rules of `IMPL_PLAN_SPEC.md`.
- The combination of `impl_defaults.target.class` and `impl_defaults.target.backend` is identifiable by `impl_defaults.selected.backend_key`.

#### V8. public_api surface (`infrastructure` nodes only)
- **`public_api` enumerates EXACTLY the controlled_spec §5 published surface** (else physics nodes omit it; their interface is derived post-hoc). The `--stage compile` gate (`_validate_infrastructure_public_api`) parses §5's "operation_ids are exactly" contract and pins `published_operations[].operation_id` == §5 ops and `published_types` == §5 types; a dropped/extra entry is a `Compile fail` to `Compile.generate`. Closes the R1 mode where Compile→IR kept only test-exercised ops, so Generate never published the helper emitters/writers a consuming runner links against. The LLM only enumerates §5 faithfully; the gate enforces equality. (V6/V7 are the `impl_defaults` fixed/knob invariants in the boundary section below.)
- **§5.1 canonical interface block + `public_api.signatures`** (same gate): an `infrastructure` controlled_spec §5 must carry a `### 5.1` fenced **language-neutral structured** interface block giving every published type + operation *signature* (the machine-readable contract, independent of the implementation language). The gate cross-checks §5.1's symbol set == §5's name lists, and pins the IR's `public_api.signatures` (each `{symbol, signature}`, the structured form) == §5.1 (the Fortran-language backend renders both to the target language and normalizes: comments stripped, `&` joined, case-folded, whitespace-insensitive; argument name/order/type/rank/`intent`/`result` and type components must match). Compile.generate transcribes §5.1 into `public_api.signatures` because `Generate.generate` is walled off from `controlled_spec.md` (phase_02 §2-1) — the IR is the leaf's only carrier of the signatures to publish. A missing/duplicate fence, a §5↔§5.1 mismatch, a missing `signatures` block, or a per-symbol drift is a `Compile fail`. The signature bodies are then pinned against the generated `<spec_id>_model.f90` at `Generate.static` (`_validate_infrastructure_generated_signatures`).

#### Verification tools
These deterministic gates run in the conductor's `Compile.static` substep (`_compile_static_inproc`), NOT inside the `Compile.verify` leaf:
- Syntax validity via `python3 tools/check_artifact_syntax.py --expect-top object <ir_ref>/spec.ir.yaml <ir_ref>/ir_meta.json`; on `fail` it is a `Compile fail` (routed to `Compile.generate`).
- `python3 tools/validate_pipeline_semantics.py --stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/`, with `exit code 0` required. The result is recorded in `compile_static_meta.json`; on `fail` the substep fails before `Compile.verify` runs, so `verification_status=pass` is never reached on a structurally-invalid IR. (The `--stage compile` validator checks the internal-consistency / shape-grammar invariants; the spec-cross-reference invariants are the `Compile.verify` LLM responsibility — see the substep structure note.)
  - **dependency direct_deps consistency** (`_validate_compile_dependency_consistency`): the IR's `dependency.direct_deps` (`(kind, spec_id)` set, version-agnostic) must equal `{all_nodes} − {self} − {transitive}` from the `dependency_graph.json` sidecar. A mismatch is a `Compile fail` routed to `Compile.generate`. Version drift is soft (gfortran backstop). Closes the former V4a/V4b gap.
  - **infrastructure public_api surface** (`_validate_infrastructure_public_api`, infra nodes only): `public_api.published_operations`/`published_types` must equal the controlled_spec §5 surface. A missing controlled_spec ref, a §5 parsing to zero ops, an absent `public_api`, or a set mismatch is a `Compile fail` to `Compile.generate` (V8).
  - **harness render preconditions** (`_validate_harness_render_preconditions`, M3c physics nodes only — `make` + `fortran` with exactly one `infrastructure` dependency): the node's `<spec_id>_runner.f90` is host-rendered from this IR alone, so IR *content* the renderer cannot render is caught here instead of at the render backstop (which terminates the workflow rather than retrying). The gate delegates to `tools/runner_renderer.ir_content_violations`, which invokes `render_runner` itself, so it cannot drift. Each violation is a `Compile fail` routed to `Compile.generate`:
    - `raw_requirements.required_evidence` holds a `state_snapshots` entry with a non-empty `schema.variables[]`, and its `schema.time_variable` is `t` (the harness's fixed snapshot-time key);
    - no snapshot variable is named `t` / `case_id` / `step` (harness-reserved snapshot keys);
    - every snapshot variable a case emits declares `shape_expr` as `scalar` or the bracket form `[d1, ...]` of rank 1–4; the `(d1, ...)` paren form this grammar otherwise permits is rejected, as is rank > 4;
    - `diagnostics_contract.checks[]` is non-empty and `verdict.fields` ⊆ `{overall, failed_checks}` — the harness fold builds only those records;
    - `case.test_case_set[]` is non-empty with pairwise-distinct `case_id`s (duplicates render overlapping `select case` labels), each ≤ 64 chars — the harness `case_id_len`, which `__parse_cases` truncates a longer id to, so the `select case` label could never match and every run would `error stop` despite compiling — and each matching `[A-Za-z0-9._-]` with no `..`: a case_id is concatenated into the per-case snapshot path (`raw/state_snapshots/<case_id>.json`), so a `/` or `..` would let the run write outside its directory;
    - `test_evidence_requirements[]` is non-empty, each `required_raw_variables` entry is a `state_snapshots` schema variable, and each `test_id` is targeted by at least one case across `test_predicates[].target_cases` (a test with no target case has no metrics-basis row to record). A multi-target test is fully supported: the renderer emits one metrics-basis entry per (`test_id`, target `case_id`) pair — `CHECKS_MODULE_CONTRACT.md` §2;
    - every name the renderer embeds in the runner (`case_id`, snapshot variable, metric address, `test_id`, `target.class`) is printable ASCII — a control character has no Fortran literal form, and a non-ASCII character makes the Fortran byte length disagree with the code-point length every render bound is measured in — and no rendered line exceeds 100 columns, so an over-long IR-sourced name is rejected rather than emitted as an unlintable runner.
  - Node-**identity** preconditions are excluded from that gate because re-authoring the IR cannot repair them: `spec_id` ≤ 55 characters (which keeps the derived `<spec_id>_runner` / `_checks` / `_model` identifiers within the f2008 63-character limit), and exactly one `infrastructure` direct dependency. The `spec_id` bound is enforced at spec-input and re-asserted at the render backstop; a node declaring more than one `infrastructure` dependency is not an M3c node, so its runner is never host-rendered.

## On-failure behavior
- When the input (`controlled_spec.md` / `tests.md` / `deps.yaml`) is insufficient, it is a `Compile fail`, and guessed completion is forbidden.
- When any self-check invariant `fail`, it is a `Compile fail`, and the violated invariant ID (V1–V8) and details are recorded in `ir_meta.json.last_fail_reason`.
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
