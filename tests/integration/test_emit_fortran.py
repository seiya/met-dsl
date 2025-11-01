from __future__ import annotations

import json
from pathlib import Path

import pytest

from metdsl.cli import emit as emit_cli
from metdsl.config.hash import compute_config_hash
from metdsl.config.models import EmissionConfig, Stage

REPO_ROOT = Path(__file__).resolve().parent.parent


def _prepare_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    models_dir = tmp_path / "models"
    configs_dir = tmp_path / "configs"
    models_dir.mkdir()
    configs_dir.mkdir()

    dsl_source = REPO_ROOT / "golden/ir/typhoon.dsl"
    dsl_path = models_dir / "typhoon.dsl"
    dsl_path.write_text(dsl_source.read_text(encoding="utf-8"), encoding="utf-8")

    config_path = configs_dir / "fortran-balanced.json"
    config_payload = {"target": "fortran2003"}
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    config_data = json.loads(config_path.read_text(encoding="utf-8"))
    config = EmissionConfig.parse_obj(config_data)
    config_hash = compute_config_hash(config)
    return dsl_path, config_path, config_hash


def test_emit_fortran_generates_expected_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    dsl_path, config_path, config_hash = _prepare_fixture(tmp_path)

    emission_config = emit_cli.load_config(config_path)
    telemetry = emit_cli.build_telemetry_emitter(emission_config.telemetry_sink)

    emit_cli._handle_stage_ir(
        dsl_path=dsl_path,
        config_path=config_path,
        emission_config=emission_config,
        config_hash=config_hash,
        report_override=None,
        telemetry=telemetry,
    )

    emit_cli._handle_stage_fortran(
        dsl_path=dsl_path,
        config_path=config_path,
        emission_config=emission_config,
        config_hash=config_hash,
        telemetry=telemetry,
    )

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

    emission_config = emit_cli.load_config(config_path)
    telemetry = emit_cli.build_telemetry_emitter(emission_config.telemetry_sink)

    emit_cli._handle_stage_ir(
        dsl_path=dsl_path,
        config_path=config_path,
        emission_config=emission_config,
        config_hash=config_hash,
        report_override=None,
        telemetry=telemetry,
    )

    def explode(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr("metdsl.cli.emit.write_manifest", explode)

    with pytest.raises(emit_cli.typer.Exit):
        emit_cli._handle_stage_fortran(
            dsl_path=dsl_path,
            config_path=config_path,
            emission_config=emission_config,
            config_hash=config_hash,
            telemetry=telemetry,
        )

    output_dir = Path(f"build/fortran/{config_hash}")
    assert not output_dir.exists()
