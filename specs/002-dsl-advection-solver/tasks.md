# Tasks: Nonlinear Advection Solver DSL

**Input**: Design documents from `/specs/002-dsl-advection-solver/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Integration and contract tests are included where they provide executable semantics for the DSL example.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [X] T001 Create example asset package scaffold in src/metdsl/examples/__init__.py
- [X] T002 Create benchmark scripts package scaffold in scripts/benchmarks/__init__.py
- [X] T003 [P] Seed example documentation outline in docs/examples/nonlinear_advection.md

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T004 Extend emission configuration models for grid, boundary, and RK4 fields in src/metdsl/config/models.py
- [X] T005 [P] Update configuration hashing to cover new emission fields in src/metdsl/config/hash.py
- [X] T006 [P] Add telemetry event definitions for solver lifecycle in src/metdsl/telemetry/events.py

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - Compose periodic solver specification (Priority: P1) 🎯 MVP

**Goal**: Scientists can author a complete 2D nonlinear advection-diffusion DSL specification with Arakawa-C staggering, dual periodic boundaries, explicit stencils, and RK4 stages that generates solver artifacts without manual loop coding.

**Independent Test**: Generate solver artifacts from the example DSL specification via CLI and inspect outputs to confirm second-order spatial accuracy and RK4 sequencing match the authored DSL definitions.

### Implementation for User Story 1

- [X] T007 [US1] Implement specification creation command in src/metdsl/cli/specs.py and register with the Typer app
- [X] T008 [P] [US1] Add contract test for specification creation CLI in tests/contract/test_spec_cli.py
- [X] T009 [P] [US1] Document specification creation workflow in docs/examples/nonlinear_advection.md
- [X] T010 [P] [US1] Author canonical DSL example in src/metdsl/examples/nonlinear_advection.dsl
- [X] T011 [P] [US1] Implement Arakawa-C staggered grid builders and stencil expansion in src/metdsl/ir/builder.py
- [X] T012 [US1] Harden specification completeness validation in src/metdsl/ir/validators.py
- [X] T013 [US1] Enforce stencil field validation for user-authored expressions in src/metdsl/ir/validators.py
- [X] T014 [US1] Update Fortran module template to honor periodic flux wrapping in src/metdsl/fortran/templates/module.f90.j2
- [X] T015 [US1] Extend emission CLI to load DSL example configs and surface completeness errors in src/metdsl/cli/emit.py
- [X] T016 [US1] Emit structured validation error telemetry payloads in src/metdsl/telemetry/events.py
- [X] T017 [US1] Add integration coverage for solver emission from example spec in tests/integration/test_advection_generation.py
- [X] T018 [P] [US1] Capture golden manifest for generated solver assets in tests/golden/advection_solver/manifest.json
- [X] T019 [US1] Add negative-path integration test for incomplete specifications in tests/integration/test_advection_validation_errors.py
- [X] T020 [P] [US1] Capture golden CLI error output for incomplete specifications in tests/golden/advection_solver/error_output.json

**Checkpoint**: User Story 1 should be fully functional and testable independently (MVP scope)

---

## Phase 4: User Story 2 - Verify solver fidelity against benchmark (Priority: P2)

**Goal**: Verification analysts can execute the generated solver, invoke an external analysis script, and confirm error metrics stay within tolerance for the rotating cosine bell benchmark.

**Independent Test**: Run solver + validation CLI flow using the benchmark scenario and confirm telemetry plus CLI output report max absolute error ≤5% and conservation drift ≤1%.

### Implementation for User Story 2

- [X] T021 [P] [US2] Implement rotating cosine bell analysis script in scripts/benchmarks/cosine_bell.py
- [X] T022 [US2] Emit solver run outputs and validation manifest hooks in src/metdsl/verify/runners.py
- [X] T023 [US2] Introduce solver run/validate Typer commands in src/metdsl/cli/solver.py and register with CLI app
- [X] T024 [P] [US2] Add contract test for validation CLI output in tests/contract/test_benchmark_cli.py
- [X] T025 [US2] Add integration test covering solver execution plus benchmark validation in tests/integration/test_advection_validation.py
- [X] T026 [US2] Run high-resolution 256×256 grid / 500-step benchmark validation in tests/integration/test_advection_highres.py
- [X] T027 [P] [US2] Capture high-resolution performance metrics manifest in tests/golden/advection_solver/highres_metrics.json
- [X] T028 [US2] Implement timestep stability warning logic in src/metdsl/verify/runners.py
- [X] T029 [P] [US2] Add integration test for unstable timestep warnings in tests/integration/test_advection_timestep_warnings.py

**Checkpoint**: User Stories 1 AND 2 should both work independently

---

## Phase 5: User Story 3 - Reuse DSL patterns across scenarios (Priority: P3)

**Goal**: Modeling leads can clone the example specification, adjust coefficients or sources, and regenerate solvers while maintaining version traceability.

**Independent Test**: Clone the DSL example via CLI, adjust physics coefficients, regenerate the solver, and confirm lineage metadata and artifacts reflect the new version.

### Implementation for User Story 3

- [X] T030 [P] [US3] Implement version identifier generator utilities in src/metdsl/ir/versioning.py
- [X] T031 [US3] Persist version records and lineage metadata in src/metdsl/io/ir_writer.py
- [X] T032 [US3] Add specification clone and list commands in src/metdsl/cli/specs.py with Typer registration
- [X] T033 [P] [US3] Add unit coverage for version lineage logic in tests/unit/test_version_ids.py
- [X] T034 [US3] Document clone workflow and reuse patterns in docs/examples/nonlinear_advection.md

**Checkpoint**: All user stories should now be independently functional

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [X] T035 [P] Finalize telemetry and CLI documentation updates in docs/examples/nonlinear_advection.md
- [X] T036 Run quickstart verification walkthrough using specs/002-dsl-advection-solver/quickstart.md
- [X] T037 [P] Instrument onboarding session timing telemetry in src/metdsl/telemetry/events.py
- [X] T038 Capture pilot feedback template in docs/feedback/nonlinear_advection_feedback.md

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3-5)**: Depend on Foundational completion; proceed in priority order (US1 → US2 → US3) or in parallel once shared prerequisites complete
- **Polish (Phase 6)**: Depends on desired user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Depends on Setup + Foundational phases; no additional story dependencies
- **User Story 2 (P2)**: Depends on User Story 1 assets and Foundational support for solver outputs
- **User Story 3 (P3)**: Depends on User Story 1 specification scaffold and Shared version metadata utilities

### Within Each User Story

- Prioritize CLI/test scaffolding before feature logic
- Implement DSL/IR updates before emission or validation steps
- Generate or update tests/fixtures after logic is in place but before final verification

---

## Parallel Execution Examples

- **User Story 1**: After T007 wires the CLI entry point, T008 (contract test) and T010 (DSL example) can run in parallel with T011 (IR builder). Golden capture tasks T018 and T020 can execute once T017 and T019 pass respectively.
- **User Story 2**: T021 (analysis script) and T022 (runners) can run in parallel; T024 contract test can be prepared while T023 CLI wiring is underway, and high-resolution plus stability validation tasks T026-T029 follow once the baseline flow passes.
- **User Story 3**: T030 version utilities and T033 unit tests can proceed in parallel; T032 CLI cloning waits on T030-T031 to land.

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phases 1 & 2 (Setup + Foundational)
2. Deliver Phase 3 (User Story 1) and validate solver generation end-to-end
3. Pause for review/demo; deploy example assets if accepted

### Incremental Delivery

1. MVP (US1) delivers core DSL example and solver generation
2. Layer on US2 to add benchmark validation workflow
3. Conclude with US3 to enable cloning and reuse patterns
4. Polish phase tidies documentation and walkthroughs

### Parallel Team Strategy

- One developer drives Shared Setup/Foundational work, then shifts to US1
- Second developer tackles US2 (benchmark pipeline) post-foundation
- Third developer focuses on US3 (versioning & cloning) once US1 metadata is available
- Coordination checkpoints after each story ensure independent verification before merge

---

## Notes

- [P] tasks target different files with no direct dependencies and can run concurrently
- Story labels ([US1], [US2], [US3]) ensure traceability between tasks and user stories
- Each user story culminates in executable tests or fixtures for independent validation
- Halt after any checkpoint to validate functionality before proceeding to later stories
