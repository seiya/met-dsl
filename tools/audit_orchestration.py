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
from pathlib import Path, PurePosixPath
from typing import Any

try:  # script run: sys.path[0] is tools/ ; package import: repo root on path
    from orchestration_diagnostics import (
        build_launch_incident,
        api_error_from_records,
        aggregate_child_usage,
        aggregate_parent_usage,
        summarize_pure_leaf_metas,
    )
except ImportError:  # pragma: no cover - import-path shim
    from tools.orchestration_diagnostics import (
        build_launch_incident,
        api_error_from_records,
        aggregate_child_usage,
        aggregate_parent_usage,
        summarize_pure_leaf_metas,
    )


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


# Audit-log continuity: a hook-policy id that was renamed forward keeps a legacy
# alias here so historical block records (carrying the old id) aggregate under the
# new id rather than splitting into a separate, stale bucket. Add an entry whenever
# a policy id is renamed; the right-hand side is the current canonical id.
_LEGACY_POLICY_ALIASES: dict[str, str] = {
    # P2-7 cleanup: the guard that rejects unauthorized direct artifact writes was
    # renamed from the guarded-apply-patch-era id to one that names its actual job.
    "enforce_guarded_apply_patch": "forbid_unauthorized_file_write",
}


def _policy_of(block: dict[str, Any]) -> str:
    """Canonical policy id for a block record, applying legacy-id aliases so a
    record written before a policy rename is counted under the current id."""
    policy = (block.get("audit_detail") or {}).get("policy", "unknown")
    return _LEGACY_POLICY_ALIASES.get(policy, policy)


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
        policy = _policy_of(b)
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
        policy = _policy_of(b)
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
        policy = _policy_of(b)
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
        policy = _policy_of(b)
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
            # Preserve None for non-block events (no policy), but alias a legacy id.
            "policy": (
                _LEGACY_POLICY_ALIASES.get(_raw_policy, _raw_policy)
                if (_raw_policy := (e.get("audit_detail") or {}).get("policy")) is not None
                else None
            ),
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


