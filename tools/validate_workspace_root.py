#!/usr/bin/env python3
"""Validate canonical workflow artifact root rules.

This checker enforces that workflow artifacts are stored under `workspace/`.
If `workspace/` is missing, the checker creates it before validation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Adv-17: TTL beyond which a "running" orchestration is considered stale
# (presumed crashed / abandoned without writing a terminal status). After
# this threshold without any artifact mtime update, tmp-script exemption
# is revoked so leaked executable scratch is surfaced. Defaults to 24h;
# override via env for long-running workloads or short-TTL tests.
_DEFAULT_LIVENESS_TTL_SECONDS = 86400  # 24 hours
_LIVENESS_TTL_ENV = "METDSL_ORCH_LIVENESS_TTL_SECONDS"

# Adv-39: TTL beyond which a cleanup-pending arid (terminal entry exists,
# cleanup_committed marker missing) loses its tmp-script exemption.
# Without a bounded recovery window, a single transient cleanup refusal
# would whitelist the run forever, hiding leaked executable content
# indefinitely. Default matches the orchestration liveness TTL (24h); a
# shorter value yields stricter detection at the cost of less time for
# operator-driven recovery before the leak is surfaced.
_DEFAULT_CLEANUP_PENDING_TTL_SECONDS = 86400  # 24 hours
_CLEANUP_PENDING_TTL_ENV = "METDSL_CLEANUP_PENDING_TTL_SECONDS"


_TTL_WARNED: set[str] = set()


def _warn_ttl_misconfig(env_name: str, raw: str, reason: str, default: int) -> None:
    """L3: emit a one-shot stderr warning when a TTL env var is set to an
    unparseable / non-positive value. Once-per-process to avoid log spam."""
    key = f"{env_name}={raw}|{reason}"
    if key in _TTL_WARNED:
        return
    _TTL_WARNED.add(key)
    import sys as _sys
    print(
        f"validate_workspace_root: WARNING — env {env_name}={raw!r} ignored "
        f"({reason}); using default {default} seconds. Did you mean a positive "
        f"integer (seconds)?",
        file=_sys.stderr,
    )


def _liveness_ttl_seconds() -> float:
    raw = os.environ.get(_LIVENESS_TTL_ENV, "").strip()
    if not raw:
        return float(_DEFAULT_LIVENESS_TTL_SECONDS)
    try:
        v = float(raw)
    except ValueError:
        _warn_ttl_misconfig(_LIVENESS_TTL_ENV, raw, "not a number", _DEFAULT_LIVENESS_TTL_SECONDS)
        return float(_DEFAULT_LIVENESS_TTL_SECONDS)
    if v <= 0:
        # Non-positive TTL would mark every orchestration stale immediately,
        # which is almost never the intended config; fall back to default
        # rather than silently break healthy in-flight runs.
        _warn_ttl_misconfig(_LIVENESS_TTL_ENV, raw, "non-positive", _DEFAULT_LIVENESS_TTL_SECONDS)
        return float(_DEFAULT_LIVENESS_TTL_SECONDS)
    return v


def _cleanup_pending_ttl_seconds() -> float:
    raw = os.environ.get(_CLEANUP_PENDING_TTL_ENV, "").strip()
    if not raw:
        return float(_DEFAULT_CLEANUP_PENDING_TTL_SECONDS)
    try:
        v = float(raw)
    except ValueError:
        _warn_ttl_misconfig(_CLEANUP_PENDING_TTL_ENV, raw, "not a number", _DEFAULT_CLEANUP_PENDING_TTL_SECONDS)
        return float(_DEFAULT_CLEANUP_PENDING_TTL_SECONDS)
    if v <= 0:
        _warn_ttl_misconfig(_CLEANUP_PENDING_TTL_ENV, raw, "non-positive", _DEFAULT_CLEANUP_PENDING_TTL_SECONDS)
        return float(_DEFAULT_CLEANUP_PENDING_TTL_SECONDS)
    return v


def _parse_finished_at(value: Any) -> float | None:
    """Parse an ISO-8601 finished_at string to epoch seconds, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Tolerate "...Z" suffix (UTC) by mapping to "+00:00" for fromisoformat.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _orchestration_last_activity_at(orch_dir: Path) -> float | None:
    """Return the most recent mtime (epoch seconds) among the orchestration's
    activity-bearing artifacts: orchestration_meta.json, agent_runs.jsonl,
    session_run_index.json, active_children/* and launches/*. Returns None
    when no candidate file is present (e.g. directory exists but is empty)."""
    candidates: list[Path] = []
    for name in (
        "orchestration_meta.json",
        "agent_runs.jsonl",
        "session_run_index.json",
        "phase_state_log.jsonl",
    ):
        p = orch_dir / name
        if p.is_file():
            candidates.append(p)
    for sub in ("active_children", "launches"):
        d = orch_dir / sub
        if d.is_dir():
            try:
                for child in d.iterdir():
                    if child.is_file():
                        candidates.append(child)
            except OSError:
                continue
    if not candidates:
        return None
    latest: float = 0.0
    for c in candidates:
        try:
            m = c.stat().st_mtime
        except OSError:
            continue
        if m > latest:
            latest = m
    return latest if latest > 0.0 else None


