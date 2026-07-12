# Overall specification: a document-driven framework that generates subroutine groups and a runner for weather/climate computation

## Final goal
Using the `Controlled Spec` and `tests` as the canonical source, generate, at operable quality, the subroutine groups (`model`) that implement the computation task defined by each `spec` on hardware such as `CPU` / `GPU`, and the `runner` responsible for input/output, execution, and judgment coordination.

## Scope
### In scope
- Input: the natural-language-centric `Controlled Spec` (physics / algorithm definition) and `tests` (verification input / judgment profile)
- Output: execution code (`model` + `runner`) and judgment artifacts (`diagnostics.json` / `perf.json` / `verdict.json` / `aggregate_verdict.json` / `summary.json`)
- Operation: iterative operation of the core workflow `Spec -> Compile -> Generate -> Build -> Validate`. As optional flows, `Tune` (implementation-discretion variant exploration) and `Promote` (promotion to the official version) are handled via separate paths.
- Hardware: includes `CPU` / `GPU` (`Phase 0` is `CPU` reference, extending to `GPU` afterward)
- The canonical source format for the `Controlled Spec` and `tests` is `Markdown`, and items that can be uniquely defined in natural language are described in prose.

### Out of scope (at this time)
- Guaranteeing bitwise agreement
- Fully automatic discovery of scientific validity (the validity definition is provided by a human)

## Invariant principles
1. The document is the canonical source. The `Controlled Spec` and `tests` must be interpretable standalone by a domain researcher and deterministically convertible into judgment input.
2. Validity assurance is done at the exit. Accept the non-reproducibility of the `LLM` as a premise, and guarantee final quality by a judgment based on the execution result.
3. Separate the `model` responsible for physics computation from the `runner` responsible for input/output and judgment coordination.

## Anti-fraud principles (overall specification)
1. Forbid generating `dummy` output for the purpose of passing `tests` or advancing the workflow.
2. When a phase input is insufficient, stop the relevant phase with `fail`, and forbid guessed completion or placeholder completion.
3. On a phase failure, an artifact must not be artificially generated for the purpose of satisfying the start condition of a downstream phase.
4. Without an explicit specification, forbid referencing the content of past workflow output (past `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id`).
5. A violation of these principles is a specification violation, and invalidates the relevant workflow execution.

## Operational principles (Spec-First)
1. Change a physics specification by updating the `Controlled Spec` first, then reflecting it into the implementation.
2. Reflect changes to experiment conditions / judgment conditions by updating `tests`.
3. Do not change a physics specification by directly modifying only the implementation.
4. Do not proceed with a provisional implementation for an unjudgeable or ambiguous specification; send it back to `Spec` and resolve it.

## Handling of the LLM (overall principles)
- The `LLM` is interchangeable regardless of model type.
- An `LLM`-using stage iterates `generate -> verify -> regenerate`, and finalizes the artifact only after verification passes.
- The `verifier` must be executed in a context independent of the `generator` (a separate session or a separate agent).
- In-stage `verify` aims to confirm structural / contract / traceability consistency, and does not substitute for the final assurance of physical validity.
- An `LLM`-using stage produces `<stage>_meta.json` (`ir_meta.json` for `Compile`, `source_meta.json` for `Generate`, `validate_meta.json` for `Validate`) as a required output.
- The required items of the metadata are `attempt_count`, `verification_status`, `last_fail_reason`, `context_isolated`, and `debug_mode`. When `context_isolated=false`, `constraint_reason` is required.
- `source_meta.json` no longer records `lint_command_ref`: `static lint` is the deterministic conductor-run `Generate.lint` substep, certified by `post_generate` against the host-authored `<pipeline_root>/lint_evidence/<source_id>.json`.
- The default for `debug_mode` is `false`. Only when `debug_mode=true` is saving failed-attempt artifacts permitted, recording `retained_failed_attempts` and the storage location.

## Architecture policy
- The core workflow generates a **single structural IR (`spec.ir.yaml`)** in the `Compile` phase, and the stages from `Generate` onward treat the IR as the canonical source. Reading `controlled_spec.md` directly from `Generate` onward is forbidden, with the sole exception of `Generate.verify`, which reads it as a requirement-fidelity cross-check (`spec.ir.yaml` remains the primary basis).
- `spec.ir.yaml` integrates and holds the following 5 sections:
  - `case`: the determined values of runtime input (with `sweep` already expanded).
  - `algorithm`: the structural representation of the physics algorithm (A). It requires the vocabulary `execution_mode` / `steps[]` / `ordering` / `control_condition` / `iteration_contract` / `update_semantics` / `temporaries` / `derived_field_rules` / `invariants` / `splitting_policy`.
  - `impl_defaults`: the default values for implementation discretion. It includes `target.backend`, `target.architecture`, `toolchain.language`, and `toolchain.build_system`.
  - `io_contract`: the verification contract. It holds `inputs` / `outputs` / `semantic_dependency.required_sources` / `raw_requirements.required_evidence` / `test_evidence_requirements`.
  - `dependency`: the LLM-authored `node_key` + `direct_deps[]` only (the directly-read edge). The derived closure/topo graph (`all_nodes` / `transitive_deps` / `topo_level`) is conductor-authored to the `<ir_ref>/dependency_graph.json` sidecar, not the IR.
