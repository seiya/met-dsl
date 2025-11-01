---

description: "Task list for Intermediate IR and Fortran emission CLI"
---

# Tasks: Intermediate IR and Fortran Emission

**Input**: Design documents from `/specs/001-intermediate-fortran/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Per Constitution Principle II, executable CLI-based tests are MANDATORY for every story. Identify the golden files, contract suites, or evaluation scripts that will validate the behaviour.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- Single project layout under `src/` and `tests/`
- CLI entrypoint at `src/metdsl/cli/emit.py`
- IR utilities under `src/metdsl/ir/`
- Fortran generation under `src/metdsl/fortran/`
- Telemetry under `src/metdsl/telemetry/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish Python package skeleton, tooling, and project structure.

- [X] T001 Create Python packaging metadata with Typer entry point in `pyproject.toml`
- [X] T002 Bootstrap package directories and `__init__` files in `src/metdsl/` and subpackages
- [X] T003 Configure development dependencies and extras (`cli`, `dev`) in `pyproject.toml`
- [X] T004 Set up linting and formatting configuration (e.g., Ruff, Black) in `pyproject.toml` and `pyproject.toml` tool sections
- [X] T005 Provision test scaffolding folders (`tests/golden/`, `tests/unit/`, `tests/contract/`, `tests/integration/`) with `__init__.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST exist before user stories begin.

- [X] T006 Implement configuration schema models with Pydantic in `src/metdsl/config/models.py`
- [X] T007 Add configuration hashing utility to compute SHA256 fingerprints in `src/metdsl/config/hash.py`
- [X] T008 Build telemetry event definitions and NDJSON logger utilities in `src/metdsl/telemetry/events.py`
- [X] T009 Scaffold CLI command group and shared options in `src/metdsl/cli/emit.py`
- [X] T010 Document developer environment setup (venv, install) in `docs/development.md`

---

## Phase 3: User Story 1 - Generate Intermediate Weather IR (Priority: P1) 🎯 MVP

**Goal**: Provide deterministic IR emission from DSL + configuration with validation feedback.

**Independent Test**: Run `metdsl emit sample.dsl --stage ir --config configs/sample.yaml --report build/ir/sample-report.json` and compare output to golden IR fixture.

### Tests for User Story 1 (MANDATORY) ⚠️

- [X] T011 [P] [US1] Create golden IR fixture and expected report in `tests/golden/ir/typhoon_ir.json`
- [X] T012 [P] [US1] Add unit tests covering IR builder/validator in `tests/unit/test_ir_builder.py`

### Implementation for User Story 1

- [X] T013 [US1] Implement IR builder that normalizes operations in `src/metdsl/ir/builder.py`
- [X] T014 [US1] Implement IR validation rules (issues, status) in `src/metdsl/ir/validators.py`
- [X] T014a [US1] Flag DSL constructs requiring numerical fidelity review and emit warnings in `src/metdsl/ir/validators.py`
- [X] T015 [US1] Wire CLI `--stage ir` flow to build + persist IR package in `src/metdsl/cli/emit.py`
- [X] T016 [P] [US1] Save IR report and package files to `build/ir/` via helper module `src/metdsl/io/ir_writer.py`
- [X] T017 [P] [US1] Emit telemetry events for IR lifecycle in `src/metdsl/telemetry/events.py`
- [X] T017a [US1] Capture IR emission duration and record in telemetry reports via `src/metdsl/telemetry/events.py`
- [X] T018 [US1] Update quickstart IR instructions and troubleshooting in `specs/001-intermediate-fortran/quickstart.md`

**Checkpoint**: IR emission CLI produces deterministic artifacts and passes golden tests.

---

## Phase 4: User Story 2 - Emit Fortran 2003 Source (Priority: P2)

**Goal**: Generate compiler-friendly Fortran using IR packages and produce manifests.

**Independent Test**: Run `metdsl emit sample.dsl --stage fortran2003 --config configs/sample.yaml --report build/fortran-report.json`, then compile generated sources with gfortran to validate manifest output.

### Tests for User Story 2 (MANDATORY) ⚠️

- [X] T019 [P] [US2] Create golden Fortran template samples and manifest baseline in `tests/golden/fortran/typhoon_manifest.json`
- [X] T020 [P] [US2] Add integration test invoking CLI `--stage fortran2003` comparing output to golden manifest in `tests/integration/test_emit_fortran.py`
- [X] T020a [P] [US2] Add failure-mode integration test ensuring Fortran artefacts are cleaned on interruption in `tests/integration/test_emit_fortran_failure.py`

### Implementation for User Story 2

- [X] T021 [US2] Implement Jinja2 template loader and environment in `src/metdsl/fortran/templates/__init__.py`
- [X] T022 [US2] Implement Fortran generator that applies templates and minimal comments in `src/metdsl/fortran/generator.py`
- [X] T023 [US2] Extend CLI to handle `--stage fortran2003` flow in `src/metdsl/cli/emit.py`
- [X] T024 [P] [US2] Generate emission manifest and trace reports in `src/metdsl/fortran/manifest.py`
- [X] T025 [P] [US2] Persist Fortran artefacts and trace files under `build/fortran/` using `src/metdsl/io/fortran_writer.py`
- [X] T025a [US2] Implement atomic writes and cleanup of partial Fortran artefacts on failure in `src/metdsl/io/fortran_writer.py`
- [X] T026 [US2] Implement discovery hook `metdsl emit --list-targets` in `src/metdsl/cli/emit.py`
- [X] T027 [US2] Update user-facing documentation about Fortran emission and discovery in `specs/001-intermediate-fortran/quickstart.md`

**Checkpoint**: Fortran emission reproducibly generates manifest + sources and discovery hook returns metadata-only targets.

---

## Phase 5: User Story 3 - Audit Transformation Trace (Priority: P3)

**Goal**: Provide verification workflow capturing compiler runs, telemetry, and governance artefacts.

**Independent Test**: Run `metdsl verify --report build/fortran-report.json` and confirm manifest updates include all compiler validations with logs.

### Tests for User Story 3 (MANDATORY) ⚠️

- [X] T028 [P] [US3] Add contract tests for `/verify` conceptual API in `tests/contract/test_verify_contract.py`
- [X] T029 [P] [US3] Add integration test covering sequential compiler mocks in `tests/integration/test_verify_cli.py`
- [X] T029a [P] [US3] Add integration test verifying telemetry sink fallback behaviour in `tests/integration/test_telemetry_fallback.py`

### Implementation for User Story 3

- [X] T030 [US3] Implement compiler runner abstraction executing gfortran/oneAPI/NVFortran sequentially in `src/metdsl/verify/runners.py`
- [X] T031 [US3] Capture compiler logs, exit codes, and durations in `src/metdsl/verify/results.py`
- [X] T032 [US3] Update CLI with `verify` subcommand writing back to manifest in `src/metdsl/cli/emit.py`
- [X] T033 [P] [US3] Emit telemetry events (`compiler_validated`, `emit_failed`) for verification in `src/metdsl/telemetry/events.py`
- [X] T033a [P] [US3] Handle telemetry sink failures with file fallback logging in `src/metdsl/telemetry/events.py`
- [X] T034 [P] [US3] Update governance manifest schema to include validation outcomes in `src/metdsl/fortran/manifest.py`
- [X] T035 [US3] Document governance review workflow and required artefacts in `docs/governance/playbook.md`
- [X] T035a [US3] Gather pilot reviewer feedback on trace usability and summarize outcomes in `docs/governance/playbook.md`
- [X] T035b [US3] Produce weekly telemetry dashboards via `scripts/reports/dashboard.py` and document cadence in `docs/governance/playbook.md`
- [X] T035c [US3] Document compiler scheduling and resource constraints for sequential runs in `docs/governance/playbook.md`
- [X] T035d [US3] Define governance ingestion interface (manifest/trace schema) and sample payloads in `docs/governance/playbook.md`
- [X] T036 [US3] Extend quickstart with compiler validation walkthrough and troubleshooting in `specs/001-intermediate-fortran/quickstart.md`

**Checkpoint**: Verification command produces governance-ready manifest with telemetry/log references for all compilers.

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Consolidate documentation, ensure resilience, and prepare for handoff.

- [ ] T037 [P] Review and update `README.md` with CLI usage and discovery hook notes
- [ ] T038 Add CLI help text and examples for all commands in `src/metdsl/cli/emit.py`
- [ ] T039 Harden error messaging and recovery guidance for interrupted runs in `src/metdsl/errors.py`
- [ ] T040 [P] Finalize automation scripts for packaging and publish instructions in `scripts/build_cli.sh`
- [ ] T041 Compile release notes summarizing artefacts and governance expectations in `docs/releases/001-intermediate-fortran.md`

---

## Dependencies & Execution Order

1. **Setup (Phase 1)** ➜ completes package skeleton and tooling.
2. **Foundational (Phase 2)** ➜ blocks all user stories (config schemas, telemetry, base CLI).
3. **User Story 1 (Phase 3)** ➜ MVP delivering IR emission; required before later stories because Fortran relies on IR artifacts.
4. **User Story 2 (Phase 4)** ➜ depends on IR package/telemetry to generate Fortran sources and manifests.
5. **User Story 3 (Phase 5)** ➜ depends on manifests from US2 to perform verification and governance updates.
6. **Polish (Phase N)** ➜ executed after desired user stories reach acceptance.

No parallel execution across phases until prerequisites complete; within each phase, tasks marked `[P]` may run concurrently.

---

## Parallel Execution Examples

```bash
# US1 parallel opportunities:
pytest tests/unit/test_ir_builder.py &
python -m metdsl emit sample.dsl --stage ir --config configs/sample.yaml --report build/ir/sample-report.json

# US2 parallel opportunities:
python -m pytest tests/integration/test_emit_fortran.py &
python -m metdsl emit --list-targets > build/targets.json

# US3 parallel opportunities:
pytest tests/integration/test_verify_cli.py &
python -m metdsl verify --report build/fortran-report.json --compilers gfortran oneapi
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational prerequisites
3. Implement Phase 3: IR emission (US1)
4. Validate golden IR fixture and telemetry outputs
5. Stop here to deliver deterministic IR emission as the MVP

### Incremental Delivery

1. Deliver MVP (US1) ➜ deterministic IR output
2. Add US2 ➜ Fortran emission + manifests + discovery metadata
3. Add US3 ➜ Verification pipeline with governance artefacts
4. Apply Polish tasks ➜ documentation, error hardening, release notes

### Parallel Team Strategy

- Developer A: Focus on IR builder/validator (US1) after foundational work
- Developer B: Build Fortran generator and discovery hook (US2) once IR artifacts exist
- Developer C: Implement verification pipeline and governance documentation (US3) after manifests are available
- Shared: Telemetry enhancements and documentation polish can proceed in parallel where `[P]` indicates independence
