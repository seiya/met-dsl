from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from metdsl.config.hash import compute_config_hash
from metdsl.config.models import EmissionConfig
from metdsl.ir.builder import build_ir_package
from metdsl.ir.validators import validate_ir_package


def test_build_ir_package_matches_golden() -> None:
    dsl_path = Path("tests/golden/ir/typhoon.dsl")
    expected_path = Path("tests/golden/ir/typhoon_ir.json")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    config = EmissionConfig()
    config_hash = compute_config_hash(config)

    fixed_clock = lambda: datetime(2025, 10, 31, 12, 0, tzinfo=timezone.utc)
    ir_package = build_ir_package(dsl_path, config_hash, config, clock=fixed_clock)

    issues = validate_ir_package(ir_package)
    ir_package["issues"] = issues

    # Update expected source path to match environment (relative path)
    expected["source_path"] = str(dsl_path)

    assert ir_package == expected
