#!/usr/bin/env python3
"""Host-owned, leaf-unwritable lint evidence for the conductor-run static lint.

Static lint is a deterministic `generate.lint` substep run in-process by the conductor
(`Conductor._lint_inproc`), NOT by the leaf. The `post_generate` validator certifies that
lint actually ran with the correct preset and succeeded. That certificate must NOT be
forgeable by the leaf, so it lives at the **pipeline root**
(`workspace/pipelines/<safe>/<pipeline_id>/lint_evidence/<source_id>.json`) — the same
leaf-non-writable location that already forces host authorship of `lineage.json` — and is
written ONLY host-side by the conductor. The validator reads it read-only and fail-closes
when it is missing/invalid.

Mirrors the codex feature-check cache pattern in `tools/hooks/codex_feature.py`. Placement
is keyed on the pipeline root (which the validator already receives as `--pipeline-root`)
rather than the orchestration id, so no extra `--orchestration-id` plumbing is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _safe_component(value: str, label: str) -> str:
    """Reject anything that is not a bare, traversal-free path component so a
    malformed/hostile id cannot redirect the evidence read/write outside the
    pipeline's `lint_evidence/` dir."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"unsafe {label} for lint evidence path: {value!r}")
    return value


def lint_evidence_path(*, pipeline_root: Path, source_id: str) -> Path:
    sid = _safe_component(source_id, "source_id")
    return pipeline_root / "lint_evidence" / f"{sid}.json"


def read_lint_evidence(
    *, pipeline_root: Path, source_id: str
) -> dict[str, Any] | None:
    """Return the validated evidence dict, or None if the file is absent/unreadable.
    Raises ValueError on a present-but-malformed certificate (callers treat that as
    fail-closed)."""
    path = lint_evidence_path(pipeline_root=pipeline_root, source_id=source_id)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        raise ValueError("lint evidence must be a json object")
    ok = doc.get("ok")
    preset = doc.get("preset")
    checked_at = doc.get("checked_at")
    run_linter = doc.get("run_linter")
    if not isinstance(ok, bool):
        raise ValueError("lint evidence ok must be bool")
    if not isinstance(preset, str) or not preset.strip():
        raise ValueError("lint evidence preset must be non-empty string")
    if not isinstance(checked_at, str):
        raise ValueError("lint evidence checked_at must be string")
    if not isinstance(run_linter, list) or not run_linter:
        raise ValueError("lint evidence run_linter must be a non-empty array")
    for idx, entry in enumerate(run_linter):
        if not isinstance(entry, dict):
            raise ValueError(f"lint evidence run_linter[{idx}] must be object")
        for key in ("preset", "command_id", "command_log_ref"):
            val = entry.get(key)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(
                    f"lint evidence run_linter[{idx}].{key} must be non-empty string"
                )
    return doc


def write_lint_evidence(
    *,
    pipeline_root: Path,
    source_id: str,
    preset: str,
    ok: bool,
    run_linter: list[dict[str, str]],
) -> None:
    path = lint_evidence_path(pipeline_root=pipeline_root, source_id=source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_id": source_id,
        "preset": preset,
        "ok": ok,
        "run_linter": run_linter,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