def _orchestration_has_active_marker(orch_dir: Path) -> bool:
    """Return True if active_children/ contains at least one marker file.

    A marker is created by record-launch and removed by deactivate-child or
    record-agent-run terminal. Presence proves at least one launched child
    has not yet been deactivated — which is the orchestration runtime's
    canonical "in-flight child exists" signal.
    """
    return bool(_orchestration_active_marker_arids(orch_dir))


def _path_recursive_max_mtime(path: Path) -> float:
    """Return the maximum mtime under `path`, recursively. 0.0 if empty/error."""
    latest = 0.0
    try:
        st = path.stat()
        if st.st_mtime > latest:
            latest = st.st_mtime
    except OSError:
        return 0.0
    if not path.is_dir():
        return latest
    try:
        for sub in path.rglob("*"):
            try:
                m = sub.stat().st_mtime
            except OSError:
                continue
            if m > latest:
                latest = m
    except OSError:
        pass
    return latest


def _orchestration_cleanup_committed_arids(orch_dir: Path) -> set[str]:
    """Adv-35: return the set of arids that have a cleanup_committed marker.

    Two-phase finalization invariant: a tmp dir's exemption is revoked only
    when (terminal entry exists) AND (cleanup_committed/<arid>.json exists).
    The committed marker is written AFTER the destructive cleanup completes,
    so its absence means 'cleanup pending' — exemption must remain so that
    the validator does not flag scratch that's still being torn down (or
    that survived a partial failure and may be needed for recovery).
    """
    d = orch_dir / "cleanup_committed"
    if not d.is_dir():
        return set()
    out: set[str] = set()
    suffix = ".json"
    try:
        for child in d.iterdir():
            if child.is_file() and child.name.endswith(suffix):
                arid = child.name[: -len(suffix)]
                if arid:
                    out.add(arid)
    except OSError:
        return set()
    return out


def _orchestration_active_marker_arids(
    orch_dir: Path,
    fresh_within_seconds: float | None = None,
    workspace_root: Path | None = None,
) -> set[str]:
    """Return the set of arids that have a per-arid active_children marker.

    When `fresh_within_seconds` is provided, only arids that show RECENT
    activity within that window survive (Adv-29). Activity = max of:
      - the per-arid marker file's own mtime
      - the recursive max mtime under workspace/tmp/<arid>/ (if workspace_root
        is supplied) — a long-running child legitimately updates its scratch
        dir even when the marker file itself was created hours ago at launch.
    A marker whose mtime AND tmp dir activity are both past TTL is treated
    as crashed/abandoned; without this gate a leaked marker would keep its
    tmp dir exempt forever.
    """
    active_dir = orch_dir / "active_children"
    if not active_dir.is_dir():
        return set()
    out: set[str] = set()
    suffix = ".txt"
    cutoff = (time.time() - fresh_within_seconds) if fresh_within_seconds is not None else None
    try:
        children = list(active_dir.iterdir())
    except OSError:
        return set()
    for child in children:
        if not (child.is_file() and child.name.endswith(suffix)):
            continue
        arid = child.name[: -len(suffix)]
        if not arid:
            continue
        if cutoff is not None:
            latest = 0.0
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                pass
            if workspace_root is not None:
                tmp_subdir = workspace_root / "tmp" / arid
                if tmp_subdir.exists():
                    latest = max(latest, _path_recursive_max_mtime(tmp_subdir))
            if latest < cutoff:
                continue
        out.add(arid)
    return out


STRICT_WORKSPACE_REF_KEYS = {
    "plan_dir",
    "pipeline_dir",
    "build_log_ref",
    "source_command_ref",
    "process_trace_ref",
}

ALLOWED_WORKSPACE_TOP_LEVEL_DIRS = {
    "orchestrations",
    "plans",
    "pipelines",
    "index",
    "tmp",
    ".pycache",
}
NODE_KEY_SAFE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$"
)
SLUG_DATE_SEQ3_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$")
AGENT_RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _normalize_workspace_root_token(workspace_root: str) -> str:
    token = workspace_root.strip().replace("\\", "/")
    token = token.lstrip("./")
    while "//" in token:
        token = token.replace("//", "/")
    return token.rstrip("/")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_relpath(path: str) -> str:
    token = path.strip()
    if token.startswith("./"):
        token = token[2:]
    return token.replace("\\", "/")


def _is_under_workspace(rel_path: str, workspace_root: str) -> bool:
    normalized_path = _normalize_relpath(rel_path)
    normalized_ws = _normalize_relpath(workspace_root).rstrip("/")
    return normalized_path == normalized_ws or normalized_path.startswith(normalized_ws + "/")


