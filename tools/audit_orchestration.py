#!/usr/bin/env python3
"""Read-only audit helper for workflow orchestrations.

Usage:
    python3 tools/audit_orchestration.py --orchestration-id <id> [--format json|markdown]

Collects and aggregates:
- Policy-level block counts from native_hook_events.jsonl
- fix_hint presence/absence per policy
- Last 5 hook events before fail_closed
- phase_state_log fail/fail_closed entries
- agent_runs.jsonl completion status
- Dangling launch (open active_child window with no child return / terminal run),
  correlated with the ephemeral ~/.claude transcript tail (see
  orchestration_diagnostics.build_launch_incident), AND any persisted
  launch_incident.runtime.*.json snapshots (which survive after --resume clears the
  window or ~/.claude cleanup removes the transcript)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # script run: sys.path[0] is tools/ ; package import: repo root on path
    from orchestration_diagnostics import build_launch_incident
except ImportError:  # pragma: no cover - import-path shim
    from tools.orchestration_diagnostics import build_launch_incident


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load records, ignoring malformed lines.

    Use `_load_jsonl_with_errors` when caller needs visibility into parse
    failures. This wrapper preserves the simple records-only interface for
    callers that don't need integrity reporting.
    """
    records, _errors = _load_jsonl_with_errors(path)
    return records


