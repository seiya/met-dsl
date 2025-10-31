from __future__ import annotations

import json
from pathlib import Path

import yaml

import pytest
from typer.testing import CliRunner

from metdsl.cli.emit import app
from metdsl.config.hash import compute_config_hash
from metdsl.config.models import EmissionConfig, Stage
from metdsl.verify.runners import run_compiler_validations
from metdsl.telemetry.events import TelemetryEmitter

RUNNER = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent


def _prepare_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    models_dir = tmp_path / "models"
    configs_dir = tmp_path / "configs"
    models_dir.mkdir()
    configs_dir.mkdir()

    dsl_source = REPO_ROOT / "golden/ir/typhoon.dsl"
    dsl_path = models_dir / "typhoon.dsl"
    dsl_path.write_text(dsl_source.read_text(encoding="utf-8"), encoding="utf-8")

    config_path = configs_dir / "fortran-balanced.yaml"
    config_path.write_text("target: fortran2003\n", encoding="utf-8")

    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = EmissionConfig.parse_obj(config_data)
    config_hash = compute_config_hash(config)
    return dsl_path, config_path, config_hash


def test_emit_fortran_generates_expected_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    dsl_path, config_path, config_hash = _prepare_fixture(tmp_path)

    result_ir = RUNNER.invoke(app, ["emit", str(dsl_path), "--stage", Stage.IR.value, "--config", str(config_path)])
    assert result_ir.exit_code == 0, result_ir.output

    result_fortran = RUNNER.invoke(
        app, ["emit", str(dsl_path), "--stage", Stage.FORTRAN2003.value, "--config", str(config_path)]
    )
    assert result_fortran.exit_code == 0, result_fortran.output

    module_path = Path(f"build/fortran/{config_hash}/typhoon.f90")
    manifest_path = Path(f"build/fortran/{config_hash}/manifest.json")
    trace_path = Path(f"build/fortran/{config_hash}/trace.json")

    expected_module = (REPO_ROOT / "golden/fortran/typhoon.f90").read_text(encoding="utf-8")
    actual_module = module_path.read_text(encoding="utf-8")
    assert actual_module.split() == expected_module.split()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    golden_manifest = json.loads((REPO_ROOT / "golden/fortran/typhoon_manifest.json").read_text(encoding="utf-8"))
    assert manifest["dsl_model_id"] == golden_manifest["dsl_model_id"]
    assert manifest["operation_count"] == golden_manifest["operation_count"]
    assert manifest["issues"] == golden_manifest["issues"]

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace["operations"][0]["subroutine"] == "op_0"
    assert trace["operations"][1]["subroutine"] == "op_1"


def test_emit_fortran_cleans_partial_files_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    dsl_path, config_path, config_hash = _prepare_fixture(tmp_path)

    result_ir = RUNNER.invoke(app, ["emit", str(dsl_path), "--stage", Stage.IR.value, "--config", str(config_path)])
    assert result_ir.exit_code == 0, result_ir.output

    def explode(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr("metdsl.cli.emit.write_manifest", explode)

    result_fortran = RUNNER.invoke(
        app, ["emit", str(dsl_path), "--stage", Stage.FORTRAN2003.value, "--config", str(config_path)]
    )
    assert result_fortran.exit_code != 0

    output_dir = Path(f"build/fortran/{config_hash}")
    assert not output_dir.exists()