def _normalize_step_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _validate_dependency_ref(json_path: Path, dotted_path: str, value: str, step: str) -> list[str]:
    if value.startswith("/"):
        return [f"{json_path}:{dotted_path}: absolute path is not allowed ({value})"]

    normalized = _normalize_relpath(value)
    if step == "compile":
        if normalized.startswith("spec/") and normalized.endswith("/deps.yaml"):
            return []
        return [
            f"{json_path}:{dotted_path}: Compile dependency_ref must be spec/.../deps.yaml ({value})"
        ]

    if normalized.startswith("workspace/"):
        return []
    if step:
        return [
            f"{json_path}:{dotted_path}: {step} dependency_ref must start with workspace/ ({value})"
        ]
    return [f"{json_path}:{dotted_path}: must start with workspace/ ({value})"]


def _git_status_paths(repo_root: Path) -> tuple[set[str], set[str], str | None]:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if not detail:
            detail = f"git status failed with returncode={proc.returncode}"
        return set(), set(), detail

    tracked_diff: set[str] = set()
    untracked_files: set[str] = set()
    for raw in proc.stdout.splitlines():
        line = raw.rstrip("\n")
        if not line:
            continue
        status = line[:2]
        payload = line[3:].strip() if len(line) > 3 else ""
        if not payload:
            continue
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1].strip()
        payload = _normalize_relpath(payload)
        if status == "??":
            untracked_files.add(payload)
        else:
            tracked_diff.add(payload)
    return tracked_diff, untracked_files, None


def _validate_write_scope_from_baseline(
    *,
    repo_root: Path,
    workspace_root: str,
    baseline_path: Path,
) -> list[str]:
    violations: list[str] = []
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        return [f"{baseline_path}: invalid write_scope_baseline.json ({exc})"]

    if not isinstance(baseline, dict):
        return [f"{baseline_path}: write_scope_baseline must be json object"]

    baseline_tracked_raw = baseline.get("tracked_diff", [])
    baseline_untracked_raw = baseline.get("untracked_files", [])
    if not isinstance(baseline_tracked_raw, list) or not isinstance(baseline_untracked_raw, list):
        return [f"{baseline_path}: tracked_diff/untracked_files must be list"]

    baseline_tracked = {_normalize_relpath(str(item)) for item in baseline_tracked_raw}
    baseline_untracked = {_normalize_relpath(str(item)) for item in baseline_untracked_raw}
    current_tracked, current_untracked, git_error = _git_status_paths(repo_root)
    if git_error is not None:
        violations.append(
            f"{baseline_path}: write_scope check requires git status but failed ({git_error})"
        )
        return violations

    new_paths = sorted((current_tracked - baseline_tracked) | (current_untracked - baseline_untracked))
    outside_workspace = [path for path in new_paths if not _is_under_workspace(path, workspace_root)]
    if outside_workspace:
        violations.append(
            f"{baseline_path}: write_scope_violation detected outside workspace ({outside_workspace})"
        )
    return violations


def _capture_write_scope_baseline(
    *,
    repo_root: Path,
    workspace_root: str,
    baseline_path: Path,
    stage: str,
    node_key: str,
    pipeline_id: str,
) -> str | None:
    tracked_diff, untracked_files, git_error = _git_status_paths(repo_root)
    if git_error is not None:
        return git_error
    payload = {
        "stage": stage,
        "node_key": node_key,
        "pipeline_id": pipeline_id,
        "captured_at": _utc_now_iso(),
        "tracked_diff": sorted(tracked_diff),
        "untracked_files": sorted(untracked_files),
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return None


def _scan_json_for_violations(json_path: Path) -> list[str]:
    violations: list[str] = []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        return [f"{json_path}: invalid json ({exc})"]

    def walk(node: Any, dotted_path: str) -> None:
        if isinstance(node, dict):
            step = _normalize_step_token(node.get("step"))
            for key, value in node.items():
                child_path = f"{dotted_path}.{key}" if dotted_path else key

                if key in STRICT_WORKSPACE_REF_KEYS and isinstance(value, str):
                    if value.startswith("/"):
                        violations.append(
                            f"{json_path}:{child_path}: absolute path is not allowed ({value})"
                        )
                    elif not value.startswith("workspace/"):
                        violations.append(
                            f"{json_path}:{child_path}: must start with workspace/ ({value})"
                        )

                if key == "dependency_ref" and isinstance(value, str):
                    violations.extend(_validate_dependency_ref(json_path, child_path, value, step))

                if key == "raw_artifact_refs" and isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, str) and not item.startswith("workspace/"):
                            violations.append(
                                f"{json_path}:{child_path}[{i}]: must start with workspace/ ({item})"
                            )

                walk(value, child_path)
            return

        if isinstance(node, list):
            for i, item in enumerate(node):
                child_path = f"{dotted_path}[{i}]"
                walk(item, child_path)
            return

    walk(data, "")
    return violations


