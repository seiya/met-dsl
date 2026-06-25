#!/usr/bin/env python3
"""Codex feature probes for hook runtime, and the host-owned feature-check cache.

The codex-hooks feature gate (`tools/hooks/cli.py`) must NOT trust a cache the leaf can
forge. Under mandatory bwrap the per-orchestration `hooks/` and `audit/` dirs are bound
writable for the leaf, so a confined codex leaf could write a forged
`{"enabled": true, ...}` to bypass the gate. The cache therefore lives at the
orchestration-dir ROOT (NOT under `hooks/`), which is read-only inside the sandbox, and is
written ONLY host-side by the conductor (`Conductor._ensure_codex_feature_cache`). The hook
reads it read-only and fail-closes when it is missing/invalid.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_FEATURE_PROBE_TIMEOUT_SECONDS = 10.0


def _command_prefix(command: str | Sequence[str]) -> list[str]:
    """Normalize a probe command to an argv prefix. A custom `--llm-command` wrapper
    (e.g. `codexwrap --x`) must be invoked verbatim — the same prefix the leaf runs —
    so the probe certifies the SAME executable the leaf will use, not a hardcoded
    `codex`."""
    return [command] if isinstance(command, str) else [str(t) for t in command]


def parse_feature_list(raw: str) -> dict[str, bool]:
    features: dict[str, bool] = {}
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        enabled = parts[-1].lower()
        if enabled not in {"true", "false"}:
            continue
        name = parts[0].strip()
        if name:
            features[name] = enabled == "true"
    return features


def codex_hooks_feature_enabled(
    *,
    command: str | Sequence[str] = "codex",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = DEFAULT_FEATURE_PROBE_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    try:
        proc = runner(
            [*_command_prefix(command), "features", "list"],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"codex features list timed out after {timeout_seconds:.1f}s"
    except OSError as exc:
        # The command could not be executed at all (missing binary, bad --llm-command
        # wrapper, permission error). Serialize as a probe failure — same shape as a
        # nonzero exit / timeout — so the caller writes a disabled `probe_error` cache and
        # fail-closes, rather than crashing with an uncaught OSError.
        return False, f"codex features list failed: {exc}"
    detail = proc.stdout.strip() or proc.stderr.strip()
    if proc.returncode != 0:
        return False, f"codex features list failed: {detail}"
    features = parse_feature_list(proc.stdout)
    if features.get("hooks") is True:
        return True, "hooks=true"
    return False, f"hooks={features.get('hooks')}"


# --- host-owned feature-check cache -------------------------------------------------
#
# Path is the orchestration-dir ROOT (sibling of, NOT under, the leaf-writable `hooks/`
# and `audit/` dirs), so it is read-only inside the bwrap sandbox and cannot be forged by
# a confined leaf. Written host-side by the conductor; read read-only by the hook.

def codex_feature_cache_path(*, repo_root: Path, orchestration_id: str) -> Path:
    # The orchestration_id must be a single, traversal-free path component. The hook's
    # orchestration_id is payload-first (a confined leaf might influence it); a value like
    # "<orch>/hooks" or "../.." could otherwise redirect this RO cache read back into the
    # leaf-writable hooks/ dir (re-opening the forge hole). Reject anything that is not a
    # bare component so a malformed/hostile id fail-closes (the gate treats ValueError as
    # "no usable cache" -> block). Trusted host callers always pass a clean slug.
    if (
        not orchestration_id
        or orchestration_id in {".", ".."}
        or "/" in orchestration_id
        or "\\" in orchestration_id
        or "\x00" in orchestration_id
    ):
        raise ValueError(f"unsafe orchestration_id for cache path: {orchestration_id!r}")
    return (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "codex_feature_check.json"
    )


def read_codex_feature_cache(
    *, repo_root: Path, orchestration_id: str
) -> tuple[bool, str, str, str] | None:
    """Return (enabled, detail, status_kind, checked_at) or None if the cache is absent
    or unreadable. Raises ValueError on a present-but-malformed cache (the gate treats
    that as fail-closed)."""
    path = codex_feature_cache_path(repo_root=repo_root, orchestration_id=orchestration_id)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    enabled = doc.get("enabled")
    detail = doc.get("detail")
    status_kind = doc.get("status_kind")
    checked_at = doc.get("checked_at")
    if not isinstance(enabled, bool):
        raise ValueError("codex_feature_check.json enabled must be bool")
    if not isinstance(detail, str):
        raise ValueError("codex_feature_check.json detail must be string")
    if not isinstance(status_kind, str):
        raise ValueError("codex_feature_check.json status_kind must be string")
    if not isinstance(checked_at, str):
        raise ValueError("codex_feature_check.json checked_at must be string")
    return (enabled, detail, status_kind, checked_at)


def write_codex_feature_cache(
    *,
    repo_root: Path,
    orchestration_id: str,
    enabled: bool,
    detail: str,
    status_kind: str,
) -> None:
    path = codex_feature_cache_path(repo_root=repo_root, orchestration_id=orchestration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "enabled": enabled,
        "detail": detail,
        "status_kind": status_kind,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def probe_and_write_codex_feature_cache(
    *,
    repo_root: Path,
    orchestration_id: str,
    command: str | Sequence[str] = "codex",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Host-side: probe the codex hooks feature and persist the result to the
    leaf-unwritable cache. Returns (enabled, detail). Called by the conductor before
    launching codex leaves; the in-sandbox hook only reads the result. `command` is the
    leaf's configured invocation prefix (so a custom `--llm-command` wrapper is probed,
    not a hardcoded `codex`)."""
    enabled, detail = codex_hooks_feature_enabled(command=command, runner=runner)
    status_kind = "ok" if enabled or not _is_retryable_probe_error(detail) else "probe_error"
    write_codex_feature_cache(
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        enabled=enabled,
        detail=detail,
        status_kind=status_kind,
    )
    return enabled, detail


def _is_retryable_probe_error(detail: str) -> bool:
    return detail.startswith("codex features list failed:") or detail.startswith(
        "codex features list timed out"
    )
