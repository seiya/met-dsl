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
    metadata: Optional[Dict[str, object]] = None

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "index": self.index,
            "verb": self.verb,
            "statement": self.statement,
            "requires_numerical_fidelity": self.requires_numerical_fidelity,
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


def _default_clock() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_dsl_lines(lines: List[str]) -> Dict[str, object]:
    operations: List[NormalizedOperation] = []
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    stencils: List[Dict[str, object]] = []
    fields: List[Dict[str, object]] = []
    rk4_stages: List[Dict[str, object]] = []

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

        metadata: Optional[Dict[str, object]] = None

        if head == "FIELD" and len(tokens) >= 2:
            field_name = tokens[1]
            staggered_axis: Optional[str] = None
            location: Optional[str] = None
            extra: Dict[str, object] = {}
            for token in tokens[2:]:
                lower_token = token.lower()
                if lower_token.startswith("staggered:"):
                    staggered_axis = lower_token.split(":", 1)[1]
                elif ":" in lower_token:
                    key, value = lower_token.split(":", 1)
                    extra[key] = value
                else:
                    location = lower_token
            field_entry = {
                "name": field_name,
                "location": location,
                "staggered_axis": staggered_axis,
            }
            if extra:
                field_entry["attributes"] = extra
            fields.append(field_entry)
            metadata = field_entry

        elif head == "STENCIL" and len(tokens) >= 2:
            stencil_name = tokens[1]
            params: Dict[str, object] = {"name": stencil_name}
            for token in tokens[2:]:
                if "=" in token:
                    key, value = token.split("=", 1)
                    key = key.lower()
                    if key == "order":
                        try:
                            params[key] = int(value)
                        except ValueError:
                            params[key] = value
                    elif key == "fields":
                        params[key] = [field.strip() for field in value.split(",") if field.strip()]
                    else:
                        params[key] = value
            if "scheme" not in params:
                params["scheme"] = "unspecified"
            if "fields" not in params:
                params["fields"] = []
            stencils.append(params)
            metadata = params

        elif head == "RK4_STAGE" and len(tokens) >= 2:
            stage_label = tokens[1]
            action = " ".join(tokens[2:]) if len(tokens) > 2 else ""
            stage_entry = {"label": stage_label, "action": action}
            rk4_stages.append(stage_entry)
            metadata = stage_entry

        operations.append(
            NormalizedOperation(
                index=idx,
                verb=head,
                statement=statement,
                requires_numerical_fidelity=requires_numeric,
                metadata=metadata,
            )
        )

    return {
        "operations": operations,
        "model_name": model_name,
        "model_version": model_version,
        "stencils": stencils,
        "fields": fields,
        "rk4_stages": rk4_stages,
    }


def build_ir_package(
    dsl_path: Path,
    config_hash: str,
    config: EmissionConfig,
    clock: Callable[[], datetime] = _default_clock,
) -> Dict[str, object]:
    """
    Build a normalized IR package from a DSL source file.

    The function is intentionally conservative - it normalizes statements into simple verb/statement
    pairs while capturing ordering and whether additional numerical fidelity review is required.
    """

    if not dsl_path.exists():
        raise FileNotFoundError(f"DSL file not found: {dsl_path}")

    parsed = _parse_dsl_lines(dsl_path.read_text(encoding="utf-8").splitlines())

    dsl_model_id = parsed["model_name"] or dsl_path.stem
    dsl_version = parsed["model_version"] or config.metadata.get("dsl_version", "0.0.1")

    operations = [op.as_dict() for op in parsed["operations"]]

    grid_dict = json.loads(config.grid.json())
    boundary_dict = json.loads(config.boundary_conditions.json())
    rk4_dict = json.loads(config.rk4.json())

    ir_package: Dict[str, object] = {
        "dsl_model_id": dsl_model_id,
        "dsl_version": dsl_version,
        "config_hash": config_hash,
        "created_at": clock().isoformat(),
        "source_path": str(dsl_path),
        "normalized_ir": operations,
        "metadata": config.metadata,
        "grid": grid_dict,
        "boundary_conditions": boundary_dict,
        "rk4": rk4_dict,
        "fields": parsed["fields"],
        "stencils": parsed["stencils"],
        "rk4_stages": parsed["rk4_stages"],
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