_AGENT_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "pass", "fail", "fail_closed", "blocked", "timeout", "cancel", "skipped",
})

# Adv-6: explicit allowlist of orchestration statuses under which
# child tmp dirs may be exempted. Anything outside this set — including a
# missing or unreadable orchestration_meta.json — is treated as "not active",
# so leaked scratch is surfaced rather than silently exempted.
# `init_orchestration` always writes status="running" at orchestration start,
# so an absent/corrupt file in practice indicates a deletion / I/O failure /
# malicious tampering and should fail closed.
_ORCHESTRATION_ACTIVE_STATUSES: frozenset[str] = frozenset({"running"})


def _read_orchestration_meta(orch_dir: Path) -> dict | None:
    """Read orchestration_meta.json once. Returns None on any read/parse
    failure or non-object root. Used by both status and arid extraction so
    they share the same fail-closed semantics."""
    meta_path = orch_dir / "orchestration_meta.json"
    if not meta_path.is_file():
        return None
    try:
        doc = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def _orchestration_status(orch_dir: Path) -> str | None:
    """Return the orchestration_meta.json `status` (lowercased) or None.

    Returns None for: missing file, unreadable file, malformed JSON,
    non-object root, or missing/non-string status field.
    """
    doc = _read_orchestration_meta(orch_dir)
    if doc is None:
        return None
    status_obj = doc.get("status")
    if isinstance(status_obj, str) and status_obj.strip():
        return status_obj.strip().lower()
    return None


def _orchestration_agent_run_id(orch_dir: Path) -> str | None:
    """Return orchestration_meta.json#orchestration_agent_run_id or None."""
    doc = _read_orchestration_meta(orch_dir)
    if doc is None:
        return None
    arid_obj = doc.get("orchestration_agent_run_id")
    if isinstance(arid_obj, str) and arid_obj.strip():
        return arid_obj.strip()
    return None


