from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from metdsl.cli.emit import app as cli_app

RUNNER = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent


def _create_spec(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "advection_highres.yaml"
    dsl_path = tmp_path / "advection_highres.dsl"
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


def test_high_resolution_run_generates_expected_metrics(tmp_path: Path) -> None:
    config_path, dsl_path = _create_spec(tmp_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload.setdefault("rk4", {})
    payload["rk4"]["total_steps"] = 500
    payload["rk4"]["time_step"] = 30.0
    payload["rk4"]["stability_limit"] = 45.0
    config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    run_dir = tmp_path / "runs" / "highres"

    generate_result = RUNNER.invoke(
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
    assert generate_result.exit_code == 0, generate_result.stdout

    run_result = RUNNER.invoke(cli_app, ["solver", "run", "--run-id", str(run_dir)])
    assert run_result.exit_code == 0, run_result.stdout

    validate_result = RUNNER.invoke(cli_app, ["solver", "validate", "--run-id", str(run_dir)])
    assert validate_result.exit_code == 0, validate_result.stdout

    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    expected = json.loads((REPO_ROOT / "golden/advection_solver/highres_metrics.json").read_text(encoding="utf-8"))
    metrics["generated_at"] = "<<DYNAMIC>>"
    metrics.setdefault("metadata", {})["config_hash"] = "<<DYNAMIC_HASH>>"
    assert metrics == expected
