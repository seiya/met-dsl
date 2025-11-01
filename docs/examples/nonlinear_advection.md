# Nonlinear Advection Solver Example

This walkthrough demonstrates the complete lifecycle for the nonlinear advection-diffusion example that ships with the Met DSL. It covers specification scaffolding, solver emission, validation, and reuse, aligning with the user stories defined in the feature spec.

## Prerequisites

- Python 3.9+ with project dependencies installed (`pip install -e .[dev]`)
- Access to the `metdsl` CLI (installed automatically via the package entry point)
- Write access to the repository so generated artefacts can be inspected under `build/`

## 1. Create the Example Specification

```bash
metdsl spec create \
  --spec-id advection-example \
  --grid 256 256 1.0 1.0 \
  --boundary periodic periodic \
  --config specs/examples/advection_periodic.yaml
```

This command copies the canonical DSL (`src/metdsl/examples/nonlinear_advection.dsl`) into the specified location and generates an emission configuration that encodes the Arakawa-C grid, dual periodic boundaries, and RK4 parameters. Telemetry for the creation event is recorded under `build/logs/spec.ndjson`.

## 2. Generate Solver Artefacts

```bash
metdsl emit \
  src/metdsl/examples/nonlinear_advection.dsl \
  --stage fortran2003 \
  --config specs/examples/advection_periodic.yaml
```

The CLI normalises the DSL into IR, validates completeness, and renders Fortran solver modules along with manifest and trace files. Generated artefacts live under `build/fortran/<config-hash>/`.

## 3. Validate Against the Benchmark

```bash
metdsl solver generate \
  --config specs/examples/advection_periodic.yaml \
  --dsl src/metdsl/examples/nonlinear_advection.dsl \
  --benchmark rotating-cosine-bell \
  --output-dir runs/advection-example/run-001
metdsl solver run --run-id runs/advection-example/run-001
metdsl solver validate --run-id runs/advection-example/run-001
```

Validation compares NetCDF outputs with the rotating cosine bell analytic solution and reports metrics the DSL specification declares. Integration tests in `tests/integration/test_advection_validation.py` mirror this flow.; Telemetry events (`solver.validation.*`) capture success or failure, and diagnostics are stored next to the run artefacts. 

## 4. Review and Clone Specification Versions

```bash
metdsl spec list advection-example
```

This displays all recorded versions, their creation timestamps, and the lineage between them. Clone an existing version to capture variations while preserving traceability:

```bash
metdsl spec clone \
  --from-version advection-example@v0001 \
  --change "Viscosity sensitivity study" \
  --set metadata.notes="nu=1.0e-4"
```

Cloning issues a new auto-generated version identifier while keeping lineage to the parent specification. Update the configuration with any parameter tweaks before re-running emission or validation commands.

## 5. Record Telemetry and Feedback

- CLI runs record telemetry under `build/logs/` (`solver.onboarding.session_recorded`, `solver.feedback.pilot_recorded`).
- After completing the walkthrough, capture the onboarding duration and qualitative observations in `docs/feedback/nonlinear_advection_feedback.md`.

## Troubleshooting

- **Missing inputs**: The CLI blocks generation if grid, physics, boundary, or timestep fields are absent and emits actionable error messages.
- **Stability warnings**: If the timestep exceeds the configured stability limit, the validator raises `solver.validation.timestep_warning` telemetry and aborts the run.
- **Boundary or physics mismatches**: Attempts to use non-periodic boundaries or unsupported physics trigger validation errors with guidance on supported configurations.