def _live_agent_tmp_run_ids(workspace_root: Path) -> set[str]:
    """Return agent_run_ids whose `workspace/tmp/<arid>/` directories are
    legitimately exempt from the forbidden-script scan.

    Exemption requires ALL of the following:
      1. The arid is launched by exactly one orchestration in the workspace
         (Adv-10: cross-orchestration arid collisions disable exemption — the
         flat `workspace/tmp/<arid>/` namespace makes it impossible to know
         which colliding orchestration owns the directory contents, so we
         cannot vouch that it contains no leaked scripts).
      2. That single owning orchestration's `orchestration_meta.json#status`
         is in `_ORCHESTRATION_ACTIVE_STATUSES` (Adv-6: fail-closed allowlist;
         missing / unreadable / malformed metadata is NOT considered active).
      3. The owning orchestration's `agent_runs.jsonl` is readable and well-
         formed in its entirety (Adv-8: any I/O / JSON / shape error treats
         the orchestration as untrustworthy, so its arids are not exempt).
      4. No terminal-status entry exists for the arid in that orchestration's
         `agent_runs.jsonl`.

    "Launched" arids include both:
      - `launches/<arid>.request.json` files (step/substep agents).
      - `orchestration_meta.json#orchestration_agent_run_id` (Adv-9: the
         orchestration agent itself owns `workspace/tmp/<orch_arid>/` even
         though it is not "launched" via record-launch and so has no request
         file).
    """
    orch_root = workspace_root / "orchestrations"
    if not orch_root.exists() or not orch_root.is_dir():
        return set()
    suffix = ".request.json"
    # Track all arids ever launched across all orchestrations (regardless of
    # current status). This is the global ownership ledger used for
    # collision detection in step (1) below.
    arid_launchers: dict[str, set[str]] = {}
    # Per-orchestration candidate live arids (arids the orchestration claims
    # are still in flight). Only populated for orchestrations that pass the
    # active-status and agent_runs.jsonl health checks.
    candidate_per_orch: dict[str, set[str]] = {}

    for orch_dir in orch_root.iterdir():
        if not orch_dir.is_dir():
            continue
        orch_id = orch_dir.name
        local_launched: set[str] = set()
        launches_dir = orch_dir / "launches"
        if launches_dir.is_dir():
            for req in launches_dir.glob(f"*{suffix}"):
                name = req.name
                if name.endswith(suffix):
                    arid = name[: -len(suffix)]
                    if arid:
                        local_launched.add(arid)
        # Adv-9: include the orchestration agent's own arid as launched-by-
        # this-orchestration so its workspace/tmp/<orch_arid>/ scratch is
        # eligible for exemption while the orchestration is active.
        orch_arid = _orchestration_agent_run_id(orch_dir)
        if orch_arid is not None:
            local_launched.add(orch_arid)
        # Record cross-orch ownership ledger BEFORE active/health gating so
        # that collisions involving terminated orchestrations still disable
        # exemption (Adv-10).
        for arid in local_launched:
            arid_launchers.setdefault(arid, set()).add(orch_id)

        # Adv-6/35/38: classify orchestration state.
        # - active: status in _ORCHESTRATION_ACTIVE_STATUSES → normal TTL gating applies
        # - cleanup-pending: status terminal but orch's own committed marker
        #   missing → still treat as effectively active (recovery in flight)
        # - fully terminated: status terminal AND orch's own committed marker
        #   present. Adv-38: even here we keep evaluating children — a child
        #   may have terminal entry but missing cleanup_committed, and that
        #   child's tmp scratch is still recovery state. Skipping the entire
        #   orchestration would orphan such cleanup-pending children.
        orch_status = _orchestration_status(orch_dir)
        orch_arid_for_state = _orchestration_agent_run_id(orch_dir)
        orch_fully_terminated = (
            orch_status not in _ORCHESTRATION_ACTIVE_STATUSES
            and orch_arid_for_state is not None
            and (
                orch_dir / "cleanup_committed" / f"{orch_arid_for_state}.json"
            ).is_file()
        )
        # Adv-6/8/12 fail-closed: if status is non-active AND we cannot even
        # identify the orch's own arid (missing/corrupt meta or absent
        # orchestration_agent_run_id field), treat the orchestration as
        # untrustworthy — its launched arids do NOT vouch for liveness.
        # Otherwise a corrupted-meta orch could still leak exemptions for
        # its children via the Adv-38 fall-through.
        if (
            orch_status not in _ORCHESTRATION_ACTIVE_STATUSES
            and orch_arid_for_state is None
        ):
            continue
        # Adv-17: freshness check — an orchestration whose status is "running"
        # but whose artifacts have not been touched in `_liveness_ttl_seconds()`
        # is presumed crashed / abandoned. Without this gate, a crash before
        # set-status leaves leaked tmp scripts permanently exempt.
        # Adv-19/Adv-22: long-running children may not write any control
        # artifacts for hours. When TTL is exceeded, fall back to per-arid
        # active_children/<arid>.txt evidence: only arids that still have
        # their own marker survive as live. A single stale marker no longer
        # whitelists the entire orchestration's tmp tree (Adv-22).
        last_activity = _orchestration_last_activity_at(orch_dir)
        if last_activity is None and not orch_fully_terminated:
            continue
        ttl_secs = _liveness_ttl_seconds()
        ttl_exceeded = (
            last_activity is None
            or (time.time() - last_activity) > ttl_secs
        )
        per_arid_marker_filter: set[str] | None = None
        # Adv-38: skip TTL freshness gating for fully-terminated orchestrations.
        # The TTL is a heuristic for "is the orch alive?"; a fully-terminated
        # orch is by definition not active, so freshness is irrelevant. The
        # remaining exemption survivors are children whose own per-arid
        # two-phase commit is incomplete (cleanup_committed missing) — those
        # are evaluated below independently of TTL.
        if ttl_exceeded and not orch_fully_terminated:
            # Adv-29: marker existence alone cannot prove liveness after TTL
            # expiry — a crashed orchestration leaves stale markers that would
            # otherwise whitelist their tmp dirs forever. Require recent
            # activity (marker mtime OR child tmp dir mtime within TTL); the
            # tmp-dir check protects long-running children whose marker file
            # was created at launch but whose scratch dir is being actively
            # written.
            per_arid_marker_filter = _orchestration_active_marker_arids(
                orch_dir,
                fresh_within_seconds=ttl_secs,
                workspace_root=workspace_root,
            )
            # Adv-31: also check the orchestration agent's own tmp dir.
            # Between child launches (or during parent-only recovery work),
            # active_children/ may be empty — but the orch agent itself can
            # still be writing to workspace/tmp/<orch_arid>/. Without this
            # check, a long-running orchestration loses the exemption for its
            # own helper scripts after 24h of child quietness.
            orch_arid_for_freshness = _orchestration_agent_run_id(orch_dir)
            cutoff = time.time() - ttl_secs
            if orch_arid_for_freshness is not None:
                orch_tmp = workspace_root / "tmp" / orch_arid_for_freshness
                if orch_tmp.exists():
                    if _path_recursive_max_mtime(orch_tmp) >= cutoff:
                        per_arid_marker_filter = per_arid_marker_filter | {orch_arid_for_freshness}
            if not per_arid_marker_filter:
                continue

        runs_path = orch_dir / "agent_runs.jsonl"
        terminated_here: set[str] = set()
        # Adv-39: track finished_at per terminal arid so cleanup-pending arids
        # can be aged out after METDSL_CLEANUP_PENDING_TTL_SECONDS instead of
        # remaining exempt forever on a transient cleanup refusal.
        terminated_finished_at: dict[str, float] = {}
        # Adv-12: a missing agent_runs.jsonl is also unhealthy. init_orchestration
        # always creates this file (writing an empty placeholder if needed), so
        # an absent file in a "running" orchestration indicates deletion or a
        # broken init, not normal operation. Without the ledger we cannot
        # confirm "no terminal entry" — treat as unhealthy and decline to
        # vouch for any of the orchestration's arids as live.
        runs_jsonl_unhealthy = not runs_path.is_file()
        text = ""
        if runs_path.is_file():
            try:
                text = runs_path.read_text(encoding="utf-8")
            except OSError:
                runs_jsonl_unhealthy = True
                text = ""
            # Adv-15: writers append to agent_runs.jsonl with plain `open("a")`,
            # no locking and no atomic replace, so a validator read that lands
            # mid-write can observe the LAST line truncated. Tolerate a single
            # JSONDecodeError on the trailing line — that is almost certainly
            # an in-flight append, not durable corruption — but still flag
            # any earlier malformed line (which can only be persistent damage).
            non_empty_lines = [s for s in (raw.strip() for raw in text.splitlines()) if s]
            n_lines = len(non_empty_lines)
            for idx, line in enumerate(non_empty_lines):
                is_last = idx == n_lines - 1
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    if is_last:
                        # Tolerate trailing partial write.
                        continue
                    runs_jsonl_unhealthy = True
                    continue
                if not isinstance(obj, dict):
                    runs_jsonl_unhealthy = True
                    continue
                arid_obj = obj.get("agent_run_id")
                status_obj = obj.get("status")
                if not isinstance(arid_obj, str) or not isinstance(status_obj, str):
                    runs_jsonl_unhealthy = True
                    continue
                if status_obj.strip().lower() in _AGENT_TERMINAL_STATUSES:
                    arid_norm = arid_obj.strip()
                    terminated_here.add(arid_norm)
                    finished_ts = _parse_finished_at(obj.get("finished_at"))
                    if finished_ts is not None:
                        # If multiple terminal entries (shouldn't happen, but
                        # tolerate), keep the latest finished_at.
                        prev = terminated_finished_at.get(arid_norm)
                        if prev is None or finished_ts > prev:
                            terminated_finished_at[arid_norm] = finished_ts
        # Adv-8: corrupt/unreadable runs file → don't include this orch.
        if runs_jsonl_unhealthy:
            continue
        # Adv-35: an arid is truly terminated (and therefore loses its tmp
        # exemption) only when BOTH the terminal entry exists AND the
        # cleanup_committed marker exists. Until the committed marker is
        # written (after cleanup completes), keep the arid in the live set
        # so the validator does not flag scratch that's still being torn
        # down or stranded by a partial failure.
        committed_arids = _orchestration_cleanup_committed_arids(orch_dir)
        truly_terminated = terminated_here & committed_arids
        # Adv-39: bound the cleanup-pending recovery window. An arid that
        # has a terminal entry but no cleanup_committed marker AND whose
        # finished_at is older than _cleanup_pending_ttl_seconds() is
        # treated as truly terminated (forced fail-closed). Without this
        # bound, a single transient cleanup refusal would whitelist the run
        # forever, hiding leaked executable content indefinitely.
        # H1 fix: when finished_at is missing or unparseable (legacy entries,
        # tz-naive timestamps, forced-timeout edge cases), do NOT age out
        # immediately. Use the agent_runs.jsonl mtime as a proxy "we know
        # this entry was at least observed by then" — that gives the same
        # recovery window length the operator configured rather than zero.
        cleanup_pending = (terminated_here - committed_arids) & local_launched
        if cleanup_pending:
            cleanup_pending_cutoff = time.time() - _cleanup_pending_ttl_seconds()
            try:
                runs_mtime_proxy = runs_path.stat().st_mtime
            except OSError:
                runs_mtime_proxy = time.time()
            for pending_arid in cleanup_pending:
                fin_ts = terminated_finished_at.get(pending_arid)
                if fin_ts is None:
                    # H1: "now" semantics — start the recovery window from
                    # the most recent observable activity (agent_runs.jsonl
                    # mtime), not from epoch zero.
                    fin_ts = runs_mtime_proxy
                if fin_ts < cleanup_pending_cutoff:
                    truly_terminated.add(pending_arid)
        candidates = local_launched - truly_terminated
        # Adv-38: when the orchestration itself is fully terminated, drop
        # the orchestration agent's own arid from the live set (it is the
        # one arid whose terminal evidence comes from orchestration_meta
        # rather than agent_runs.jsonl). Children with missing
        # cleanup_committed remain — their per-arid evidence governs.
        if orch_fully_terminated and orch_arid_for_state is not None:
            candidates.discard(orch_arid_for_state)
        # Adv-22: when TTL was exceeded, narrow the survivor set to arids
        # whose own per-arid marker still exists. Without this, a single
        # leaked marker would whitelist the entire orchestration's tmp tree.
        if per_arid_marker_filter is not None:
            # Adv-23: the orchestration agent itself never has an
            # active_children marker (record-launch only writes them for child
            # runs). For a long-running but legitimately-active orchestration
            # whose control artifacts have aged out of the TTL window, we must
            # NOT strip its own arid from the live set — the exemption-loss
            # would block subsequent gates by flagging
            # workspace/tmp/<orch_arid>/*.py. We only reach this branch when
            # at least one child marker exists (otherwise Adv-19 already
            # `continue`d), so the orchestration is plausibly alive; keep its
            # own arid alongside.
            orch_arid = _orchestration_agent_run_id(orch_dir)
            extended_filter = per_arid_marker_filter | ({orch_arid} if orch_arid else set())
            candidates &= extended_filter
            if not candidates:
                continue
        candidate_per_orch[orch_id] = candidates

    # Final live set: arid is exempt iff exactly ONE orchestration ever
    # launched it AND that owning orchestration is active and claims the arid
    # as in flight. Any cross-orch collision (≥2 launchers across the entire
    # ledger, regardless of their current statuses) disables exemption — the
    # flat namespace cannot disambiguate which orchestration's content lives
    # in workspace/tmp/<arid>/.
    live: set[str] = set()
    for arid, launchers in arid_launchers.items():
        if len(launchers) != 1:
            continue
        owner = next(iter(launchers))
        owner_candidates = candidate_per_orch.get(owner)
        if owner_candidates is not None and arid in owner_candidates:
            live.add(arid)
    return live