- The physics algorithm (A) is determined by the `algorithm` section, and case dependence is held by the `case` section.
- The **default values for implementation discretion (B) are stored in `impl_defaults`, but in the core workflow these are fixed values**. Variant exploration of implementation discretion is the responsibility of the optional flow `Tune`; `Tune` treats `spec.ir.yaml` as invariant, separately reads `tuning.spec`, and generates code variants.
- The `name`, `shape_expr`, and `evidence_ref` of judgment-target outputs are managed in `io_contract.outputs` of `spec.ir.yaml`.
- Whether `raw` primary evidence is required is managed in `io_contract.raw_requirements.required_evidence` of `spec.ir.yaml`, and a fixed computation style or fixed evidence composition must not be uniformly required.
- By separately managing the physics algorithm (A) and implementation discretion (B) in different sections of a single IR, both physics reproducibility and Tune exploration are achieved.

## Large-scale spec operation design
### Purpose
- While maintaining per-problem `spec`, make reusable physics operations the canonical source per `component`.
- Manage only interchangeable units as `spec`, and prevent over-division.
- Maintain identifier / dependency / artifact traceability even as the generation targets increase.

### Scope
- The `spec` kinds (`problem` / `component` / `profile` / `infrastructure`) and their dependency hierarchy
- The naming rules for `spec` / `component` / `operation`
- Inter-`spec` dependency declaration and registry consistency
- The placement rules for official-version artifacts (`releases/`)

