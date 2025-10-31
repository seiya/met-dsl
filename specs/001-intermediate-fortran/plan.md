# Implementation Plan: Intermediate IR and Fortran Emission

**Branch**: `001-intermediate-fortran` | **Date**: 2025-10-31 | **Spec**: [specs/001-intermediate-fortran/spec.md](specs/001-intermediate-fortran/spec.md)
**Input**: Feature specification from `/specs/001-intermediate-fortran/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/commands/plan.md` for the execution workflow.

## Summary

Build a staged CLI that transforms weather-model DSL files into a normalized intermediate representation and then emits Fortran 2003 source ready for multi-compiler validation. The implementation emphasises deterministic IR identity, compiler-friendly Fortran output with minimal annotations, telemetry dashboards with fallbacks, and governance artefacts (manifests, audit traces, ingestion interface) for production approval.

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: Python 3.9+  
**Primary Dependencies**: Typer (CLI), Pydantic (config validation), Jinja2 (Fortran templating), Rich (CLI status output)  
**Storage**: N/A (filesystem artefacts only)  
**Testing**: pytest, golden IR fixtures, compiler smoke tests (gfortran 11+, Intel oneAPI Fortran, NVIDIA NVFortran)  
**Target Platform**: Linux HPC build nodes (x86_64) with required Fortran toolchains  
**Project Type**: Single-project CLI tool  
**Performance Goals**: IR emission <5 min per model; ≥90% first-pass compile success across three compilers; CLI operations stream status updates within 5 seconds of each stage; weekly dashboards summarize telemetry for governance review  
**Constraints**: Must keep emitted Fortran compiler-optimisation friendly with minimal annotations; staged workflow must remain deterministic per DSL/config hash; discovery hook exposes future targets without codegen; IR telemetry captures emission duration and feeds dashboard automation for Principle V reporting; sequential compiler executions must respect documented cluster scheduling/resource constraints  
**Scale/Scope**: Pilot cohort (~10 DSL models) with expansion to wider catalogue after validation; supports up to 3 compiler profiles per run; single-team maintenance

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- [x] **Principle I** – Specification `specs/001-intermediate-fortran/spec.md` defines syntax, success criteria, and governance inputs; plan blocks work if spec gaps emerge.
- [x] **Principle II** – CLI-based golden IR fixtures and multi-compiler smoke tests captured in Testing/Success Criteria to prove deterministic behaviour.
- [x] **Principle III** – `metdsl emit` CLI remains the single interface, emits structured JSON/NDJSON, and provides progress feedback.
- [x] **Principle IV** – User stories map to independently releasable stages (IR emission, Fortran emission, audit verification) with explicit blocking relationships.
- [x] **Principle V** – Documentation updates, telemetry events, and governance manifests are enumerated to keep observability and audit trails intact.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)
<!--
  ACTION REQUIRED: Replace the placeholder tree below with the concrete layout
  for this feature. Delete unused options and expand the chosen structure with
  real paths (e.g., apps/admin, packages/something). The delivered plan must
  not include Option labels.
-->

```text
src/
├── metdsl/
│   ├── cli/
│   │   └── emit.py
│   ├── ir/
│   │   ├── builder.py
│   │   └── validators.py
│   ├── fortran/
│   │   ├── generator.py
│   │   └── templates/
│   └── telemetry/
│       └── events.py
└── __init__.py

tests/
├── golden/
│   └── README.md
├── contract/
├── integration/
└── unit/
```

**Structure Decision**: Single CLI-oriented Python package (`src/metdsl`) with dedicated submodules for IR, Fortran emission, telemetry, and Typer-powered CLI entrypoint; tests mirror artefact types (golden, contract, integration, unit).

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| *None* | *N/A* | *N/A* |
