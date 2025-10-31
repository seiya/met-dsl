from __future__ import annotations

from typing import Dict, List


def validate_ir_package(ir_package: Dict[str, object]) -> List[Dict[str, object]]:
    issues: List[Dict[str, object]] = []

    normalized_ir = ir_package.get("normalized_ir", [])
    for op in normalized_ir:
        if isinstance(op, dict) and op.get("requires_numerical_fidelity"):
            issues.append(
                {
                    "code": "NUMERICAL_FIDELITY_REVIEW",
                    "severity": "warning",
                    "message": (
                        "Operation requires numerical fidelity review before deploying to production."
                    ),
                    "location": {"index": op.get("index"), "statement": op.get("statement")},
                }
            )

        verb = op.get("verb") if isinstance(op, dict) else None
        if verb == "UNSUPPORTED":
            issues.append(
                {
                    "code": "UNSUPPORTED_OPERATION",
                    "severity": "error",
                    "message": "Operation verb 'UNSUPPORTED' is not recognized by the IR builder.",
                    "location": {"index": op.get("index"), "statement": op.get("statement")},
                }
            )

    return issues


__all__ = ["validate_ir_package"]
