from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List


def build_manifest(
    ir_package: Dict[str, object],
    module_name: str,
    module_path: str,
    config_hash: str,
    config_metadata: Dict[str, str],
    issues: List[Dict[str, object]],
) -> Dict[str, object]:
    return {
        "dsl_model_id": ir_package.get("dsl_model_id"),
        "dsl_version": ir_package.get("dsl_version"),
        "config_hash": config_hash,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "module_name": module_name,
        "module_path": module_path,
        "operation_count": len(ir_package.get("normalized_ir", [])),
        "issues": issues,
        "metadata": config_metadata,
    }


def build_trace(ir_package: Dict[str, object]) -> Dict[str, object]:
    trace_entries = []
    for index, operation in enumerate(ir_package.get("normalized_ir", [])):
        trace_entries.append(
            {
                "operation_index": operation.get("index", index),
                "statement": operation.get("statement"),
                "subroutine": f"op_{index}",
            }
        )
    return {"operations": trace_entries}


__all__ = ["build_manifest", "build_trace"]
