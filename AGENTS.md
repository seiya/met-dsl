# met-dsl Development Guidelines

Auto-generated from all feature plans. Last updated: 2025-10-31

## Active Technologies
- Python 3.9+ + Typer, Pydantic, NumPy, SciPy, Xarray, netCDF4, Rich, Jinja2 (002-dsl-advection-solver)
- Local CF-compliant NetCDF files per run (002-dsl-advection-solver)
- Python 3.9+ (aligned with existing Typer CLI toolchain) + Typer, Pydantic, NumPy, SciPy, Xarray, netCDF4, Rich, Jinja2, lark-parser (003-generate-fortran-stencil)
- Local filesystem outputs (Fortran sources, manifests, NetCDF benchmark data) (003-generate-fortran-stencil)

- Python 3.9+ + Typer (CLI), Pydantic (config validation), Jinja2 (Fortran templating), Rich (CLI status output) (001-intermediate-fortran)

## Project Structure

```text
src/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.9+: Follow standard conventions

## Recent Changes
- 003-generate-fortran-stencil: Added Python 3.9+ (aligned with existing Typer CLI toolchain) + Typer, Pydantic, NumPy, SciPy, Xarray, netCDF4, Rich, Jinja2, lark-parser
- 002-dsl-advection-solver: Added Python 3.9+ + Typer, Pydantic, NumPy, SciPy, Xarray, netCDF4, Rich, Jinja2

- 001-intermediate-fortran: Added Python 3.9+ + Typer (CLI), Pydantic (config validation), Jinja2 (Fortran templating), Rich (CLI status output)

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