def _load_jsonl_with_errors(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load records AND return a list of parse errors for integrity reporting.

    Each error entry is `{"path": str, "line_number": int, "message": str}`.
    Missing files return ([], []).
    """
    if not path.exists():
        return [], []
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append({
                "path": str(path),
                "line_number": idx,
                "message": str(exc),
            })
    return records, errors


def _orch_root(repo_root: Path, orchestration_id: str) -> Path:
    return repo_root / "workspace" / "orchestrations" / orchestration_id


def _load_json_if_dict(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


_EXPECTED_BENIGN_POLICIES: frozenset[str] = frozenset({
    # Claude Code platform-level auto-reads at session start.  Hooks must keep
    # blocking these to preserve the read trust boundary, but they are not real
    # agent violations and should be aggregated separately from substantive
    # policy hits (read_manifest_read_guard, output_manifest_write_guard, etc.).
    "auto_read_expected_block",
})

# Per-policy "expected count" budget.  A benign block count above this budget
# (per agent_run_id) suggests the orchestration agent is making EXPLICIT (not
# just startup) reads of allowlisted paths — which the hook still blocks, but
# which should NOT be silently filed under "benign noise."  Operators see these
# excess counts surfaced in the audit report.
#
# auto_read_expected_block: Claude Code auto-reads up to 5 allowlisted files
# at session start (MEMORY.md, README.md, TODO.md, CLAUDE.md, .claude/settings.json,
# project-memory MEMORY.md).  We allow some slack (2x) for retries/double-fires
# before flagging as suspicious.
_BENIGN_POLICY_EXPECTED_MAX_PER_AGENT: dict[str, int] = {
    "auto_read_expected_block": 12,
}


def collect_policy_block_counts(
    blocks: list[dict[str, Any]],
) -> dict[str, int]:
    counter: Counter = Counter()
    for b in blocks:
        policy = (b.get("audit_detail") or {}).get("policy", "unknown")
        counter[policy] += 1
    return dict(counter.most_common())


def collect_allow_auto_approve_stats(
    hook_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the total of `action=allow_auto_approve` and the breakdown by tool_name.

    For visualizing Write/Edit events that bypassed the harness's permission
    prompt with `hookSpecificOutput.permissionDecision="allow"`. When the volume
    is unexpectedly large, it is a signal that manifest verification is lax, or
    that unintended multiple writes are running on the agent side.
    """
    by_tool: Counter = Counter()
    total = 0
    for e in hook_events:
        if e.get("action") != "allow_auto_approve":
            continue
        total += 1
        tool_name = e.get("tool_name") or (e.get("audit_detail") or {}).get("tool_name") or "unknown"
        by_tool[tool_name] += 1
    return {
        "total": total,
        "by_tool": dict(by_tool.most_common()),
    }


def split_substantive_and_benign(
    blocks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition block events into (substantive, benign) groups.

    Benign blocks (e.g. auto_read_expected_block) represent expected
    platform-level noise; substantive blocks indicate real policy hits that
    deserve operator attention.
    """
    substantive: list[dict[str, Any]] = []
    benign: list[dict[str, Any]] = []
    for b in blocks:
        policy = (b.get("audit_detail") or {}).get("policy", "unknown")
        if policy in _EXPECTED_BENIGN_POLICIES:
            benign.append(b)
        else:
            substantive.append(b)
    return substantive, benign


def detect_suspicious_benign_volume(
    benign_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag benign-policy blocks that exceed expected per-agent volume.

    The hook policy still blocks these reads, so the trust boundary holds, but
    a count well above the platform's startup auto-read budget suggests the
    orchestration agent is making EXPLICIT post-startup reads of allowlisted
    files — which an operator should see in the audit report rather than have
    silently aggregated into the benign bucket.

    Returns one entry per (agent_run_id, policy) combination that exceeds the
    expected budget, including the actual count and the budget threshold.
    """
    counts: dict[tuple[str, str], int] = {}
    for b in benign_blocks:
        audit_detail = b.get("audit_detail") or {}
        policy = audit_detail.get("policy", "unknown")
        # Resolution order for agent_run_id: audit_detail (canonical, set by
        # validate_read_access for auto_read_expected_block) → top-level →
        # payload_summary → "<unknown>".
        agent_id = audit_detail.get("agent_run_id") or b.get("agent_run_id") or ""
        if not agent_id:
            payload = b.get("payload_summary")
            if isinstance(payload, dict):
                agent_id = payload.get("agent_run_id", "")
        agent_id = str(agent_id) if agent_id else "<unknown>"
        counts[(agent_id, policy)] = counts.get((agent_id, policy), 0) + 1

    flagged: list[dict[str, Any]] = []
    for (agent_id, policy), cnt in sorted(counts.items()):
        budget = _BENIGN_POLICY_EXPECTED_MAX_PER_AGENT.get(policy)
        if budget is not None and cnt > budget:
            flagged.append({
                "agent_run_id": agent_id,
                "policy": policy,
                "count": cnt,
                "expected_max": budget,
                "note": (
                    "benign count exceeds expected startup-read budget; "
                    "may indicate explicit post-startup reads"
                ),
            })
    return flagged


def collect_fix_hint_stats(
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    hint_present: Counter = Counter()
    hint_absent: Counter = Counter()
    repeated: defaultdict = defaultdict(list)
    seen_commands: list[str] = []
    for b in blocks:
        policy = (b.get("audit_detail") or {}).get("policy", "unknown")
        fix_hint = (b.get("audit_detail") or {}).get("fix_hint")
        cmd = (
            (b.get("payload_summary") or {}).get("command", "")
            if isinstance(b.get("payload_summary"), dict)
            else str(b.get("payload_summary", ""))
        )
        if fix_hint and fix_hint.get("next_command"):
            hint_present[policy] += 1
        else:
            hint_absent[policy] += 1
        if cmd and cmd in seen_commands:
            repeated[policy].append(cmd[:200])
        if cmd:
            seen_commands.append(cmd)
    return {
        "hint_present": dict(hint_present.most_common()),
        "hint_absent": dict(hint_absent.most_common()),
        "repeated": {k: v for k, v in repeated.items()},
    }


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into a timezone-aware datetime.

    Accepts both `Z` (UTC) and offset suffixes. Naive timestamps are assumed
    UTC. Returns None if value is missing or unparseable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    # Python <3.11 only accepts +00:00, not Z.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def collect_fail_closed_timeline(
    hook_events: list[dict[str, Any]],
    phase_log: list[dict[str, Any]],
    n: int = 5,
) -> dict[str, Any]:
    # Pick the LATEST fail_closed transition by parsed datetime (not raw
    # string max, which can disagree with chronological order under mixed
    # offsets/precisions). Then slice the last n hook events sorted by
    # parsed timestamp — file order is not reliable when multiple hook
    # processes append concurrently.
    fail_entries: list[tuple[datetime, str]] = []
    for entry in phase_log:
        new_state = entry.get("to") or entry.get("new_state", "")
        if new_state == "fail_closed" or (
            entry.get("event") == "set_status" and new_state == "fail_closed"
        ):
            raw_ts = entry.get("ts") or entry.get("timestamp")
            parsed = _parse_ts(raw_ts)
            if parsed is not None and isinstance(raw_ts, str):
                fail_entries.append((parsed, raw_ts))
    if not fail_entries:
        return {"fail_closed_at": None, "last_events": []}
    fail_entries.sort(key=lambda x: x[0])
    fail_dt, fail_ts = fail_entries[-1]

    # Sort hook events by parsed timestamp.  Events with an unparseable
    # timestamp would otherwise be silently dropped from the timeline,
    # which is an observability gap during incident analysis (the worst
    # case is a malformed-timestamp event RIGHT before fail_closed).
    # We sort the parseable ones, then append unparseable ones at the end
    # so they appear in `last_events`, AND we surface their count as an
    # integrity warning the caller can render.
    indexed: list[tuple[datetime | None, int, dict[str, Any]]] = []
    for i, e in enumerate(hook_events):
        raw = e.get("ts") or e.get("timestamp")
        indexed.append((_parse_ts(raw), i, e))
    sortable = [(dt, idx, e) for (dt, idx, e) in indexed if dt is not None]
    unparseable = [e for (dt, _idx, e) in indexed if dt is None]
    sortable.sort(key=lambda x: (x[0], x[1]))
    events_before_sorted = [e for (dt, _i, e) in sortable if dt <= fail_dt]
    # Combine sliced parseable events with all unparseable events at the
    # end (they have no time signal — append rather than drop).
    candidate_window = events_before_sorted + unparseable
    last_n = candidate_window[-n:] if len(candidate_window) > n else candidate_window

    def _render_event(e: dict[str, Any]) -> dict[str, Any]:
        return {
            "ts": e.get("ts") or e.get("timestamp"),
            "action": e.get("action"),
            "tool_name": e.get("tool_name") or (
                (e.get("payload_summary") or {}).get("tool_name") if isinstance(e.get("payload_summary"), dict) else None
            ),
            "policy": (e.get("audit_detail") or {}).get("policy"),
            "payload_summary": str(e.get("payload_summary", ""))[:120],
        }

    return {
        "fail_closed_at": fail_ts,
        "last_events": [_render_event(e) for e in last_n],
        "unparseable_timestamp_count": len(unparseable),
        "unparseable_events": [_render_event(e) for e in unparseable[:10]],
    }


def collect_agent_run_summary(
    agent_runs: list[dict[str, Any]],
    invalid_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate per-status counts across both terminal records and
    fail-validation fallback records.

    `agent_runs` carries successful terminal records; `invalid_runs` carries
    entries appended to `agent_runs_invalid.jsonl` when terminal payload
    validation rejected an otherwise-completed run.  Operators investigating
    a stuck workflow need to see those failed-validation runs in the
    per-status breakdown — not just in the separate `invalid_run_count`
    field — so they're rolled into `status_counts` (typically as `fail`).
    """
    status_counts: Counter = Counter()
    missing_entries: list[str] = []
    for run in agent_runs:
        status = run.get("status", "unknown")
        status_counts[status] += 1
        if not run.get("finished_at"):
            missing_entries.append(run.get("agent_run_id", "?"))
    for run in (invalid_runs or []):
        status = run.get("status", "fail")
        status_counts[status] += 1
    return {
        "status_counts": dict(status_counts),
        "missing_finished_at": missing_entries,
    }


def audit(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    root = _orch_root(repo_root, orchestration_id)
    hook_events, hook_errs = _load_jsonl_with_errors(root / "hooks" / "native_hook_events.jsonl")
    phase_log, phase_errs = _load_jsonl_with_errors(root / "phase_state_log.jsonl")
    agent_runs, runs_errs = _load_jsonl_with_errors(root / "agent_runs.jsonl")
    invalid_runs, inv_errs = _load_jsonl_with_errors(root / "agent_runs_invalid.jsonl")

    all_blocks = [e for e in hook_events if e.get("action") == "block"]
    substantive_blocks, benign_blocks = split_substantive_and_benign(all_blocks)
    suspicious_benign = detect_suspicious_benign_volume(benign_blocks)
    parse_errors = hook_errs + phase_errs + runs_errs + inv_errs
    timeline = collect_fail_closed_timeline(hook_events, phase_log)
    unparseable_count = timeline.get("unparseable_timestamp_count", 0)
    # Dangling launch (open active_child window with no child return / terminal
    # run): reproduces the post-mortem of an interrupted/hung child launch and
    # correlates the (ephemeral) ~/.claude transcript. None when the window is
    # closed. Defensive: degrades to found=False rather than raising.
    try:
        launch_incident = build_launch_incident(repo_root, orchestration_id)
    except Exception:  # noqa: BLE001 - diagnostics must never break the audit
        launch_incident = None
    # Persisted incident snapshots captured at run time. These survive after
    # `--resume` clears the active-child markers (live detection then returns None)
    # and after ~/.claude cleanup removes the transcript, so they are the durable
    # diagnosis source for the documented later-analysis path. Surfaced even when
    # the live window is closed.
    launch_incident_snapshots: list[dict[str, Any]] = []
    for snap_path in sorted(root.glob("launch_incident.runtime.*.json")):
        doc = _load_json_if_dict(snap_path)
        if doc is None:
            continue
        launch_incident_snapshots.append(
            {"ref": str(snap_path.relative_to(repo_root)), "incident": doc}
        )

    return {
        "orchestration_id": orchestration_id,
        "total_hook_events": len(hook_events),
        "total_blocks": len(all_blocks),
        "substantive_block_count": len(substantive_blocks),
        "benign_block_count": len(benign_blocks),
        "policy_block_counts": collect_policy_block_counts(substantive_blocks),
        "benign_policy_block_counts": collect_policy_block_counts(benign_blocks),
        "suspicious_benign_volume": suspicious_benign,
        "allow_auto_approve_stats": collect_allow_auto_approve_stats(hook_events),
        "fix_hint_stats": collect_fix_hint_stats(substantive_blocks),
        "fail_closed_timeline": timeline,
        "launch_incident": launch_incident,
        "launch_incident_snapshots": launch_incident_snapshots,
        "agent_run_summary": collect_agent_run_summary(agent_runs, invalid_runs),
        "invalid_run_count": len(invalid_runs),
        "invalid_run_ids": [r.get("agent_run_id") for r in invalid_runs if r.get("agent_run_id")],
        "data_integrity_warning": (len(parse_errors) > 0) or (unparseable_count > 0),
        "parse_error_count": len(parse_errors),
        "parse_errors": parse_errors,
        "unparseable_timestamp_count": unparseable_count,
    }


def _render_incident_body(incident: dict[str, Any], lines: list[str]) -> None:
    """Render the decisive fields of one launch-incident dict (live or persisted)."""
    child = incident.get("dangling_child", {})
    lines.append("| field | value |")
    lines.append("|---|---|")
    lines.append(f"| child agent_run_id | `{child.get('agent_run_id')}` |")
    lines.append(f"| node_key | `{child.get('node_key_safe')}` |")
    lines.append(f"| step / substep | `{child.get('step')}` / `{child.get('substep')}` |")
    lines.append(f"| launch_recorded_at | `{child.get('launch_recorded_at')}` |")
    elapsed = child.get("elapsed_seconds")
    lines.append(f"| elapsed since launch | {f'{elapsed:.0f}s' if isinstance(elapsed, (int, float)) else 'n/a'} |")
    lines.append(f"| host_session_id | `{incident.get('host_session_id')}` |")
    lines.append("")

    transcripts = incident.get("transcripts", {})
    ct = transcripts.get("child_transcript", {})
    if ct.get("found"):
        dead_air = ct.get("dead_air_seconds")
        lines.append("Child subagent transcript (decisive evidence):")
        lines.append("")
        lines.append(f"- transcript: `{ct.get('path')}` (matched via `{ct.get('match_method')}`)")
        lines.append(f"- last activity: `{ct.get('last_activity_ts')}` (event `{ct.get('last_event_type')}`)")
        last_tool = ct.get("last_tool_use") or {}
        if last_tool:
            lines.append(f"- last tool_use: `{last_tool.get('name')}` — {last_tool.get('input_preview')}")
        lines.append(
            f"- dead-air before abort: "
            f"{f'{dead_air:.0f}s' if isinstance(dead_air, (int, float)) else 'n/a'}"
        )
        if ct.get("interrupted"):
            lines.append(
                f"- abort marker: `{ct.get('interrupt_text')}` at `{ct.get('interrupt_ts')}`"
            )
    else:
        # Live re-derivation: ~/.claude transcript ephemeral. A persisted snapshot
        # (rendered from "Captured incident snapshots" below) keeps the evidence even
        # then, since the decisive tail was copied in-repo at incident time.
        abort = incident.get("abort_marker")
        if isinstance(abort, dict) and abort:
            dead_air = abort.get("dead_air_seconds")
            lines.append("Child subagent transcript (decisive evidence, from snapshot):")
            lines.append("")
            lines.append(f"- last activity: `{abort.get('last_activity_ts')}`")
            lines.append(
                f"- dead-air before abort: "
                f"{f'{dead_air:.0f}s' if isinstance(dead_air, (int, float)) else 'n/a'}"
            )
            if abort.get("interrupted"):
                lines.append(
                    f"- abort marker: `{abort.get('interrupt_text')}` at `{abort.get('interrupt_ts')}`"
                )
        else:
            lines.append(
                f"Child subagent transcript not available: {ct.get('reason', 'unknown')} "
                "(~/.claude transcripts are machine-local and ephemeral)."
            )
    lines.append("")


def _render_launch_incident(
    incident: dict[str, Any] | None,
    snapshots: list[dict[str, Any]] | None,
    lines: list[str],
) -> None:
    """Render the dangling-launch section: live window and/or persisted snapshots."""
    snapshots = snapshots or []
    lines.append("## Dangling launch (active_child window)")
    lines.append("")

    if incident:
        lines.append(
            "An open active_child window was found with no child return / terminal "
            "agent_runs row — the child launch never completed."
        )
        lines.append("")
        _render_incident_body(incident, lines)
    elif not snapshots:
        lines.append(
            "No dangling active_child window detected and no captured incident snapshots."
        )
        lines.append("")
        return
    else:
        lines.append(
            "No active_child window is currently open (e.g. cleared by `--resume`), but "
            "incident snapshot(s) captured at run time are preserved in-repo below."
        )
        lines.append("")

    if snapshots:
        lines.append("### Captured incident snapshots (`launch_incident.runtime.*.json`)")
        lines.append("")
        for snap in snapshots:
            ref = snap.get("ref")
            doc = snap.get("incident")
            lines.append(f"- `{ref}`")
            lines.append("")
            if isinstance(doc, dict):
                _render_incident_body(doc, lines)
            else:
                lines.append("  (unreadable snapshot)")
                lines.append("")


def _render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    orch_id = result["orchestration_id"]
    lines.append(f"# Audit: {orch_id}")
    lines.append("")
    lines.append(f"Total hook events: {result['total_hook_events']}  ")
    lines.append(f"Total blocks: {result['total_blocks']}")
    lines.append("")

    lines.append("## Policy block counts (substantive)")
    lines.append("")
    lines.append("| Policy | Count | Note |")
    lines.append("|---|---|---|")
    for policy, cnt in result["policy_block_counts"].items():
        note = "**REPEATED ERROR PATTERN**" if cnt >= 5 else ""
        lines.append(f"| `{policy}` | {cnt} | {note} |")
    lines.append("")

    benign_counts = result.get("benign_policy_block_counts", {})
    if benign_counts:
        lines.append("## Benign block counts (platform auto-reads)")
        lines.append("")
        lines.append(
            "These reads are blocked by policy (the trust boundary holds), but "
            "are expected Claude Code platform behavior at session start. "
            "Counts are surfaced here so operators can detect anomalous volume."
        )
        lines.append("")
        lines.append("| Policy | Count |")
        lines.append("|---|---|")
        for policy, cnt in benign_counts.items():
            lines.append(f"| `{policy}` | {cnt} |")
        lines.append("")

    suspicious = result.get("suspicious_benign_volume", [])
    if suspicious:
        lines.append("## ⚠ Suspicious benign-block volume")
        lines.append("")
        lines.append(
            "Benign blocks exceed the expected platform-auto-read budget for "
            "one or more agents — possible explicit (post-startup) reads of "
            "allowlisted paths."
        )
        lines.append("")
        lines.append("| agent_run_id | policy | count | expected_max |")
        lines.append("|---|---|---|---|")
        for entry in suspicious:
            lines.append(
                f"| `{entry['agent_run_id']}` | `{entry['policy']}` | "
                f"{entry['count']} | {entry['expected_max']} |"
            )
        lines.append("")

    aa = result.get("allow_auto_approve_stats", {})
    if aa.get("total", 0) > 0:
        lines.append("## Auto-approved Write/Edit (hookSpecificOutput)")
        lines.append("")
        lines.append(
            "Count of `action=allow_auto_approve` events. These are Write/Edit "
            "calls where the hook returned `permissionDecision=\"allow\"` because "
            "the target path matched `output_manifest.allowed_file_tool_paths`, "
            "bypassing the Claude Code harness permission prompt. High volume on "
            "a single tool may indicate an agent doing more direct writes than "
            "expected; check the corresponding agent's output_manifest scope."
        )
        lines.append("")
        lines.append(f"Total: {aa.get('total', 0)}")
        # Because collect_allow_auto_approve_stats() always includes at least 1
        # by_tool entry when total>0 (adopting "unknown" when tool_name is unknown),
        # the empty check can be omitted here.
        lines.append("")
        lines.append("| Tool | Count |")
        lines.append("|---|---|")
        for tool_name, cnt in aa.get("by_tool", {}).items():
            lines.append(f"| `{tool_name}` | {cnt} |")
        lines.append("")

    fhs = result["fix_hint_stats"]
    lines.append("## fix_hint stats")
    lines.append("")
    lines.append("### Hints present")
    for p, c in fhs["hint_present"].items():
        lines.append(f"- `{p}`: {c}")
    lines.append("")
    lines.append("### Hints absent (possible docs gap)")
    for p, c in fhs["hint_absent"].items():
        lines.append(f"- `{p}`: {c}")
    lines.append("")
    if fhs["repeated"]:
        lines.append("### Repeated commands (hint possibly ignored)")
        for p, cmds in fhs["repeated"].items():
            lines.append(f"- `{p}`: {len(cmds)} repeat(s)")
    lines.append("")

    fc = result["fail_closed_timeline"]
    lines.append("## fail_closed timeline")
    lines.append("")
    if fc["fail_closed_at"] is None:
        lines.append("No fail_closed event found.")
    else:
        lines.append(f"fail_closed at: `{fc['fail_closed_at']}`")
        lines.append("")
        lines.append("Last events before fail_closed:")
        lines.append("")
        lines.append("| ts | action | tool | policy | summary |")
        lines.append("|---|---|---|---|---|")
        for e in fc["last_events"]:
            lines.append(
                f"| {e['ts']} | {e['action']} | {e.get('tool_name','')} "
                f"| {e.get('policy','')} | {e['payload_summary']} |"
            )
    lines.append("")

    _render_launch_incident(
        result.get("launch_incident"), result.get("launch_incident_snapshots"), lines
    )

    if result.get("data_integrity_warning"):
        lines.append("## ⚠ data integrity warning")
        lines.append("")
        lines.append(f"Parse errors: {result['parse_error_count']}")
        lines.append("")
        for err in result.get("parse_errors", [])[:10]:
            lines.append(f"- `{err['path']}:{err['line_number']}` — {err['message']}")
        lines.append("")

    ar = result["agent_run_summary"]
    lines.append("## agent_runs summary")
    lines.append("")
    for status, cnt in ar["status_counts"].items():
        lines.append(f"- `{status}`: {cnt}")
    if ar["missing_finished_at"]:
        lines.append("")
        lines.append("Missing `finished_at` (incomplete records):")
        for run_id in ar["missing_finished_at"]:
            lines.append(f"- `{run_id}`")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only orchestration audit helper")
    parser.add_argument("--orchestration-id", required=True, help="Orchestration ID to audit")
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root (default: current directory)",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    result = audit(repo_root, args.orchestration_id)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_render_markdown(result))

    # Exit non-zero when log corruption is detected so CI / scripts can flag it.
    if result.get("data_integrity_warning"):
        sys.exit(2)


if __name__ == "__main__":
    main()
