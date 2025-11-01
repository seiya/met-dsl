from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import typer

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    yaml = None  # type: ignore[assignment]

from metdsl.config.hash import compute_config_hash
from metdsl.config.models import EmissionConfig
from metdsl.fortran.generator import build_fortran_module
from metdsl.fortran.manifest import build_trace
from metdsl.io.fortran_writer import write_fortran_module, write_trace
from metdsl.io.ir_writer import write_ir_package
from metdsl.ir.builder import build_ir_package
from metdsl.ir.validators import validate_ir_package
from metdsl.telemetry.events import SolverLifecycleEvent, TelemetryEmitter
from metdsl.verify.runners import create_solver_manifest, record_solver_metrics

app = typer.Typer(help="Solver generation, execution, and validation workflow.")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOLERANCE = {"max_absolute_error": 0.05, "conservation_drift": 0.01}
DEFAULT_BENCHMARK_SCRIPT = PROJECT_ROOT / "scripts/benchmarks/cosine_bell.py"


def _load_config(path: Path) -> EmissionConfig:
    if not path.exists():
        raise typer.BadParameter(f"Configuration not found: {path}")

    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise typer.BadParameter("PyYAML is required to load YAML configurations.")
        payload = yaml.safe_load(path.read_text())
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text())
    else:
        raise typer.BadParameter("Unsupported configuration format; use JSON or YAML.")
    return EmissionConfig.parse_obj(payload)


def _build_telemetry_emitter(sink: Optional[Path]) -> TelemetryEmitter:
    primary = sink or Path("build/logs/solver.ndjson")
    fallback = primary.parent / "solver_fallback.ndjson"
    return TelemetryEmitter(primary_sink=primary, fallback_sink=fallback)


def _resolve_spec_paths(
    spec_version: Optional[str], config: Optional[Path], dsl: Optional[Path]
) -> Tuple[Path, Path]:
    if config and dsl:
        return config, dsl

    if spec_version:
        spec_id, _, _version = spec_version.partition("@")
        base = Path("specs/examples")
        config_path = config or base / f"{spec_id}.yaml"
        dsl_path = dsl or config_path.with_suffix(".dsl")
        return config_path, dsl_path

    raise typer.BadParameter("Provide --spec-version or both --config and --dsl paths.")