def _scan_workspace_for_forbidden_scripts(workspace_root: Path) -> list[str]:
    """Reject *.py under workspace/ EXCEPT inside per-agent tmp dirs of LIVE runs.

    workspace/tmp/<agent_run_id>/ is a sanctioned scratch root while the agent
    is still in flight. record-agent-run / record-timeout removes the directory
    at terminal status, so any *.py that survives a terminal entry indicates a
    cleanup bug and MUST be surfaced (otherwise leaked executable content
    accumulates undetected).

    Exemption rule:
      - Path is under workspace/tmp/<arid>/...  AND
      - <arid> is in `_live_agent_tmp_run_ids(workspace_root)`
    Otherwise the *.py is flagged. This narrows an earlier blanket exemption
    raised in adversarial review (Adv-2): a tmp dir for a terminated, never-
    launched, or stale agent_run_id no longer fails open.
    """
    violations: list[str] = []
    tmp_root = workspace_root / "tmp"
    tmp_root_present = tmp_root.exists() and tmp_root.is_dir()
    live_arids = _live_agent_tmp_run_ids(workspace_root) if tmp_root_present else set()
    # Adv-32: use the LEXICAL path under workspace/ for the exemption decision,
    # not Path.resolve(). resolve() follows symlinks, so a symlink such as
    # workspace/ir/foo/helper.py -> ../../tmp/<live-arid>/helper.py would
    # resolve INTO workspace/tmp/ and inherit the exemption. The actual
    # workspace entry being validated lives outside tmp/, so we must judge by
    # where the file appears in the workspace tree, not where it dereferences.
    # As an additional defense, refuse to exempt symlinked descendants — even
    # a symlink whose target also lives under workspace/tmp/<arid>/ should not
    # introduce a second copy under a non-tmp path.
    try:
        workspace_root_resolved = workspace_root.resolve(strict=False)
    except OSError:
        workspace_root_resolved = workspace_root
    for py_path in sorted(workspace_root.rglob("*.py")):
        # Lexical path relative to workspace/.
        try:
            rel_to_ws = py_path.relative_to(workspace_root)
        except ValueError:
            try:
                rel_to_ws = py_path.resolve(strict=False).relative_to(workspace_root_resolved)
            except (OSError, ValueError):
                rel_to_ws = None
        if (
            tmp_root_present
            and rel_to_ws is not None
            and len(rel_to_ws.parts) >= 3
            and rel_to_ws.parts[0] == "tmp"
        ):
            # Path is lexically under workspace/tmp/<arid>/...
            # Adv-32: also reject symlinked descendants. Any symlink in the
            # path chain (the .py file itself OR an intermediate component)
            # disqualifies exemption — only true tmp-tree files are sanctioned.
            symlink_in_chain = False
            try:
                cur = py_path
                workspace_tmp_resolved = (workspace_root / "tmp").resolve(strict=False)
                stop_at = workspace_tmp_resolved.parent  # workspace_root.resolve()
                while True:
                    if cur.is_symlink():
                        symlink_in_chain = True
                        break
                    parent = cur.parent
                    if parent == cur or cur.resolve(strict=False) == stop_at:
                        break
                    cur = parent
            except OSError:
                symlink_in_chain = True
            if not symlink_in_chain:
                arid_dir = rel_to_ws.parts[1]
                if arid_dir in live_arids:
                    # Live run scratch — sanctioned, skip.
                    continue
        violations.append(
            f"{py_path}: python script under workspace/ is forbidden"
        )
    return violations


