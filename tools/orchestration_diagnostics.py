#!/usr/bin/env python3
"""Post-mortem diagnostics for incomplete (``dangling``) child launches.

Background
----------
When a child ``Agent`` launch hangs or is interrupted *after* ``record-launch``
opened the active_child window but *before* the child returned, the orchestration
is left mid-launch:

- ``active_child_agent_run_id.txt`` / ``active_children/<arid>.txt`` are set,
- ``child_returns/<arid>.txt`` is absent,
- no terminal ``agent_runs.jsonl`` row exists for ``<arid>``.

If the host ``claude`` process then exits cleanly (e.g. the orchestration agent
ends its turn with an "I've paused" message, returncode 0), nothing in-repo
records *why* it stopped, and the only decisive evidence (the child's last
activity and the dead-air before the abort) lives in the **ephemeral**
``~/.claude/projects/<slug>/<host_session_id>/subagents/agent-*.jsonl`` transcript,
which ``~/.claude`` cleanup can delete.

This module makes that diagnosis reproducible and persists the decisive transcript
tail in-repo. It is intentionally dependency-free (stdlib only) and **defensive**
against the Claude Code transcript format: parse failures degrade to raw tails
and ``found=False`` markers rather than raising.

Callers:
- ``tools/audit_orchestration.py`` invokes it on demand for after-the-fact analysis
  of a dangling launch (open active_child window with no child return / terminal run).
  (Legacy ``launch_incident.runtime.<uuid12>.json`` snapshots from older runs are also
  surfaced when present; the conductor does not write new ones.)
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Terminal agent_runs statuses: a row carrying one of these (or any finished_at)
# proves the child completed and the window is NOT dangling.
_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"pass", "fail", "fail_closed", "blocked", "timeout", "cancel", "error"}
)

# Substrings that mark a transcript record as an interrupt/abort rather than real
# agent activity. Matched case-insensitively against text blocks.
_INTERRUPT_MARKERS: tuple[str, ...] = (
    "[request interrupted",
    "request interrupted by user",
)


def _read_json(path: Path) -> dict[str, Any] | None:
    # `ValueError` (not just `json.JSONDecodeError`, which subclasses it) because
    # `read_text` raises `UnicodeDecodeError` — also a `ValueError`, NOT an `OSError`
    # — on non-UTF-8 bytes. Catching only the narrower pair let a corrupt-byte file
    # escape this module's "degrade, never raise" contract.
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            payload = json.loads(token)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware datetime (``Z`` or offset)."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _orch_root(repo_root: Path, orchestration_id: str) -> Path:
    return repo_root / "workspace" / "orchestrations" / orchestration_id


def detect_dangling_active_child(
    repo_root: Path, orchestration_id: str
) -> dict[str, Any] | None:
    """Detect an open active_child window with no child return / terminal run.

    Returns a dict describing the (primary) dangling child, or ``None`` when no
    window is open. Detection keys off the **backend-neutral** per-arid markers
    ``active_children/<arid>.txt``, which ``record_launch`` writes for ALL backends
    — the Claude-only ``active_child_agent_run_id.txt`` pointer is used merely to
    choose the primary among several dangling children (Claude is sequential, so it
    is the one). codex has no such pointer but still leaves the per-arid
    marker, so keying off it covers every backend. Path conventions mirror
    ``tools/orchestration_runtime.py`` (``_active_children_dir`` /
    ``_child_returns_dir``); paths are rebuilt as strings to avoid importing the
    heavy runtime module.
    """
    root = _orch_root(repo_root, orchestration_id)
    markers_dir = root / "active_children"

    # Candidate arids = the Claude sequential pointer (written FIRST by record_launch,
    # so a crash before the per-arid marker leaves a pointer-only open window that
    # still blocks the next record-launch) UNION the backend-neutral per-arid markers
    # (written for ALL backends; codex has no pointer).
    candidate_arids: list[str] = []
    try:
        pointed = (root / "active_child_agent_run_id.txt").read_text(encoding="utf-8").strip()
    except OSError:
        pointed = ""
    if pointed:
        candidate_arids.append(pointed)
    if markers_dir.is_dir():
        for m in sorted(markers_dir.glob("*.txt")):
            if m.stem and m.stem not in candidate_arids:
                candidate_arids.append(m.stem)
    if not candidate_arids:
        return None

    # A terminal agent_runs row proves completion.
    terminal_arids: set[str] = set()
    for run in _read_jsonl(root / "agent_runs.jsonl"):
        rid = str(run.get("agent_run_id") or "").strip()
        if not rid:
            continue
        status = str(run.get("status") or "").strip().lower()
        if run.get("finished_at") or status in _TERMINAL_RUN_STATUSES:
            terminal_arids.add(rid)
    # A child diverted to agent_runs_invalid.jsonl (terminal-payload validation
    # failure: sandbox / session-id / output-manifest) DID reach record-agent-run —
    # record_agent_run raises before clearing the marker AND before appending to
    # agent_runs.jsonl, so without this it would be misclassified as an abandoned
    # (launch_incomplete_active_child) launch, overwriting the real failure. It is an
    # invalid terminal ATTEMPT, not a dangling launch.
    attempted_arids: set[str] = set()
    for rec in _read_jsonl(root / "agent_runs_invalid.jsonl"):
        rid = str(rec.get("agent_run_id") or "").strip()
        if rid:
            attempted_arids.add(rid)

    def _is_dangling(arid: str) -> bool:
        # A child-return ack closes the window even before the terminal run lands.
        if (root / "child_returns" / f"{arid}.txt").is_file():
            return False
        return arid not in terminal_arids and arid not in attempted_arids

    dangling = [a for a in candidate_arids if _is_dangling(a)]
    if not dangling:
        return None

    # Launch metadata per arid from the phase_state_log record_launch events.
    launch_meta: dict[str, dict[str, Any]] = {}
    for entry in _read_jsonl(root / "phase_state_log.jsonl"):
        if entry.get("event") != "record_launch":
            continue
        rid = str(entry.get("agent_run_id") or "").strip()
        if rid and rid not in launch_meta:
            launch_meta[rid] = {
                "ts": entry.get("ts") or entry.get("timestamp"),
                "node_key_safe": entry.get("node_key_safe"),
                "step": entry.get("step"),
            }

    # Primary = the Claude sequential pointer (read above) if it is itself dangling,
    # else the most recently launched dangling child (what was in flight).
    def _launch_dt(arid: str) -> datetime:
        ts = (launch_meta.get(arid) or {}).get("ts")
        return _parse_ts(ts) or datetime.min.replace(tzinfo=timezone.utc)

    primary = pointed if pointed in dangling else max(dangling, key=_launch_dt)

    meta = launch_meta.get(primary) or {}
    launch_recorded_at: str | None = meta.get("ts")
    node_key_safe: str | None = meta.get("node_key_safe")
    step: str | None = meta.get("step")
    response = _read_json(root / "launches" / f"{primary}.response.json") or {}
    if launch_recorded_at is None:
        launch_recorded_at = response.get("started_at")

    # substep is not in phase_state_log; recover from the launch request when present.
    request = _read_json(root / "launches" / f"{primary}.request.json") or {}
    substep = request.get("substep")
    if node_key_safe is None:
        node_key_safe = request.get("node_key") or request.get("node_key_safe")
    if step is None:
        step = request.get("step")

    elapsed_seconds: float | None = None
    launched_dt = _parse_ts(launch_recorded_at)
    if launched_dt is not None:
        elapsed_seconds = (datetime.now(timezone.utc) - launched_dt).total_seconds()

    return {
        "agent_run_id": primary,
        "node_key_safe": node_key_safe,
        "step": step,
        "substep": substep,
        "launch_recorded_at": launch_recorded_at,
        "elapsed_seconds": elapsed_seconds,
        "dangling_child_arids": dangling,
    }


def _record_text_blocks(record: dict[str, Any]) -> list[str]:
    """Extract human-readable text fragments from a transcript record."""
    texts: list[str] = []
    tur = record.get("toolUseResult")
    if isinstance(tur, str):
        texts.append(tur)
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "tool_result":
                    rc = block.get("content")
                    if isinstance(rc, str):
                        texts.append(rc)
                    elif isinstance(rc, list):
                        for sub in rc:
                            if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                                texts.append(sub["text"])
    return texts


def _is_interrupt_record(record: dict[str, Any]) -> bool:
    for text in _record_text_blocks(record):
        low = text.lower()
        if any(marker in low for marker in _INTERRUPT_MARKERS):
            return True
    return False


def _last_tool_use(record: dict[str, Any]) -> dict[str, Any] | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return {
                "name": block.get("name"),
                "input_preview": json.dumps(block.get("input", {}), ensure_ascii=False)[:200],
            }
    return None


# HTTP statuses that the Claude transport retries / that are transient and safe to
# `--resume` without investigation (overload, rate limit, gateway/server blips).
_RETRYABLE_API_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 529})


def _api_error(record: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a transient API-error marker from a transcript record.

    Claude Code writes a synthetic assistant record with ``isApiErrorMessage:true``
    and ``apiErrorStatus:<code>`` when a model turn fails (e.g. ``529 Overloaded``).
    Surfacing it structurally lets the operator see at a glance that a dangling
    launch was a transient transport failure (safe to resume) rather than a hang.
    """
    if record.get("isApiErrorMessage") is not True:
        return None
    status = record.get("apiErrorStatus")
    status_int = status if isinstance(status, int) else None
    blocks = _record_text_blocks(record)
    message = blocks[-1][:200] if blocks else None
    return {
        "status": status_int,
        "message": message,
        "retryable": status_int in _RETRYABLE_API_STATUSES,
    }


