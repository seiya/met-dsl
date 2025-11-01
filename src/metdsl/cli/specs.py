from __future__ import annotations

import json
import json
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import typer

from metdsl.telemetry.events import SolverLifecycleEvent, TelemetryEmitter
from metdsl.ir.versioning import (
    list_versions as registry_list_versions,
    parse_version_reference,
    register_version,
    resolve_version,
)

app = typer.Typer(help="Manage DSL specifications for the nonlinear advection example.")


def _read_example_dsl() -> str:
    template_path = resources.files("metdsl.examples").joinpath("nonlinear_advection.dsl")
    return template_path.read_text(encoding="utf-8")


def _build_telemetry_emitter(sink: Optional[Path]) -> TelemetryEmitter:
    primary = sink or Path("build/logs/spec.ndjson")
    fallback = primary.parent / "spec_fallback.ndjson"
    return TelemetryEmitter(primary_sink=primary, fallback_sink=fallback)


@app.command("create")
def create_spec(
    spec_id: str = typer.Option(..., "--spec-id", "-s", help="Identifier for the generated specification."),
    grid: Tuple[int, int, float, float] = typer.Option(
        (256, 256, 1.0, 1.0),
        "--grid",
        help="Grid dimensions and spacing: NX NY DX DY.",
    ),
    boundary: Tuple[str, str] = typer.Option(
        ("periodic", "periodic"),
        "--boundary",
        help="Boundary types for the x and y axes.",
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Output path for the emission configuration (JSON/YAML). Defaults to specs/examples/<spec-id>.yaml",
    ),
    dsl_output: Optional[Path] = typer.Option(
        None,
        "--dsl-output",
        help="Output path for the DSL source. Defaults next to the configuration file.",
    ),
    telemetry_sink: Optional[Path] = typer.Option(
        None, "--telemetry-sink", help="Optional telemetry sink for creation events."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow overwriting existing files."),
) -> None:
    """
    Scaffold the nonlinear advection example specification and its emission configuration.
    """

    nx, ny, dx, dy = grid
    boundary_x, boundary_y = (value.lower() for value in boundary)

    if boundary_x != "periodic" or boundary_y != "periodic":
        raise typer.BadParameter("Example specification currently supports only periodic boundaries.")

    config_path = config or Path("specs/examples") / f"{spec_id}.yaml"
    dsl_path = dsl_output or config_path.with_suffix(".dsl")

    for target in (config_path, dsl_path):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise typer.BadParameter(f"File already exists: {target}. Use --overwrite to replace it.")

    config_payload = {
        "target": "fortran2003",
        "grid": {"nx": nx, "ny": ny, "dx": dx, "dy": dy, "staggering": "arakawa_c"},
        "boundary_conditions": {"x": boundary_x, "y": boundary_y},
        "rk4": {
            "total_steps": 100,
            "time_step": 60.0,
            "stability_limit": None,
            "stage_labels": ["k1", "k2", "k3", "k4"],
        },
        "metadata": {
            "spec_id": spec_id,
            "dsl_version": "1.0.0",
            "description": "Nonlinear advection-diffusion DSL example with dual periodic boundaries.",
        },
    }

    if telemetry_sink:
        config_payload["telemetry_sink"] = str(telemetry_sink)

    config_path.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")
    dsl_path.write_text(_read_example_dsl(), encoding="utf-8")

    emitter = _build_telemetry_emitter(telemetry_sink)
    base_dir = config_path.parent if config_path.parent != Path(".") else Path("specs/examples")
    entry = register_version(
        spec_id,
        base_dir=base_dir,
        config_path=config_path,
        dsl_path=dsl_path,
        change_summary="Initial creation",
    )

    config_payload.setdefault("metadata", {})
    config_payload["metadata"]["spec_id"] = spec_id
    config_payload["metadata"]["version_id"] = entry.version_id
    config_payload["metadata"]["created_at"] = entry.created_at

    config_path.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")

    emitter.emit(
        SolverLifecycleEvent.SPEC_CREATED,
        spec_id=spec_id,
        version_id=entry.version_id,
        config_path=str(config_path.resolve()),
        dsl_path=str(dsl_path.resolve()),
        grid={"nx": nx, "ny": ny, "dx": dx, "dy": dy},
        boundary={"x": boundary_x, "y": boundary_y},
    )

    typer.echo(f"Specification created: {dsl_path}")
    typer.echo(f"Configuration written to: {config_path}")


def _apply_overrides(payload: Dict[str, object], overrides: List[str]) -> None:
    for item in overrides:
        if "=" not in item:
            raise typer.BadParameter(f"Override '{item}' must use key=value syntax.")
        key, value = item.split("=", 1)
        segments = key.split(".")
        cursor: Dict[str, object] = payload
        for segment in segments[:-1]:
            existing = cursor.get(segment)
            if not isinstance(existing, dict):
                existing = {}
                cursor[segment] = existing
            cursor = existing
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            parsed_value = value
        cursor[segments[-1]] = parsed_value


@app.command("clone")
def clone_spec(
    from_version: str = typer.Option(..., "--from-version", help="Source version reference (spec@v0001)."),
    change: Optional[str] = typer.Option(None, "--change", help="Summary describing the changes."),
    set_override: List[str] = typer.Option([], "--set", help="Configuration overrides in key=value form."),
    telemetry_sink: Optional[Path] = typer.Option(None, "--telemetry-sink", help="Telemetry sink override."),
    config: Optional[Path] = typer.Option(None, "--config", help="Output path for the cloned configuration."),
    dsl_output: Optional[Path] = typer.Option(None, "--dsl-output", help="Output path for the cloned DSL source."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow overwriting outputs."),
    registry_root: Path = typer.Option(Path("specs/examples"), "--registry-root", hidden=True),
) -> None:
    spec_id, version_id = parse_version_reference(from_version)
    source_entry = resolve_version(spec_id, version_id, registry_root)

    source_config = Path(source_entry["config_path"])
    source_dsl = Path(source_entry["dsl_path"])
    if not source_config.exists() or not source_dsl.exists():
        raise typer.BadParameter("Source configuration or DSL file not found.")

    target_config = config or source_config.with_name(f"{source_config.stem}_{version_id}.yaml")
    target_dsl = dsl_output or source_dsl.with_name(f"{source_dsl.stem}_{version_id}.dsl")

    for target in (target_config, target_dsl):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise typer.BadParameter(f"File already exists: {target}. Use --overwrite to replace it.")

    config_payload = json.loads(source_config.read_text(encoding="utf-8"))
    _apply_overrides(config_payload, set_override)

    base_dir = registry_root
    entry = register_version(
        spec_id,
        base_dir=base_dir,
        config_path=target_config,
        dsl_path=target_dsl,
        derived_from=version_id,
        change_summary=change,
    )

    config_payload.setdefault("metadata", {})
    config_payload["metadata"]["spec_id"] = spec_id
    config_payload["metadata"]["version_id"] = entry.version_id
    config_payload["metadata"]["derived_from"] = version_id
    if change:
        config_payload["metadata"]["change_summary"] = change
    config_payload["metadata"]["created_at"] = entry.created_at

    target_config.write_text(json.dumps(config_payload, indent=2) + "\n", encoding="utf-8")
    target_dsl.write_text(source_dsl.read_text(encoding="utf-8"), encoding="utf-8")

    emitter = _build_telemetry_emitter(telemetry_sink)
    emitter.emit(
        SolverLifecycleEvent.SPEC_CLONED,
        spec_id=spec_id,
        version_id=entry.version_id,
        derived_from=version_id,
        config_path=str(target_config.resolve()),
        dsl_path=str(target_dsl.resolve()),
    )

    typer.echo(f"[metdsl] Cloned specification version {version_id} -> {entry.version_id}")
    typer.echo(f"  DSL: {target_dsl}")
    typer.echo(f"  Config: {target_config}")


@app.command("list")
def list_versions(
    spec_id: str = typer.Argument(..., help="Specification identifier to list versions for."),
    registry_root: Path = typer.Option(Path("specs/examples"), "--registry-root", hidden=True),
) -> None:
    base_dir = registry_root
    entries = registry_list_versions(spec_id, base_dir)
    if not entries:
        typer.echo(f"No versions recorded for '{spec_id}'.")
        return

    typer.echo(f"Versions for '{spec_id}':")
    for entry in entries:
        version_id = entry.get("version_id")
        created_at = entry.get("created_at")
        derived = entry.get("derived_from") or "-"
        summary = entry.get("change_summary") or "-"
        typer.echo(f"  {version_id}  created={created_at}  derived_from={derived}  summary={summary}")


__all__ = ["app", "create_spec", "clone_spec", "list_versions"]
