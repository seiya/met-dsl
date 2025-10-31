# Met DSL Emission CLI

CLI tooling for transforming Meteorological DSL source files into normalized intermediate
representations and compiler-friendly Fortran 2003 artefacts with governance-ready telemetry.

## Quick Start (Ubuntu 22.04)

```bash
sudo apt update
sudo apt install python3-typer python3-pydantic python3-jinja2 python3-rich python3-yaml python3-pytest

PYTHONPATH=src pytest
```
