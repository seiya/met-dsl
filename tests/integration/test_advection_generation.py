from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from metdsl.cli.emit import app as cli_app
from metdsl.config.hash import compute_config_hash
from metdsl.config.models import EmissionConfig

RUNNER = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent


def _scaffold_spec(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "advection_config.yaml"
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
    return dsl_path, config_path


def test_advection_generation_produces_periodic_fortran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsl_path, config_path = _scaffold_spec(tmp_path)
    monkeypatch.chdir(tmp_path)

    emit_result = RUNNER.invoke(
        cli_app,
        [
            "emit",
            str(dsl_path),
            "--stage",
            "fortran2003",
            "--config",
            str(config_path),
        ],
    )
    assert emit_result.exit_code == 0, emit_result.stdout

    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    config = EmissionConfig.parse_obj(config_payload)
    config_hash = compute_config_hash(config)

    build_root = tmp_path / "build" / "fortran" / config_hash
    module_path = build_root / "nonlinear_advection.f90"
    manifest_path = build_root / "manifest.json"

    assert module_path.exists()
    module_source = module_path.read_text(encoding="utf-8")
    assert "Periodic flux wrapping enabled" in module_source

    actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert actual_manifest["module_name"] == "nonlinear_advection"
    assert actual_manifest["operation_count"] >= 1


def test_advection_generation_fails_for_incomplete_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsl_path, config_path = _scaffold_spec(tmp_path)

    dsl_lines = [line for line in dsl_path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("STENCIL")]
    dsl_path.write_text("\n".join(dsl_lines) + "\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    emit_result = RUNNER.invoke(
        cli_app,
        [
            "emit",
            str(dsl_path),
            "--stage",
            "fortran2003",
            "--config",
            str(config_path),
        ],
    )

    assert emit_result.exit_code != 0
    stdout = emit_result.stdout.strip()
    expected_output = json.loads(
        (REPO_ROOT / "golden/advection_solver/error_output.json").read_text(encoding="utf-8")
    )["stdout"].strip()
    assert stdout == expected_output
