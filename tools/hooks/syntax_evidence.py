#!/usr/bin/env python3
"""Host-owned, leaf-unwritable syntax evidence for the conductor-run compiler syntax gate.

The compiler syntax gate is a deterministic `generate.syntax` substep run in-process by the
conductor (`Conductor._gate_inproc -> _gate_syntax_check`), NOT by the leaf. It runs the MCP `run_syntax_check`
compiler adapters (gfortran `-fsyntax-only` first, then any optional target-compiler stages
from `METDSL_SYNTAX_COMPILERS`) over the staged sources. The `post_generate` validator
certifies that the gate actually ran with the mandatory gfortran stage passing. That
certificate must NOT be forgeable by the leaf, so it lives at the **pipeline root**
(`workspace/pipelines/<safe>/<pipeline_id>/syntax_evidence/<source_id>.json`) — the same
leaf-non-writable location as `lint_evidence/` — and is written ONLY host-side by the
conductor. The validator reads it read-only and fail-closes when it is missing/invalid.

Mirrors `tools/hooks/lint_evidence.py` (see its docstring for the full non-forgeability
rationale). Like the lint certificate, this one is written DURING the in-process
`generate.syntax` substep, so the write-attribution check
(`orchestration_runtime._validate_actual_write_paths`) explicitly EXEMPTS the EXACT
`<pipeline_root>/syntax_evidence/<source_id>.json` certificate, scoped to
step==generate ∧ substep==syntax. The exemption is the exact file, NOT the whole
`syntax_evidence/` directory; the sandboxed `generate.generate` leaf is never exempted.

Stage schema: each entry of `stages` records one compiler adapter run —
`{compiler, status: "pass"|"fail"|"skipped", compiler_version?, command_id?, command_log_ref?}`.
`command_id`/`command_log_ref` are required for pass/fail stages (they bind the stage to a
`command_log.jsonl` record) and absent/ignored for a `skipped` stage (an optional target
compiler that is not installed in this environment — nothing ran, so there is no record).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_STAGE_STATUSES = frozenset({"pass", "fail", "skipped"})


def _safe_component(value: str, label: str) -> str:
    """Reject anything that is not a bare, traversal-free path component so a
    malformed/hostile id cannot redirect the evidence read/write outside the
    pipeline's `syntax_evidence/` dir."""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"unsafe {label} for syntax evidence path: {value!r}")
    return value


def syntax_evidence_path(*, pipeline_root: Path, source_id: str) -> Path:
    sid = _safe_component(source_id, "source_id")
    return pipeline_root / "syntax_evidence" / f"{sid}.json"


def read_syntax_evidence(
    *, pipeline_root: Path, source_id: str
) -> dict[str, Any] | None:
    """Return the validated evidence dict, or None if the file is absent/unreadable.
    Raises ValueError on a present-but-malformed certificate (callers treat that as
    fail-closed)."""
    path = syntax_evidence_path(pipeline_root=pipeline_root, source_id=source_id)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        raise ValueError("syntax evidence must be a json object")
    ok = doc.get("ok")
    checked_at = doc.get("checked_at")
    stages = doc.get("stages")
    if not isinstance(ok, bool):
        raise ValueError("syntax evidence ok must be bool")
    if not isinstance(checked_at, str):
        raise ValueError("syntax evidence checked_at must be string")
    if not isinstance(stages, list) or not stages:
        raise ValueError("syntax evidence stages must be a non-empty array")
    for idx, entry in enumerate(stages):
        if not isinstance(entry, dict):
            raise ValueError(f"syntax evidence stages[{idx}] must be object")
        compiler = entry.get("compiler")
        if not isinstance(compiler, str) or not compiler.strip():
            raise ValueError(
                f"syntax evidence stages[{idx}].compiler must be non-empty string"
            )
        status = entry.get("status")
        if status not in _STAGE_STATUSES:
            raise ValueError(
                f"syntax evidence stages[{idx}].status must be one of "
                f"{sorted(_STAGE_STATUSES)}"
            )
        if status == "skipped":
            continue
        for key in ("command_id", "command_log_ref"):
            val = entry.get(key)
            if not isinstance(val, str) or not val.strip():
                raise ValueError(
                    f"syntax evidence stages[{idx}].{key} must be non-empty string "
                    f"for a {status} stage"
                )
    return doc


def write_syntax_evidence(
    *,
    pipeline_root: Path,
    source_id: str,
    ok: bool,
    stages: list[dict[str, str]],
) -> None:
    path = syntax_evidence_path(pipeline_root=pipeline_root, source_id=source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_id": source_id,
        "ok": ok,
        "stages": stages,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
