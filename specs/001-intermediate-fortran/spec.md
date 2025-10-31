# Feature Specification: Intermediate IR and Fortran Emission

**Feature Branch**: `001-intermediate-fortran`  
**Created**: 2025-10-31  
**Status**: Draft  
**Input**: User description: "まずは中間言語に変換し、その後ターゲットのプログラミング言語に出力する。プログラミング言語としては、まずは fortran 2003 とする。"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Generate Intermediate Weather IR (Priority: P1)

A DSL author converts a weather model written in the DSL into a normalized intermediate representation (IR) so that downstream code generation is predictable.

**Why this priority**: The IR is the foundation for all future targets; without it no target language can be supported.

**Independent Test**: Run `metdsl emit model.dsl --stage ir --report build/ir.json` and confirm the produced IR matches the approved golden file and validation diagnostics.

**Acceptance Scenarios**:

1. **Given** a valid DSL model, **When** the author requests IR emission, **Then** the system outputs a structured IR file with clear metadata and no target-specific artifacts.
2. **Given** a DSL model containing unsupported constructs, **When** IR emission is attempted, **Then** the system stops and returns precise error messages naming the offending constructs.

---

### User Story 2 - Emit Fortran 2003 Source (Priority: P2)

An HPC engineer uses the previously generated IR to produce Fortran 2003 source code ready for integration with existing simulation pipelines.

**Why this priority**: Fortran remains the primary execution environment for weather models; delivering compliant output unlocks immediate operational value.

**Independent Test**: Run `metdsl emit model.dsl --target fortran2003 --report build/fortran.json` and verify the emitted source compiles with the reference compiler and matches the golden output.

**Acceptance Scenarios**:

1. **Given** an IR file derived from the DSL model, **When** the engineer emits Fortran 2003 code, **Then** the system produces compilable source alongside a manifest describing modules, entry points, and dependencies.
2. **Given** a request for a target flag other than Fortran 2003, **When** emission runs, **Then** the system warns that only Fortran 2003 is currently supported and prevents ambiguous output.

---

### User Story 3 - Audit Transformation Trace (Priority: P3)

A compliance reviewer inspects the IR-to-target trace to ensure governance requirements are met before promoting generated code to production.

**Why this priority**: Governance demands transparency; reviewers must see how DSL statements translate into target constructs.

**Independent Test**: Execute `metdsl verify --report build/fortran.json` and confirm the trace includes mappings from IR nodes to Fortran source lines alongside validation results.

**Acceptance Scenarios**:

1. **Given** a completed emission run, **When** the reviewer opens the verification report, **Then** the trace clearly lists each transformation step, source linkage, and validation outcome.
2. **Given** missing trace data, **When** verification runs, **Then** the system fails the audit with explicit instructions on regenerating artifacts with trace logging enabled.

### Edge Cases

- Configurations requesting simultaneous multiple targets must be rejected with guidance to run separate emission passes.
- IR emission should flag DSL constructs that cannot be lowered without numerical fidelity information.
- Target emission must handle Fortran reserved keywords gracefully to avoid naming collisions.
- Interruption during Fortran emission should leave no partial files that mislead downstream tooling; artefacts MUST be written atomically and temporary files cleaned on failure.
- Discovery hooks for future targets must not produce artifacts beyond metadata stubs to avoid implying official support.
- Generated Fortran must annotate critical sections minimally (comments or markers) while keeping structure optimization-friendly to aid audits without impeding compiler performance.
- Telemetry sinks may become unavailable; the CLI must fall back to local NDJSON storage and surface actionable warnings.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST convert any valid DSL model into a normalized intermediate representation independent of target language concerns.
- **FR-002**: System MUST validate the intermediate representation and emit actionable diagnostics when conversion fails.
- **FR-003**: System MUST allow users to request Fortran 2003 emission via CLI flags or configuration entries referencing the target.
- **FR-004**: System MUST translate the intermediate representation into Fortran 2003 source files that compile with the designated reference toolchain while prioritizing compiler-friendly structure and inserting minimal explanatory comments around generated regions.
- **FR-005**: System MUST produce an artifact manifest enumerating generated files, entry points, and required compilation options.
- **FR-006**: System MUST generate a transformation trace linking DSL constructs to IR nodes and final Fortran segments for audit purposes.
- **FR-007**: System MUST prevent emission when configurations reference unsupported targets, providing clear next steps.
- **FR-008**: System MUST store emission reports (including success, warnings, failures) alongside generated artifacts for later verification.
- **FR-009**: System MUST enable rerunning emission deterministically given the same DSL source and configuration inputs.
- **FR-010**: System MUST validate emitted Fortran 2003 artifacts across GNU Fortran (11+), Intel oneAPI Fortran, and NVIDIA NVFortran toolchains before release.
- **FR-011**: System MUST expose a discovery-only hook that lists candidate future targets without generating executable artifacts for them.

