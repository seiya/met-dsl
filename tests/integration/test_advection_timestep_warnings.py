from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from metdsl.cli.emit import app as cli_app

RUNNER = CliRunner()


def _create_spec(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "advection_unstable.yaml"
    dsl_path = tmp_path / "advection_unstable.dsl"
    result = RUNNER.invoke(
        cli_app,
        [
            "spec",
            "create",
            "--spec-id",
            "advection-example",
            "--config",
            str(config_path),
            "--dsl-output",
            str(dsl_path),
            "--overwrite",
        ],
    )
    assert result.exit_code == 0, result.stdout
    return config_path, dsl_path


def test_timestep_warning_triggers_exit(tmp_path: Path) -> None:
    config_path, dsl_path = _create_spec(tmp_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload.setdefault("rk4", {})
    payload["rk4"]["total_steps"] = 200
    payload["rk4"]["time_step"] = 120.0
    payload["rk4"]["stability_limit"] = 60.0
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    run_dir = tmp_path / "runs" / "unstable"

    generate_result = RUNNER.invoke(
        cli_app,
        [
            "solver",
            "generate",
            "--config",
            str(config_path),
            "--dsl",
            str(dsl_path),
            "--output-dir",
            str(run_dir),
            "--overwrite",
        ],
    )
    assert generate_result.exit_code == 0, generate_result.stdout

    run_result = RUNNER.invoke(cli_app, ["solver", "run", "--run-id", str(run_dir)])
    assert run_result.exit_code != 0
    assert "Timestep warning" in run_result.stdout