def collect_token_cost_summary(
    repo_root: Path,
    meta: dict[str, Any] | None,
    agent_runs: list[dict[str, Any]],
    invalid_runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attribute token cost across the orchestration (parent) and its children.

    Resolves the measurement blind spot where child ``Agent`` subagents — the
    majority of a node's cost — are not sidechains in the host transcript. Child
    usage comes first from the durable ``usage`` field ``finalize_child`` writes
    into ``agent_runs.jsonl`` (survives ``~/.claude`` cleanup), then falls back to
    the ephemeral subagent transcript for any child lacking it. ``parent`` is the
    orchestration session(s)' own usage. Best-effort — reports ``available=False``
    (never raises) when neither side yields data.
    """
    meta = meta or {}
    # The orchestration agent's own arid appears in agent_runs.jsonl but is the
    # parent, not a child subagent — exclude it so it isn't reported as an
    # unlocatable child.
    parent_arid = str(meta.get("orchestration_agent_run_id") or "").strip()
    arids: list[str] = []
    persisted: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for run in list(agent_runs) + list(invalid_runs or []):
        arid = run.get("agent_run_id")
        if not (isinstance(arid, str) and arid and arid != parent_arid and arid not in seen):
            continue
        seen.add(arid)
        arids.append(arid)
        # Durable per-child usage persisted into agent_runs.jsonl by finalize_child
        # survives ~/.claude cleanup; prefer it over the ephemeral transcript so a
        # later post-cleanup audit still sees child totals. The
        # {"status": "unavailable"} marker (no numeric total) is not data.
        u = run.get("usage")
        if isinstance(u, dict) and isinstance(u.get("total_tokens"), int):
            entry = dict(u)
            entry.setdefault("source", "agent_runs.jsonl")
            persisted[arid] = entry

    # Reconstruct from the ephemeral transcripts only for children that lack durable
    # usage (legacy rows, or non-finalize-child paths) — skipping ~/.claude entirely
    # when every child is already covered.
    missing = [a for a in arids if a not in persisted]
    transcripts = (
        aggregate_child_usage(repo_root, missing)
        if missing
        else {"available": True, "per_child": {}, "unmatched_arids": [], "matched_count": 0}
    )
    per_child: dict[str, Any] = dict(persisted)
    for c_arid, c_usage in (transcripts.get("per_child") or {}).items():
        if isinstance(c_usage, dict):
            c_usage.setdefault("source", "transcript")
        per_child[c_arid] = c_usage

    sum_keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
        "assistant_turns",
    )
    children_total = {k: 0 for k in sum_keys}
    for u in per_child.values():
        for k in sum_keys:
            children_total[k] += int(u.get(k, 0) or 0)
    children_total["peak_context_tokens"] = max(
        (int(u.get("peak_context_tokens", 0) or 0) for u in per_child.values()),
        default=0,
    )
    children: dict[str, Any] = {
        "available": bool(per_child) or bool(transcripts.get("available")),
        "per_child": per_child,
        "children_total": children_total,
        "matched_count": len(per_child),
        "unmatched_arids": sorted(set(arids) - set(per_child)),
        "projects_dir": transcripts.get("projects_dir"),
    }
    if not per_child:
        children["reason"] = transcripts.get("reason") or "no child usage located"

    # Parent usage: the multi-session sum across the orchestration agent's host
    # sessions (a resumed node may run the parent under more than one host session).
    parent: dict[str, Any] = {"found": False}
    if parent_arid:
        agg_parent = aggregate_parent_usage(repo_root, parent_arid)
        if agg_parent.get("available"):
            parent = agg_parent

    # Available when EITHER side actually yielded data: in a post-cleanup audit the
    # parent session may survive while child transcripts are gone (or vice-versa),
    # and the surviving total is worth showing. But when NEITHER side matched
    # anything, report unavailable rather than a misleading 0-token breakdown — a
    # present-but-empty ~/.claude dir must not count as "available".
    summary: dict[str, Any] = {
        "available": bool(per_child) or bool(parent.get("found")),
        "parent": parent,
        "children": children,
    }
    parent_total = int(parent.get("total_tokens", 0) or 0) if parent.get("found") else 0
    child_total = int((children.get("children_total") or {}).get("total_tokens", 0) or 0)
    node_total = parent_total + child_total
    summary["node_total_tokens"] = node_total
    summary["parent_total_tokens"] = parent_total
    summary["children_total_tokens"] = child_total
    if node_total > 0:
        summary["children_fraction"] = round(child_total / node_total, 3)
    return summary


# The `generate-executor` vocabulary as HISTORICALLY RECORDED on `invocation.generate_executor`.
# Since M-F, run_workflow no longer validates this value on cold init (legacy execution was removed
# and the executor is not selectable — cold runs always record `pure`), but past orchestrations on
# disk still carry `legacy`, so the audit keeps both to stay a faithful reader of historical
# records. Duplicated as a literal rather than imported so the read-only audit does not pull in the
# workflow launcher; the render only uses it to flag an out-of-vocabulary value, never to reject one.
_KNOWN_GENERATE_EXECUTORS: tuple[str, ...] = ("legacy", "pure")


def _clean_str(value: Any) -> str | None:
    """Return a stripped non-empty string, else None.

    A missing / null / wrong-typed provenance field (`generate_executor`,
    `agent_version`) is reported as absent rather than rendered as a spurious
    recorded value. The value is stripped, not just validated: these render inline
    into markdown, so surrounding whitespace from a hand-edited or foreign
    artifact would otherwise break the line. (The live writers already strip —
    `_probe_claude_backend` stores `claude --version` as `stdout.strip()` — so
    this is defence in depth, not a live defect.)"""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _pure_source_dirs_of(
    repo_root: Path, orchestration_id: str
) -> tuple[list[str], list[str]]:
    """Every repo-relative source directory under this orchestration's node
    pipeline(s), in sorted order.

    Discovery reads this orchestration's OWN pipeline reservations
    (`reservations/<node_key_safe>/generate.json#reserved_ir_id`), which
    `prepare_node` writes before Compile runs and which `resume_node_refs` already
    treats as the authority for the pipeline id.

    The `orchestration_checkpoint.json` is NOT usable for this: `update_checkpoint`
    fills `pipeline_ref` from the step result, and the conductor only supplies it
    (via `launch_request_ref`) for the `validate` step — so every real
    `compile` / `generate` / `build` entry records `pipeline_ref: ""`. Discovering
    from the checkpoint would therefore find nothing on a `generate`-only run, which
    is exactly the A/B command, and nothing for a terminally-failed generate.

    Globbing `<pipeline_ref>/source/*` (rather than reading pass-only
    `completed_steps[].output_refs`) is what keeps a terminally-failed generate and
    every cold-restart-rotated source dir measured — otherwise the pure-arm totals
    silently undercount. The node's `pipeline_id` is allocated once per orchestration
    node and reused across restarts, so the glob is exactly this orchestration's
    generate attempts. A legacy source dir under the same pipeline carries no
    bundle_meta/verdict_meta and is dropped by the caller's `found` filter.

    Returns `(source_dirs, pipeline_refs)`. The accepted `pipeline_refs` are returned
    alongside so the caller can tell an EMPTY result apart: no pipeline reservation
    at all (the node was never prepared) versus a reserved pipeline whose `source/`
    does not exist yet (`Generate` has not run — the normal state of a run stopped at
    Compile, e.g. `run_workflow.py <spec> Compile`, and of a `--with-deps` dependency
    node when the TARGET stops at Compile: `dep_until_phase` follows the target, so a
    `generate`/`validate` target drives its deps all the way to Validate and those do
    produce source dirs). Those two states must not share a diagnosis. A
    `reserved_ir_id` that is not a single clean path segment is rejected (it would
    escape the pipeline root).
    """
    dirs: list[str] = []
    pipeline_refs: list[str] = []
    res_root = _orch_root(repo_root, orchestration_id) / "reservations"
    if not res_root.is_dir():
        return dirs, pipeline_refs
    for node_dir in sorted(res_root.iterdir()):
        if not node_dir.is_dir():
            continue
        reserved = (_load_json_if_dict(node_dir / "generate.json") or {}).get("reserved_ir_id")
        if not (isinstance(reserved, str) and reserved):
            continue
        # The reserved id is JSON-sourced: require a single clean segment so it can
        # never traverse out of `workspace/pipelines/<node_key_safe>/`.
        if reserved in {".", ".."} or PurePosixPath(reserved).parts != (reserved,):
            continue
        pref = f"workspace/pipelines/{node_dir.name}/{reserved}"
        if pref not in pipeline_refs:
            pipeline_refs.append(pref)
    for pref in pipeline_refs:
        source_root = repo_root / pref / "source"
        if not source_root.is_dir():
            continue
        for child in sorted(source_root.iterdir()):
            if not child.is_dir():
                continue
            rel = f"{pref}/source/{child.name}"
            if rel not in dirs:
                dirs.append(rel)
    return dirs, pipeline_refs


def collect_pure_leaf_ab_summary(
    repo_root: Path,
    orchestration_id: str,
    meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """A/B-measurement rollup for the Z2 pure `generate` leaves (milestone M-E).

    Surfaces the executor selection (`orchestration_meta.json#invocation.
    generate_executor`) and the probed backend's CLI version
    (`preflight.json#agent_version` — already persisted, so no new file is written;
    it is `claude --version` on a claude run and `codex --version` on a codex run,
    so `backend` is carried with it) alongside per-node pure-leaf metrics read from
    `bundle_meta.json` / `verdict_meta.json`. `available` is true only when a pure node was located,
    so a legacy (agentic) run reports `available=False` with the executor still
    surfaced; `reason` then says why, distinguishing "this run wrote no pure meta"
    from "Generate has not produced a source dir yet" and from "the node was never
    prepared", which would otherwise all render as a silent, legacy-looking zero.

    I/O: `meta` is passed in because `audit()` already loads it for its other
    sections; `preflight.json` and the pipeline reservations are read here because
    this is their only consumer. Best-effort — the caller wraps it so a diagnostics
    failure never breaks the audit.
    """
    meta = meta or {}
    invocation = meta.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}
    generate_executor = _clean_str(invocation.get("generate_executor"))

    root = _orch_root(repo_root, orchestration_id)
    preflight = _load_json_if_dict(root / "preflight.json") or {}
    # `agent_version` is backend-agnostic: `probe_execution_platform` stores whatever
    # the selected backend's prober returned — `claude --version` on a claude run,
    # `codex --version` on a codex run. Carry the recorded `backend` so the renderer
    # can label it truthfully; naming it "claude" unconditionally would report false
    # provenance for every codex orchestration (which this section still renders,
    # since a codex node runs the agentic residual leaf, not the pure producer).
    backend = _clean_str(preflight.get("backend"))
    agent_cli_version = _clean_str(preflight.get("agent_version"))

    source_dirs, pipeline_refs = _pure_source_dirs_of(repo_root, orchestration_id)
    nodes: list[dict[str, Any]] = []
    for source_dir in source_dirs:
        summary = summarize_pure_leaf_metas(repo_root / source_dir)
        if summary.get("found"):
            # Label repo-relative here (the callee sets no `source_dir`). Build a new
            # dict rather than mutating the returned one, so the ownership is a
            # visible fact of this expression, not an implicit callee obligation.
            nodes.append({**summary, "source_dir": source_dir})

    result: dict[str, Any] = {
        "available": bool(nodes),
        "generate_executor": generate_executor,
        "backend": backend,
        "agent_cli_version": agent_cli_version,
        "pure_nodes": nodes,
    }
    if not nodes:
        if not pipeline_refs:
            # No pipeline reservation at all: `prepare_node` never ran for any node of
            # this orchestration (or the reservations were removed). Name it — this is
            # the only case where discovery itself could not proceed, and it must not
            # be confused with the routine "Generate hasn't run yet" below.
            result["reason"] = (
                "no pipeline reservation under this orchestration "
                "— discovery found no node to measure"
            )
        elif not source_dirs:
            result["reason"] = (
                "no generate source directory under the pipeline yet "
                "(Generate has not produced one)"
            )
        else:
            result["reason"] = "no pure-leaf meta located"
    return result


def audit(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    root = _orch_root(repo_root, orchestration_id)
    hook_events, hook_errs = _load_jsonl_with_errors(root / "hooks" / "native_hook_events.jsonl")
    phase_log, phase_errs = _load_jsonl_with_errors(root / "phase_state_log.jsonl")
    agent_runs, runs_errs = _load_jsonl_with_errors(root / "agent_runs.jsonl")
    invalid_runs, inv_errs = _load_jsonl_with_errors(root / "agent_runs_invalid.jsonl")
    meta = _load_json_if_dict(root / "orchestration_meta.json") or {}

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
    # Token-cost attribution (parent orchestration vs child subagents). Children
    # carry no usage in agent_runs.jsonl and aren't sidechains, so this reads the
    # ephemeral ~/.claude transcripts. Best-effort — must never break the audit.
    try:
        token_cost_summary = collect_token_cost_summary(
            repo_root, meta, agent_runs, invalid_runs
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break the audit
        token_cost_summary = {"available": False, "reason": "token-cost collection failed"}
    # Pure-leaf A/B rollup (Z2 M-E): executor selection + claude --version +
    # per-node bundle_meta/verdict_meta metrics. Reads only in-repo artifacts
    # (no ~/.claude). Best-effort — must never break the audit.
    try:
        pure_leaf_ab_summary = collect_pure_leaf_ab_summary(repo_root, orchestration_id, meta)
    except Exception:  # noqa: BLE001 - diagnostics must never break the audit
        pure_leaf_ab_summary = {"available": False, "reason": "pure-leaf A/B collection failed"}
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
        "token_cost_summary": token_cost_summary,
        "pure_leaf_ab_summary": pure_leaf_ab_summary,
        "invalid_run_count": len(invalid_runs),
        "invalid_run_ids": [r.get("agent_run_id") for r in invalid_runs if r.get("agent_run_id")],
        "data_integrity_warning": (len(parse_errors) > 0) or (unparseable_count > 0),
        "parse_error_count": len(parse_errors),
        "parse_errors": parse_errors,
        "unparseable_timestamp_count": unparseable_count,
    }


def _render_api_error_line(api_error: Any, lines: list[str]) -> None:
    """Render a transient-API-error line (e.g. 529 Overloaded) when present, so the
    reader sees the dangling launch was a transport blip rather than a hang."""
    if not isinstance(api_error, dict) or api_error.get("status") is None:
        return
    retry_hint = " (retryable — safe to `--resume`)" if api_error.get("retryable") else ""
    msg = str(api_error.get("message") or "").strip()
    lines.append(f"- transient API error: `{api_error.get('status')}` {msg}{retry_hint}")


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
        # Fall back to parsing raw_tail for legacy snapshots captured before the
        # structured api_error field existed.
        _render_api_error_line(
            ct.get("api_error") or api_error_from_records(ct.get("raw_tail")), lines
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
            # Legacy snapshot fallback: abort_marker predates api_error; recover it
            # from the child transcript's raw_tail if that field is missing.
            _render_api_error_line(
                abort.get("api_error") or api_error_from_records(ct.get("raw_tail")), lines
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


def _fmt_tok(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "n/a"


def _render_token_cost(summary: dict[str, Any] | None, lines: list[str]) -> None:
    """Render the parent-vs-children token breakdown — the measurement that makes
    the (usually dominant) child subagent cost visible."""
    lines.append("## Token cost breakdown (parent vs child subagents)")
    lines.append("")
    if not isinstance(summary, dict) or not summary.get("available"):
        reason = (summary or {}).get("reason") or (
            (summary or {}).get("children", {}) or {}
        ).get("reason", "unavailable")
        lines.append(
            f"Child token attribution unavailable: {reason}. "
            "(`~/.claude` transcripts are machine-local and ephemeral; run the "
            "audit on the machine that executed the workflow, before cleanup.)"
        )
        lines.append("")
        return

    children = summary.get("children", {}) or {}
    parent = summary.get("parent", {}) or {}
    parent_ok = bool(parent.get("found"))
    # "available" but zero matched transcripts (dir present, all arids cleaned) is
    # not a measurement — showing "0 (0%)" would read as "children cost nothing".
    children_ok = bool(children.get("available")) and int(children.get("matched_count", 0) or 0) > 0
    node = _fmt_tok(summary.get("node_total_tokens"))
    parent_t = summary.get("parent_total_tokens", 0)
    child_t = summary.get("children_total_tokens", 0)
    frac = summary.get("children_fraction")
    frac_str = f" ({frac:.0%} of node)" if isinstance(frac, (int, float)) else ""
    note = ""
    if not (parent_ok and children_ok):
        # Partial data (post-cleanup audit): node total covers only the side(s) below.
        missing = "child subagent" if not children_ok else "parent"
        note = f" (partial — {missing} transcript(s) unavailable)"
    lines.append(f"- **node total**: {node} tokens{note}")
    lines.append(
        f"- parent orchestration: {_fmt_tok(parent_t) if parent_ok else 'unavailable'}"
    )
    lines.append(
        f"- **child subagents**: "
        f"{(_fmt_tok(child_t) + frac_str) if children_ok else 'unavailable'}"
    )
    if parent.get("found"):
        lines.append(
            f"  - parent peak context: {_fmt_tok(parent.get('peak_context_tokens'))} "
            f"over {parent.get('assistant_turns', 'n/a')} turns"
        )
    unmatched = children.get("unmatched_arids") or []
    if unmatched:
        lines.append(
            f"  - ⚠ {len(unmatched)} child arid(s) had no locatable transcript "
            "(ephemeral/cleaned)"
        )
    lines.append("")
    per_child = children.get("per_child") or {}
    if per_child:
        ranked = sorted(
            per_child.items(),
            key=lambda kv: int(kv[1].get("total_tokens", 0) or 0),
            reverse=True,
        )
        lines.append("| child agent_run_id | total | turns | peak ctx |")
        lines.append("|---|---|---|---|")
        for arid, usage in ranked:
            lines.append(
                f"| `{arid}` | {_fmt_tok(usage.get('total_tokens'))} | "
                f"{usage.get('assistant_turns', 'n/a')} | "
                f"{_fmt_tok(usage.get('peak_context_tokens'))} |"
            )
        lines.append("")


def _render_pure_leaf_row(label: str, row: dict[str, Any], lines: list[str]) -> None:
    """Render one pure-leaf (`generate` / `verify`) metrics row. Within a located
    pure node an absent sub-row means only that leaf's meta was not written (not
    that the node is legacy)."""
    if not isinstance(row, dict) or not row.get("found"):
        lines.append(f"- `{label}`: no {label} meta recorded")
        return
    usage = row.get("usage_total") or {}
    # Collapse the per-attempt model list: a repair loop normally resolves the same
    # alias every turn, and joining it raw renders "model(s): m, m, m", which reads
    # as several distinct models (or as an attempt count). Distinct-in-order keeps
    # the genuine multi-model case visible.
    models = list(dict.fromkeys(row.get("models") or []))
    model_str = f", model(s): {', '.join(models)}" if models else ""
    cat = row.get("failure_category")
    cat_str = f", failure: `{cat}`" if cat else ""
    # The prompt contract version is the A/B comparability check — two arms run
    # under different contract versions are not measuring the same thing.
    contract = row.get("prompt_contract_version")
    contract_str = f", contract=`{contract}`" if contract else ""
    lines.append(
        f"- `{label}`: result=`{row.get('result')}`, attempts={row.get('attempts')} "
        f"(repair turns={row.get('repair_turns')}){cat_str}{contract_str}"
    )
    # `total` sums all four token classes; show cache_creation too so the four
    # displayed numbers reconcile with `total`.
    lines.append(
        f"  - tokens — in {_fmt_tok(usage.get('input_tokens'))}, "
        f"out {_fmt_tok(usage.get('output_tokens'))}, "
        f"cache_read {_fmt_tok(usage.get('cache_read_input_tokens'))}, "
        f"cache_creation {_fmt_tok(usage.get('cache_creation_input_tokens'))}, "
        f"total {_fmt_tok(usage.get('total_tokens'))}{model_str}"
    )


def _render_pure_leaf_ab(summary: dict[str, Any] | None, lines: list[str]) -> None:
    """Render the Z2 pure-leaf A/B rollup: executor + claude --version + per-node
    generate/verify attempt and token metrics (the P-arm provenance of a billed
    A/B comparison)."""
    lines.append("## Pure-leaf A/B metrics (Z2)")
    lines.append("")
    summary = summary if isinstance(summary, dict) else {}
    recorded_executor = summary.get("generate_executor")
    executor = recorded_executor or "unknown"
    version = summary.get("agent_cli_version") or "unrecorded"
    backend = summary.get("backend")
    lines.append(f"- generate-executor: `{executor}`")
    # Report the recorded value verbatim (diagnostics say what IS recorded, not what
    # should be) but flag an out-of-vocabulary one: the branches below key on the
    # exact value, so an unrecognized executor must not be silently read as legacy.
    if recorded_executor and recorded_executor not in _KNOWN_GENERATE_EXECUTORS:
        lines.append(
            f"  - ⚠ unrecognized executor value (expected one of "
            f"{', '.join(f'`{e}`' for e in _KNOWN_GENERATE_EXECUTORS)})"
        )
    # Label the version by the backend that was actually probed. `agent_version` is
    # whichever CLI ran, so a fixed "claude --version" label would misreport every
    # codex orchestration's version as Claude's.
    version_label = f"{backend} --version" if backend else "backend CLI --version"
    lines.append(f"- {version_label}: `{version}` (from `preflight.json#agent_version`)")
    nodes = summary.get("pure_nodes") or []
    if not summary.get("available") or not nodes:
        # Say which case this is. Under executor=pure, "legacy/agentic run" would
        # contradict the executor line rendered directly above; under an unknown or
        # unrecognized executor we cannot claim either arm.
        reason = summary.get("reason")
        if executor == "pure":
            hint = reason or "pure run with no `bundle_meta.json` / `verdict_meta.json` written"
        elif executor == "legacy":
            hint = "legacy/agentic run"
            hint += f", or {reason}" if reason else ", or the pure metas are absent"
        else:
            hint = reason or "executor not recorded or unrecognized; no pure metas on disk"
        lines.append(f"- no pure-leaf node located ({hint})")
        lines.append("")
        return
    lines.append("")
    for node in nodes:
        lines.append(f"### `{node.get('source_dir')}`")
        _render_pure_leaf_row("generate", node.get("generate") or {}, lines)
        _render_pure_leaf_row("verify", node.get("verify") or {}, lines)
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

    _render_token_cost(result.get("token_cost_summary"), lines)

    _render_pure_leaf_ab(result.get("pure_leaf_ab_summary"), lines)

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
