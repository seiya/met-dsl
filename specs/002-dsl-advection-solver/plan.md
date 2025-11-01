# Implementation Plan: Nonlinear Advection Solver DSL

**Branch**: `[002-dsl-advection-solver]` | **Date**: 2025-11-01 | **Spec**: [spec.md](./spec.md)  
**Input**: Feature specification from `/specs/002-dsl-advection-solver/spec.md`

## Summary

Deliver an instructional DSL example that produces a 2D nonlinear advection–diffusion solver on an Arakawa-C grid, honoring dual periodic boundaries, user-authored second-order stencils, and RK4 time integration while emitting outputs for an external validation script and supporting reusable templates.

## Technical Context

**Language/Version**: Python 3.9+  
**Primary Dependencies**: Typer, Pydantic, NumPy, SciPy, Xarray, netCDF4, Rich, Jinja2  
**Storage**: Local CF-compliant NetCDF files per run  
**Testing**: pytest, golden regression fixtures via CLI, ruff check  
**Target Platform**: POSIX CLI environments (developer workstations, CI runners)  
**Project Type**: Single CLI-oriented DSL toolchain  
**Performance Goals**: Complete 256×256 grid runs for 500 RK4 steps within documented tolerances and emit benchmark diagnostics in one guided session (<30 minutes author effort)  
**Constraints**: Preserve DSL readability, apply only essential optimizations, enforce 2D scope with dual periodic boundaries, and surface actionable CLI/telemetry errors for incomplete specifications  
**Scale/Scope**: One exemplar specification plus reusable templates covering solver generation, validation, and cloning workflows

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Specification-Driven Language Changes**: PASS – specification enumerates syntax, semantics, user stories, and success criteria for the DSL example.  
- **II. Executable Semantics as Tests**: PASS – plan will extend CLI regression flow with benchmark fixtures and failure diagnostics.  
- **III. CLI-First Tooling Exposure**: PASS – workflows remain exposed through existing Typer CLI with structured outputs for validation scripts.  
- **IV. Incremental Feature Delivery**: PASS – stories remain independent (spec creation, validation, reuse) and will map to separable tasks.  
- **V. Traceable Observability and Documentation**: PASS – plan includes documentation updates, structured outputs, and telemetry hooks for validation.  

✅ **Gate Result**: All constitution principles satisfied; proceed to Phase 0 research.  
🔁 **Post-Phase 1 Review**: No new violations introduced; principles remain satisfied.

## Project Structure

### Documentation (this feature)

```text
specs/002-dsl-advection-solver/
├── plan.md              # This file (/speckit.plan output)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Created by /speckit.tasks (Phase 2)
```

### Source Code (repository root)

```text
src/
└── metdsl/
    ├── cli/             # Typer CLI entry points and commands (emit, solver, specs)
    ├── config/          # Pydantic configuration schemas
    ├── fortran/         # Jinja2 templates for generated Fortran code
    ├── io/              # NetCDF persistence and Xarray helpers
    ├── ir/              # DSL intermediate representations
    ├── telemetry/       # Logging and diagnostics integrations
    └── verify/          # Validation pipelines and benchmark utilities

tests/
├── contract/            # Contract and CLI interaction tests
├── golden/              # Golden files / executable semantics
├── integration/         # End-to-end solver generation tests
└── unit/                # Focused unit tests
```

**Structure Decision**: Reuse existing single-project layout under `src/metdsl` with complementary test suites; new feature work extends CLI, IR, IO, telemetry, and verification modules without altering top-level structure.

## Implementation Strategy Highlights

- Extend the CLI with `spec create`, `spec clone`, and validation subcommands so authors can manage example specifications end-to-end.
- Harden specification completeness checks to reject missing grid, physics, boundary, or timestep data with human-readable CLI output plus structured telemetry.
- Capture both positive (solver generation) and negative (validation failures) integration tests, including golden outputs for manifests and error messages.
- Execute a high-resolution (256×256 grid, 500 RK4 steps) benchmark scenario to validate performance envelopes and surface telemetry thresholds.
- Surface timestep stability warnings with recommended limits when user-selected steps violate CFL-like constraints.
- Instrument onboarding CLI flows to record authoring duration and collect pilot feedback artifacts for success criteria evaluation.

## Complexity Tracking

No constitution exceptions required; complexity tracking table not applicable.