def _scan_workspace_layout(workspace_root: Path) -> list[str]:
    violations: list[str] = []
    for child in sorted(workspace_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in ALLOWED_WORKSPACE_TOP_LEVEL_DIRS:
            continue
        violations.append(
            f"{child}: non-canonical workspace directory name; allowed top-level directories are {sorted(ALLOWED_WORKSPACE_TOP_LEVEL_DIRS)}"
        )

    tmp_root = workspace_root / "tmp"
    if tmp_root.exists() and tmp_root.is_dir():
        for child in sorted(tmp_root.iterdir()):
            if not child.is_dir():
                violations.append(
                    f"{child}: non-directory entry directly under workspace/tmp/ is not allowed"
                )
                continue
            if not AGENT_RUN_ID_PATTERN.match(child.name):
                violations.append(
                    f"{child}: invalid workspace/tmp/ subdirectory name; expected alphanumeric agent_run_id (no dots, slashes, or spaces)"
                )

    for stage_root_name in ("plans", "pipelines"):
        stage_root = workspace_root / stage_root_name
        if not stage_root.exists() or not stage_root.is_dir():
            continue

        for node_safe_dir in sorted(stage_root.iterdir()):
            if not node_safe_dir.is_dir():
                continue
            node_safe = node_safe_dir.name
            if not NODE_KEY_SAFE_PATTERN.match(node_safe):
                violations.append(
                    f"{node_safe_dir}: invalid node_key_safe directory name; expected <spec_kind>__<spec_id>__<spec_version>"
                )
                continue

            for id_dir in sorted(node_safe_dir.iterdir()):
                if not id_dir.is_dir():
                    continue
                if not SLUG_DATE_SEQ3_PATTERN.match(id_dir.name):
                    violations.append(
                        f"{id_dir}: invalid {stage_root_name} id directory name; expected <slug>_<YYYYMMDD>_<seq3>"
                    )
    return violations


def validate(repo_root: Path, workspace_root: str) -> tuple[list[str], bool]:
    return validate_with_scope(
        repo_root=repo_root,
        workspace_root=workspace_root,
        write_scope_baseline=None,
        stage="",
        node_key="",
        pipeline_id="",
    )


def validate_with_scope(
    repo_root: Path,
    workspace_root: str,
    write_scope_baseline: str | None,
    stage: str,
    node_key: str,
    pipeline_id: str,
) -> tuple[list[str], bool]:
    violations: list[str] = []
    created_workspace = False
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"], created_workspace

    canonical_root = repo_root / workspace_root
    if canonical_root.exists():
        if canonical_root.is_symlink():
            violations.append(f"{canonical_root}: symlink workspace root is not allowed")
        elif not canonical_root.is_dir():
            violations.append(f"{canonical_root}: workspace root must be a directory")
    else:
        canonical_root.mkdir(parents=True, exist_ok=True)
        created_workspace = True

    if canonical_root.exists() and canonical_root.is_dir():
        for json_file in sorted(canonical_root.rglob("*.json")):
            violations.extend(_scan_json_for_violations(json_file))

    if canonical_root.exists() and canonical_root.is_dir():
        violations.extend(_scan_workspace_for_forbidden_scripts(canonical_root))
        violations.extend(_scan_workspace_layout(canonical_root))

    if write_scope_baseline:
        baseline_path = Path(write_scope_baseline)
        if not baseline_path.is_absolute():
            baseline_path = repo_root / baseline_path
        baseline_path = baseline_path.resolve()
        try:
            baseline_path.relative_to(canonical_root.resolve())
        except ValueError:
            violations.append(
                f"{baseline_path}: write_scope_baseline must be under {canonical_root}"
            )
            return violations, created_workspace

        if baseline_path.exists():
            violations.extend(
                _validate_write_scope_from_baseline(
                    repo_root=repo_root,
                    workspace_root=workspace_root,
                    baseline_path=baseline_path,
                )
            )
        else:
            git_error = _capture_write_scope_baseline(
                repo_root=repo_root,
                workspace_root=workspace_root,
                baseline_path=baseline_path,
                stage=stage,
                node_key=node_key,
                pipeline_id=pipeline_id,
            )
            if git_error is not None:
                violations.append(
                    f"{baseline_path}: write_scope baseline capture failed ({git_error})"
                )

    return violations, created_workspace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--workspace-root", default="workspace")
    parser.add_argument(
        "--write-scope-baseline",
        default=None,
        help="Path to write_scope_baseline.json. If file exists, validate diff from baseline.",
    )
    parser.add_argument("--stage", default="")
    parser.add_argument("--node-key", default="")
    parser.add_argument("--pipeline-id", default="")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    violations, created_workspace = validate_with_scope(
        repo_root=repo_root,
        workspace_root=args.workspace_root,
        write_scope_baseline=args.write_scope_baseline,
        stage=args.stage,
        node_key=args.node_key,
        pipeline_id=args.pipeline_id,
    )
    if violations:
        print("workspace root validation: FAIL")
        for line in violations:
            print(f"- {line}")
        return 1

    if created_workspace:
        print(f"workspace root created: {repo_root / args.workspace_root}")
    print("workspace root validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
