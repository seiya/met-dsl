from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Dict


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def write_fortran_module(module_name: str, source: str, destination: Path) -> Path:
    module_path = destination / f"{module_name}.f90"
    _atomic_write(module_path, source)
    return module_path


def write_manifest(manifest: Dict[str, object], destination: Path) -> Path:
    manifest_path = destination / "manifest.json"
    _atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True))
    return manifest_path


def write_trace(trace: Dict[str, object], destination: Path) -> Path:
    trace_path = destination / "trace.json"
    _atomic_write(trace_path, json.dumps(trace, indent=2, sort_keys=True))
    return trace_path


__all__ = ["write_fortran_module", "write_manifest", "write_trace"]