### Requirements
1. A `spec` requires `spec_kind`, and the value allows only `problem` / `component` / `profile` / `infrastructure`. (`infrastructure` is the R1 harness kind: a per-`(language, hardware)` target node that the workflow generates + certifies to supply the shared runner plumbing every physics node's runner is built against — humans author only its `controlled_spec.md` / `tests.md` / `deps.yaml`, like any other `spec`.)
2. A `spec` requires the following hierarchy.

```text
spec/
  registry/
    spec_catalog.yaml
  problem/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
  component/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
  profile/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
  infrastructure/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
releases/
  registry/
    component_catalog.yaml
  <spec_kind>/
    <domain>/
      <family>/
        <spec_id>/
          <target_architecture>/
            <toolchain_language>/
              <release_id>/
```

3. Make the definitions of `domain` and `family` match the "`spec` classification vocabulary" in `GLOSSARY.md`.
4. `spec_id` must be unique within the repository, and requires the form `^[a-z][a-z0-9_]{2,63}$` and a length of **at most 55 characters**. The 55-character bound keeps the identifiers derived from it (`<spec_id>_model` / `<spec_id>_runner` / `<spec_id>_checks`) within the `f2008` 63-character identifier limit. It is checked at **spec-input**, before any phase runs, for the target `spec` and for every member of a `--with-deps` dependency closure alike; an over-length `spec_id` is an error there and is resolved only by a rename (re-authoring the `IR` or the source cannot resolve it). The bound reflects the identifier limit of the only current backend (`fortran`); when a backend with a different limit is added, the bound moves to a language-aware point and does not enter the name grammar above.
5. `tests.md` allows placing only 1 file per `spec`.
6. `component_id` requires the form `^[a-z][a-z0-9_]{2,63}$`, and the recommended form is `<domain>_<family>_<operator>_<dim>d_<scheme>`. A `component spec`'s `component_id` is its `spec_id`, so the 55-character bound of requirement 4 applies to it as well.
7. `operation_id` requires the form `<component_id>__<action>`.
8. The published names of the generated code require compatibility management, and a change that breaks `major` compatibility is separated into a different name.
9. Every `spec` declares its dependencies in `deps.yaml`, and direct path references (relative `import`) are forbidden.
10. A `problem spec` must declare its dependent `component` and adopted `profile`.
11. Unregistered dependencies, unimplemented dependencies, and compatibility-violating dependencies are not allowed.
12. `releases/registry/component_catalog.yaml` holds the per-`component` responsibility, the published `operation`, compatibility information, and implementation state.
13. Each `tests.md` must define at least 1 `L0` test.
14. Official-version artifacts must not be placed under `spec`. The storage location requires `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/`.
15. The granularity decision requires the following criteria.
- Replaceability: make only a boundary where there is a decision to replace it independently into a `component spec`.
- Contract independence: divide only a unit whose input/output contract, preconditions, and failure conditions can be described standalone.
- Verification independence: divide only a unit for which an independent pass/fail condition can be defined in `tests.md`.
- Internal-only functions: forbid making internal `helper` groups that are not externally published into an independent `spec`.

### Design Policy
- A `problem spec` defines the integration scenario and guarantees the consistency of multiple `component`.
- A `component spec` defines the reusable physics-operation contract and guarantees interchangeability and `API` stability.
- A `profile spec` defines the `component` selection rules and parameter constraints and manages operational differences.
- An operation shared across `spec` is managed independently as a `component`.

### Operations Rules
- When a `spec` is newly created, and whenever any of its three input files (`controlled_spec.md` / `deps.yaml` / `tests.md`) is updated, run the `spec-input-check` skill (`skills/spec-input-check/SKILL.md`) against it before the workflow is started. The check is read-only and advisory: it reports findings and never modifies the `spec`.
- The check is a pre-`Compile` step that sits **outside** the phase sequence. It is not a `phase`, `tools/workflow_conductor.py` does not launch it, and it forms no part of any `step agent` / `substep agent` contract. The `spec` author runs it.
- Updating a `spec` also requires running the check against every `spec` that declares it as a dependency. A rename or a contract change is applied in lockstep across the dependent's `deps.yaml` and the prose of its `controlled_spec.md`, and a half-applied change is a contradiction that only the dependent's own check surfaces.
- When adding a new `spec`, registration into `spec/registry/spec_catalog.yaml` is required.
- The required items of `spec_catalog.yaml` are `spec_kind`, `domain`, `family`, `spec_id`, `spec_version`, `status`, `controlled_spec_path`, and `tests_path`.
- When the reuse boundary of a `problem spec` is changed, update `releases/registry/component_catalog.yaml` at the same time.
- While the implementation state of a `component` is `spec_defined_not_implemented`, set the depending `problem spec` to `status=draft`.
- `workspace/` is a working area for trial artifacts and must not be used as the canonical source for official-version artifacts.
- On promotion, add `official_releases` to the target `spec_id` of `spec_catalog.yaml`, recording `release_id`, `target_architecture`, `toolchain_language`, `target_backend`, `source_pipeline_id`, `source_source_id`, `source_binary_id`, `source_run_id`, `artifact_root`, `promoted_at`, and `status`. Promote is an optional flow separated from the core workflow.
- `official_releases` allows only 1 entry with `status=active` per `target_architecture + toolchain_language` of each `spec_id`.
- The placement of a `spec` is canonically `spec/<spec_kind>/<domain>/<family>/<spec_id>/...`.

### Decision Criteria
- A `spec` whose input check reports a `blocker` must not be handed to the workflow. A `blocker` is resolved by re-authoring the `spec`; no `phase` can repair it. A `spec` that is structurally complete, registered, and free of ambiguity can still declare a test that cannot reach its `expected_outcome` — a swept case parameter that no rule reads, or a judged metric that no diagnostics field carries — so a clean structural review is not a substitute for running the check.
- `CI` must `pass` the format verification of `spec_kind` / `spec_id` / `component_id` / `operation_id`.
- `CI` must `pass` the existence verification of each `spec`'s `tests.md` and the `L0`-test existence verification.
- `CI` must `pass` the consistency verification between the `spec_ref` of `tests.md` and `controlled_spec.md`.
- For the dependency declaration of a `problem spec`, `CI` must `fail` unregistered, unimplemented, and compatibility-violating dependencies.
- `CI` must `fail` a missing required item of `spec_catalog.yaml` and `component_catalog.yaml`.
- `CI` must `fail` when the `artifact_root` of `official_releases` does not point under `releases/`.

## Success conditions (minimal)
- The structuring from the `Controlled Spec` into `spec.ir.yaml`, and further the conversion into the `model` and `runner` that implement each `spec`'s computation task, is reproducible.
- The responsibility boundary of `problem` / `component` / `profile` is maintained, and the dependency declarations are consistent with the registry.
- The artifact contract needed for physical-validity judgment and performance evaluation is reproducible in the core workflow.
- The optional flow `Tune` can explore `impl_defaults` (B), and can evaluate performance improvement while maintaining physical passing.

## References
- `CONTROLLED_SPEC.md`
- `TESTS.md`
- `WORKFLOW.md` (entry point) / `workflow/WORKFLOW_CORE.md` / `workflow/phases/`
- `PHYSICAL_VALIDATION.md`
- `GLOSSARY.md`
