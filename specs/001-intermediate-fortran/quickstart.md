# Quickstart: Intermediate IR and Fortran Emission

## Prerequisites

- Python 3.9+ with `pip` or `uv`
- GNU Fortran 11+, Intel oneAPI Fortran, and NVIDIA NVFortran available on `PATH`
- Access to the weather-model DSL source files (`*.dsl`) and matching configuration files (`*.yaml` or `*.json`)

## 1. Install the Tooling

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[cli,dev]
```

The editable install exposes the `metdsl` CLI while keeping dependencies (Typer, Pydantic, Jinja2, Rich, pytest) isolated.

## 2. Prepare a Configuration

Create `configs/fortran-balanced.yaml` with the following structure:

```yaml
target: fortran2003
optimization_preset: balanced
compiler_overrides:
  gfortran: ["-fopenmp"]
  oneapi: ["-qopenmp"]
  nvfortran: ["-mp"]
metadata:
  author: "Jane Doe"
  purpose: "Typhoon forecast baseline"
```

## 3. Emit the Intermediate Representation

```bash
metdsl emit models/typhoon.dsl --stage ir --config configs/fortran-balanced.yaml --report build/ir/typhoon-report.json
```

Outputs:
- `build/ir/<config_hash>/package.json` – deterministic IR package captured for golden comparisons
- `build/ir/typhoon-report.json` – metadata + canonical configuration hash + validation issues
- NDJSON telemetry in `build/logs/emit.ndjson` (fallback telemetry written to `build/logs/fallback.ndjson` if the primary sink is unavailable)

## 4. Generate Fortran 2003 Artefacts

```bash
metdsl emit models/typhoon.dsl --stage fortran2003 --config configs/fortran-balanced.yaml --report build/fortran-report.json
```

Outputs:
- `build/fortran/` – compiler-friendly Fortran sources with minimal inline comments
- `build/manifest.json` – emission manifest referencing IR package
- `build/trace.json` – IR-to-Fortran mapping for governance review
- `build/logs/emit.ndjson` – telemetry covering all stages (fallback events recorded in `build/logs/fallback.ndjson` if needed)

## 5. Validate Compilers

```bash
metdsl verify --report build/fortran-report.json
```

The command sequentially runs gfortran, Intel oneAPI, and NVFortran smoke tests, appending results to the manifest and telemetry stream.

## 6. Discovery Hook for Future Targets

```bash
metdsl emit --list-targets
```

Returns supported targets with metadata describing discovery-only status.

## Troubleshooting

- **Compiler missing**: Ensure each compiler binary is discoverable via `PATH`; rerun `metdsl verify --report ...` after loading environment modules.
- **IR hash mismatch**: Delete stale artefacts under `build/` and regenerate both IR and Fortran stages to realign hashes.
- **Validation failure**: Inspect `build/logs/emit.ndjson` for `emit_failed` events; the log entry includes remediation hints derived from validation codes.

## Next Steps

- Review `build/manifest.json` and `build/trace.json` with the governance team.
- Archive IR package, manifest, telemetry, and compiler logs together for reproducibility.
