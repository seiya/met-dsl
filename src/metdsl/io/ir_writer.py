from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Dict


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        tmp.write(content)
        tmp.flush()
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def write_ir_package(ir_package: Dict[str, object], destination: Path) -> Path:
    package = json.loads(json.dumps(ir_package))  # deep copy for mutation safety
    metadata = package.get("metadata")
    if isinstance(metadata, dict):
        version_info = {
            key: metadata.get(key)
            for key in ("spec_id", "version_id", "derived_from", "change_summary")
            if metadata.get(key) is not None
        }
        if version_info:
            package["version_info"] = version_info

    package_path = destination / "package.json"
    _atomic_write(package_path, json.dumps(package, indent=2, sort_keys=True))
    return package_path


def write_ir_report(report: Dict[str, object], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    report_path = path if path.suffix else path / "report.json"
    _atomic_write(report_path, json.dumps(report, indent=2, sort_keys=True))
    return report_path


__all__ = ["write_ir_package", "write_ir_report"]
