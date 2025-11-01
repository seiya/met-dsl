from __future__ import annotations

from typing import Dict, List, Optional


def _add_issue(
    issues: List[Dict[str, object]],
    *,
    code: str,
    severity: str,
    message: str,
    location: Optional[Dict[str, object]] = None,
) -> None:
    issue: Dict[str, object] = {"code": code, "severity": severity, "message": message}
    if location:
        issue["location"] = location
    issues.append(issue)


def validate_ir_package(ir_package: Dict[str, object]) -> List[Dict[str, object]]:
    issues: List[Dict[str, object]] = []

    normalized_ir = ir_package.get("normalized_ir", [])
    for op in normalized_ir:
        if isinstance(op, dict) and op.get("requires_numerical_fidelity"):
            _add_issue(
                issues,
                code="NUMERICAL_FIDELITY_REVIEW",
                severity="warning",
                message="Operation requires numerical fidelity review before deploying to production.",
                location={"index": op.get("index"), "statement": op.get("statement")},
            )

        verb = op.get("verb") if isinstance(op, dict) else None
        if verb == "UNSUPPORTED":
            _add_issue(
                issues,
                code="UNSUPPORTED_OPERATION",
                severity="error",
                message="Operation verb 'UNSUPPORTED' is not recognized by the IR builder.",
                location={"index": op.get("index"), "statement": op.get("statement")},
            )

    metadata = ir_package.get("metadata") or {}
    spec_id = metadata.get("spec_id") if isinstance(metadata, dict) else None

    if spec_id:
        grid = ir_package.get("grid", {})
        if not grid:
            _add_issue(
                issues,
                code="GRID_MISSING",
                severity="error",
                message="Grid definition is required for nonlinear advection specifications.",
            )
        else:
            nx = grid.get("nx")
            ny = grid.get("ny")
            if isinstance(nx, int) and nx % 2 != 0:
                _add_issue(
                    issues,
                    code="GRID_NX_ODD",
                    severity="error",
                    message="Grid dimension nx must be even for Arakawa-C staggering.",
                )
            if isinstance(ny, int) and ny % 2 != 0:
                _add_issue(
                    issues,
                    code="GRID_NY_ODD",
                    severity="error",
                    message="Grid dimension ny must be even for Arakawa-C staggering.",
                )

        boundaries = ir_package.get("boundary_conditions", {})
        if boundaries.get("x") != "periodic" or boundaries.get("y") != "periodic":
            _add_issue(
                issues,
                code="BOUNDARY_UNSUPPORTED",
                severity="error",
                message="Only dual periodic boundary conditions are supported for the example specification.",
            )

        stencils = ir_package.get("stencils") or []
        if not stencils:
            _add_issue(
                issues,
                code="STENCILS_MISSING",
                severity="error",
                message="At least one stencil definition is required in the DSL specification.",
            )
        else:
            for stencil in stencils:
                if not stencil.get("fields"):
                    _add_issue(
                        issues,
                        code="STENCIL_FIELDS_MISSING",
                        severity="error",
                        message="Stencil definitions must declare participating fields.",
                        location={"stencil": stencil.get("name")},
                    )

        fields = ir_package.get("fields") or []
        if not fields:
            _add_issue(
                issues,
                code="FIELDS_MISSING",
                severity="error",
                message="At least one field declaration is required for solver generation.",
            )

        rk4_stages = ir_package.get("rk4_stages") or []
        rk4_config = ir_package.get("rk4") or {}
        expected_stage_count = len(rk4_config.get("stage_labels", []))
        if expected_stage_count and len(rk4_stages) != expected_stage_count:
            _add_issue(
                issues,
                code="RK4_STAGE_MISMATCH",
                severity="error",
                message="RK4 stage definitions must match the configured stage labels.",
                location={"expected": expected_stage_count, "actual": len(rk4_stages)},
            )

    return issues


__all__ = ["validate_ir_package"]