def api_error_from_records(records: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Derive the transient API error to report from a sequence of transcript records.

    Reports an API error only when it is the FINAL relevant activity: any later
    non-interrupt, non-error record means the error was recovered, so it is cleared
    (otherwise a later unrelated hang would be mislabeled as a retryable transport
    blip). Shared by `summarize_transcript_tail` and the audit renderer's fallback
    for legacy incident snapshots that predate the structured `api_error` field but
    still carry `isApiErrorMessage` / `apiErrorStatus` in their `raw_tail`.
    """
    if not records:
        return None
    api_error: dict[str, Any] | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        if _is_interrupt_record(record):
            continue
        err = _api_error(record)
        api_error = err if err is not None else None
    return api_error


def summarize_transcript_tail(path: Path, *, n: int = 40) -> dict[str, Any]:
    """Summarize the last ``n`` records of a transcript jsonl.

    Returns last activity timestamp, last tool_use, interrupt-marker presence,
    the dead-air gap (last real activity -> interrupt / now), and the raw tail
    records (so the decisive evidence survives ``~/.claude`` cleanup even if the
    parsing assumptions later drift).
    """
    if not path.exists():
        return {"found": False, "path": str(path)}
    records = _read_jsonl(path)
    tail = records[-n:] if len(records) > n else records

    last_activity_ts: str | None = None
    last_activity_dt: datetime | None = None
    last_event_type: str | None = None
    last_tool: dict[str, Any] | None = None
    interrupt_ts: str | None = None
    interrupt_dt: datetime | None = None
    interrupt_text: str | None = None

    for record in records:
        ts = record.get("timestamp") or record.get("ts")
        dt = _parse_ts(ts)
        if _is_interrupt_record(record):
            if isinstance(ts, str):
                interrupt_ts = ts
            interrupt_dt = dt
            blocks = _record_text_blocks(record)
            interrupt_text = blocks[-1][:200] if blocks else None
            continue
        if isinstance(ts, str):
            last_activity_ts = ts
        if dt is not None:
            last_activity_dt = dt
        last_event_type = record.get("type")
        tu = _last_tool_use(record)
        if tu is not None:
            last_tool = tu

    # Surface an API error only when it is the final relevant activity (see helper).
    api_error = api_error_from_records(records)

    dead_air_seconds: float | None = None
    if last_activity_dt is not None:
        end_dt = interrupt_dt or datetime.now(timezone.utc)
        dead_air_seconds = (end_dt - last_activity_dt).total_seconds()

    return {
        "found": True,
        "path": str(path),
        "record_count": len(records),
        "last_activity_ts": last_activity_ts,
        "last_event_type": last_event_type,
        "last_tool_use": last_tool,
        "interrupted": interrupt_ts is not None,
        "interrupt_ts": interrupt_ts,
        "interrupt_text": interrupt_text,
        "dead_air_seconds": dead_air_seconds,
        "api_error": api_error,
        "raw_tail": tail,
    }


def _claude_projects_dir(repo_root: Path) -> Path:
    # Claude Code derives the project slug from the absolute cwd, with every "/"
    # replaced by "-" (e.g. /home/seiya/work/met-dsl -> -home-seiya-work-met-dsl).
    # Resolve first so a relative repo_root (e.g. Path(".")) still maps correctly.
    try:
        abs_root = repo_root.resolve()
    except OSError:
        abs_root = repo_root
    slug = str(abs_root).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug


def _last_agent_tool_use_id(host_records: list[dict[str, Any]]) -> str | None:
    """Find the id of the last ``Agent``/``Task`` tool_use in the host transcript."""
    for record in reversed(host_records):
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in ("Agent", "Task")
            ):
                tid = block.get("id")
                if isinstance(tid, str) and tid:
                    return tid
    return None


def resolve_transcripts(
    repo_root: Path, meta: dict[str, Any], child_arid: str
) -> dict[str, Any]:
    """Resolve host + child subagent transcript paths from ``~/.claude``.

    Primary child match: the last host ``Agent`` tool_use id <-> a
    ``subagents/agent-*.meta.json#toolUseId``. Fallback: a ``subagents/agent-*.jsonl``
    whose body references ``child_arid`` (the child prompt embeds it).
    """
    host_session_id = str(meta.get("host_session_id") or "").strip()
    projects_dir = _claude_projects_dir(repo_root)
    result: dict[str, Any] = {
        "host_session_id": host_session_id or None,
        "projects_dir": str(projects_dir),
    }
    if not host_session_id:
        result["host_transcript"] = {"found": False, "reason": "no host_session_id in meta"}
        result["child_transcript"] = {"found": False, "reason": "no host_session_id in meta"}
        return result

    host_path = projects_dir / f"{host_session_id}.jsonl"
    host_records = _read_jsonl(host_path) if host_path.exists() else []
    result["host_transcript"] = {
        "found": host_path.exists(),
        "path": str(host_path),
    }

    subagents_dir = projects_dir / host_session_id / "subagents"
    child_path: Path | None = None
    match_method: str | None = None

    if subagents_dir.is_dir():
        # Primary: toolUseId correlation.
        target_tool_id = _last_agent_tool_use_id(host_records)
        if target_tool_id:
            for meta_file in sorted(subagents_dir.glob("agent-*.meta.json")):
                sub_meta = _read_json(meta_file) or {}
                if str(sub_meta.get("toolUseId") or "") == target_tool_id:
                    # meta_file is agent-<id>.meta.json; transcript is agent-<id>.jsonl
                    candidate = subagents_dir / (meta_file.name[: -len(".meta.json")] + ".jsonl")
                    if candidate.exists():
                        child_path = candidate
                        match_method = "tool_use_id"
                    break
        # Fallback: body references child_arid.
        if child_path is None:
            for jsonl_file in sorted(subagents_dir.glob("agent-*.jsonl")):
                try:
                    if child_arid in jsonl_file.read_text(encoding="utf-8"):
                        child_path = jsonl_file
                        match_method = "arid_in_body"
                        break
                except OSError:
                    continue

    if child_path is None:
        result["child_transcript"] = {
            "found": False,
            "subagents_dir": str(subagents_dir),
            "reason": (
                "subagents dir missing (transcript may be ephemeral/cleaned)"
                if not subagents_dir.is_dir()
                else "no matching subagent transcript"
            ),
        }
    else:
        summary = summarize_transcript_tail(child_path)
        summary["match_method"] = match_method
        result["child_transcript"] = summary

    return result


def summarize_jsonl_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum token usage across the assistant turns of a Claude Code transcript.

    Each assistant record carries ``message.usage`` with input / output /
    cache_read / cache_creation token counts. ``total_tokens`` is their sum;
    ``peak_context_tokens`` is the max per-turn resident context
    (input + cache_read + cache_creation), which exposes the quadratic
    cache_read growth that dominates long agent sessions. Defensive: records
    without a ``message.usage`` dict are skipped.
    """
    inp = out = cache_read = cache_creation = turns = 0
    peak = 0
    for record in records:
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        turns += 1
        i = int(usage.get("input_tokens") or 0)
        o = int(usage.get("output_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        cc = int(usage.get("cache_creation_input_tokens") or 0)
        inp += i
        out += o
        cache_read += cr
        cache_creation += cc
        peak = max(peak, i + cr + cc)
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "total_tokens": inp + out + cache_read + cache_creation,
        "assistant_turns": turns,
        "peak_context_tokens": peak,
    }


def summarize_transcript_usage(path: Path) -> dict[str, Any]:
    """Token-usage summary for a single transcript jsonl (``found=False`` if absent)."""
    if not path.exists():
        return {"found": False, "path": str(path)}
    summary = summarize_jsonl_usage(_read_jsonl(path))
    summary["found"] = True
    summary["path"] = str(path)
    return summary


# The four token classes a Claude CLI `usage` object reports. Both sum-key tuples
# below derive from this one, so a new token class (or a rename) cannot land in one
# aggregation and silently miss the other — the transcript sum would then disagree
# with the pure-leaf sum with nothing to catch it.
_CLI_TOKEN_USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# Transcript-usage sum keys: the token classes plus the two values
# `summarize_jsonl_usage` derives itself. `total_tokens` is load-bearing here (it is
# summed across transcripts by `aggregate_child_usage` / `aggregate_parent_usage`)
# and must not be dropped when re-deriving this tuple.
_USAGE_SUM_KEYS: tuple[str, ...] = (
    *_CLI_TOKEN_USAGE_KEYS,
    "total_tokens",
    "assistant_turns",
)

# A child's OWN arid is the one in its capability / output-manifest paths. Its
# PARENT arid also appears in the body (as ``parent_agent_run_id``), so a bare
# substring match would misattribute the transcript to the parent. These paths
# disambiguate: they only ever name the child itself.
_OWN_ARID_PATH_RE = re.compile(
    r"(?:capabilities|output_manifests|read_manifests|sandbox_profiles)/"
    r"([0-9a-fA-F-]{36})\.json"
)


def _own_arid_of_transcript(text: str, targets: set[str]) -> str | None:
    """Identify which target arid a child subagent transcript belongs to.

    Prefers the arid named in the child's own capability/output-manifest paths
    (unambiguous). Falls back to the most frequently mentioned target arid, since
    the child's own arid dominates its transcript while the parent arid appears
    only incidentally.
    """
    owned = [a for a in _OWN_ARID_PATH_RE.findall(text) if a in targets]
    if owned:
        return Counter(owned).most_common(1)[0][0]
    present = [a for a in targets if a in text]
    if not present:
        return None
    counts = {a: text.count(a) for a in present}
    return max(counts, key=counts.get)


def _first_user_text_contains(records: list[dict[str, Any]], needle: str) -> bool:
    """True if the first ``type=="user"`` record's text content contains ``needle``."""
    for record in records:
        if record.get("type") != "user":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif isinstance(block.get("content"), str):
                        parts.append(block["content"])
            text = " ".join(parts)
        # A type=="user" record carrying only a tool_result block yields no text
        # here and is intentionally skipped, so the real opening launch-prompt user
        # record is the one tested — do not "fix" this to inspect tool_result bodies.
        if not text.strip():
            continue
        return needle in text
    return False


def aggregate_child_usage(
    repo_root: Path,
    agent_run_ids: list[str],
    *,
    host_session_id: str | None = None,
) -> dict[str, Any]:
    """Attribute per-child token usage from the ephemeral ``~/.claude`` transcripts.

    Child ``Agent`` subagents are NOT recorded as sidechains in the host
    transcript and ``agent_runs.jsonl`` carries no usage fields, so child token
    cost (empirically the majority of a workflow node's cost) is otherwise
    invisible. This locates each child's
    ``~/.claude/projects/<slug>/<host>/subagents/agent-*.jsonl`` transcript by
    arid-in-body (the child launch prompt embeds its own arid) and sums usage.

    By default scans every host session's ``subagents`` dir (not just one), which
    is robust to multi-session nodes — e.g. a ``--resume`` that ran some children
    under a different host session than the others. Pass ``host_session_id`` to
    restrict the scan to a single session's ``subagents`` dir; the live
    ``finalize_child`` path uses this because the just-returned child is always
    under the current host session, which avoids reading every other session's
    transcripts on each finalize. Best-effort: returns ``available=False`` with a
    reason when ``~/.claude`` is absent/cleaned; never raises.
    """
    targets = [a for a in agent_run_ids if isinstance(a, str) and a]
    projects_dir = _claude_projects_dir(repo_root)
    result: dict[str, Any] = {
        "projects_dir": str(projects_dir),
        "per_child": {},
        "matched_count": 0,
        "unmatched_arids": sorted(set(targets)),
    }
    if not targets:
        result["available"] = False
        result["reason"] = "no agent_run_ids to attribute"
        return result
    if not projects_dir.is_dir():
        result["available"] = False
        result["reason"] = "claude projects dir missing (transcripts machine-local/ephemeral)"
        return result

    target_set = set(targets)
    per_child: dict[str, Any] = {}
    glob_pat = (
        f"{host_session_id}/subagents/agent-*.jsonl"
        if host_session_id
        else "*/subagents/agent-*.jsonl"
    )
    for sub in sorted(projects_dir.glob(glob_pat)):
        try:
            text = sub.read_text(encoding="utf-8")
        except OSError:
            continue
        owner = _own_arid_of_transcript(text, target_set)
        if owner is None or owner in per_child:
            continue
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            token = line.strip()
            if not token:
                continue
            try:
                payload = json.loads(token)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        usage = summarize_jsonl_usage(records)
        usage["transcript"] = str(sub)
        per_child[owner] = usage
        if len(per_child) == len(target_set):
            break

    totals = {k: 0 for k in _USAGE_SUM_KEYS}
    for usage in per_child.values():
        for key in _USAGE_SUM_KEYS:
            totals[key] += int(usage.get(key, 0) or 0)
    totals["peak_context_tokens"] = max(
        (int(u.get("peak_context_tokens", 0) or 0) for u in per_child.values()),
        default=0,
    )

    result["available"] = True
    result["per_child"] = per_child
    result["children_total"] = totals
    result["matched_count"] = len(per_child)
    result["unmatched_arids"] = sorted(target_set - set(per_child))
    return result


def aggregate_parent_usage(
    repo_root: Path, orchestration_agent_run_id: str
) -> dict[str, Any]:
    """Sum the orchestration (parent) agent's token usage across all its host sessions.

    A node that was ``--resume``-d runs the parent under more than one host session
    (the original plus each resume), so reading only the current
    ``host_session_id`` understates the parent total and skews the parent/children
    ratio. A parent session's *first user message* is the orchestration launch
    prompt, which embeds ``workspace/tmp/<orchestration_agent_run_id>`` (its
    allowed_tmp_root) — a token unique to this parent. Matching on the FIRST user
    message (not anywhere in the body) is what makes this precise: a diagnostic /
    ``/plan`` session that merely *discusses* this orchestration also contains the
    token, but not as its opening prompt. Best-effort: ``available=False`` (never
    raises) when ``~/.claude`` is gone or the id is empty.
    """
    arid = (orchestration_agent_run_id or "").strip()
    projects_dir = _claude_projects_dir(repo_root)
    result: dict[str, Any] = {"sessions": []}
    if not arid:
        result["available"] = False
        result["reason"] = "no orchestration_agent_run_id"
        return result
    if not projects_dir.is_dir():
        result["available"] = False
        result["reason"] = "claude projects dir missing (transcripts machine-local/ephemeral)"
        return result

    marker = f"workspace/tmp/{arid}"
    totals = {k: 0 for k in _USAGE_SUM_KEYS}
    peak = 0
    sessions: list[dict[str, Any]] = []
    for sess in sorted(projects_dir.glob("*.jsonl")):
        try:
            text = sess.read_text(encoding="utf-8")
        except OSError:
            continue
        if marker not in text:
            continue
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            token = line.strip()
            if not token:
                continue
            try:
                payload = json.loads(token)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        # Precise gate: the marker must be in this session's FIRST user message
        # (the launch prompt), not merely somewhere in the body — otherwise a
        # session that only discusses this orchestration would be counted.
        if not _first_user_text_contains(records, marker):
            continue
        usage = summarize_jsonl_usage(records)
        usage["transcript"] = str(sess)
        for key in _USAGE_SUM_KEYS:
            totals[key] += int(usage.get(key, 0) or 0)
        peak = max(peak, int(usage.get("peak_context_tokens", 0) or 0))
        sessions.append(usage)

    if not sessions:
        result["available"] = False
        result["reason"] = "no parent transcript located"
        return result
    totals["peak_context_tokens"] = peak
    result["available"] = True
    result["found"] = True
    result.update(totals)
    result["session_count"] = len(sessions)
    result["sessions"] = sessions
    return result


# Token fields summed across a pure leaf's repair attempts: the CLI result-envelope
# `usage` keys the conductor persists per attempt into bundle_meta.json /
# verdict_meta.json (`per_attempt[].usage`), the ~/.claude-free provenance for
# pure-leaf cost (transcripts are ephemeral; these files are in-repo). Exactly the
# CLI token classes — `total_tokens` is derived here, and `assistant_turns` is
# meaningless for a single-turn pure envelope, so neither belongs.
_PURE_ATTEMPT_USAGE_KEYS: tuple[str, ...] = _CLI_TOKEN_USAGE_KEYS


# The structural discriminator of a pure-leaf meta envelope. `per_attempt` is the
# measurement payload the conductor always writes (`_write_bundle_meta` /
# `_write_verdict_meta`) and is what an unrelated or stale JSON document at the
# same path will not carry. Keying on it (rather than on common keys like `result`
# / `attempts`) keeps a foreign `{"result": "ok"}` from being reported as a
# pure-leaf row of all-zero metrics.
_PURE_META_PAYLOAD_KEY = "per_attempt"


def _nonneg_int_or_none(value: Any) -> int | None:
    """Return a JSON value as a non-negative count, or None when it is not one.

    Only a real non-negative `int` qualifies. A `bool` (an `int` subclass), a
    float (`1.9`; or `Infinity`, which Python's JSON decoder accepts), a numeric
    string, or a negative value is corrupt metadata and yields None. Rejection is
    structural — the `isinstance` guard runs *before* any coercion, so nothing is
    ever passed to `int()` and the aggregation cannot truncate `1.9`, sign-flip a
    negative, or raise `OverflowError` on `Infinity`. Callers pick their own
    fallback (0 for a token sum, the valid-attempt count for `attempts`).
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonneg_int(value: Any) -> int:
    """`_nonneg_int_or_none` with 0 as the fallback (token sums)."""
    coerced = _nonneg_int_or_none(value)
    return 0 if coerced is None else coerced


def _sum_pure_attempt_usage(
    per_attempt: list[dict[str, Any]],
) -> tuple[dict[str, int], list[str]]:
    """Sum `per_attempt[].usage` token counts and collect the per-attempt models.

    Each attempt is `{"agent_run_id", "model": str|None, "usage": dict|None}`
    (conductor `_run_pure_generate_substep` / `_run_pure_verify_substep`). Missing
    or malformed `usage` / `model` entries are skipped rather than raising —
    diagnostics degrade, never break (see `_nonneg_int`). `models` preserves
    attempt order (a repair loop may resolve a different model per turn; the alias
    is recorded, never pinned).
    """
    totals = {k: 0 for k in _PURE_ATTEMPT_USAGE_KEYS}
    models: list[str] = []
    for attempt in per_attempt:
        if not isinstance(attempt, dict):
            continue
        usage = attempt.get("usage")
        if isinstance(usage, dict):
            for key in _PURE_ATTEMPT_USAGE_KEYS:
                totals[key] += _nonneg_int(usage.get(key))
        model = attempt.get("model")
        if isinstance(model, str) and model:
            models.append(model)
    totals["total_tokens"] = sum(totals[k] for k in _PURE_ATTEMPT_USAGE_KEYS)
    return totals, models


def _summarize_one_pure_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Project one bundle_meta.json / verdict_meta.json into an A/B metrics row.

    `bundle_meta.json` (producer) and `verdict_meta.json` (reviewer) share a
    schema: `{result, failure_category, attempts, prompt_contract_version,
    per_attempt[], failure_excerpt?}`. `result` is `pass`/`fail`; `attempts`
    counts turns, so `repair_turns = attempts - 1` is the bounded-repair count.
    `found=False` when the file is absent, unparseable, or not a pure-leaf meta
    envelope (see `_PURE_META_PAYLOAD_KEY`). When `attempts` is absent or corrupt
    it falls back to the count of structurally valid (dict) attempt entries.
    """
    if not isinstance(meta, dict) or _PURE_META_PAYLOAD_KEY not in meta:
        return {"found": False}
    per_attempt = meta.get(_PURE_META_PAYLOAD_KEY)
    per_attempt = per_attempt if isinstance(per_attempt, list) else []
    valid_attempt_count = sum(1 for a in per_attempt if isinstance(a, dict))
    usage_total, models = _sum_pure_attempt_usage(per_attempt)
    attempts = _nonneg_int_or_none(meta.get("attempts"))
    if attempts is None:
        attempts = valid_attempt_count
    return {
        "found": True,
        "result": meta.get("result"),
        "attempts": attempts,
        "repair_turns": max(attempts - 1, 0),
        "failure_category": meta.get("failure_category"),
        "prompt_contract_version": meta.get("prompt_contract_version"),
        "usage_total": usage_total,
        "models": models,
    }


def summarize_pure_leaf_metas(source_dir: Path) -> dict[str, Any]:
    """A/B metrics for the pure `generate.generate` / `generate.verify` leaves of
    one source directory (Z2, milestone M-E).

    Reads `<source_dir>/bundle_meta.json` (producer) and `verdict_meta.json`
    (reviewer) — the in-repo, ~/.claude-free per-attempt usage/model provenance —
    and returns `{generate, verify, found}`. `generate` / `verify` are per-leaf
    rows (see `_summarize_one_pure_meta`); `found` is true when either file was
    present. Best-effort: never raises. A legacy (agentic) node has neither file
    and yields `found=False`, so the caller can tell a pure node from a legacy one
    by presence alone. The row carries no `source_dir` key: the caller passes the
    directory in and owns how it labels the result (the audit rollup labels it
    repo-relative), so there is no second, conflicting notion of the same field.
    """
    generate = _summarize_one_pure_meta(_read_json(source_dir / "bundle_meta.json"))
    verify = _summarize_one_pure_meta(_read_json(source_dir / "verdict_meta.json"))
    return {
        "generate": generate,
        "verify": verify,
        "found": bool(generate.get("found") or verify.get("found")),
    }


def build_launch_incident(
    repo_root: Path, orchestration_id: str
) -> dict[str, Any] | None:
    """Assemble a launch-incident report, or ``None`` if no dangling window.

    Combines dangling-child detection (in-repo artifacts) with transcript
    correlation (``~/.claude``). The transcript portion degrades gracefully when
    ``~/.claude`` is absent or cleaned.
    """
    dangling = detect_dangling_active_child(repo_root, orchestration_id)
    if dangling is None:
        return None

    meta = _read_json(_orch_root(repo_root, orchestration_id) / "orchestration_meta.json") or {}
    transcripts = resolve_transcripts(repo_root, meta, dangling["agent_run_id"])

    child = transcripts.get("child_transcript", {})
    abort_marker = None
    if isinstance(child, dict) and child.get("found"):
        abort_marker = {
            "interrupted": child.get("interrupted"),
            "interrupt_ts": child.get("interrupt_ts"),
            "interrupt_text": child.get("interrupt_text"),
            "last_activity_ts": child.get("last_activity_ts"),
            "dead_air_seconds": child.get("dead_air_seconds"),
            "api_error": child.get("api_error"),
        }

    return {
        "schema": "launch_incident/v1",
        "orchestration_id": orchestration_id,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "dangling_child": dangling,
        "host_session_id": transcripts.get("host_session_id"),
        "transcripts": transcripts,
        "abort_marker": abort_marker,
    }
