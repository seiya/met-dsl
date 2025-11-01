from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader, Template


_TEMPLATE_DIR = Path(__file__).parent


def get_environment() -> Environment:
    loader = FileSystemLoader(str(_TEMPLATE_DIR))
    env = Environment(loader=loader, trim_blocks=True, lstrip_blocks=True)
    return env


def render_module(module_name: str, operations: List[Dict[str, object]]) -> str:
    env = get_environment()
    template: Template = env.get_template("module.f90.j2")
    return template.render(module_name=module_name, operations=operations)


__all__ = ["get_environment", "render_module"]
