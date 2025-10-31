from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

import typer
import yaml

from metdsl.config.hash import compute_config_hash
from metdsl.config.models import EmissionConfig, Stage
from metdsl.telemetry.events import TelemetryEmitter
from metdsl.fortran.generator import build_fortran_module
from metdsl.fortran.manifest import build_manifest, build_trace
from metdsl.io.fortran_writer import write_fortran_module, write_manifest, write_trace
from metdsl.io.ir_writer import write_ir_package, write_ir_report
from metdsl.ir.builder import build_ir_package, build_ir_report
from metdsl.ir.validators import validate_ir_package

app = typer.Typer(help="Met DSL emission CLI (IR, Fortran generation, verification).")


def load_config(path: Path) -> EmissionConfig:
    if not path.exists():
        raise typer.BadParameter(f"Config file not found: {path}")

    data: Dict[str, Any]
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(path.read_text())
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
    else:
        raise typer.BadParameter("Unsupported config format; use JSON or YAML.")

    return EmissionConfig.parse_obj(data)


def build_telemetry_emitter(
    telemetry_sink: Optional[Path], fallback: Path = Path("build/logs/fallback.ndjson")
) -> TelemetryEmitter:
    sink = telemetry_sink or Path("build/logs/emit.ndjson")
    return TelemetryEmitter(primary_sink=sink, fallback_sink=fallback)


@app.command()
def emit(
    dsl_path: Path = typer.Argument(..., exists=True, file_okay=True, dir_okay=False),
    stage: Stage = typer.Option(Stage.IR, case_sensitive=False),
    config: Path = typer.Option(..., exists=True, file_okay=True, dir_okay=False),
    report: Optional[Path] = typer.Option(
        None,
        file_okay=True,
        dir_okay=False,
        help="Optional path for the generated report. Defaults to build/<stage>/<dsl>.report.json",
    ),
) -> None:
    """
    Execute an emission stage.

    For Phase 2 scaffold this command validates configuration and emits a telemetry seed event.
    Implementation phases will extend this command to build IR, Fortran, and verification artefacts.
    """

    emission_config = load_config(config)
    telemetry = build_telemetry_emitter(emission_config.telemetry_sink)
    config_hash = compute_config_hash(emission_config)
    telemetry.emit(
        "command_received",
        stage=stage.value,
        config_path=str(config.resolve()),
        config_hash=config_hash,
        discovery_only=emission_config.discovery_only,
    )

    if stage == Stage.IR:
        _handle_stage_ir(
            dsl_path=dsl_path,
            config_path=config,
            emission_config=emission_config,
            config_hash=config_hash,
            report_override=report,
            telemetry=telemetry,
        )
    elif stage == Stage.FORTRAN2003:
        _handle_stage_fortran(
            dsl_path=dsl_path,
            config_path=config,
            emission_config=emission_config,
            config_hash=config_hash,
            telemetry=telemetry,
        )
    else:
        typer.echo(
            f"Stage '{stage.value}' not yet implemented. Use 'ir' for Phase 3 functionality."
        )


@app.command("list-targets")
def list_targets() -> None:
    """
    Discovery-only hook reporting supported and experimental targets.
    """

    payload = {
        "supported": [
            {"name": "fortran2003", "stage": "emit", "discovery_only": False}
        ],
        "experimental": [
            {"name": "experimental", "stage": "emit", "discovery_only": True}
        ],
    }
    typer.echo(json.dumps(payload, indent=2))


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


from metdsl.fortran.generator import build_fortran_module
from metdsl.fortran.manifest import build_manifest, build_trace
from metdsl.io.fortran_writer import write_fortran_module, write_manifest, write_trace


def _handle_stage_ir(
    dsl_path: Path,
    config_path: Path,
    emission_config: EmissionConfig,
    config_hash: str,
    report_override: Optional[Path],
    telemetry: TelemetryEmitter,
) -> Dict[str, object]:
    output_dir = Path("build/ir") / config_hash
    report_path = report_override or (output_dir / "report.json")

    try:
        telemetry.emit(
            "emit_started",
            stage=Stage.IR.value,
            config_hash=config_hash,
            dsl_path=str(dsl_path.resolve()),
        )
        start_ts = time.perf_counter()
        ir_package = build_ir_package(dsl_path=dsl_path, config_hash=config_hash, config=emission_config)
        issues = validate_ir_package(ir_package)
        ir_package["issues"] = issues

        package_path = write_ir_package(ir_package, output_dir)

        report_dict = build_ir_report(ir_package, issues, config_path)
        report_dict["artifact_paths"]["package"] = str(package_path.resolve())
        report_dict["artifact_paths"]["report"] = str(Path(report_path).resolve())
        duration_ms = round((time.perf_counter() - start_ts) * 1000, 3)
        report_dict["duration_ms"] = duration_ms
        write_ir_report(report_dict, report_path)

        telemetry.emit(
            "emit_completed",
            stage=Stage.IR.value,
            config_hash=config_hash,
            issues=len(issues),
            package_path=str(package_path.resolve()),
            report_path=str(report_path.resolve()),
            duration_ms=duration_ms,
        )

        typer.echo(
            f"[metdsl] IR emission complete. Package -> {package_path}, Report -> {report_path}."
        )
        return {
            "ir_package": ir_package,
            "issues": issues,
            "package_path": package_path,
            "report_path": report_path,
        }
    except Exception as exc:  # pragma: no cover - defensive telemetry
        telemetry.emit(
            "emit_failed",
            stage=Stage.IR.value,
            config_hash=config_hash,
            error=str(exc),
        )
        raise typer.Exit(code=1) from exc


