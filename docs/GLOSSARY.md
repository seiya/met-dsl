# Glossary / Notation / Level Definitions

This document aggregates the terms referenced by other documents in one place, so it reads coherently on its own.

## 1. Artifacts
- **controlled_spec.md**: The canonical source for the physics / numerical algorithm definition. The generator references it to create the `model` (the implementation body).
- **problem spec**: A `controlled_spec.md` with `spec_kind=problem`. It defines the integration scenario, the runtime input contract, and the dependent `component` / adopted `profile`.
- **component spec**: A `controlled_spec.md` with `spec_kind=component`. It defines the input/output contract of a reusable physics operation and the published `operation`.
- **profile spec**: A `controlled_spec.md` with `spec_kind=profile`. It defines the selection rules, defaults, and constraints for a `component`.
- **tests.md**: The canonical source for the verification profile (input instances, case expansion, decision conditions). It is used for all `spec_kind` of `problem` / `component` / `profile`, and the test runner deterministically interprets and references the necessary parts.
- **spec_catalog.yaml**: The registry of `spec`. It holds `spec_kind`, `domain`, `family`, `spec_id`, placement (`controlled_spec_path`, `tests_path`, etc.), state, and `official_releases` (registration information for official-version implementations).
- **component_catalog.yaml**: The registry of reusable `component` / `operation`. Its storage location is `releases/registry/component_catalog.yaml`, and it holds responsibilities, the published `API`, compatibility, and implementation state.
- **deps.yaml**: The dependency declarations required by each `spec`. It defines `component_id` / `profile_id` and `version constraint`.
- **case.yaml**: A test-case definition written by a human (or, in the future, generated from `Spec`). It can include `sweep` / `refinement` etc.
- **spec.ir.yaml**: The canonical source for the structured intermediate representation (IR) derived by `Compile`. It integrates the 5 sections `case`, `algorithm`, `impl_defaults`, `io_contract`, `dependency` into a single file and uses them as the sole generation/verification contract from `Generate` onward. It structures the natural-language intent of `controlled_spec.md`, and from `Generate` onward only this IR is referenced as authoritative (reading `controlled_spec.md` is forbidden). The `algorithm` section requires the vocabulary `execution_mode`, `steps[]`, `ordering`, `control_condition`, `iteration_contract`, `update_semantics`, `temporaries`, `derived_field_rules`, `invariants`, and `splitting_policy`, and the `io_contract` section holds `inputs` / `outputs` / `semantic_dependency.required_sources` / `raw_requirements.required_evidence` / `test_evidence_requirements` / `diagnostics_contract`.
- **dependency.resolved.yaml**: The canonical source for the dependency-resolution result. It holds `node_key`, `direct_deps`, `transitive_deps`, and `topo_level`.
- **direct dependency compile readiness**: A state in which, for the immediate dependency `node` of the target `node`, the corresponding `ir_id` has been issued and `ir_meta.json.verification_status=pass` is satisfied. An upper `node` that does not satisfy this condition must not start `Compile`.
- **direct dependency execution readiness**: A state in which, for the immediate dependency `node` of the target `node`, the corresponding `ir_id` and `pipeline_id` have been issued and the latest `aggregate_verdict` is `pass` or `xfail`. An upper `node` that does not satisfy this condition must not start `Generate` onward.
- **expected_node_set**: The expected `node` set reconstructed from `deps.yaml` and `spec_catalog.yaml`. Used for the completeness verification of `dependency.resolved.yaml`.
- **node workflow**: One series of `Compile -> Generate -> Build -> Validate` execution targeting a single `node_key` (the core 5-phase).
- **orchestration agent**: The supervising agent responsible for controlling the progress of the whole `workflow`. It is responsible for launching `step` / `substep`, managing dependency ordering, and aggregating state, and does not directly generate phase artifacts. For a phase that has `substep`, it directly manages the `substep agent` and aggregates `step_result.json`.
- **step agent**: An agent responsible for a single `step` of a single `node`. It is responsible for artifact generation and verification of a phase that does not have a standard `substep`.
- **substep agent**: An agent responsible for a single `substep`. It generates the artifact according to the input contract and returns it to the `orchestration agent`.
- **node_key_safe**: The storage notation of `node_key`. The recommended form is `<spec_kind>__<spec_id>__<spec_version>`.
- **orchestration_id**: The `ID` that identifies one entire `workflow` execution. Used as the storage key for `workspace/orchestrations/<orchestration_id>/`.
- **ir_id**: The `ID` that identifies the `spec.ir.yaml` per `node`. The recommended form is `<slug>_<date>_<seq3>`. The IR and `ir_meta.json` are placed under `workspace/ir/<node_key_safe>/<ir_id>/`.
- **pipeline_id**: The `ID` that identifies the `Generate -> Build -> Validate` series per `node`. The recommended form is `<slug>_<date>_<seq3>`.
- **source_id / binary_id / run_id**: The `ID` that identifies the trial of each stage. The recommended form is `<prefix>_<date>_<seq3>` (`prefix` is `src` / `bin` / `run`). `source_id` identifies the `Generate` output (the full source set), `binary_id` the `Build` output (the binary), and `run_id` the `Validate` execution (execute + judge).
- **agent_run_id**: The `ID` that identifies one execution of a `step agent` / `substep agent` / `orchestration agent`. Together with `parent_agent_run_id` it expresses the parent-child relationship.
- **issue_severity**: The severity of a problem in a child `agent` artifact. The 3 values `minor` / `major` / `critical` are used.
- **repair_strategy**: The re-submission policy for a child `agent`. `reuse` means continuing repair with the same `agent_session_id`, and `restart` means a fresh restart with a new `agent_session_id`.
- **repair_target_agent_run_id**: A reference `ID` indicating the immediately preceding `agent_run` that was the target of the re-submission decision.
- **node_key**: The identifier of the `node` to execute / judge. The format is `<spec_kind>/<spec_id>@<spec_version>`.
- **topo_level**: The topological level in the dependency `DAG`. A smaller value represents a lower `node`.
- **release_id**: The `ID` that identifies the official-version implementation of each `spec`. The recommended form is `<spec_version>_<utc_ts>_<seq3>`.
- **target_architecture**: The architecture identifier that separates official-version artifacts. Examples: `x86_64`, `aarch64`, `nvidia_sm80`.
- **release artifact root**: The storage root for official-version artifacts. `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` is the canonical source.
- **official_releases**: The registration array of official-version implementations held in `spec_catalog.yaml`. It has `target_architecture`, `toolchain_language`, `target_backend`, `source_pipeline_id`, `source_source_id`, `source_binary_id`, `source_run_id`, `artifact_root`, `promoted_at`, and `status`. The optional flow Promote updates it.
- **lineage.json**: A provenance file that records the relationship of `spec_ref`, `ir_ref`, `pipeline_id`, and each stage `ID` (`source_id` / `binary_id` / `run_id`).
- **orchestration_meta.json**: `orchestration` execution metadata. It records `orchestration_id`, the target `spec_ref`, `source_dependency_ref` (a reference to the orchestration's starting `spec/.../deps.yaml`; a separate concept from the per-phase `dependency_ref` of the launch_request), the start time, and the execution state. When checkpoint resume is permitted, it adds `resume_enabled` (boolean) and `resumed_at` (optional).
- **orchestration_checkpoint.json**: The orchestration evidence that holds the `pass`-completed `step` and the SHA-256 of `output_refs`. It is updated by `tools/orchestration_runtime.py` when `write-step-result` completes with `status=pass`. Manual editing is forbidden.
- **resume_enabled**: A boolean field of `orchestration_meta.json`. Only an orchestration with `true` may use `orchestration_checkpoint.json` as input to the skip decision.
- **skipped_by_checkpoint**: One of the `agent_role` values of `agent_runs.jsonl`. It records that checkpoint consistency was confirmed and the relevant `step` was not launched.
- **agent_graph.json**: The `agent` parent-child relationships in an `orchestration`. It records `parent_agent_run_id`, `child_agent_run_id`, and `relation_type`.
- **context_id**: The `LLM` execution-context identifier. It has a unique value per `step agent` / `substep agent`, and duplicates within the same `orchestration_id` are forbidden.
- **context_isolated**: A boolean indicating that the `step agent` / `substep agent` was executed in an isolated context. `true` is required.
- **agent_runs.jsonl**: A chronological log of `agent` execution events. It records `agent_run_id`, `parent_agent_run_id`, `agent_role`, `status`, `started_at`, `finished_at`, `agent_backend`, `agent_model`, `context_id`, `context_isolated`, `launch_request_ref`, `launch_response_ref`, `launch_prompt_ref`, `launch_reply_ref`, `agent_result_ref`, and `agent_summary_ref`. On re-submission, it records `issue_severity`, `repair_strategy`, `repair_target_agent_run_id`, and `repair_reason` at the `launch_request_ref` target.
- **step_result.json**: The phase aggregation result. It records `status`, `required_outputs`, `failed_substeps`, `executor_agent_run_id`, and `substep_agent_run_ids`. For a phase that has `substep`, `executor_agent_run_id` is the `orchestration agent_run_id`; for a phase that does not have a standard `substep`, it is the `step agent_run_id`. `substep_agent_run_ids` may be an empty array for a phase that does not have `substep`. A phase that performed re-submission adds `retry_decisions`, holding `issue_severity`, `repair_strategy`, `repair_target_agent_run_id`, `new_agent_run_id`, and `repair_reason`.
- **model**: The computation component / library that performs the physics computation. It is responsible for computing the next state from the input state.
- **runner (e.g. `simulate`)**: The execution entry point. It is responsible for reading input, calling `model`, and outputting `diagnostics` / `perf`.
- **`<stage>_meta.json`**: The execution metadata of an `LLM`-using stage. It holds `attempt_count`, `verification_status`, `last_fail_reason`, `context_isolated`, and `debug_mode`. With `context_isolated=false`, `constraint_reason` is required. When failed attempts are saved with `debug_mode=true`, it holds `retained_failed_attempts` and the storage location.
- **source_meta.json**: The `<stage>_meta.json` of the `Generate` stage. Placed under `source_id`.
- **verifier (in-stage)**: The consistency-check responsibility executed inside the `LLM` stage. It takes only artifacts as input and returns a pass/fail in the `generate -> verify -> regenerate` loop.
- **diagnostics.json**: The physics / numerical diagnostics emitted by the `runner` (conserved quantities, errors, `CFL`, etc.). It does not include the workflow pass/fail (`verdict.json`). When `io_contract.diagnostics_contract` is present, it must carry the contracted `checks.<id>` entries and, when required, a `verdict` object (the runner's self-assessment `verdict.overall` / `verdict.failed_checks`, distinct from the judge's `verdict.json`).
- **perf.json**: The performance diagnostics emitted by the `runner` (at minimum `walltime_sec`, `throughput_cells_per_sec`, `parallelism`). It does not include pass/fail.
- **verdict.json**: The pass/fail judgment of the relevant `node` (`self_verdict`) and its basis.
- **aggregate_verdict.json**: The aggregated pass/fail judgment including the relevant `node` and its transitive dependency `node`.
- **summary.json**: The aggregation of the whole `run`. It must hold `self_summary` and `dependency_summary`.
- **dependency_summary**: The dependency-aggregation counts. It holds `total`, `pass`, `xfail`, `fail`, and `blocked`.
- **dependency workflow coverage check**: A verification that confirms the `node_key` set of `dependency.resolved.yaml` and the `node` set of `workspace/ir` / `workspace/pipelines` match one-to-one.
- **dependency implementation encapsulation**: A boundary rule that does not copy, relocate, or redefine the implementation body of a dependency `node` under the depending `node`'s `source/<source_id>/src/`. The depending `node` may hold only calls to the published `operation` of the dependency `node`, a shared `library`, or a `profile` reference.
- **blocked_reason**: The direct reason a `node` ended in `blocked`. It records the `fail` / `blocked` of the dependency `node` in an identifiable way.
- **blocking_direct_deps**: The array of immediate dependency `node_key` that caused the `blocked`.
- **stdout.log / stderr.log**: Execution logs (always saved to make post-hoc debugging possible).
- **attempts/**: A failed-attempt storage directory created only when `debug_mode=true`. It is not created in standard operation (`debug_mode=false`).
- **dummy output**: An artifact artificially generated without execution basis, for the purpose of advancing the workflow or passing `tests`. It includes `diagnostics` / `perf` / `verdict` / `aggregate_verdict`.
- **dummy computation**: An implementation that does not perform the physics computation and substitutes the computation result with only fixed values or boilerplate strings.
- **fail-fast stop**: The operational rule of stopping the relevant phase with `fail` at the moment a phase input shortage or contract mismatch is detected, without continuing via guessed completion or artificial generation.
- **pipeline semantic validation**: The content-verification gate via the `--stage` invocation of `python3 tools/validate_pipeline_semantics.py`. `--stage compile` / `post_generate` / `post_build` verify the relevant stage's contract and outputs without execution artifacts, and `post_execute` / `pre_judge` / omitted (`full`) mechanically verify the `raw` primary evidence, `trial_meta` tracking consistency, the `quality check` comparison canonical source, fixed-value generation patterns, `copy_based_artifact_reuse`, etc.
- **static lint**: The source static analysis executed by the MCP `run_linter` in the `Generate` stage. It is a separate step from a build via `Build`'s `compile_project` or `toolchain.build_system`. It is distinct from `quality check` (`run_quality_checks`).
- **lint_command_ref**: The `static lint` MCP evidence held by `source_meta.json`. It is required when `verification_status=pass`, and records under the `run_linter` key an object array with `command_id`, `command_log_ref`, and `preset`.
- **metrics basis**: The per-test evidence index saved in `raw/metrics_basis.json`. It holds all `test_id` of `test_evidence_requirements` and, for each `test_id`, holds the `required_raw_variables` needed for `Validate.judge` recomputation as raw values or raw references. It must not be substituted by a whole-suite summary or a copy of `diagnostics.json`.
- **diagnostics_contract**: The `io_contract` subsection that encodes the `tests.md §3` diagnostics contract for `Generate` to consume (Generate reads only the IR, never `tests.md`). It declares `checks[].id` (every `checks.<id>` key the runner must emit in `diagnostics.json`) and a `verdict` block (`required`, `fields`) obligating the runner to self-emit a `verdict` object in `diagnostics.json` when some test's `pass_when` references `verdict.*`. Derived by `Compile.verify`, consumed by `Generate`, and checked at `Validate.judge` start.
- **raw snapshot schema**: The item definition saved in `raw/state_snapshots/snapshot_schema.json` of a `problem` `node`. Via `variables[].name`, `variables[].shape_expr`, `time_variable`, and `time_shape_expr`, it expresses the state quantities and time information used for judgment recomputation in each problem setting.
- **algorithm contract**: The operation-composition IR held by the `algorithm` section of `spec.ir.yaml`. It requires the vocabulary `execution_mode`, `steps[]`, `ordering`, `control_condition`, `iteration_contract`, `update_semantics`, `temporaries`, `derived_field_rules`, `invariants`, and `splitting_policy`.

Notes:
- `perf.json` is output separately from `diagnostics.json` (they do not coexist).
- The `verifier` prioritizes execution in a context independent of the `generator` as far as possible.
- When an isolated context cannot be secured due to execution-environment constraints, same-context execution is permitted, and the constraint reason is recorded in each stage's `<stage>_meta.json`.
- Intermediate artifacts of failed attempts are not saved in standard operation. Saving is permitted only when `debug_mode=true`.

## 2. Test Levels (L0-L3)
`L0-L3` is a classification representing "the granularity and purpose of a test", not an implementation layer number.

- **L0: Component tests (Unit / Operator / Guard)**
- **L1: Analytic-solution / convergence-trend tests (Analytic / MMS / Refinement)**
- **L2: Conservation-law / constraint tests (Invariants / Constraints)**
- **L3: Robustness / equivalence tests (Robustness / Equivalence)**
- Equivalence also includes "performance regression" (comparing performance on top of physical passing).

## 3. Expected Failure (Guard / XFAIL)
- A test that "should fail" if correctly implemented.
- When the expected-failure condition is satisfied, judge it as `PASS`.

## 3-1. Dependency Block (Blocked)
- A state in which the judgment of an upper `node` cannot start due to a `fail` of an immediate dependency `node` or unresolved dependency.
- `blocked` must be recorded in `aggregate_verdict` and `dependency_summary`.
- The workflow execution result of an upper `node` where `blocked` occurred is `fail`.

## 4. Physical Validity
Bitwise agreement is not required. Agreement is judged by the following properties.
- Conservation-law drift is within tolerance
- Constraints (non-negativity, excessive overshoot) are within tolerance
- The error against the analytic solution or reference solution is within tolerance
- The error improves with `refinement`
- Future: statistical, spectral, and ensemble metrics

## 5. Algorithm Classes
This project divides "algorithms" into 2 kinds.

### A) Physics algorithms (Physics-affecting)
- Choices that affect the physics result (accuracy, stability).
- Examples: spatial discretization (central 2nd order, first-order upwind, `WENO`, etc.), time integration, filters, diffusion, approximation of physical processes, numerical implementation of boundary conditions.
- **Determined by the `case` section and `algorithm` section of `spec.ir.yaml`, and must be deterministic** (the same `case` and the same `algorithm` are expected to give the same physics solution).

### B) Execution algorithms (Execution-only / Performance-affecting)
- Choices that (ideally) do not change the physics result but affect the computation process (performance, memory, parallel efficiency).
- Examples: loop order, tiling / blocking, array layout, fusion / splitting, vectorization, `GPU` kernel splitting, async, numerically equivalent expression transformation, communication overlap.
- **The `impl_defaults` of `spec.ir.yaml` expresses the core-workflow defaults. The Tune flow specifies the knob-override exploration targets via `tuning.spec` (an input dedicated to the optional flow)**.

Note:
- Differences in rounding error can still occur even in execution algorithms. The tolerance is absorbed by "physical-validity agreement".

## 6. Determinism
- Determinism is necessary to guarantee "reproducibility of the physics result".
- However, the determinism that guarantees the physics result is mainly related to the determination of the **physics algorithm (A)** and the input conditions.
- **The execution algorithm (B) is not necessarily fixed.** In performance tuning, B is intentionally varied to explore.

## 7. run_id
- The identifier assigned to one execution (`execute` + `judge`) of the `Validate` phase.
- Format: `run_<YYYYMMDD>_<seq3>` (e.g. `run_20260511_001`).
- The primary evidence and judgment artifacts are aggregated under `workspace/pipelines/<node_key_safe>/<pipeline_id>/runs/<run_id>/`.

## 8. MCP (Model Context Protocol)
- A protocol for standardizing tool execution.
- In this project, `compile` / `run` / `quality check` are executed through the `MCP` server.
- The `compile` of `fortran` / `c` / `cpp` / `mixed` families is executed via a standard build tool that can handle dependencies (default `make`).

## 9. Automatic Differentiation (AD)
- A technique for mechanically obtaining derivatives (`JVP` / `VJP` / `gradient`) for a discretely implemented computation graph.
- This project assumes future support, and at the current stage requires a "specification / implementation structure that does not impede `AD`".
- When non-differentiable operations (e.g. `clip`, `limiter`, branching) are included, their handling is made explicit in the specification.

## 10. `spec` Classification Vocabulary (`spec_kind` / `domain` / `family`)
- **spec_kind**: The kind of `spec`. Only the 3 values `problem` / `component` / `profile` are allowed.
- **domain**: The top-level classification of the physics model. A fixed vocabulary to keep `spec` placement and the `component_id` prefix consistent. Examples: `dynamics`, `microphysics`, `radiation`, `land_surface`.
- **family**: The classification unit within a `domain`. In `problem` it represents a group of equations, in `component` a group of reusable operations, and in `profile` a group of selection rules.
- **component**: A reusable physics-operation unit defined by a `component spec`. Divided by equation system or discretization responsibility. Examples: `advection_flux`, `time_integrator`, `boundary_periodic`.
- **operation**: The callable unit published by a `component`. A vocabulary that abstracts the actual entity of a language-specific function, procedure, method, etc.
- **Application rule**: The placement of a `spec` is `spec/<spec_kind>/<domain>/<family>/<spec_id>/...`. The first 2 elements of the recommended `component_id` form `<domain>_<family>_<operator>_<dim>d_<scheme>` match `domain` and `family`. The `operation_id` uses the `<component_id>__<action>` form.
