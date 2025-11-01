from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from metdsl.cli.emit import app as cli_app

RUNNER = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent


def _create_spec(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "advection.yaml"
    dsl_path = tmp_path / "advection.dsl"
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


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_solver_pipeline_success(tmp_path: Path) -> None:
    config_path, dsl_path = _create_spec(tmp_path)
    run_dir = tmp_path / "runs" / "success"

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

    validate_result = RUNNER.invoke(cli_app, ["solver", "validate", "--run-id", str(run_dir)])
    assert validate_result.exit_code == 0, validate_result.stdout

    payload = json.loads(validate_result.stdout)
    assert payload["status"] == "passed"

    manifest = _load_json(run_dir / "manifest.json")
    manifest["generated_at"] = "<<DYNAMIC>>"
    manifest["module_path"] = "<<DYNAMIC_PATH>>"
    manifest["config_path"] = "<<DYNAMIC_PATH>>"
    manifest["dsl_path"] = "<<DYNAMIC_PATH>>"
    manifest["ir_package_path"] = "<<DYNAMIC_PATH>>"
    manifest["outputs"]["results_path"] = "<<DYNAMIC_PATH>>"
    manifest["analysis"]["script"] = "<<DYNAMIC_PATH>>"
    manifest["config_hash"] = "<<DYNAMIC_HASH>>"
    manifest["metadata"]["created_at"] = "<<DYNAMIC>>"
    expected_manifest = _load_json(REPO_ROOT / "golden/advection_solver/manifest.json")
    assert manifest == expected_manifest


def test_solver_pipeline_reports_validation_error(tmp_path: Path) -> None:
    config_path, dsl_path = _create_spec(tmp_path)
    run_dir = tmp_path / "runs" / "incomplete"

    filtered_lines = [
        line
        for line in Path(dsl_path).read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("STENCIL")
    ]
    Path(dsl_path).write_text("\n".join(filtered_lines) + "\n", encoding="utf-8")

    result = RUNNER.invoke(
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

    assert result.exit_code != 0
    stdout = result.stdout.strip()
    assert "STENCILS_MISSING" in stdout
