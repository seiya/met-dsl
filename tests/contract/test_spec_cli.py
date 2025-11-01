from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from metdsl.cli.emit import app as cli_app


def test_spec_create_generates_expected_assets(tmp_path: Path) -> None:
    config_path = tmp_path / "advection.yaml"
    dsl_path = tmp_path / "advection.dsl"

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "spec",
            "create",
            "--spec-id",
            "advection-example",
            "--grid",
            "128",
            "64",
            "2.0",
            "1.5",
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
    assert dsl_path.exists()
    dsl_contents = dsl_path.read_text(encoding="utf-8")
    assert "STENCIL" in dsl_contents
    assert "RK4_STAGE" in dsl_contents

    config_payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert config_payload["grid"] == {"nx": 128, "ny": 64, "dx": 2.0, "dy": 1.5, "staggering": "arakawa_c"}
    assert config_payload["boundary_conditions"] == {"x": "periodic", "y": "periodic"}
    assert config_payload["metadata"]["spec_id"] == "advection-example"
    assert config_payload["metadata"]["version_id"].startswith("v")
    assert config_payload["target"] == "fortran2003"


def test_spec_clone_and_list_versions(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    config_root = registry_root
    config_root.mkdir(parents=True, exist_ok=True)

    runner = CliRunner()
    base_config = config_root / "advection.yaml"
    base_dsl = config_root / "advection.dsl"

    result = runner.invoke(
        cli_app,
        [
            "spec",
            "create",
            "--spec-id",
            "advection-example",
            "--config",
            str(base_config),
            "--dsl-output",
            str(base_dsl),
            "--overwrite",
        ],
    )
    assert result.exit_code == 0, result.stdout

    list_result = runner.invoke(
        cli_app,
        [
            "spec",
            "list",
            "advection-example",
            "--registry-root",
            str(registry_root),
        ],
    )
    assert list_result.exit_code == 0, list_result.stdout
    assert "v0001" in list_result.stdout

    clone_config = config_root / "advection_clone.yaml"
    clone_dsl = config_root / "advection_clone.dsl"
    clone_result = runner.invoke(
        cli_app,
        [
            "spec",
            "clone",
            "--from-version",
            "advection-example@v0001",
            "--registry-root",
            str(registry_root),
            "--change",
            "Viscosity adjustment",
            "--set",
            "metadata.notes=\"clone\"",
            "--config",
            str(clone_config),
            "--dsl-output",
            str(clone_dsl),
            "--overwrite",
        ],
    )
    assert clone_result.exit_code == 0, clone_result.stdout

    clone_payload = json.loads(clone_config.read_text(encoding="utf-8"))
    assert clone_payload["metadata"]["version_id"] == "v0002"
    assert clone_payload["metadata"]["derived_from"] == "v0001"
    assert clone_payload["metadata"]["change_summary"] == "Viscosity adjustment"

    list_result = runner.invoke(
        cli_app,
        [
            "spec",
            "list",
            "advection-example",
            "--registry-root",
            str(registry_root),
        ],
    )
    assert "v0002" in list_result.stdout
