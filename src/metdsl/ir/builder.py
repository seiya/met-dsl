from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from metdsl.config.models import EmissionConfig


@dataclass
class NormalizedOperation:
    index: int
    verb: str
    statement: str
    requires_numerical_fidelity: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "verb": self.verb,
            "statement": self.statement,
            "requires_numerical_fidelity": self.requires_numerical_fidelity,
        }


def _default_clock() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dsl_lines(lines: List[str]) -> Dict[str, object]:
    operations: List[NormalizedOperation] = []
    model_name: Optional[str] = None
    model_version: Optional[str] = None

    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        tokens = line.split()
        head = tokens[0].upper()

        if head == "MODEL" and len(tokens) >= 2:
            model_name = tokens[1]
            continue

        if head == "VERSION" and len(tokens) >= 2:
            model_version = tokens[1]
            continue

        requires_numeric = "!REQUIRES_NUMERICS" in [token.upper() for token in tokens]
        statement = " ".join(tokens)

        operations.append(
            NormalizedOperation(
                index=idx,
                verb=head,
                statement=statement,
                requires_numerical_fidelity=requires_numeric,
            )
        )

    return {
        "operations": operations,
        "model_name": model_name,
        "model_version": model_version,
    }


def build_ir_package(
    dsl_path: Path,
    config_hash: str,
    config: EmissionConfig,
    clock: Callable[[], datetime] = _default_clock,
) -> Dict[str, object]:
    """
    Build a normalized IR package from a DSL source file.

    The function is intentionally conservative – it normalizes statements into simple verb/statement
    pairs while capturing ordering and whether additional numerical fidelity review is required.
    """

    if not dsl_path.exists():
        raise FileNotFoundError(f"DSL file not found: {dsl_path}")

    parsed = _parse_dsl_lines(dsl_path.read_text(encoding="utf-8").splitlines())

    dsl_model_id = parsed["model_name"] or dsl_path.stem
    dsl_version = parsed["model_version"] or config.metadata.get("dsl_version", "0.0.1")

    operations = [op.as_dict() for op in parsed["operations"]]

    ir_package: Dict[str, object] = {
        "dsl_model_id": dsl_model_id,
        "dsl_version": dsl_version,
        "config_hash": config_hash,
        "created_at": clock().isoformat(),
        "source_path": str(dsl_path),
        "normalized_ir": operations,
        "metadata": config.metadata,
    }

    return ir_package


def build_ir_report(
    ir_package: Dict[str, object],
    issues: List[Dict[str, object]],
    config_path: Path,
) -> Dict[str, object]:
    return {
        "dsl_model_id": ir_package["dsl_model_id"],
        "config_hash": ir_package["config_hash"],
        "config_path": str(config_path.resolve()),
        "issues": issues,
        "artifact_paths": {},
    }


def serialize_ir_package(ir_package: Dict[str, object]) -> str:
    return json.dumps(ir_package, sort_keys=True, indent=2)


__all__ = ["build_ir_package", "build_ir_report", "serialize_ir_package"]