def _ensure_ir_package(
    dsl_path: Path,
    config_path: Path,
    emission_config: EmissionConfig,
    config_hash: str,
    telemetry: TelemetryEmitter,
) -> Dict[str, object]:
    ir_dir = Path("build/ir") / config_hash
    package_path = ir_dir / "package.json"
    if package_path.exists():
        data = json.loads(package_path.read_text(encoding="utf-8"))
        issues = data.get("issues", [])
        return {"ir_package": data, "issues": issues, "package_path": package_path}

    return _handle_stage_ir(
        dsl_path=dsl_path,
        config_path=config_path,
        emission_config=emission_config,
        config_hash=config_hash,
        report_override=None,
        telemetry=telemetry,
    )


def _cleanup_directory(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.glob("**/*"), reverse=True):
        if child.is_file():
            try:
                child.unlink()
            except OSError:
                pass
        elif child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _handle_stage_fortran(
    dsl_path: Path,
    config_path: Path,
    emission_config: EmissionConfig,
    config_hash: str,
    telemetry: TelemetryEmitter,
) -> None:
    fortran_root = Path("build/fortran")
    output_dir = fortran_root / config_hash
    temp_dir = fortran_root / f"{config_hash}.tmp"
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        telemetry.emit(
            "emit_started",
            stage=Stage.FORTRAN2003.value,
            config_hash=config_hash,
            dsl_path=str(dsl_path.resolve()),
        )

        start_ts = time.perf_counter()
        ir_info = _ensure_ir_package(
            dsl_path=dsl_path,
            config_path=config_path,
            emission_config=emission_config,
            config_hash=config_hash,
            telemetry=telemetry,
        )
        ir_package = ir_info["ir_package"]

        module_name, source = build_fortran_module(ir_package, emission_config)
        module_path_tmp = write_fortran_module(module_name, source, temp_dir)

        manifest = build_manifest(
            ir_package=ir_package,
            module_name=module_name,
            module_path=str((temp_dir / module_path_tmp.name).resolve()),
            config_hash=config_hash,
            config_metadata=emission_config.metadata,
            issues=ir_package.get("issues", []),
        )
        manifest_path_tmp = write_manifest(manifest, temp_dir)
        trace_path_tmp = write_trace(build_trace(ir_package), temp_dir)

        if output_dir.exists():
            shutil.rmtree(output_dir, ignore_errors=True)
        temp_dir.rename(output_dir)

        module_path = output_dir / module_path_tmp.name
        manifest_path = output_dir / manifest_path_tmp.name
        trace_path = output_dir / trace_path_tmp.name

        duration_ms = round((time.perf_counter() - start_ts) * 1000, 3)
        telemetry.emit(
            "emit_completed",
            stage=Stage.FORTRAN2003.value,
            config_hash=config_hash,
            module_path=str(module_path.resolve()),
            manifest_path=str(manifest_path.resolve()),
            trace_path=str(trace_path.resolve()),
            duration_ms=duration_ms,
        )

        typer.echo(
            "[metdsl] Fortran emission complete. "
            f"Module -> {module_path}, Manifest -> {manifest_path}, Trace -> {trace_path}."
        )
    except Exception as exc:  # pragma: no cover - defensive telemetry
        telemetry.emit(
            "emit_failed",
            stage=Stage.FORTRAN2003.value,
            config_hash=config_hash,
            error=str(exc),
        )
        _cleanup_directory(temp_dir)
        raise typer.Exit(code=1) from exc


def _cleanup_directory(path: Path) -> None:
    if path.exists():
        for item in path.iterdir():
            if item.is_file():
                item.unlink(missing_ok=True)
            elif item.is_dir():
                _cleanup_directory(item)
                item.rmdir()
