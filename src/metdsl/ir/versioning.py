from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class VersionEntry:
    version_id: str
    created_at: str
    config_path: str
    dsl_path: str
    derived_from: Optional[str] = None
    change_summary: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "version_id": self.version_id,
            "created_at": self.created_at,
            "config_path": self.config_path,
            "dsl_path": self.dsl_path,
        }
        if self.derived_from is not None:
            payload["derived_from"] = self.derived_from
        if self.change_summary:
            payload["change_summary"] = self.change_summary
        return payload


REGISTRY_SUFFIX = ".versions.json"


def _registry_path(spec_id: str, base_dir: Path) -> Path:
    return base_dir / f"{spec_id}{REGISTRY_SUFFIX}"


def load_registry(spec_id: str, base_dir: Path) -> Dict[str, object]:
    path = _registry_path(spec_id, base_dir)
    if not path.exists():
        return {"spec_id": spec_id, "versions": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(spec_id: str, base_dir: Path, registry: Dict[str, object]) -> Path:
    path = _registry_path(spec_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    return path


def _next_sequence(versions: Iterable[Dict[str, object]]) -> int:
    max_seq = 0
    for entry in versions:
        version_id = entry.get("version_id", "")
        if version_id.startswith("v"):
            try:
                seq = int(version_id[1:])
            except ValueError:
                continue
            max_seq = max(max_seq, seq)
    return max_seq + 1


def format_version_id(sequence: int) -> str:
    return f"v{sequence:04d}"


def register_version(
    spec_id: str,
    *,
    base_dir: Path,
    config_path: Path,
    dsl_path: Path,
    derived_from: Optional[str] = None,
    change_summary: Optional[str] = None,
) -> VersionEntry:
    registry = load_registry(spec_id, base_dir)
    versions: List[Dict[str, object]] = list(registry.get("versions", []))

    sequence = _next_sequence(versions)
    version_id = format_version_id(sequence)
    created_at = datetime.now(tz=timezone.utc).isoformat()

    entry = VersionEntry(
        version_id=version_id,
        created_at=created_at,
        config_path=str(config_path),
        dsl_path=str(dsl_path),
        derived_from=derived_from,
        change_summary=change_summary,
    )

    versions.append(entry.to_dict())
    registry["versions"] = versions
    save_registry(spec_id, base_dir, registry)
    return entry


def list_versions(spec_id: str, base_dir: Path) -> List[Dict[str, object]]:
    registry = load_registry(spec_id, base_dir)
    return list(registry.get("versions", []))


def parse_version_reference(reference: str) -> Tuple[str, str]:
    if "@" not in reference:
        raise ValueError("Version reference must use the format <spec-id>@<version-id>.")
    spec_id, version_id = reference.split("@", 1)
    if not spec_id or not version_id:
        raise ValueError("Version reference must include both spec id and version id.")
    return spec_id, version_id


def resolve_version(spec_id: str, version_id: str, base_dir: Path) -> Dict[str, object]:
    versions = list_versions(spec_id, base_dir)
    for entry in versions:
        if entry.get("version_id") == version_id:
            return entry
    raise ValueError(f"Version '{version_id}' not found for specification '{spec_id}'.")


__all__ = [
    "VersionEntry",
    "register_version",
    "list_versions",
    "parse_version_reference",
    "resolve_version",
    "load_registry",
    "save_registry",
    "format_version_id",
]
