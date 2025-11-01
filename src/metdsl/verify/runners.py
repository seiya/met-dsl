from __future__ import annotations

import subprocess
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

from metdsl.config.models import EmissionConfig
from metdsl.config.hash import compute_config_hash
from metdsl.ir.builder import build_ir_package
from metdsl.ir.validators import validate_ir_package
from metdsl.io.ir_writer import write_ir_package
from metdsl.telemetry.events import TelemetryEmitter
from metdsl.fortran.generator import build_fortran_module
from metdsl.io.fortran_writer import write_fortran_module
from metdsl.fortran.manifest import build_manifest, build_trace
from metdsl.io.fortran_writer import write_manifest, write_trace
from .results import CompilerResult


COMPILERS = {
    "gfortran": ["gfortran", "-c"],
    "oneapi": ["ifort", "-c"],
    "nvfortran": ["nvfortran", "-c"],
}


class CompilerRunner:
    def __init__(self, config: EmissionConfig, telemetry: TelemetryEmitter) -> None:
        self.config = config
        self.telemetry = telemetry

    def run(self, module_path: Path, compiler: str) -> CompilerResult:
        executable = COMPILERS.get(compiler, [compiler])
        command = executable + [str(module_path)]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
            )
            return CompilerResult(
                compiler=compiler,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except FileNotFoundError:
            return CompilerResult(
                compiler=compiler,
                exit_code=-1,
                stdout="",
                stderr=f"Compiler '{compiler}' not found on PATH.",
            )


def run_compiler_validations(
    dsl_path: Path,
    config_path: Path,
    emission_config: EmissionConfig,
    telemetry: TelemetryEmitter,
    compilers: Sequence[str] | None = None,
) -> List[CompilerResult]:
    config_hash = compute_config_hash(emission_config)
    results: List[CompilerResult] = []

    ir_package = build_ir_package(dsl_path, config_hash, emission_config)
    ir_package["issues"] = validate_ir_package(ir_package)

    module_name, source = build_fortran_module(ir_package, emission_config)
    module_path = write_fortran_module(module_name, source, Path("build/verify") / config_hash)

    compiler_list = list(compilers) if compilers else list(COMPILERS.keys())
    runner = CompilerRunner(emission_config, telemetry)
    for compiler in compiler_list:
        result = runner.run(module_path, compiler)
        results.append(result)

    manifest = build_manifest(
        ir_package,
        module_name,
        str(module_path.resolve()),
        config_hash,
        emission_config.metadata,
        ir_package.get("issues", []),
    )
    trace = build_trace(ir_package)
    output_dir = Path("build/verify") / config_hash
    write_manifest(manifest, output_dir)
    write_trace(trace, output_dir)

    return results


def create_solver_manifest(
    *,
    run_dir: Path,
    ir_package: Dict[str, object],
    emission_config: EmissionConfig,
    module_name: str,
    module_path: Path,
    benchmark: str,
    config_path: Path,
    dsl_path: Path,
    benchmark_script: Path,
    tolerance: Dict[str, float],
) -> Path:
    config_hash = compute_config_hash(emission_config)
    manifest = build_manifest(
        ir_package,
        module_name,
        str(module_path.resolve()),
        config_hash,
        emission_config.metadata,
        ir_package.get("issues", []),
    )
    manifest.update(
        {
            "grid": json.loads(emission_config.grid.json()),
            "boundary_conditions": json.loads(emission_config.boundary_conditions.json()),
            "rk4": json.loads(emission_config.rk4.json()),
            "analysis": {
                "benchmark": benchmark,
                "script": str(benchmark_script.resolve()),
                "tolerance": tolerance,
            },
            "outputs": {
                "results_path": str((run_dir / "outputs" / "results.json").resolve()),
            },
            "config_path": str(config_path.resolve()),
            "dsl_path": str(dsl_path.resolve()),
            "ir_package_path": str((run_dir / "ir" / "package.json").resolve()),
        }
    )
    return write_manifest(manifest, run_dir)


def record_solver_metrics(run_dir: Path, metrics: Dict[str, float]) -> Path:
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "metrics": metrics,
    }
    path = run_dir / "outputs" / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
