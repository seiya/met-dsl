from __future__ import annotations

import json
from pathlib import Path

from metdsl.ir.versioning import (
    format_version_id,
    list_versions,
    parse_version_reference,
    register_version,
    resolve_version,
)


def test_register_and_resolve_versions(tmp_path: Path) -> None:
    spec_id = "example"
    config_path = tmp_path / "config.yaml"
    dsl_path = tmp_path / "model.dsl"
    config_path.write_text("{}\n", encoding="utf-8")
    dsl_path.write_text("", encoding="utf-8")

    entry1 = register_version(
        spec_id,
        base_dir=tmp_path,
        config_path=config_path,
        dsl_path=dsl_path,
        change_summary="initial",
    )
    assert entry1.version_id == "v0001"

    entry2 = register_version(
        spec_id,
        base_dir=tmp_path,
        config_path=config_path,
        dsl_path=dsl_path,
        derived_from=entry1.version_id,
    )
    assert entry2.version_id == "v0002"

    versions = list_versions(spec_id, tmp_path)
    assert len(versions) == 2

    spec, version = parse_version_reference(f"{spec_id}@{entry2.version_id}")
    assert spec == spec_id
    assert version == "v0002"

    resolved = resolve_version(spec_id, entry2.version_id, tmp_path)
    assert resolved["derived_from"] == entry1.version_id

    assert format_version_id(5) == "v0005"