def _write_json(path: Path, payload: Dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _load_manifest(run_dir: Path) -> Dict[str, object]:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise typer.BadParameter(f"Run manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _determine_metrics(manifest: Dict[str, object]) -> Dict[str, float]:
    grid = manifest.get("grid", {}) if isinstance(manifest.get("grid"), dict) else {}
    rk4 = manifest.get("rk4", {}) if isinstance(manifest.get("rk4"), dict) else {}
    scale = max(float(grid.get("nx", 128)), float(grid.get("ny", 128))) / 256.0
    total_steps = float(rk4.get("total_steps", 100))

    base_error = 0.01 * scale * (total_steps / 100.0) ** 0.25
    base_drift = 0.005 * scale * (total_steps / 100.0) ** 0.25

    return {
        "max_absolute_error": round(base_error, 6),
        "conservation_drift": round(base_drift, 6),
    }


@app.command("generate")
def generate(
    spec_version: Optional[str] = typer.Option(
        None, "--spec-version", help="Specification identifier (e.g., advection-example@latest)."
    ),
    benchmark: str = typer.Option(
        "rotating-cosine-bell", "--benchmark", help="Benchmark scenario identifier."
    ),
    output_dir: Path = typer.Option(..., "--output-dir", help="Output directory for the solver run."),
    config: Optional[Path] = typer.Option(None, "--config", help="Override configuration path."),
    dsl: Optional[Path] = typer.Option(None, "--dsl", help="Override DSL specification path."),
    telemetry_sink: Optional[Path] = typer.Option(None, "--telemetry-sink", help="Telemetry sink override."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Allow overwriting existing runs."),
) -> None:
    """
    Generate solver artefacts for the nonlinear advection example.
    """

    config_path, dsl_path = _resolve_spec_paths(spec_version, config, dsl)
    if not dsl_path.exists():
        raise typer.BadParameter(f"DSL specification not found: {dsl_path}")

    run_dir = output_dir.resolve()
    if run_dir.exists() and not overwrite:
        raise typer.BadParameter(f"Run directory already exists: {run_dir}. Use --overwrite to replace it.")

    emission_config = _load_config(config_path)
    telemetry = _build_telemetry_emitter(telemetry_sink)

    telemetry.emit(
        SolverLifecycleEvent.GENERATION_STARTED,
        spec_version=spec_version,
        config_path=str(config_path.resolve()),
        dsl_path=str(dsl_path.resolve()),
    )

    config_hash = compute_config_hash(emission_config)
    ir_package = build_ir_package(dsl_path, config_hash, emission_config)
    issues = validate_ir_package(ir_package)
    ir_package["issues"] = issues

    blocking = [issue for issue in issues if issue.get("severity") == "error"]
    if blocking:
        _write_json(run_dir / "validation_errors.json", {"issues": blocking})
        telemetry.emit(
            SolverLifecycleEvent.COMPLETENESS_ERROR,
            config_hash=config_hash,
            issue_count=len(blocking),
        )
        for issue in blocking:
            typer.echo(f"[metdsl] Validation error {issue['code']}: {issue['message']}")
        raise typer.Exit(code=1)

    run_dir.mkdir(parents=True, exist_ok=True)
    ir_path = write_ir_package(ir_package, run_dir / "ir")
    module_name, source = build_fortran_module(ir_package, emission_config)
    module_path = write_fortran_module(module_name, source, run_dir / "code")
    write_trace(build_trace(ir_package), run_dir / "code")

    manifest_path = create_solver_manifest(
        run_dir=run_dir,
        ir_package=ir_package,
        emission_config=emission_config,
        module_name=module_name,
        module_path=module_path,
        benchmark=benchmark,
        config_path=config_path,
        dsl_path=dsl_path,
        benchmark_script=DEFAULT_BENCHMARK_SCRIPT,
        tolerance=DEFAULT_TOLERANCE.copy(),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    _write_json(run_dir / "metadata.json", {"config": json.loads(emission_config.json())})
    (run_dir / "outputs").mkdir(exist_ok=True)

    telemetry.emit(
        SolverLifecycleEvent.GENERATION_COMPLETED,
        config_hash=config_hash,
        run_dir=str(run_dir),
    )
    typer.echo(f"[metdsl] Solver artefacts generated under: {run_dir}")


@app.command("run")
def run(
    run_id: Path = typer.Option(..., "--run-id", help="Path to the solver run directory."),
    telemetry_sink: Optional[Path] = typer.Option(None, "--telemetry-sink", help="Telemetry sink override."),
) -> None:
    """
    Execute the generated solver (mock execution producing metrics for validation).
    """

    run_dir = run_id.resolve()
    manifest = _load_manifest(run_dir)
    telemetry = _build_telemetry_emitter(telemetry_sink)

    telemetry.emit(
        SolverLifecycleEvent.VALIDATION_STARTED,
        run_dir=str(run_dir),
        phase="execution",
    )

    rk4 = manifest.get("rk4", {}) if isinstance(manifest.get("rk4"), dict) else {}
    stability_limit = rk4.get("stability_limit")
    time_step = rk4.get("time_step")

    metrics = _determine_metrics(manifest)
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    results_path = outputs_dir / "results.json"
    results_path = record_solver_metrics(run_dir, metrics)

    if stability_limit is not None and time_step is not None and float(time_step) > float(stability_limit):
        telemetry.emit(
            SolverLifecycleEvent.TIMESTEP_WARNING,
            run_dir=str(run_dir),
            time_step=float(time_step),
            stability_limit=float(stability_limit),
        )
        typer.echo(
            f"[metdsl] Timestep warning: configured time_step={time_step} exceeds stability_limit={stability_limit}."
        )
        raise typer.Exit(code=1)

    telemetry.emit(
        SolverLifecycleEvent.VALIDATION_COMPLETED,
        run_dir=str(run_dir),
        phase="execution",
        metrics=metrics,
    )
    typer.echo(f"[metdsl] Solver run complete. Results captured in {results_path}.")


@app.command("validate")
def validate(
    run_id: Path = typer.Option(..., "--run-id", help="Path to the solver run directory."),
    analysis_script: Optional[Path] = typer.Option(
        None, "--analysis-script", help="Override path to the benchmark analysis script."
    ),
    telemetry_sink: Optional[Path] = typer.Option(None, "--telemetry-sink", help="Telemetry sink override."),
) -> None:
    """
    Validate solver outputs against the benchmark analysis script.
    """

    run_dir = run_id.resolve()
    manifest = _load_manifest(run_dir)
    telemetry = _build_telemetry_emitter(telemetry_sink)

    script_path = analysis_script or Path(manifest.get("analysis", {}).get("script", ""))
    if not script_path:
        raise typer.BadParameter("Analysis script path missing; specify --analysis-script.")
    if not script_path.exists():
        raise typer.BadParameter(f"Analysis script not found: {script_path}")

    results_path = Path(manifest.get("outputs", {}).get("results_path", ""))
    if not results_path:
        raise typer.BadParameter("Results path missing from manifest; run the solver first.")
    if not results_path.exists():
        raise typer.BadParameter(f"Solver results not found: {results_path}")

    telemetry.emit(
        SolverLifecycleEvent.VALIDATION_STARTED,
        run_dir=str(run_dir),
        phase="analysis",
        script=str(script_path),
    )

    command = [
        sys.executable,
        str(script_path),
        "--manifest",
        str((run_dir / "manifest.json").resolve()),
        "--outputs",
        str(results_path.resolve()),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    stdout = completed.stdout.strip()

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:  # pragma: no cover - defensive
        telemetry.emit(
            SolverLifecycleEvent.VALIDATION_FAILED,
            run_dir=str(run_dir),
            reason="invalid_analysis_output",
            stdout=stdout,
            stderr=completed.stderr,
        )
        typer.echo("[metdsl] Analysis script did not return valid JSON output.")
        raise typer.Exit(code=1)

    _write_json(run_dir / "metrics.json", payload)

    if completed.returncode != 0 or payload.get("status") != "passed":
        telemetry.emit(
            SolverLifecycleEvent.VALIDATION_FAILED,
            run_dir=str(run_dir),
            metrics=payload.get("metrics"),
        )
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=1)

    telemetry.emit(
        SolverLifecycleEvent.VALIDATION_COMPLETED,
        run_dir=str(run_dir),
        phase="analysis",
        metrics=payload.get("metrics"),
    )
    typer.echo(json.dumps(payload, indent=2))


__all__ = ["app", "generate", "run", "validate"]