### Key Entities *(include if feature involves data)*

- **Intermediate Representation (IR) Package**: Structured, target-agnostic model containing normalized operations, metadata, and validation status.
  Each package is uniquely identified by the DSL model identifier combined with the configuration hash used to generate it.
- **Target Configuration**: User-provided instructions specifying the desired target language (Fortran 2003), compiler expectations, and emission options.
- **Emission Manifest**: Summary document describing produced source files, trace reports, and verification artifacts for downstream stakeholders.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 95% of baseline DSL models emit an IR package in under 5 minutes without manual intervention.
- **SC-002**: 90% of generated Fortran 2003 artifacts compile successfully on the first attempt using GNU Fortran 11+, Intel oneAPI Fortran, and NVIDIA NVFortran toolchains.
- **SC-003**: At least 80% of pilot users report being able to retrace DSL-to-Fortran transformations using the emitted trace without engineering support.
- **SC-004**: Audit reviews log zero missing emission reports or manifests across the first three production-ready models.

## CLI Exposure & Tooling *(mandatory)*

- **Command/Flag**: `metdsl emit <model.dsl> --stage ir|fortran2003 --report <path>` – Produces IR artifacts or Fortran 2003 output depending on the selected stage.
- **Execution Example**:

```bash
metdsl emit models/frontogenesis.dsl --stage fortran2003 --report build/fortran.json
```

- **Automation Output**: Emits JSON reports containing target, artifact paths, diagnostics, and trace summaries; streaming NDJSON logs list validation and emission events for CI readers.
- **Backward Compatibility Notes**: Existing compile workflows continue to function; a deprecation notice encourages teams to migrate to the staged emit command for target-specific outputs.

## Observability & Diagnostics *(mandatory)*

- **Telemetry/Events**: Emit `emit_started`, `emit_completed`, and `emit_failed` events including `{stage, outcome, duration_ms, config_hash, dsl_model_id}` and publish the JSON schema in `docs/governance/playbook.md`.
- **Telemetry Fallback**: When the configured telemetry sink is unreachable, log the failure, continue execution, and append events to `build/logs/fallback.ndjson` for later ingestion.
- **Logging**: Provide structured logs listing each normalization and emission pass, including warning severity, source locations, and remediation hints.
- **Dashboards**: Generate weekly (Fridays 18:00 UTC) dashboards summarizing IR duration, compiler success rates, and trace coverage; store artefacts under `build/reports/dashboard-YYYYMMDD.json`.
- **Governance Integration**: Supply machine-readable manifest and trace schemas along with sample payloads for governance tooling ingestion.
- **Documentation Updates**: Refresh the DSL quickstart, CLI reference, and governance handbook to include staged emission instructions and audit checklist requirements.

## Constraints & Tradeoffs

- Generated Fortran prioritizes predictable compiler optimization, accepting reduced human readability compared to hand-crafted source. Manual tuning occurs after emission if needed.
- Sequential compiler executions must respect documented HPC queue and resource constraints to avoid starving shared infrastructure.

## Terminology

- **Manifest**: JSON document (`build/fortran/<config_hash>/manifest.json`) enumerating generated Fortran files, supporting artefacts, and compiler validation outcomes.
- **Report**: CLI output metadata files (`build/ir/<config_hash>/report.json`, `build/fortran/<config_hash>/report.json`) summarizing command inputs, configuration hash, and execution metrics.
- **Trace**: Mapping file (`build/fortran/<config_hash>/trace.json`) linking IR nodes to generated Fortran line ranges for audit review.

## Clarifications

### Session 2025-10-31

- Q: Which compiler toolchains must the Fortran emission be validated against for release readiness? → A: gfortran, Intel oneAPI Fortran, NVIDIA NVFortran (all)
- Q: What is explicitly out of scope for this release? → A: Deliver a proof-of-concept hook for a second target only
- Q: How are intermediate representation packages uniquely identified? → A: DSL model identifier plus configuration hash
- Q: How should the generated Fortran balance readability versus optimization? → A: Favor compiler-friendly structure with minimal explanatory comments
- Q: What tradeoff governs Fortran emission priorities? → A: Prioritize predictable compiler performance over maintainability

## Assumptions & Dependencies

- Reference Fortran 2003 compilers and runtime libraries are available in the build environment for validation runs.
- Governance tooling can ingest the JSON reports produced by the staged emit workflow.
- Pilot DSL models supply the necessary metadata (naming, versioning) to populate manifests without additional input.
