from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from metdsl.cli.emit import app as cli_app

RUNNER = CliRunner()


def _init_spec(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "advection.yaml"
    dsl_path = tmp_path / "advection.dsl"
    result = RUNNER.invoke(
        cli_app,
        [
            "spec",
            "create",
            "--spec-id",
            "advection-example",
            "--grid",
            "256",
            "256",
            "1.0",
            "1.0",
            "--boundary",
            "periodic",
            "periodic",
            "--config",
            str(config_path),
            "--dsl-output",
            str(dsl_path),
            "--overwrite",
        ],
    )
    assert result.exit_code == 0, result.stdout
    return config_path, dsl_path


def test_solver_validation_contract(tmp_path: Path) -> None:
    config_path, dsl_path = _init_spec(tmp_path)
    run_dir = tmp_path / "runs" / "advection"

    result = RUNNER.invoke(
        cli_app,
        [
            "solver",
            "generate",
            "--config",
            str(config_path),
            "--dsl",
            str(dsl_path),
            "--benchmark",
            "rotating-cosine-bell",
            "--output-dir",
            str(run_dir),
            "--overwrite",
        ],
    )
    assert result.exit_code == 0, result.stdout

    run_result = RUNNER.invoke(cli_app, ["solver", "run", "--run-id", str(run_dir)])
    assert run_result.exit_code == 0, run_result.stdout

    validate_result = RUNNER.invoke(
        cli_app,
        [
            "solver",
            "validate",
            "--run-id",
            str(run_dir),
        ],
    )
    assert validate_result.exit_code == 0, validate_result.stdout

    payload = json.loads(validate_result.stdout)
    assert payload["status"] == "passed"
    assert payload["benchmark"] == "rotating-cosine-bell"
    assert set(payload["metrics"]) == {"max_absolute_error", "conservation_drift"}
