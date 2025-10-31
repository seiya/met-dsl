from __future__ import annotations

import re
from typing import Dict, Tuple

from metdsl.config.models import EmissionConfig
from metdsl.fortran.templates import render_module

MODULE_RE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_module_name(name: str) -> str:
    sanitized = MODULE_RE.sub("_", name or "module")
    if not sanitized:
        sanitized = "generated_module"
    if sanitized[0].isdigit():
        sanitized = f"m_{sanitized}"
    return sanitized.lower()


def build_fortran_module(
    ir_package: Dict[str, object],
    config: EmissionConfig,
) -> Tuple[str, str]:
    """Render the Fortran module source from the IR package.

    Returns a tuple of (module_name, source).
    """

    module_name = _sanitize_module_name(str(ir_package.get("dsl_model_id", "module")))
    operations = ir_package.get("normalized_ir", [])
    source = render_module(module_name=module_name, operations=operations)  # type: ignore[arg-type]
    return module_name, source


__all__ = ["build_fortran_module"]
