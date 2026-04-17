#!/usr/bin/env python3
"""Helpers for Codex workflow orchestration artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import traceback
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


TERMINAL_STATUSES = {"pass", "fail", "blocked", "timeout", "cancel"}
SUPPORTED_BACKENDS = {"codex", "cursor", "claude"}
PREFLIGHT_TTL_DEFAULT_SECONDS: int = 1800
VALID_REPAIR_STRATEGIES = frozenset({"none", "reuse", "restart"})
VALID_ISSUE_SEVERITIES = frozenset({"none", "minor", "major", "critical"})

# Must match tools/validate_workspace_root.py (canonical pipeline/plan id directory naming).
_NODE_KEY_SAFE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$"
)
_SLUG_DATE_SEQ3_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$")
DEFAULT_BACKEND_COMMANDS = {
    "codex": "codex",
    "cursor": "agent",
    "claude": "claude",
}

# Child agent `skill_must_read_refs`: split workflow spec (see docs/workflow/).
WORKFLOW_CORE_REF = "docs/workflow/WORKFLOW_CORE.md"
WORKFLOW_PHASE_DOC_BY_STEP: dict[str, str] = {
    "plan": "docs/workflow/phases/phase_01_plan.md",
    "generate": "docs/workflow/phases/phase_02_generate.md",
    "build": "docs/workflow/phases/phase_03_build.md",
    "execute": "docs/workflow/phases/phase_04_execute.md",
    "judge": "docs/workflow/phases/phase_05_judge.md",
    "tune": "docs/workflow/phases/phase_06_tune.md",
    "promote": "docs/workflow/phases/phase_07_promote.md",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text if text.endswith("\n") else f"{text}\n"
    path.write_text(body, encoding="utf-8")


def _orchestration_root(repo_root: Path, orchestration_id: str) -> Path:
    return repo_root / "workspace" / "orchestrations" / orchestration_id


# --- Phase 1: access policy / phase state artifact layout (Item 10) ---

DEFAULT_ALLOWED_GATE_SERVICES: tuple[str, ...] = (
    "validate_pipeline_semantics",
    "check_artifact_syntax",
    "validate_workspace_root",
    "orchestration_read",
    "apply_patch_writes",
)

STEP_REQUIRED_CHILD_AGENT: dict[str, str] = {
    "plan": "substep",
    "generate": "substep",
    "tune": "substep",
    "build": "step",
    "execute": "step",
    "judge": "step",
    "promote": "step",
}

FAIL_CLOSED_REASON_CODES = {
    "child_agent_forbidden_by_session_policy",
    "child_agent_unavailable_on_execution_platform",
    "required_child_agent_kind_mismatch",
    "phase_body_started_before_launch",
    "noncanonical_phase_write_attempt",
    "dependency_not_ready",
}

PHASE_ARTIFACT_GUARDED_PREFIXES: tuple[str, ...] = ("workspace/plans/", "workspace/pipelines/")

STEP_KEYS_FOR_NODE_STATE: tuple[str, ...] = (
    "plan",
    "generate",
    "build",
    "execute",
    "judge",
)


def _access_policies_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "access_policies"


def _access_logs_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "access_logs"


def _violations_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "violations"


def _capabilities_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "capabilities"


def _gates_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "gates"


def _phase_state_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "phase_state.json"


def _phase_state_log_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "phase_state_log.jsonl"


def _ensure_orchestration_audit_dirs(repo_root: Path, orchestration_id: str) -> None:
    root = _orchestration_root(repo_root, orchestration_id)
    for sub in ("access_policies", "access_logs", "violations", "capabilities"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def _new_phase_state_document(orchestration_id: str) -> dict[str, Any]:
    return {
        "orchestration_id": orchestration_id,
        "current_state": "initialized",
        "node_states": {},
    }


def _append_phase_state_log(
    repo_root: Path,
    orchestration_id: str,
    entry: dict[str, Any],
) -> None:
    path = _phase_state_log_path(repo_root, orchestration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _write_phase_state(repo_root: Path, orchestration_id: str, doc: dict[str, Any]) -> None:
    _write_json(_phase_state_path(repo_root, orchestration_id), doc)


def _load_phase_state(repo_root: Path, orchestration_id: str) -> dict[str, Any] | None:
    path = _phase_state_path(repo_root, orchestration_id)
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"phase_state.json is invalid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"phase_state.json must be object: {path}")
    return data


def _merge_node_states(
    existing: Any,
    orchestration_id: str,
) -> dict[str, dict[str, str]]:
    """checkpoint と矛盾しないよう既存 node_states を保持しつつ欠損キーを補う。"""
    merged: dict[str, dict[str, str]] = {}
    if isinstance(existing, dict):
        for node_key, steps in existing.items():
            if not isinstance(node_key, str) or not node_key.strip():
                continue
            if not isinstance(steps, dict):
                continue
            inner: dict[str, str] = {}
            for sk in STEP_KEYS_FOR_NODE_STATE:
                v = steps.get(sk)
                if isinstance(v, str) and v.strip():
                    inner[sk] = v.strip()
                else:
                    inner[sk] = "not_started"
            merged[node_key.strip()] = inner
    return merged


def init_phase_state_json(
    repo_root: Path,
    orchestration_id: str,
    *,
    reason: str = "init",
) -> dict[str, Any]:
    """`phase_state.json` を新規 orchestration 用に書き出す。"""
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    doc = _new_phase_state_document(orchestration_id)
    _write_phase_state(repo_root, orchestration_id, doc)
    _append_phase_state_log(
        repo_root,
        orchestration_id,
        {
            "ts": _utc_now_iso(),
            "event": reason,
            "from": None,
            "to": doc["current_state"],
        },
    )
    return doc


def _initial_current_state_when_phase_state_missing(
    repo_root: Path,
    orchestration_id: str,
) -> str:
    """レガシー orchestration で `phase_state.json` が無い場合の `current_state` 推定値。"""
    path = _preflight_path(repo_root, orchestration_id)
    if not path.exists():
        return "initialized"
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError):
        return "initialized"
    if isinstance(payload, dict) and _preflight_allows_agent_launch(payload):
        return "preflight_passed"
    return "initialized"


def merge_phase_state_for_resume(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """`--resume-from-checkpoint` 時: 既存 `phase_state` を破棄せず `node_states` を保持する。

    `orchestration_checkpoint.json` の完了情報とは別ファイルのため直接マージは行わない。
    欠損の `phase_state.json` のみ初期化し、既存がある場合は `current_state` と
    `node_states` を上書きしない。監査用に `phase_state_log.jsonl` へ `resume_enabled` を追記する。
    """
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    existing = _load_phase_state(repo_root, orchestration_id)
    if existing is None:
        inferred = _initial_current_state_when_phase_state_missing(repo_root, orchestration_id)
        _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
        doc = _new_phase_state_document(orchestration_id)
        doc["current_state"] = inferred
        _write_phase_state(repo_root, orchestration_id, doc)
        _append_phase_state_log(
            repo_root,
            orchestration_id,
            {
                "ts": _utc_now_iso(),
                "event": "resume_missing_phase_state",
                "from": None,
                "to": inferred,
                "note": "created for checkpoint resume; inferred from preflight when possible",
            },
        )
        return doc
    orch_id = existing.get("orchestration_id")
    if orch_id != orchestration_id:
        raise RuntimeError(
            f"phase_state.json orchestration_id mismatch: expected {orchestration_id!r}, got {orch_id!r}"
        )
    merged = dict(existing)
    merged["node_states"] = _merge_node_states(merged.get("node_states"), orchestration_id)
    _write_phase_state(repo_root, orchestration_id, merged)
    _append_phase_state_log(
        repo_root,
        orchestration_id,
        {
            "ts": _utc_now_iso(),
            "event": "checkpoint_resume_enabled",
            "from": merged.get("current_state"),
            "to": merged.get("current_state"),
            "note": "orchestration_meta resume_enabled; phase_state preserved",
        },
    )
    return merged


def _transition_phase_state(
    repo_root: Path,
    orchestration_id: str,
    *,
    new_state: str,
    event: str,
) -> dict[str, Any]:
    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        doc = _new_phase_state_document(orchestration_id)
    elif doc.get("orchestration_id") not in (orchestration_id, None):
        raise RuntimeError(
            "phase_state.json orchestration_id mismatch: "
            f"expected {orchestration_id!r}, got {doc.get('orchestration_id')!r}"
        )
    prev = doc.get("current_state")
    doc["current_state"] = new_state
    if doc.get("orchestration_id") != orchestration_id:
        doc["orchestration_id"] = orchestration_id
    if not isinstance(doc.get("node_states"), dict):
        doc["node_states"] = _merge_node_states({}, orchestration_id)
    _write_phase_state(repo_root, orchestration_id, doc)
    _append_phase_state_log(
        repo_root,
        orchestration_id,
        {
            "ts": _utc_now_iso(),
            "event": event,
            "from": prev,
            "to": new_state,
        },
    )
    return doc


def _default_capability_expires_at_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace("+00:00", "Z")


def _parse_iso_z_expiry(raw: str) -> datetime | None:
    token = raw.strip()
    if not token:
        return None
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _mcp_permissions_for_launch(role: str, step: str) -> list[str]:
    r = role.strip().lower()
    st = step.strip().lower()
    if r == "orchestration":
        return []
    if r not in {"step", "substep"}:
        return []
    if st == "generate":
        return ["run_linter"]
    if st == "build":
        return ["compile_project"]
    if st == "execute":
        return ["run_program", "run_quality_checks"]
    return []


def _write_roots_for_launch(
    *,
    role: str,
    step: str,
    orchestration_id: str,
    plan_ref: str,
    pipeline_ref: str,
) -> list[str]:
    r = role.strip().lower()
    st = step.strip().lower()
    orch_root = _with_trailing_slash(_normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}"))
    plan_norm = _with_trailing_slash(_normalize_rel_posix(plan_ref))
    pipe_norm = _with_trailing_slash(_normalize_rel_posix(pipeline_ref))
    if r == "orchestration":
        return [orch_root]
    if r not in {"step", "substep"}:
        return []
    if st == "plan":
        return [plan_norm]
    if st == "generate":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/generate"))]
    if st == "build":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/build"))]
    if st == "execute":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/execute"))]
    if st == "judge":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/judge"))]
    return []


def build_capability_document(
    *,
    agent_run_id: str,
    orchestration_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """`capabilities/<agent_run_id>.json` のペイロードを組み立てる。"""
    role_raw = request_payload.get("agent_role")
    role = role_raw.strip().lower() if isinstance(role_raw, str) and role_raw.strip() else ""
    if role not in {"orchestration", "step", "substep"}:
        ss0 = request_payload.get("substep")
        if isinstance(ss0, str) and ss0.strip():
            role = "substep"
        elif isinstance(request_payload.get("step"), str) and str(request_payload.get("step")).strip():
            role = "step"
    if role not in {"orchestration", "step", "substep"}:
        raise ValueError("capability requires agent_role orchestration|step|substep")
    step_raw = request_payload.get("step")
    if not isinstance(step_raw, str) or not step_raw.strip():
        raise ValueError("capability requires step")
    step = step_raw.strip().lower()
    node_raw = request_payload.get("node_key")
    if not isinstance(node_raw, str) or not node_raw.strip():
        raise ValueError("capability requires node_key")
    node_key = node_raw.strip()
    plan_ref = str(request_payload.get("plan_ref") or "").strip()
    pipeline_ref = str(request_payload.get("pipeline_ref") or "").strip()
    if not plan_ref or not pipeline_ref:
        raise ValueError("capability requires plan_ref and pipeline_ref")

    substep_val: str | None = None
    ss = request_payload.get("substep")
    if isinstance(ss, str) and ss.strip():
        substep_val = ss.strip().lower()

    token = secrets.token_hex(32)
    body: dict[str, Any] = {
        "agent_run_id": agent_run_id.strip(),
        "capability_token": token,
        "orchestration_id": orchestration_id,
        "agent_role": role,
        "node_key": node_key,
        "step": step,
        "write_roots": _write_roots_for_launch(
            role=role,
            step=step,
            orchestration_id=orchestration_id,
            plan_ref=plan_ref,
            pipeline_ref=pipeline_ref,
        ),
        "mcp_permissions": _mcp_permissions_for_launch(role, step),
        "expires_at": _default_capability_expires_at_iso(),
    }
    if substep_val is not None:
        body["substep"] = substep_val
    return body


def _write_capability_for_launch(
    repo_root: Path,
    orchestration_id: str,
    child_agent_run_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    cap = build_capability_document(
        agent_run_id=child_agent_run_id,
        orchestration_id=orchestration_id,
        request_payload=request_payload,
    )
    out = _capabilities_dir(repo_root, orchestration_id) / f"{child_agent_run_id}.json"
    _write_json(out, cap)
    return cap


def _transition_node_step_phase_state(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    new_state: str,
    event: str,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    """`phase_state.json` の `node_states[node_key_safe][step]` を更新する。"""
    node_safe = _node_key_to_safe(node_key.strip())
    step_key = step.strip().lower()
    if step_key not in STEP_KEYS_FOR_NODE_STATE:
        raise ValueError(f"unsupported workflow step for phase_state: {step_key!r}")

    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        doc = _new_phase_state_document(orchestration_id)
    elif doc.get("orchestration_id") not in (orchestration_id, None):
        raise RuntimeError(
            "phase_state.json orchestration_id mismatch: "
            f"expected {orchestration_id!r}, got {doc.get('orchestration_id')!r}"
        )
    doc["orchestration_id"] = orchestration_id
    ns_any = doc.get("node_states")
    ns: dict[str, Any] = ns_any if isinstance(ns_any, dict) else {}
    inner_any = ns.get(node_safe)
    inner: dict[str, str]
    if isinstance(inner_any, dict):
        inner = {}
        for sk in STEP_KEYS_FOR_NODE_STATE:
            v = inner_any.get(sk)
            inner[sk] = v.strip() if isinstance(v, str) and v.strip() else "not_started"
    else:
        inner = {sk: "not_started" for sk in STEP_KEYS_FOR_NODE_STATE}
    prev = inner.get(step_key, "not_started")
    inner[step_key] = new_state
    ns[node_safe] = inner
    doc["node_states"] = ns
    _write_phase_state(repo_root, orchestration_id, doc)

    log_entry: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "event": event,
        "node_key_safe": node_safe,
        "step": step_key,
        "from": prev,
        "to": new_state,
    }
    if agent_run_id:
        log_entry["agent_run_id"] = agent_run_id
    _append_phase_state_log(repo_root, orchestration_id, log_entry)
    return doc


def _phase_state_allows_write_step_result(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
) -> None:
    """`write_step_result` は `child_finished` 到達済み step のみ許可する。"""
    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        raise RuntimeError("write_step_result phase gate: phase_state.json missing")
    node_safe = _node_key_to_safe(node_key.strip())
    step_key = step.strip().lower()
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        raise RuntimeError("write_step_result phase gate: phase_state.node_states missing")
    inner = ns.get(node_safe)
    if not isinstance(inner, dict):
        raise RuntimeError(f"write_step_result phase gate: phase_state missing node {node_safe!r}")
    st = inner.get(step_key)
    if not isinstance(st, str):
        raise RuntimeError(
            "write_step_result phase gate: phase_state missing node step "
            f"(node_key_safe={node_safe!r}, step={step_key!r})"
        )
    token = st.strip()
    if token == "child_finished":
        return
    raise RuntimeError(
        "write_step_result phase gate: node step must be child_finished "
        f"(node_key_safe={node_safe!r}, step={step_key!r}, current={token!r})"
    )


def _write_rule_source_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    read_path: str,
    matched_prefix: str | None,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.rule_source_violation.json"
    payload = {
        "kind": "rule_source_violation",
        "agent_run_id": agent_run_id,
        "read_path": read_path,
        "matched_denied_prefix": matched_prefix,
        "evaluated_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return out


def _write_phase_authority_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    actor_role: str,
    rejected_paths: list[str],
    reason: str,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.phase_authority_violation.json"
    payload = {
        "kind": "phase_authority_violation",
        "actor_role": actor_role,
        "agent_run_id": agent_run_id,
        "rejected_paths": rejected_paths,
        "reason": reason,
        "evaluated_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return out


def _write_noncanonical_phase_write_attempt(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    actor_role: str,
    attempted_paths: list[str],
    node_key: str | None,
    step: str | None,
    required_child_agent: str | None,
    current_phase_state: str | None,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = (
        _violations_dir(repo_root, orchestration_id)
        / f"{agent_run_id}.noncanonical_phase_write_attempt.json"
    )
    payload = {
        "kind": "noncanonical_phase_write_attempt",
        "agent_run_id": agent_run_id,
        "actor": actor_role,
        "attempted_paths": attempted_paths,
        "node_key": node_key,
        "step": step,
        "required_child_agent": required_child_agent,
        "current_phase_state": current_phase_state,
        "reason_code": "noncanonical_phase_write_attempt",
        "detected_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return out


def _required_child_agent_kind(step: str) -> str:
    step_token = step.strip().lower()
    required = STEP_REQUIRED_CHILD_AGENT.get(step_token)
    if required is None:
        raise ValueError(f"unsupported workflow step for child-agent requirement: {step!r}")
    return required


def _phase_write_requires_child_running(path: str) -> bool:
    p = _normalize_rel_posix(path)
    return any(p.startswith(prefix) for prefix in PHASE_ARTIFACT_GUARDED_PREFIXES)


def _execution_platform_launchable(preflight: dict[str, Any], required_child_agent: str) -> bool:
    if required_child_agent == "step":
        return preflight.get("can_launch_step_agents") is True
    if required_child_agent == "substep":
        return preflight.get("can_launch_substep_agents") is True
    return False


def _check_session_policy_launchable(
    preflight: dict[str, Any], required_child_agent: str
) -> dict[str, Any]:
    session_policy = preflight.get("session_policy")
    fallback_key = (
        "can_launch_step_agents" if required_child_agent == "step" else "can_launch_substep_agents"
    )
    launchable = False
    scope = "session_policy_missing"
    if isinstance(session_policy, dict):
        key = (
            "allow_step_agent_launch"
            if required_child_agent == "step"
            else "allow_substep_agent_launch"
        )
        if isinstance(session_policy.get(key), bool):
            launchable = bool(session_policy.get(key))
            scope = f"session_policy.{key}"
        elif isinstance(session_policy.get(fallback_key), bool):
            launchable = bool(session_policy.get(fallback_key))
            scope = f"session_policy.{fallback_key}"
    elif isinstance(preflight.get("session_policy_launchable"), bool):
        launchable = bool(preflight.get("session_policy_launchable"))
        scope = "session_policy_launchable"
    return {"launchable": launchable, "blocking_policy_scope": scope}


def _resolve_current_phase_state(
    repo_root: Path, orchestration_id: str, node_key: str, step: str
) -> str | None:
    doc = _load_phase_state(repo_root, orchestration_id)
    if not isinstance(doc, dict):
        return None
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        return None
    node_safe = _node_key_to_safe(node_key)
    inner = ns.get(node_safe)
    if not isinstance(inner, dict):
        return None
    value = inner.get(step.strip().lower())
    return value if isinstance(value, str) else None


def _reject_noncanonical_phase_write(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    actor_role: str,
    attempted_paths: list[str],
    node_key: str | None,
    step: str | None,
    current_phase_state: str | None,
) -> None:
    required: str | None = None
    if isinstance(step, str) and step.strip():
        try:
            required = _required_child_agent_kind(step)
        except ValueError:
            required = None
    _write_noncanonical_phase_write_attempt(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
        actor_role=actor_role,
        attempted_paths=attempted_paths,
        node_key=node_key,
        step=step,
        required_child_agent=required,
        current_phase_state=current_phase_state,
    )
    raise RuntimeError(
        "apply_patch gate: noncanonical phase write attempt detected before child_running"
    )


def _dependency_ready(
    repo_root: Path, orchestration_id: str, *, step: str
) -> tuple[bool, str | None]:
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        return False, "orchestration_meta_missing"
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        return False, "orchestration_meta_invalid"
    step_token = step.strip().lower()
    readiness = meta.get("dependency_readiness")
    if not isinstance(readiness, dict):
        return False, "dependency_readiness_missing"
    if step_token == "plan":
        token = readiness.get("direct_dependency_plan_readiness")
        if token is not True:
            return False, "direct_dependency_plan_readiness_not_pass"
    elif step_token in {"generate", "build", "execute", "judge", "tune", "promote"}:
        token = readiness.get("direct_dependency_execution_readiness")
        if token is not True:
            return False, "direct_dependency_execution_readiness_not_pass"
    else:
        return False, f"unsupported_step_for_dependency_readiness:{step_token}"
    detail = readiness.get("detail")
    if not isinstance(detail, dict):
        return False, "dependency_readiness_detail_missing"
    required_detail_keys: tuple[str, ...]
    if step_token == "plan":
        required_detail_keys = ("plan_ref_verified",)
    else:
        required_detail_keys = ("plan_ref_verified", "pipeline_ref_verified", "aggregate_verdict_verified")
    for required_key in required_detail_keys:
        if detail.get(required_key) is not True:
            return False, f"dependency_readiness_detail_not_pass:{required_key}"
    return True, None


def workflow_launch_check(
    repo_root: Path,
    *,
    orchestration_id: str,
    node_key: str,
    step: str,
    backend: str,
    require_child_agent: str,
) -> dict[str, Any]:
    required_by_step = _required_child_agent_kind(step)
    required_flag = require_child_agent.strip().lower()
    if required_flag not in {"step", "substep"}:
        raise ValueError("--require-child-agent must be step or substep")

    execution_platform_launchable = False
    session_policy_launchable = True
    blocking_scope = "default_allow"
    reason_code: str | None = None
    reason_detail: str | None = None

    if required_by_step != required_flag:
        reason_code = "required_child_agent_kind_mismatch"
        reason_detail = (
            f"step {step.strip().lower()!r} requires {required_by_step!r}, "
            f"but flag is {required_flag!r}"
        )

    try:
        preflight = _require_preflight_launchable(repo_root, orchestration_id, enforce_live_probe=False)
    except RuntimeError as exc:
        return {
            "status": "fail_closed",
            "orchestration_id": orchestration_id,
            "node_key": node_key,
            "step": step.strip().lower(),
            "required_child_agent": required_flag,
            "required_child_agent_by_step": required_by_step,
            "execution_platform_launchable": False,
            "session_policy_launchable": False,
            "reason_code": "child_agent_unavailable_on_execution_platform",
            "reason_detail": str(exc),
            "blocking_policy_scope": "preflight",
            "next_action": "stop_before_phase_body",
        }
    preflight_backend = preflight.get("backend")
    if isinstance(preflight_backend, str) and preflight_backend.strip().lower() != backend.strip().lower():
        reason_code = reason_code or "child_agent_unavailable_on_execution_platform"
        reason_detail = reason_detail or (
            f"preflight backend mismatch: expected {backend.strip().lower()!r}, "
            f"got {preflight_backend.strip().lower()!r}"
        )

    execution_platform_launchable = _execution_platform_launchable(preflight, required_flag)
    if not execution_platform_launchable and reason_code is None:
        reason_code = "child_agent_unavailable_on_execution_platform"
        reason_detail = f"preflight cannot launch required child agent kind: {required_flag}"

    session_eval = _check_session_policy_launchable(preflight, required_flag)
    session_policy_launchable = bool(session_eval.get("launchable"))
    blocking_scope = str(session_eval.get("blocking_policy_scope") or "default_allow")
    if not session_policy_launchable and reason_code is None:
        reason_code = "child_agent_forbidden_by_session_policy"
        reason_detail = f"session policy forbids required child agent kind: {required_flag}"

    dep_ready, dep_detail = _dependency_ready(repo_root, orchestration_id, step=step)
    if not dep_ready and reason_code is None:
        reason_code = "dependency_not_ready"
        reason_detail = dep_detail

    status = "pass" if reason_code is None else "fail_closed"
    return {
        "status": status,
        "orchestration_id": orchestration_id,
        "node_key": node_key,
        "step": step.strip().lower(),
        "required_child_agent": required_flag,
        "required_child_agent_by_step": required_by_step,
        "execution_platform_launchable": execution_platform_launchable,
        "session_policy_launchable": session_policy_launchable,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "blocking_policy_scope": blocking_scope,
        "next_action": "proceed_phase_body" if status == "pass" else "stop_before_phase_body",
    }


def _path_under_any_write_root(rel_posix: str, write_roots: list[str]) -> bool:
    p = _normalize_rel_posix(rel_posix)
    for root in write_roots:
        if not isinstance(root, str) or not root.strip():
            continue
        base = _with_trailing_slash(_normalize_rel_posix(root))
        if not base:
            continue
        base_no = base.rstrip("/")
        if _repo_path_under_prefix(p, base_no):
            return True
    return False


def gate_apply_patch_writes(
    repo_root: Path,
    *,
    orchestration_id: str,
    actor_role: str,
    changed_paths: Sequence[str],
    agent_run_id: str,
    capability_token: str | None = None,
) -> dict[str, Any]:
    """`apply_patch` 相当の書き込み先が actor の権限と整合するか検査する。

    違反時は `phase_authority_violation` を書き、RuntimeError を送出する。
    """
    role = actor_role.strip().lower()
    if not agent_run_id.strip():
        raise ValueError("agent_run_id must be non-empty for apply-patch gate")

    normalized_paths = [_normalize_rel_posix(p) for p in changed_paths if str(p).strip()]
    if not normalized_paths:
        return {"allowed": True, "checked_paths": []}

    if role == "orchestration":
        allowed_roots = [
            _with_trailing_slash(
                _normalize_rel_posix(f"workspace/orchestrations/{orchestration_id.strip()}")
            ),
            _with_trailing_slash(_normalize_rel_posix(f"workspace/.pycache/{orchestration_id.strip()}")),
        ]
        bad = [p for p in normalized_paths if not _path_under_any_write_root(p, allowed_roots)]
        if bad:
            _reject_noncanonical_phase_write(
                repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=agent_run_id.strip(),
                actor_role=role,
                attempted_paths=bad,
                node_key=None,
                step=None,
                current_phase_state=None,
            )
        return {"allowed": True, "checked_paths": normalized_paths}

    if role in {"step", "substep"}:
        if not capability_token or not str(capability_token).strip():
            raise ValueError("capability_token is required for step/substep apply-patch gate")
        cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
        if not cap_path.exists():
            raise RuntimeError(f"capability file not found: {cap_path}")
        cap = _read_json(cap_path)
        if not isinstance(cap, dict):
            raise RuntimeError(f"capability must be object: {cap_path}")
        if str(cap.get("capability_token", "")).strip() != str(capability_token).strip():
            _write_phase_authority_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id.strip(),
                actor_role=role,
                rejected_paths=normalized_paths,
                reason="capability_token mismatch",
            )
            raise RuntimeError("apply_patch gate: invalid capability_token")
        roots_obj = cap.get("write_roots")
        roots = [str(x) for x in roots_obj] if isinstance(roots_obj, list) else []
        node_key = str(cap.get("node_key", "")).strip()
        step = str(cap.get("step", "")).strip().lower()
        if node_key and step:
            for p in normalized_paths:
                if not _phase_write_requires_child_running(p):
                    continue
                current = _resolve_current_phase_state(repo_root, orchestration_id, node_key, step)
                if current != "child_running":
                    _reject_noncanonical_phase_write(
                        repo_root,
                        orchestration_id=orchestration_id,
                        agent_run_id=agent_run_id.strip(),
                        actor_role=role,
                        attempted_paths=[p],
                        node_key=node_key,
                        step=step,
                        current_phase_state=current,
                    )
        bad = [p for p in normalized_paths if not _path_under_any_write_root(p, roots)]
        if bad:
            _write_phase_authority_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id.strip(),
                actor_role=role,
                rejected_paths=bad,
                reason="path not under capability write_roots",
            )
            raise RuntimeError("apply_patch gate: path outside write_roots for child agent")
        return {"allowed": True, "checked_paths": normalized_paths}

    raise ValueError(f"unsupported actor_role for apply-patch gate: {actor_role!r}")


def validate_mcp_build_tool_invocation(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    capability_token: str,
    tool_name: str,
) -> None:
    """`compile_project` / `run_linter` / `run_program` / `run_quality_checks` 呼び出し前の位相ゲート。"""
    _require_preflight_launchable(repo_root, orchestration_id, enforce_live_probe=False)

    root = _orchestration_root(repo_root, orchestration_id)
    launch_resp = root / "launches" / f"{agent_run_id.strip()}.response.json"
    if not launch_resp.exists():
        raise RuntimeError(
            "MCP phase gate: record-launch did not complete (missing launches/*.response.json) "
            f"for agent_run_id={agent_run_id!r}"
        )

    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        raise RuntimeError("MCP phase gate: phase_state.json missing")
    cur = doc.get("current_state")
    if cur != "preflight_passed":
        raise RuntimeError(f"MCP phase gate: unexpected orchestration current_state: {cur!r}")

    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    if not cap_path.exists():
        raise RuntimeError(f"MCP phase gate: capability file missing: {cap_path}")
    cap = _read_json(cap_path)
    if not isinstance(cap, dict):
        raise RuntimeError(f"MCP phase gate: capability must be object: {cap_path}")
    if str(cap.get("capability_token", "")).strip() != str(capability_token).strip():
        raise RuntimeError("MCP phase gate: capability_token mismatch")

    exp = cap.get("expires_at")
    if isinstance(exp, str):
        exp_dt = _parse_iso_z_expiry(exp)
        if exp_dt is not None and datetime.now(timezone.utc) > exp_dt:
            raise RuntimeError("MCP phase gate: capability token expired")

    perms = cap.get("mcp_permissions")
    allowed = [str(x) for x in perms] if isinstance(perms, list) else []
    if tool_name not in allowed:
        raise RuntimeError(
            f"MCP phase gate: tool {tool_name!r} not permitted by capability "
            f"(allowed={allowed!r})"
        )

    node_raw = cap.get("node_key")
    step_raw = cap.get("step")
    if not isinstance(node_raw, str) or not node_raw.strip():
        raise RuntimeError("MCP phase gate: capability.node_key missing")
    if not isinstance(step_raw, str) or not step_raw.strip():
        raise RuntimeError("MCP phase gate: capability.step missing")
    node_safe = _node_key_to_safe(node_raw.strip())
    step_key = step_raw.strip().lower()
    required_child = _required_child_agent_kind(step_key)
    role = str(cap.get("agent_role", "")).strip().lower()
    if role != required_child:
        raise RuntimeError(
            "MCP phase gate: capability agent_role does not satisfy required child agent kind "
            f"(step={step_key!r}, required={required_child!r}, actual={role!r})"
        )
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        raise RuntimeError("MCP phase gate: phase_state.node_states missing")
    inner = ns.get(node_safe)
    if not isinstance(inner, dict):
        raise RuntimeError(f"MCP phase gate: phase_state missing node {node_safe!r}")
    st = inner.get(step_key)
    if st != "child_running":
        raise RuntimeError(
            "MCP phase gate: node step must be child_running "
            f"(node_key_safe={node_safe!r}, step={step_key!r}, current={st!r})"
        )


def _gate_script_command(
    *,
    repo_root: Path,
    gate_name: str,
    args_json: dict[str, Any],
) -> list[str]:
    gate = gate_name.strip()
    tools_dir = Path(__file__).resolve().parent
    tool_path: Path
    if gate == "validate_pipeline_semantics":
        tool_path = tools_dir / "validate_pipeline_semantics.py"
    elif gate == "check_artifact_syntax":
        tool_path = tools_dir / "check_artifact_syntax.py"
    elif gate == "validate_workspace_root":
        tool_path = tools_dir / "validate_workspace_root.py"
    else:
        raise ValueError(f"unsupported gate name: {gate_name!r}")
    if not tool_path.exists():
        raise RuntimeError(f"gate script not found: {tool_path}")

    cmd: list[str] = [sys.executable, str(tool_path)]
    positionals = args_json.get("paths")
    if positionals is None:
        positionals = args_json.get("positional_args")
    if positionals is not None:
        if not isinstance(positionals, list) or not all(isinstance(x, str) for x in positionals):
            raise ValueError("args_json.paths/positional_args must be array of strings")
    positional_list: list[str] = [str(x) for x in (positionals or []) if str(x).strip()]

    for key in sorted(args_json.keys()):
        if key in {"paths", "positional_args"}:
            continue
        value = args_json[key]
        if value is None:
            continue
        if isinstance(key, str) and key.startswith("--"):
            flag = key
        else:
            flag = "--" + str(key).strip().replace("_", "-")
        if not flag.strip():
            continue
        if isinstance(value, bool):
            if value:
                cmd.append(flag)
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, (str, int, float)) and str(item).strip():
                    cmd.extend([flag, str(item)])
            continue
        if isinstance(value, (str, int, float)) and str(value).strip():
            cmd.extend([flag, str(value)])

    cmd.extend(positional_list)
    return cmd


def _extract_gate_violations(stdout: str, stderr: str, returncode: int) -> list[str]:
    lines: list[str] = []
    for source in (stdout, stderr):
        for raw in source.splitlines():
            token = raw.strip()
            if not token:
                continue
            if token.startswith("- ") or token.startswith("FAIL:"):
                lines.append(token)
                continue
            if token.endswith(": FAIL") or " validation: FAIL" in token:
                lines.append(token)
                continue
    deduped: list[str] = []
    seen: set[str] = set()
    for item in lines:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    if returncode != 0 and not deduped:
        deduped.append(f"gate command failed with exit code {returncode}")
    return deduped


def _inline_gate_result(
    repo_root: Path,
    *,
    orchestration_id: str,
    gate_name: str,
    agent_run_id: str,
    args_json: dict[str, Any],
    capability_token: str,
) -> dict[str, Any]:
    gate = gate_name.strip()
    if gate == "orchestration_read":
        read_path = args_json.get("read_path")
        if not isinstance(read_path, str) or not read_path.strip():
            raise ValueError("run-gate orchestration_read requires non-empty args_json.read_path")
        return log_orchestration_read(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id,
            read_path=read_path,
        )
    if gate == "apply_patch_writes":
        actor_role = args_json.get("actor_role")
        if not isinstance(actor_role, str) or not actor_role.strip():
            raise ValueError("run-gate apply_patch_writes requires non-empty args_json.actor_role")
        changed_paths = args_json.get("changed_paths")
        if not isinstance(changed_paths, list) or not all(isinstance(x, str) for x in changed_paths):
            raise ValueError("run-gate apply_patch_writes requires args_json.changed_paths as string array")
        tok_raw = args_json.get("capability_token")
        token_for_gate = str(tok_raw).strip() if isinstance(tok_raw, str) and tok_raw.strip() else capability_token
        return gate_apply_patch_writes(
            repo_root,
            orchestration_id=orchestration_id,
            actor_role=actor_role,
            changed_paths=changed_paths,
            agent_run_id=agent_run_id,
            capability_token=token_for_gate,
        )
    raise ValueError(f"unsupported inline gate name: {gate_name!r}")


def _validate_run_gate_permissions(
    repo_root: Path,
    *,
    orchestration_id: str,
    gate_name: str,
    agent_run_id: str,
    capability_token: str,
) -> None:
    _require_preflight_launchable(repo_root, orchestration_id, enforce_live_probe=False)
    root = _orchestration_root(repo_root, orchestration_id)

    launch_resp = root / "launches" / f"{agent_run_id.strip()}.response.json"
    if not launch_resp.exists():
        raise RuntimeError(
            "run-gate phase gate: record-launch did not complete "
            f"(missing launches/{agent_run_id.strip()}.response.json)"
        )

    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    if not cap_path.exists():
        raise RuntimeError(f"run-gate phase gate: capability file missing: {cap_path}")
    cap = _read_json(cap_path)
    if not isinstance(cap, dict):
        raise RuntimeError(f"run-gate phase gate: capability must be object: {cap_path}")
    if str(cap.get("capability_token", "")).strip() != capability_token.strip():
        raise RuntimeError("run-gate phase gate: capability_token mismatch")
    exp = cap.get("expires_at")
    if isinstance(exp, str):
        exp_dt = _parse_iso_z_expiry(exp)
        if exp_dt is not None and datetime.now(timezone.utc) > exp_dt:
            raise RuntimeError("run-gate phase gate: capability token expired")

    policy_path = _access_policies_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    if not policy_path.exists():
        raise RuntimeError(f"run-gate phase gate: access policy missing: {policy_path}")
    policy = _read_json(policy_path)
    if not isinstance(policy, dict):
        raise RuntimeError(f"run-gate phase gate: access policy must be object: {policy_path}")
    allowed_svcs = policy.get("allowed_gate_services")
    allowed = [str(x) for x in allowed_svcs] if isinstance(allowed_svcs, list) else []
    if gate_name not in allowed:
        raise RuntimeError(
            f"run-gate phase gate: gate {gate_name!r} not permitted by access policy (allowed={allowed!r})"
        )

    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        raise RuntimeError("run-gate phase gate: phase_state.json missing")
    if doc.get("current_state") != "preflight_passed":
        raise RuntimeError(
            f"run-gate phase gate: unexpected orchestration current_state: {doc.get('current_state')!r}"
        )
    node_raw = cap.get("node_key")
    step_raw = cap.get("step")
    if not isinstance(node_raw, str) or not node_raw.strip():
        raise RuntimeError("run-gate phase gate: capability.node_key missing")
    if not isinstance(step_raw, str) or not step_raw.strip():
        raise RuntimeError("run-gate phase gate: capability.step missing")
    node_safe = _node_key_to_safe(node_raw.strip())
    step_key = step_raw.strip().lower()
    required_child = _required_child_agent_kind(step_key)
    role = str(cap.get("agent_role", "")).strip().lower()
    if role != required_child:
        raise RuntimeError(
            "run-gate phase gate: capability agent_role does not satisfy required child agent kind "
            f"(step={step_key!r}, required={required_child!r}, actual={role!r})"
        )
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        raise RuntimeError("run-gate phase gate: phase_state.node_states missing")
    node_state = ns.get(node_safe)
    if not isinstance(node_state, dict):
        raise RuntimeError(f"run-gate phase gate: phase_state missing node {node_safe!r}")
    if node_state.get(step_key) != "child_running":
        raise RuntimeError(
            "run-gate phase gate: node step must be child_running "
            f"(node_key_safe={node_safe!r}, step={step_key!r}, current={node_state.get(step_key)!r})"
        )


def run_gate(
    repo_root: Path,
    *,
    orchestration_id: str,
    gate_name: str,
    agent_run_id: str,
    args_json: dict[str, Any],
    capability_token: str,
) -> dict[str, Any]:
    gate = gate_name.strip()
    if gate not in DEFAULT_ALLOWED_GATE_SERVICES:
        raise ValueError(f"unsupported gate name: {gate_name!r}")
    if not capability_token.strip():
        raise ValueError("capability_token is required for run-gate")
    if not isinstance(args_json, dict):
        raise ValueError("args_json must be object")

    _validate_run_gate_permissions(
        repo_root,
        orchestration_id=orchestration_id,
        gate_name=gate,
        agent_run_id=agent_run_id,
        capability_token=capability_token,
    )
    inline_result: dict[str, Any] | None = None
    if gate in {"orchestration_read", "apply_patch_writes"}:
        inline_result = _inline_gate_result(
            repo_root,
            orchestration_id=orchestration_id,
            gate_name=gate,
            agent_run_id=agent_run_id,
            args_json=args_json,
            capability_token=capability_token,
        )
        violations: list[str] = []
        status = "pass"
        exit_code = 0
    else:
        cmd = _gate_script_command(repo_root=repo_root, gate_name=gate, args_json=args_json)
        gate_env = os.environ.copy()
        gate_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        gate_env["PYTHONPYCACHEPREFIX"] = str((repo_root / "workspace" / ".pycache").resolve())
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=gate_env,
            text=True,
            capture_output=True,
            check=False,
        )
        violations = _extract_gate_violations(proc.stdout or "", proc.stderr or "", proc.returncode)
        status = "pass" if proc.returncode == 0 else "fail"
        exit_code = proc.returncode
    gate_doc: dict[str, Any] = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "gate": gate,
        "args_json": args_json,
        "status": status,
        "exit_code": exit_code,
        "violations": violations,
        "evaluated_at": _utc_now_iso(),
    }
    if inline_result is not None:
        gate_doc["result"] = inline_result
    out_path = _gates_dir(repo_root, orchestration_id) / agent_run_id.strip() / f"{gate}.json"
    _write_json(out_path, gate_doc)
    gate_ref = (
        f"workspace/orchestrations/{orchestration_id}/gates/"
        f"{agent_run_id.strip()}/{gate}.json"
    )
    result: dict[str, Any] = {"violations": violations, "gate_result_ref": gate_ref}
    if inline_result is not None:
        result["result"] = inline_result
    return result


def _normalize_rel_posix(path_token: str) -> str:
    """リポジトリ相対 path を POSIX 形式の先頭無し・末尾スラッシュなしに正規化する。"""
    t = path_token.strip().replace("\\", "/").lstrip("/")
    while "//" in t:
        t = t.replace("//", "/")
    return t.rstrip("/")


def _with_trailing_slash(rel_posix: str) -> str:
    if not rel_posix:
        return ""
    return rel_posix if rel_posix.endswith("/") else rel_posix + "/"


def _repo_path_under_prefix(rel_posix: str, prefix_rel: str) -> bool:
    p = _normalize_rel_posix(rel_posix)
    base = _normalize_rel_posix(prefix_rel)
    if not base:
        return False
    return p == base or p.startswith(base + "/")


def build_access_policy_payload(
    *,
    agent_run_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """`access_policies/<agent_run_id>.json` の内容を組み立てる。"""
    node_key = request_payload.get("node_key")
    step = request_payload.get("step")
    if not isinstance(node_key, str) or not node_key.strip():
        raise ValueError("access policy requires node_key")
    if not isinstance(step, str) or not step.strip():
        raise ValueError("access policy requires step")
    plan_ref = request_payload.get("plan_ref")
    pipeline_ref = request_payload.get("pipeline_ref")
    if not isinstance(plan_ref, str) or not plan_ref.strip():
        raise ValueError("access policy requires plan_ref")
    if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
        raise ValueError("access policy requires pipeline_ref")

    allowed_read_roots = [
        "docs/",
        "spec/",
        _with_trailing_slash(_normalize_rel_posix(plan_ref)),
        _with_trailing_slash(_normalize_rel_posix(pipeline_ref)),
    ]
    body: dict[str, Any] = {
        "agent_run_id": agent_run_id.strip(),
        "node_key": node_key.strip(),
        "step": step.strip().lower(),
        "allowed_read_roots": allowed_read_roots,
        "denied_read_roots": ["tools/"],
        "allowed_gate_services": list(DEFAULT_ALLOWED_GATE_SERVICES),
    }
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        body["substep"] = substep.strip().lower()
    return body


def _write_access_policy_for_launch(
    repo_root: Path,
    orchestration_id: str,
    child_agent_run_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    policy = build_access_policy_payload(agent_run_id=child_agent_run_id, request_payload=request_payload)
    out = _access_policies_dir(repo_root, orchestration_id) / f"{child_agent_run_id}.json"
    _write_json(out, policy)
    return policy


def _append_access_log_line(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    entry: dict[str, Any],
) -> None:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    path = _access_logs_dir(repo_root, orchestration_id) / f"{agent_run_id}.jsonl"
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_orchestration_read(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    read_path: str,
) -> dict[str, Any]:
    """read 監査: `denied_read_roots`（`tools/`）一致時は `rule_source_violation` を記録し orchestration を fail にする。

    許可された read のみ本文を返す。
    """
    policies_dir = _access_policies_dir(repo_root, orchestration_id)
    policy_path = policies_dir / f"{agent_run_id}.json"
    if not policy_path.exists():
        raise FileNotFoundError(f"access policy not found: {policy_path}")
    policy = _read_json(policy_path)
    if not isinstance(policy, dict):
        raise ValueError(f"access policy must be object: {policy_path}")

    denied = policy.get("denied_read_roots")
    if not isinstance(denied, list):
        denied = []
    allowed = policy.get("allowed_read_roots")
    if not isinstance(allowed, list):
        allowed = []

    rel = _normalize_rel_posix(read_path)
    hit_denied = False
    matched_prefix: str | None = None
    for item in denied:
        if not isinstance(item, str) or not item.strip():
            continue
        prefix = _with_trailing_slash(_normalize_rel_posix(item))
        if not prefix:
            continue
        base_no_slash = prefix.rstrip("/")
        if _repo_path_under_prefix(rel, base_no_slash):
            hit_denied = True
            matched_prefix = prefix
            break

    matched_allowed_prefix: str | None = None
    hit_allowed = False
    for item in allowed:
        if not isinstance(item, str) or not item.strip():
            continue
        prefix = _with_trailing_slash(_normalize_rel_posix(item))
        if not prefix:
            continue
        base_no_slash = prefix.rstrip("/")
        if _repo_path_under_prefix(rel, base_no_slash):
            hit_allowed = True
            matched_allowed_prefix = prefix
            break

    log_entry = {
        "ts": _utc_now_iso(),
        "path": rel,
        "allowed_match": hit_allowed,
        "matched_allowed_prefix": matched_allowed_prefix,
        "denied_match": hit_denied,
        "matched_denied_prefix": matched_prefix,
    }
    _append_access_log_line(repo_root, orchestration_id, agent_run_id, log_entry)

    abs_path = (repo_root / rel).resolve()
    try:
        abs_path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes repo_root: {read_path!r}") from exc

    if hit_denied or not hit_allowed:
        _write_rule_source_violation(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id.strip(),
            read_path=rel,
            matched_prefix=matched_prefix if hit_denied else None,
        )
        try:
            update_orchestration_status(repo_root, orchestration_id, status="fail")
        except Exception:
            pass
        if hit_denied:
            raise RuntimeError(
                f"orchestration-read denied: path {rel!r} matches rule-source deny list "
                f"(prefix={matched_prefix!r}, agent_run_id={agent_run_id})"
            )
        raise RuntimeError(
            f"orchestration-read denied: path {rel!r} is outside allowed_read_roots "
            f"(agent_run_id={agent_run_id})"
        )

    content: str | None = None
    file_exists = abs_path.is_file()
    if file_exists:
        content = abs_path.read_text(encoding="utf-8")

    return {
        "read_path": rel,
        "file_exists": file_exists,
        "denied_match": False,
        "logged": True,
        "content": content,
    }


def _checkpoint_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "orchestration_checkpoint.json"


def _compute_sha256(path: Path) -> str:
    """ファイルの SHA-256 ハッシュを "sha256:<hex>" 形式で返す。

    ファイルが存在しない場合は "sha256:missing" を返す（エラーにしない）。
    """
    if not path.exists():
        return "sha256:missing"
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _build_artifact_hashes(
    repo_root: Path,
    output_refs: list[str],
) -> dict[str, str]:
    """output_refs の各パスを repo_root 起点で解決し SHA-256 を計算する。"""
    hashes: dict[str, str] = {}
    for ref in output_refs:
        if not isinstance(ref, str) or not ref.strip():
            continue
        r = ref.strip()
        hashes[r] = _compute_sha256(repo_root / r)
    return hashes


def _load_checkpoint(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any] | None:
    """orchestration_checkpoint.json を読み込む。存在しない場合は None を返す。

    JSON 構造が不正な場合は RuntimeError を送出する。
    """
    path = _checkpoint_path(repo_root, orchestration_id)
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"orchestration_checkpoint.json is invalid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"orchestration_checkpoint.json must be object: {path}")
    if data.get("orchestration_id") != orchestration_id:
        raise RuntimeError(
            "orchestration_checkpoint.json orchestration_id mismatch: "
            f"expected {orchestration_id!r}, got {data.get('orchestration_id')!r}"
        )
    return data


def _preflight_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "preflight.json"


def _preflight_allows_agent_launch(payload: dict[str, Any]) -> bool:
    feature_states = payload.get("feature_states")
    if not isinstance(feature_states, dict):
        return False
    if feature_states.get("multi_agent") is not True:
        return False

    checks = payload.get("checks")
    if not isinstance(checks, list):
        return False
    multi_agent_check_pass: bool | None = None
    for item in checks:
        if not isinstance(item, dict):
            continue
        if item.get("name") != "multi_agent_enabled":
            continue
        pass_value = item.get("pass")
        if isinstance(pass_value, bool):
            multi_agent_check_pass = pass_value
            break

    return (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
        and multi_agent_check_pass is True
    )


def _validate_preflight_payload(payload: dict[str, Any]) -> None:
    if (
        payload.get("can_launch_step_agents") is True
        or payload.get("can_launch_substep_agents") is True
    ) and payload.get("status") != "pass":
        raise ValueError(
            "preflight status must be pass when can_launch_step_agents/can_launch_substep_agents is true"
        )

    feature_states = payload.get("feature_states")
    if isinstance(feature_states, dict):
        multi_agent = feature_states.get("multi_agent")
        if isinstance(multi_agent, bool) and not multi_agent:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "feature_states.multi_agent=false is incompatible with launchable preflight"
                )

    checks = payload.get("checks")
    if isinstance(checks, list):
        multi_agent_check_pass: bool | None = None
        for item in checks:
            if not isinstance(item, dict):
                continue
            if item.get("name") != "multi_agent_enabled":
                continue
            pass_value = item.get("pass")
            if isinstance(pass_value, bool):
                multi_agent_check_pass = pass_value
                break
        if multi_agent_check_pass is False:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "checks.multi_agent_enabled.pass=false is incompatible with launchable preflight"
                )
    elif (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
    ):
        raise ValueError(
            "checks must include multi_agent_enabled.pass=true when preflight is launchable"
        )

    if (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
    ):
        if not isinstance(feature_states, dict) or feature_states.get("multi_agent") is not True:
            raise ValueError(
                "feature_states.multi_agent=true is required when preflight is launchable"
            )


def _live_preflight_mode() -> str:
    """CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT の値から動作モードを返す。

    戻り値: 'never' | 'always' | 'ttl'
    - 'never' : プローブをスキップ
    - 'always': 毎回プローブ（TTL 無視、後方互換）
    - 'ttl'   : TTL キャッシュ付きプローブ（デフォルト）
    """
    raw = os.environ.get("CODEX_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", "").strip().lower()
    if raw in {"0", "false", "no"}:
        return "never"
    if raw == "1":
        return "always"
    return "ttl"


def _live_preflight_ttl_seconds() -> int:
    """CODEX_PREFLIGHT_TTL_SECONDS を読み非負整数を返す。

    未設定または無効値の場合は PREFLIGHT_TTL_DEFAULT_SECONDS を返す。
    """
    raw = os.environ.get("CODEX_PREFLIGHT_TTL_SECONDS", "").strip()
    if not raw:
        return PREFLIGHT_TTL_DEFAULT_SECONDS
    try:
        value = int(raw)
        return max(0, value)
    except ValueError:
        return PREFLIGHT_TTL_DEFAULT_SECONDS


def _is_within_preflight_ttl(probed_at_iso: str, ttl_seconds: int) -> bool:
    """probed_at_iso からの経過秒が ttl_seconds 未満なら True。

    ttl_seconds == 0 の場合は常に False（キャッシュなし）。
    パース失敗時は False（安全側に倒してプローブを実行する）。
    """
    if ttl_seconds <= 0:
        return False
    try:
        probed_at = datetime.fromisoformat(probed_at_iso.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - probed_at).total_seconds()
        return elapsed < ttl_seconds
    except (ValueError, TypeError):
        return False


def _live_preflight_enforced() -> bool:
    """後方互換ラッパー。

    新規コードは _live_preflight_mode() を使用すること。
    """
    return _live_preflight_mode() != "never"


def _update_preflight_probed_at(
    repo_root: Path,
    orchestration_id: str,
    probed_at_iso: str,
) -> None:
    """preflight.json の probed_at フィールドのみを更新する。

    他のフィールド（status / can_launch_* 等）は変更しない。
    preflight.json が存在しない場合は何もしない（エラーにしない）。
    """
    path = _preflight_path(repo_root, orchestration_id)
    if not path.exists():
        return
    try:
        file_payload = _read_json(path)
    except json.JSONDecodeError:
        return
    if not isinstance(file_payload, dict):
        return
    file_payload["probed_at"] = probed_at_iso
    _write_json(path, file_payload)


def _run_live_probe_and_update(
    repo_root: Path,
    orchestration_id: str,
    cached_payload: dict[str, Any],
) -> None:
    """live probe を実行し、成功時に preflight.json の probed_at を更新する。

    失敗時は RuntimeError を送出する（呼び出し元で orchestration を fail に遷移させる）。
    """
    backend = cached_payload.get("backend")
    if not isinstance(backend, str) or backend.strip() not in SUPPORTED_BACKENDS:
        backend = "codex"
    command = cached_payload.get("probe_command")
    probe_command = command.strip() if isinstance(command, str) and command.strip() else None

    live_probe = probe_execution_platform(backend=backend, agent_command=probe_command)
    if not _preflight_allows_agent_launch(live_probe):
        raise RuntimeError(
            "live preflight gate failed: execution platform multi_agent must be enabled at launch time"
        )
    probed_at = live_probe.get("checked_at") or _utc_now_iso()
    _update_preflight_probed_at(repo_root, orchestration_id, probed_at)


def _require_preflight_launchable(
    repo_root: Path,
    orchestration_id: str,
    *,
    enforce_live_probe: bool = True,
) -> dict[str, Any]:
    path = _preflight_path(repo_root, orchestration_id)
    if not path.exists():
        raise RuntimeError(f"preflight missing: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"preflight must be object: {path}")
    if not _preflight_allows_agent_launch(payload):
        raise RuntimeError(
            "preflight gate failed: launchable preflight with multi_agent=true is required"
        )

    if not enforce_live_probe:
        return payload

    mode = _live_preflight_mode()

    if mode == "never":
        return payload

    if mode == "always":
        _run_live_probe_and_update(repo_root, orchestration_id, payload)
        return payload

    ttl_seconds = _live_preflight_ttl_seconds()
    probed_at = payload.get("probed_at")

    if isinstance(probed_at, str) and _is_within_preflight_ttl(probed_at, ttl_seconds):
        return payload

    _run_live_probe_and_update(repo_root, orchestration_id, payload)
    return payload


def get_preflight_ttl_status(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """preflight-status コマンド向け: TTL 状態の詳細を返す。"""
    mode = _live_preflight_mode()
    ttl_seconds = _live_preflight_ttl_seconds()
    path = _preflight_path(repo_root, orchestration_id)

    if not path.exists():
        return {
            "orchestration_id": orchestration_id,
            "preflight_exists": False,
            "live_probe_mode": mode,
            "ttl_seconds": ttl_seconds,
            "within_ttl": None,
            "ttl_remaining_seconds": None,
            "probe_skippable": False,
        }

    try:
        file_payload = _read_json(path)
    except json.JSONDecodeError:
        file_payload = {}

    probed_at = file_payload.get("probed_at") if isinstance(file_payload, dict) else None
    checked_at = file_payload.get("checked_at") if isinstance(file_payload, dict) else None
    preflight_status = file_payload.get("status") if isinstance(file_payload, dict) else None
    backend = file_payload.get("backend") if isinstance(file_payload, dict) else None

    within_ttl: bool | None = None
    ttl_remaining: float | None = None
    if mode == "ttl" and isinstance(probed_at, str):
        within_ttl = _is_within_preflight_ttl(probed_at, ttl_seconds)
        if within_ttl and ttl_seconds > 0:
            try:
                pa = datetime.fromisoformat(probed_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - pa).total_seconds()
                ttl_remaining = max(0.0, ttl_seconds - elapsed)
            except (ValueError, TypeError):
                ttl_remaining = None

    probe_skippable = mode == "never" or (mode == "ttl" and within_ttl is True)

    return {
        "orchestration_id": orchestration_id,
        "preflight_exists": True,
        "preflight_status": preflight_status,
        "backend": backend,
        "checked_at": checked_at,
        "probed_at": probed_at,
        "live_probe_mode": mode,
        "ttl_seconds": ttl_seconds,
        "within_ttl": within_ttl,
        "ttl_remaining_seconds": ttl_remaining,
        "probe_skippable": probe_skippable,
    }


def _launch_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/launches/{agent_run_id}"
    return f"{prefix}.request.json", f"{prefix}.response.json"


def _launch_dialog_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/launches/{agent_run_id}"
    return f"{prefix}.prompt.txt", f"{prefix}.reply.txt"


def _child_launch_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/child"
    return f"{prefix}.request.json", f"{prefix}.response.json"


def _child_dialog_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/child"
    return f"{prefix}.prompt.txt", f"{prefix}.reply.txt"


def _agent_result_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/agent"
    return f"{prefix}.result.json", f"{prefix}.summary.txt"


def _coerce_launch_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if isinstance(value, (bool, int, float)):
        return str(value)
    return None


def _coerce_nested_launch_text(payload: dict[str, Any], path: tuple[str, ...]) -> str | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _coerce_launch_text(current)


def _is_placeholder_ref(value: str) -> bool:
    token = value.strip()
    if not token:
        return False
    return "agent-determined" in token or ("<" in token and ">" in token)


def _launch_prompt_template_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "skills"
        / "workflow-orchestration"
        / "references"
        / "launch_prompts.md"
    )


@lru_cache(maxsize=1)
def _load_launch_prompt_templates() -> dict[str, str]:
    text = _launch_prompt_template_path().read_text(encoding="utf-8")
    pattern = re.compile(
        r"## `(?P<name>step agent|substep agent)` 起動要求テンプレート\s+```text\n(?P<body>.*?)\n```",
        re.DOTALL,
    )
    templates: dict[str, str] = {}
    for match in pattern.finditer(text):
        templates[match.group("name")] = match.group("body")
    if set(templates) != {"step agent", "substep agent"}:
        raise RuntimeError("launch prompt templates must define step agent and substep agent")
    return templates


def _launch_prompt_template_name(request_payload: dict[str, Any]) -> str:
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return "substep agent"
    return "step agent"


def _template_placeholder_values(request_payload: dict[str, Any]) -> dict[str, str]:
    return {
        "node_key": str(request_payload.get("node_key", "")),
        "step": str(request_payload.get("step", "")),
        "substep": str(request_payload.get("substep", "")),
        "orchestration_id": str(request_payload.get("orchestration_id", "")),
        "agent_run_id": str(request_payload.get("agent_run_id", "")),
        "parent_agent_run_id": str(request_payload.get("parent_agent_run_id", "")),
        "plan_ref": str(request_payload.get("plan_ref", "")),
        "pipeline_ref": str(request_payload.get("pipeline_ref", "")),
        "dependency_ref": str(request_payload.get("dependency_ref", "")),
        "skill_name": str(request_payload.get("skill_name", "")),
        "skill_ref": str(request_payload.get("skill_ref", "")),
        "skill_must_read_refs": str(request_payload.get("skill_must_read_refs", "")),
        "issue_severity": str(request_payload.get("issue_severity", "")),
        "repair_strategy": str(request_payload.get("repair_strategy", "")),
        "repair_target_agent_run_id": str(request_payload.get("repair_target_agent_run_id", "")),
        "repair_reason": str(request_payload.get("repair_reason", "")),
    }


def _render_launch_prompt_template(request_payload: dict[str, Any]) -> str:
    template = _load_launch_prompt_templates()[_launch_prompt_template_name(request_payload)]
    rendered = template
    for key, value in _template_placeholder_values(request_payload).items():
        rendered = rendered.replace(f"<{key}>", value)
    return rendered


def build_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    return _render_launch_prompt_template(request_payload).split("\n\n", 1)[0]


def _skill_name_for_request(request_payload: dict[str, Any]) -> str | None:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return None
    step_token = step.strip().lower()
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return f"workflow-{step_token}-{substep.strip().lower()}"
    return f"workflow-{step_token}"


def _required_verify_skill_refs(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    substep = request_payload.get("substep")
    plan_ref = request_payload.get("plan_ref")
    if (
        not isinstance(step, str)
        or step.strip().lower() not in {"plan", "generate"}
        or not isinstance(substep, str)
        or substep.strip().lower() != "verify"
        or not isinstance(plan_ref, str)
        or not plan_ref.strip()
    ):
        return []
    plan_root = plan_ref.strip().rstrip("/")
    refs = [
        f"{plan_root}/case.resolved.yaml",
        f"{plan_root}/algorithm.resolved.yaml",
        f"{plan_root}/impl.resolved.yaml",
        f"{plan_root}/dependency.resolved.yaml",
        f"{plan_root}/derived_contract.json",
    ]
    if step.strip().lower() == "generate":
        pipeline_ref = request_payload.get("pipeline_ref")
        generation_id = request_payload.get("generation_id")
        if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
            raise ValueError("generate verify launch request must include non-empty pipeline_ref")
        if not isinstance(generation_id, str) or not generation_id.strip():
            raise ValueError("generate verify launch request must include non-empty generation_id")
        pr = pipeline_ref.strip().rstrip("/")
        gid = generation_id.strip()
        refs.extend(
            [
                f"{pr}/lineage.json",
                f"{pr}/generate/{gid}/generate_meta.json",
            ]
        )
    return refs


def _merge_unique_refs(*ref_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in ref_groups:
        for ref in group:
            token = ref.strip()
            if not token or token in seen:
                continue
            merged.append(token)
            seen.add(token)
    return merged


def _workflow_contract_refs_for_launch(request_payload: dict[str, Any]) -> list[str]:
    refs = [WORKFLOW_CORE_REF, "docs/ORCHESTRATION.md"]
    step = request_payload.get("step")
    if isinstance(step, str) and step.strip():
        phase_doc = WORKFLOW_PHASE_DOC_BY_STEP.get(step.strip().lower())
        if phase_doc:
            refs.append(phase_doc)
    return refs


def build_skill_must_read_refs(request_payload: dict[str, Any]) -> list[str]:
    skill_ref = request_payload.get("skill_ref")
    skill_refs = [skill_ref.strip()] if isinstance(skill_ref, str) and skill_ref.strip() else []
    existing_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))
    common_refs = _workflow_contract_refs_for_launch(request_payload)
    verify_refs = _required_verify_skill_refs(request_payload)
    return _merge_unique_refs(skill_refs, common_refs, existing_refs, verify_refs)


def render_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    return _render_launch_prompt_template(request_payload)


def prepare_launch_request_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(request_payload)
    if not isinstance(payload.get("skill_name"), str) or not payload.get("skill_name", "").strip():
        skill_name = _skill_name_for_request(payload)
        if skill_name is not None:
            payload["skill_name"] = skill_name
    if not isinstance(payload.get("skill_ref"), str) or not payload.get("skill_ref", "").strip():
        skill_name = payload.get("skill_name")
        if isinstance(skill_name, str) and skill_name.strip():
            payload["skill_ref"] = f"skills/{skill_name.strip()}/SKILL.md"
    payload.setdefault("issue_severity", "none")
    payload.setdefault("repair_strategy", "none")
    payload.setdefault("repair_target_agent_run_id", "none")
    payload.setdefault("repair_reason", "none")
    payload["skill_must_read_refs"] = ",".join(build_skill_must_read_refs(payload))
    explicit_prompt_present = any(
        _coerce_nested_launch_text(payload, path) is not None
        for path in (
            ("launch_prompt_full",),
            ("execution_prompt",),
            ("prompt",),
            ("task",),
            ("instruction",),
            ("instructions",),
            ("message",),
            ("spawn_request", "prompt"),
            ("spawn_request", "task"),
            ("spawn_request", "instruction"),
            ("spawn_request", "instructions"),
            ("spawn_request", "message"),
            ("launch_prompt",),
        )
    )
    if not explicit_prompt_present:
        payload["launch_prompt_full"] = render_launch_prompt_text(payload)
    return payload


def _extract_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    # Prefer explicit full execution prompts, then fall back to short launch summaries.
    for path in (
        ("launch_prompt_full",),
        ("execution_prompt",),
        ("prompt",),
        ("task",),
        ("instruction",),
        ("instructions",),
        ("message",),
        ("spawn_request", "prompt"),
        ("spawn_request", "task"),
        ("spawn_request", "instruction"),
        ("spawn_request", "instructions"),
        ("spawn_request", "message"),
        ("launch_prompt",),
    ):
        text = _coerce_nested_launch_text(request_payload, path)
        if text is not None:
            return text
    return json.dumps(request_payload, ensure_ascii=False, indent=2)


def _extract_launch_reply_text(response_payload: dict[str, Any]) -> str:
    for key in ("launch_reply", "reply", "response_text", "message", "result"):
        text = _coerce_launch_text(response_payload.get(key))
        if text is not None:
            return text
    return json.dumps(response_payload, ensure_ascii=False, indent=2)


def _extract_response_agent_session_id(response_payload: dict[str, Any]) -> str | None:
    candidate_paths: tuple[tuple[str, ...], ...] = (
        ("agent_session_id",),
        ("agent_id",),
        ("session_id",),
        ("child_agent_id",),
        ("child_agent_session_id",),
        ("id",),
        ("agent", "id"),
        ("agent", "session_id"),
        ("child_agent", "id"),
        ("child_agent", "session_id"),
        ("data", "id"),
        ("data", "session_id"),
    )
    for path in candidate_paths:
        current: Any = response_payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return None


def _validate_response_agent_session_id(response_payload: dict[str, Any]) -> str:
    session_id = _extract_response_agent_session_id(response_payload)
    if session_id is None:
        raise ValueError("launch response must include child agent identifier from spawn_agent")
    if _is_placeholder_ref(session_id):
        raise ValueError("launch response child agent identifier must not contain placeholder tokens")
    return session_id


def _required_launch_prompt_markers(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
    markers = [
        "orchestration_id:",
        "agent_run_id:",
        "parent_agent_run_id:",
        "plan_ref:",
        "pipeline_ref:",
        "dependency_ref:",
        "skill_name:",
        "skill_ref:",
        "skill_must_read_refs:",
        "必須要件:",
    ]
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return [
            "あなたは substep agent である。",
            "対象 node_key:",
            "対象 step:",
            "対象 substep:",
            *markers,
        ]
    return [
        "あなたは step agent である。",
        "対象 node_key:",
        "対象 step:",
        *markers,
    ]


def _required_launch_prompt_lines(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
    return build_launch_prompt_text(request_payload).splitlines()


def _validate_launch_prompt_text(request_payload: dict[str, Any], prompt_text: str) -> None:
    required_markers = _required_launch_prompt_markers(request_payload)
    if not required_markers:
        return
    missing_markers = [marker for marker in required_markers if marker not in prompt_text]
    if missing_markers:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration template markers: "
            + ", ".join(missing_markers)
        )
    required_lines = _required_launch_prompt_lines(request_payload)
    missing_lines = [line for line in required_lines if line not in prompt_text]
    if missing_lines:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration template field values: "
            + ", ".join(missing_lines)
        )


def _extract_agent_summary_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in (
        "agent_run_id",
        "agent_role",
        "node_key",
        "step",
        "substep",
        "status",
        "agent_backend",
        "agent_model",
        "context_id",
        "agent_session_id",
        "started_at",
        "finished_at",
        "result_summary",
    ):
        value = payload.get(key)
        if value is None:
            continue
        lines.append(f"{key}: {value}")

    output_refs = payload.get("output_refs")
    if isinstance(output_refs, list) and output_refs:
        lines.append("output_refs:")
        for item in output_refs:
            if isinstance(item, str) and item.strip():
                lines.append(f"- {item.strip()}")

    if lines:
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_agent_summary_text(payload: dict[str, Any], summary_text: str) -> None:
    text = summary_text.strip()
    if not text:
        raise ValueError("agent.summary.txt must be non-empty")
    agent_role = payload.get("agent_role")
    if isinstance(agent_role, str) and agent_role.strip().lower() == "skipped_by_checkpoint":
        return

    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(non_empty_lines) < 2:
        raise ValueError("agent.summary.txt must not be single-line summary")

    status = payload.get("status")
    if isinstance(status, str) and status.strip():
        marker = f"status: {status.strip()}"
        if marker not in text:
            raise ValueError("agent.summary.txt must include final status line")

    output_refs = payload.get("output_refs")
    if isinstance(output_refs, list) and any(isinstance(item, str) and item.strip() for item in output_refs):
        if "output_refs:" not in text:
            raise ValueError("agent.summary.txt must include output_refs section for pass result")
    elif (
        isinstance(status, str)
        and status.strip().lower() in TERMINAL_STATUSES
        and not any(token in text for token in ("result_summary:", "summary:", "reason:", "failure_reason:"))
    ):
        raise ValueError("agent.summary.txt must include summary or failure reason")


def _split_skill_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        refs: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                refs.append(item.strip())
        return refs
    return []


def _node_key_to_safe(node_key: str) -> str:
    token = node_key.strip()
    if "/" not in token or "@" not in token:
        raise ValueError(f"invalid node_key: {node_key}")
    spec_kind, tail = token.split("/", 1)
    spec_id, spec_version = tail.rsplit("@", 1)
    spec_kind = spec_kind.strip()
    spec_id = spec_id.strip()
    spec_version = spec_version.strip()
    if not spec_kind or not spec_id or not spec_version:
        raise ValueError(f"invalid node_key: {node_key}")
    return f"{spec_kind}__{spec_id}__{spec_version}"


def update_checkpoint(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """write_step_result 完了後にチェックポイントへ完了エントリを追記/更新する。

    status=pass の場合のみ記録する。それ以外は即時 return する。

    既に同一 (node_key, step) のエントリが存在する場合は上書きする。
    """
    status = result.get("status")
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return {}

    node_safe = _node_key_to_safe(node_key)
    output_refs: list[str] = []
    required = result.get("required_outputs")
    if isinstance(required, list) and required:
        output_refs = [r.strip() for r in required if isinstance(r, str) and r.strip()]
    if not output_refs:
        raw = result.get("output_refs")
        if isinstance(raw, list):
            output_refs = [r.strip() for r in raw if isinstance(r, str) and r.strip()]

    plan_ref = str(result.get("plan_ref") or "")
    pipeline_ref = str(result.get("pipeline_ref") or "")

    if not plan_ref or not pipeline_ref:
        lr_ref = result.get("launch_request_ref")
        if isinstance(lr_ref, str) and lr_ref.strip():
            lr_path = repo_root / lr_ref.strip()
            if lr_path.exists():
                try:
                    lr_data = _read_json(lr_path)
                    if isinstance(lr_data, dict):
                        plan_ref = plan_ref or str(lr_data.get("plan_ref") or "")
                        pipeline_ref = pipeline_ref or str(
                            lr_data.get("pipeline_ref") or ""
                        )
                except json.JSONDecodeError:
                    pass

    artifact_hashes = _build_artifact_hashes(repo_root, output_refs)

    entry: dict[str, Any] = {
        "node_key": node_key.strip(),
        "node_key_safe": node_safe,
        "step": step.strip().lower(),
        "agent_run_id": agent_run_id.strip(),
        "status": "pass",
        "completed_at": _utc_now_iso(),
        "plan_ref": plan_ref.strip(),
        "pipeline_ref": pipeline_ref.strip(),
        "output_refs": output_refs,
        "artifact_hashes": artifact_hashes,
    }

    path = _checkpoint_path(repo_root, orchestration_id)
    checkpoint = _load_checkpoint(repo_root, orchestration_id) or {
        "orchestration_id": orchestration_id,
        "schema_version": "1",
        "completed_steps": [],
    }

    steps: list[dict[str, Any]] = list(checkpoint.get("completed_steps", []))
    steps = [
        s
        for s in steps
        if not (s.get("node_key") == entry["node_key"] and s.get("step") == entry["step"])
    ]
    steps.append(entry)
    checkpoint["completed_steps"] = steps
    checkpoint["last_updated_at"] = _utc_now_iso()

    _write_json(path, checkpoint)
    return entry


def read_checkpoint(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any] | None:
    """orchestration_checkpoint.json を読んで返す。存在しない場合は None。"""
    return _load_checkpoint(repo_root, orchestration_id)


def verify_checkpoint_integrity(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """チェックポイントの全 artifact ハッシュを再計算し整合性を検証する。"""
    checkpoint = _load_checkpoint(repo_root, orchestration_id)
    if checkpoint is None:
        return {
            "orchestration_id": orchestration_id,
            "valid": False,
            "error": "orchestration_checkpoint.json not found",
            "steps": [],
        }

    step_results: list[dict[str, Any]] = []
    all_ok = True

    for entry in checkpoint.get("completed_steps", []):
        node_key = entry.get("node_key", "")
        step = entry.get("step", "")
        stored_hashes: dict[str, str] = entry.get("artifact_hashes", {})
        if not isinstance(stored_hashes, dict):
            stored_hashes = {}
        mismatches: list[dict[str, str]] = []
        missing: list[str] = []

        for ref, expected_hash in stored_hashes.items():
            if not isinstance(ref, str):
                continue
            if not isinstance(expected_hash, str):
                continue
            if expected_hash == "sha256:missing":
                missing.append(ref)
                continue
            actual_hash = _compute_sha256(repo_root / ref)
            if actual_hash != expected_hash:
                mismatches.append(
                    {
                        "ref": ref,
                        "expected": expected_hash,
                        "actual": actual_hash,
                    }
                )

        if missing:
            integrity = "missing_artifacts"
            all_ok = False
        elif mismatches:
            integrity = "stale"
            all_ok = False
        else:
            integrity = "ok"

        step_results.append(
            {
                "node_key": node_key,
                "step": step,
                "integrity": integrity,
                "mismatches": mismatches,
                "missing_artifacts": missing,
            }
        )

    return {
        "orchestration_id": orchestration_id,
        "valid": all_ok,
        "steps": step_results,
    }


def check_step_completed(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    verify_integrity: bool = True,
) -> dict[str, Any] | None:
    """指定 (node_key, step) の完了状況を返す。

    未完了またはチェックポイントが存在しない場合は None を返す。
    verify_integrity=True の場合はハッシュ検証を実施し、stale なら None を返す。
    """
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if meta_path.exists():
        try:
            meta = _read_json(meta_path)
            if isinstance(meta, dict) and not meta.get("resume_enabled"):
                return None
        except json.JSONDecodeError:
            return None
    else:
        return None

    checkpoint = _load_checkpoint(repo_root, orchestration_id)
    if checkpoint is None:
        return None

    node_key_norm = node_key.strip()
    step_norm = step.strip().lower()

    entry = next(
        (
            s
            for s in checkpoint.get("completed_steps", [])
            if s.get("node_key") == node_key_norm and s.get("step") == step_norm
        ),
        None,
    )
    if entry is None:
        return None

    if verify_integrity:
        stored_hashes: dict[str, str] = entry.get("artifact_hashes", {})
        if not isinstance(stored_hashes, dict):
            return None
        for ref, expected_hash in stored_hashes.items():
            if not isinstance(ref, str) or not isinstance(expected_hash, str):
                return None
            if expected_hash == "sha256:missing":
                continue
            actual_hash = _compute_sha256(repo_root / ref)
            if actual_hash != expected_hash:
                return None

    return {
        "node_key": entry.get("node_key"),
        "step": entry.get("step"),
        "agent_run_id": entry.get("agent_run_id"),
        "plan_ref": entry.get("plan_ref"),
        "pipeline_ref": entry.get("pipeline_ref"),
        "output_refs": entry.get("output_refs", []),
        "completed_at": entry.get("completed_at"),
        "integrity": "ok",
    }


def enable_checkpoint_resume(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """orchestration_meta.json に resume_enabled=true を設定する。

    orchestration が存在しない場合は RuntimeError を送出する。
    """
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        raise RuntimeError(
            f"orchestration not found: {orchestration_id}. "
            "Run 'init' before enabling checkpoint resume."
        )
    try:
        meta = _read_json(meta_path)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"orchestration_meta.json is invalid: {meta_path}") from exc
    if not isinstance(meta, dict):
        raise RuntimeError(f"orchestration_meta.json is invalid: {meta_path}")
    meta["resume_enabled"] = True
    meta["resumed_at"] = _utc_now_iso()
    _write_json(meta_path, meta)
    merge_phase_state_for_resume(repo_root, orchestration_id)
    return meta


def _validate_canonical_workspace_root_ref(
    *,
    ref: str,
    node_safe: str,
    kind: str,
    label: str,
) -> None:
    """Require ref == workspace/{kind}/{node_safe}/{root_id} with no extra path segments."""
    token = ref.strip().strip("/")
    parts = token.split("/")
    if len(parts) != 4:
        raise ValueError(
            f"launch request {label} must be exactly workspace/{kind}/<node_key_safe>/<id> "
            f"(directory root only); got {ref!r}"
        )
    if parts[0] != "workspace" or parts[1] != kind:
        raise ValueError(f"launch request {label} must be under workspace/{kind}/; got {ref!r}")
    seg_node = parts[2]
    root_id = parts[3]
    if seg_node != node_safe:
        raise ValueError(
            f"launch request {label} node directory must be {node_safe!r}; got {ref!r}"
        )
    if not _NODE_KEY_SAFE_PATTERN.match(seg_node):
        raise ValueError(f"launch request {label} has invalid node_key_safe segment: {ref!r}")
    if not _SLUG_DATE_SEQ3_PATTERN.match(root_id):
        raise ValueError(
            f"launch request {label} root id must match <slug>_<YYYYMMDD>_<seq3>; got {ref!r}"
        )


def _workspace_path_is_under_ref(path: str, base: str) -> bool:
    p = path.strip().rstrip("/")
    b = base.strip().rstrip("/")
    return p == b or p.startswith(b + "/")


def _validate_pass_output_refs_against_launch(
    repo_root: Path,
    payload: dict[str, Any],
) -> None:
    """Require each output_ref to lie under plan_ref or pipeline_ref from the saved launch request.

    Only applies to ``step`` / ``substep`` runs that have a launch request on disk.
    ``orchestration`` and other roles do not set ``launch_request_ref``; skip validation.
    """
    role = payload.get("agent_role")
    if not isinstance(role, str) or role.strip().lower() not in {"step", "substep"}:
        return

    output_refs = payload.get("output_refs")
    if not isinstance(output_refs, list) or not output_refs:
        return

    launch_request_ref = payload.get("launch_request_ref")
    if not isinstance(launch_request_ref, str) or not launch_request_ref.strip():
        raise ValueError("launch_request_ref must be non-empty string for pass output_refs validation")
    launch_path = repo_root / launch_request_ref.strip()
    if not launch_path.exists():
        raise ValueError(f"launch_request_ref target not found: {launch_request_ref}")
    try:
        launch_payload = _read_json(launch_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"launch_request_ref must be valid json: {launch_request_ref}") from exc
    if not isinstance(launch_payload, dict):
        raise ValueError(f"launch request must be object: {launch_request_ref}")

    plan_ref = launch_payload.get("plan_ref")
    pipeline_ref = launch_payload.get("pipeline_ref")
    if not isinstance(plan_ref, str) or not plan_ref.strip():
        raise ValueError("launch request plan_ref missing for output_refs validation")
    if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
        raise ValueError("launch request pipeline_ref missing for output_refs validation")

    plan_root = plan_ref.strip().rstrip("/")
    pipe_root = pipeline_ref.strip().rstrip("/")

    for idx, ref in enumerate(output_refs):
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"output_refs[{idx}] must be non-empty string")
        r = ref.strip()
        if not r.startswith("workspace/"):
            raise ValueError(f"output_refs[{idx}] must start with workspace/: {r!r}")
        if _workspace_path_is_under_ref(r, plan_root) or _workspace_path_is_under_ref(r, pipe_root):
            continue
        raise ValueError(
            f"output_refs[{idx}] must be under plan_ref or pipeline_ref root "
            f"({plan_root!r} or {pipe_root!r}); got {r!r}"
        )


def _validate_launch_request_payload(request_payload: dict[str, Any]) -> None:
    node_key = request_payload.get("node_key")
    step = request_payload.get("step")
    substep = request_payload.get("substep")
    if not isinstance(node_key, str) or not node_key.strip():
        raise ValueError("launch request must include non-empty node_key")
    if not isinstance(step, str) or not step.strip():
        raise ValueError("launch request must include non-empty step")
    if isinstance(node_key, str) and node_key.strip():
        node_safe = _node_key_to_safe(node_key.strip())
    else:
        node_safe = None

    for key in ("plan_ref", "pipeline_ref", "dependency_ref"):
        value = request_payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"launch request must include non-empty {key}")
        if _is_placeholder_ref(value):
            raise ValueError(f"launch request {key} must not contain placeholder tokens")

    plan_ref = request_payload.get("plan_ref")
    pipeline_ref = request_payload.get("pipeline_ref")
    dependency_ref = request_payload.get("dependency_ref")
    if node_safe is not None:
        if isinstance(plan_ref, str) and plan_ref.strip():
            _validate_canonical_workspace_root_ref(
                ref=plan_ref,
                node_safe=node_safe,
                kind="plans",
                label="plan_ref",
            )
        if isinstance(pipeline_ref, str) and pipeline_ref.strip():
            _validate_canonical_workspace_root_ref(
                ref=pipeline_ref,
                node_safe=node_safe,
                kind="pipelines",
                label="pipeline_ref",
            )
    if isinstance(dependency_ref, str) and _is_placeholder_ref(dependency_ref):
        raise ValueError("launch request dependency_ref must not contain placeholder tokens")

    # repair_strategy / issue_severity の値検証
    repair_strategy = str(request_payload.get("repair_strategy", "none")).strip()
    if repair_strategy not in VALID_REPAIR_STRATEGIES:
        raise ValueError(
            f"launch request repair_strategy must be one of {sorted(VALID_REPAIR_STRATEGIES)}; "
            f"got {repair_strategy!r}"
        )

    issue_severity = str(request_payload.get("issue_severity", "none")).strip()
    if issue_severity not in VALID_ISSUE_SEVERITIES:
        raise ValueError(
            f"launch request issue_severity must be one of {sorted(VALID_ISSUE_SEVERITIES)}; "
            f"got {issue_severity!r}"
        )

    # repair_strategy が reuse/restart のとき repair フィールドを必須にする
    if repair_strategy in {"reuse", "restart"}:
        repair_target = str(request_payload.get("repair_target_agent_run_id", "none")).strip()
        if not repair_target or repair_target == "none":
            raise ValueError(
                "repair launch request requires non-empty repair_target_agent_run_id "
                f"(repair_strategy={repair_strategy!r})"
            )
        repair_reason = str(request_payload.get("repair_reason", "none")).strip()
        if not repair_reason or repair_reason == "none":
            raise ValueError(
                "repair launch request requires non-empty repair_reason "
                f"(repair_strategy={repair_strategy!r})"
            )

    is_verify_substep = (
        isinstance(step, str)
        and step.strip().lower() in {"plan", "generate"}
        and isinstance(substep, str)
        and substep.strip().lower() == "verify"
    )
    if is_verify_substep and isinstance(step, str) and step.strip().lower() == "generate":
        gen_id = request_payload.get("generation_id")
        if not isinstance(gen_id, str) or not gen_id.strip():
            raise ValueError("generate verify launch request must include non-empty generation_id")

    if not is_verify_substep:
        return

    skill_name = request_payload.get("skill_name")
    skill_ref = request_payload.get("skill_ref")
    skill_must_read_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))

    if not isinstance(skill_name, str) or not skill_name.strip():
        raise ValueError("verify launch request must include non-empty skill_name")
    if not isinstance(skill_ref, str) or not skill_ref.strip():
        raise ValueError("verify launch request must include non-empty skill_ref")
    if not skill_must_read_refs:
        raise ValueError("verify launch request must include non-empty skill_must_read_refs")

    required_refs = _required_verify_skill_refs(request_payload)

    missing_refs = [ref for ref in required_refs if ref not in skill_must_read_refs]
    if missing_refs:
        raise ValueError(
            "request payload skill_must_read_refs missing required verify inputs: "
            + ", ".join(missing_refs)
        )


def _load_run_records(orchestration_root: Path) -> dict[str, dict[str, Any]]:
    runs_path = orchestration_root / "agent_runs.jsonl"
    records: dict[str, dict[str, Any]] = {}
    if not runs_path.exists():
        return records
    for raw in runs_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            continue
        run_id = item.get("agent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            records[run_id.strip()] = item
    return records


def _validate_terminal_run_payload(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
) -> None:
    role = payload.get("agent_role")
    status = payload.get("status")
    if not isinstance(role, str) or role not in {"step", "substep"}:
        return
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return

    output_refs = payload.get("output_refs")
    if not isinstance(output_refs, list) or not output_refs:
        raise ValueError("pass status for step/substep requires non-empty output_refs")
    for idx, item in enumerate(output_refs):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"output_refs[{idx}] must be non-empty string")

    _validate_pass_output_refs_against_launch(repo_root, payload)
    _validate_apply_patch_gate_coverage(repo_root, orchestration_id, payload)


def _validate_apply_patch_gate_coverage(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
) -> None:
    """`apply_patch` 書き込み経路の gate 実行証跡を終端時に強制する。"""
    role = payload.get("agent_role")
    if not isinstance(role, str):
        return
    actor_role = role.strip().lower()
    if actor_role not in {"step", "substep"}:
        return

    agent_run_id = payload.get("agent_run_id")
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("agent_run_id must be non-empty string for apply_patch gate coverage")
    run_id = agent_run_id.strip()

    output_refs_obj = payload.get("output_refs")
    output_refs = (
        [str(item).strip() for item in output_refs_obj if isinstance(item, str) and item.strip()]
        if isinstance(output_refs_obj, list)
        else []
    )
    if not output_refs:
        return

    gate_path = _gates_dir(repo_root, orchestration_id) / run_id / "apply_patch_writes.json"
    if not gate_path.exists():
        raise ValueError(
            "pass status for step/substep requires apply_patch_writes gate evidence: "
            f"{gate_path}"
        )
    gate_doc = _read_json(gate_path)
    if not isinstance(gate_doc, dict):
        raise ValueError(f"apply_patch_writes gate artifact must be object: {gate_path}")
    if str(gate_doc.get("status", "")).strip().lower() != "pass":
        raise ValueError(f"apply_patch_writes gate must pass before terminal run record: {gate_path}")
    args_json = gate_doc.get("args_json")
    if not isinstance(args_json, dict):
        raise ValueError(f"apply_patch_writes gate args_json must be object: {gate_path}")
    gate_actor_role = args_json.get("actor_role")
    if not isinstance(gate_actor_role, str) or gate_actor_role.strip().lower() != actor_role:
        raise ValueError(
            "apply_patch_writes gate actor_role mismatch: "
            f"expected={actor_role!r} got={gate_actor_role!r}"
        )
    changed_paths_obj = args_json.get("changed_paths")
    if not isinstance(changed_paths_obj, list) or not all(isinstance(x, str) for x in changed_paths_obj):
        raise ValueError(f"apply_patch_writes gate changed_paths must be string array: {gate_path}")
    changed_paths = [_normalize_rel_posix(x) for x in changed_paths_obj if x.strip()]
    if not changed_paths:
        raise ValueError(f"apply_patch_writes gate changed_paths must be non-empty: {gate_path}")

    uncovered: list[str] = []
    for output_ref in output_refs:
        rel = _normalize_rel_posix(output_ref)
        if not any(_repo_path_under_prefix(rel, cp) for cp in changed_paths):
            uncovered.append(output_ref)
    if uncovered:
        raise ValueError(
            "apply_patch_writes gate does not cover terminal output_refs: "
            + ", ".join(uncovered)
        )


def _validate_step_or_substep_launch_refs(repo_root: Path, payload: dict[str, Any]) -> None:
    for key in (
        "launch_request_ref",
        "launch_response_ref",
        "launch_prompt_ref",
        "launch_reply_ref",
    ):
        ref = payload.get(key)
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"{key} must be non-empty string")
        target = repo_root / ref.strip()
        if not target.exists():
            raise ValueError(f"{key} target not found: {ref}")
        if key in {"launch_prompt_ref", "launch_reply_ref"}:
            text = target.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                raise ValueError(f"{key} target must be non-empty: {ref}")


def _iter_step_result_paths(root: Path) -> list[Path]:
    steps_root = root / "steps"
    if not steps_root.exists():
        return []
    return sorted(steps_root.glob("*/*/*/step_result.json"))


def _validate_orchestration_completion_for_pass(
    repo_root: Path,
    orchestration_id: str,
) -> None:
    root = _orchestration_root(repo_root, orchestration_id)
    graph_path = root / "agent_graph.json"
    runs = _load_run_records(root)
    if not runs:
        raise RuntimeError("cannot mark orchestration pass without agent_runs.jsonl records")

    orchestration_runs = [
        payload
        for payload in runs.values()
        if isinstance(payload.get("agent_role"), str)
        and payload.get("agent_role") == "orchestration"
    ]
    if not orchestration_runs:
        raise RuntimeError("cannot mark orchestration pass without orchestration agent run record")

    graph = _load_graph(graph_path)
    edges = graph.get("edges")
    if not isinstance(edges, list) or not edges:
        raise RuntimeError("cannot mark orchestration pass without agent_graph edges")

    step_result_refs_by_substep: dict[str, Path] = {}
    for result_path in _iter_step_result_paths(root):
        try:
            result = _read_json(result_path)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid step_result.json: {result_path}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"step_result.json must be object: {result_path}")
        executor_run_id = result.get("executor_agent_run_id")
        if not isinstance(executor_run_id, str) or not executor_run_id.strip():
            raise RuntimeError(f"executor_agent_run_id missing: {result_path}")
        substep_run_ids = result.get("substep_agent_run_ids")
        if not isinstance(substep_run_ids, list):
            raise RuntimeError(f"substep_agent_run_ids must be list: {result_path}")
        for substep_run_id in substep_run_ids:
            if isinstance(substep_run_id, str) and substep_run_id.strip():
                step_result_refs_by_substep[substep_run_id.strip()] = result_path

    for idx, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise RuntimeError(f"agent_graph edge must be object: index={idx}")
        parent_id = edge.get("parent_agent_run_id")
        child_id = edge.get("child_agent_run_id")
        if not isinstance(parent_id, str) or not parent_id.strip() or parent_id.strip() not in runs:
            raise RuntimeError(
                f"agent_graph edge parent_agent_run_id missing from agent_runs.jsonl: index={idx}"
            )
        if not isinstance(child_id, str) or not child_id.strip() or child_id.strip() not in runs:
            raise RuntimeError(
                f"agent_graph edge child_agent_run_id missing from agent_runs.jsonl: index={idx}"
            )

    for run_id, payload in runs.items():
        role = payload.get("agent_role")
        if not isinstance(role, str) or role not in {"step", "substep"}:
            continue
        status = payload.get("status")
        if not isinstance(status, str) or status.strip().lower() not in TERMINAL_STATUSES:
            raise RuntimeError(f"{role} agent_run_id must be terminal before pass: {run_id}")
        _validate_step_or_substep_launch_refs(repo_root, payload)
        node_key = payload.get("node_key")
        step = payload.get("step")
        if not isinstance(node_key, str) or not node_key.strip():
            raise RuntimeError(f"{role} node_key missing: {run_id}")
        if not isinstance(step, str) or not step.strip():
            raise RuntimeError(f"{role} step missing: {run_id}")
        node_safe = _node_key_to_safe(node_key.strip())
        step_token = step.strip().lower()
        if role == "step":
            result_path = root / "steps" / node_safe / step_token / run_id / "step_result.json"
            if not result_path.exists():
                raise RuntimeError(f"step_result.json missing for step agent_run_id={run_id}")
        else:
            if run_id not in step_result_refs_by_substep:
                raise RuntimeError(
                    f"step_result.json missing substep_agent_run_ids entry for substep agent_run_id={run_id}"
                )


_STEP_META_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "generate": ("attempt_count", "verification_status", "last_fail_reason", "debug_mode", "context_isolated"),
    "plan": ("attempt_count", "verification_status", "context_isolated"),
}
_STEP_META_FILENAME: dict[str, str] = {
    "generate": "generate_meta.json",
    "plan": "plan_meta.json",
}

STEP_REQUIRED_VALIDATION_STAGES: dict[str, frozenset[str]] = {
    "generate": frozenset({"post_generate", "post_build", "full"}),
    "build": frozenset({"post_build", "full"}),
    "execute": frozenset({"post_execute", "pre_judge", "full"}),
    "judge": frozenset({"pre_judge", "full"}),
}


def _validate_step_result_payload(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
) -> None:
    step_token = step.strip().lower()
    status = payload.get("status")
    status_token = status.strip().lower() if isinstance(status, str) else ""

    # validation_stage チェック（generate/build/execute/judge の pass 時のみ）
    if step_token in STEP_REQUIRED_VALIDATION_STAGES and status_token == "pass":
        allowed = STEP_REQUIRED_VALIDATION_STAGES[step_token]
        validation_stage = payload.get("validation_stage")
        if not isinstance(validation_stage, str) or validation_stage.strip() not in allowed:
            raise ValueError(
                f"pass step_result for {step_token} requires validation_stage in "
                f"{sorted(allowed)}; got {validation_stage!r}"
            )

    # 以下は既存の substep 検証（plan/generate/tune のみ）
    if step_token not in {"plan", "generate", "tune"}:
        return
    if status_token != "pass":
        return
    substep_run_ids = payload.get("substep_agent_run_ids")
    if not isinstance(substep_run_ids, list) or not substep_run_ids:
        raise ValueError(f"pass step_result for {step_token} requires non-empty substep_agent_run_ids")

    run_records = _load_run_records(_orchestration_root(repo_root, orchestration_id))
    required_outputs = payload.get("required_outputs")
    if not isinstance(required_outputs, list):
        raise ValueError("step_result.required_outputs must be list")
    declared_outputs = {item.strip() for item in required_outputs if isinstance(item, str) and item.strip()}

    substep_outputs: set[str] = set()
    for idx, substep_run_id in enumerate(substep_run_ids):
        if not isinstance(substep_run_id, str) or not substep_run_id.strip():
            raise ValueError(f"substep_agent_run_ids[{idx}] must be non-empty string")
        substep_record = run_records.get(substep_run_id.strip())
        if not isinstance(substep_record, dict):
            raise ValueError(f"missing substep run record: {substep_run_id}")
        substep_status = substep_record.get("status")
        if not isinstance(substep_status, str) or substep_status.strip().lower() != "pass":
            raise ValueError(f"substep {substep_run_id} must be pass before step_result can pass")
        output_refs = substep_record.get("output_refs")
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError(f"substep {substep_run_id} must publish non-empty output_refs")
        for output_ref in output_refs:
            if isinstance(output_ref, str) and output_ref.strip():
                substep_outputs.add(output_ref.strip())

    # meta ファイル検証（plan/generate の pass 時のみ）
    if step_token in _STEP_META_REQUIRED_KEYS:
        meta_filename = _STEP_META_FILENAME[step_token]
        required_meta_keys = _STEP_META_REQUIRED_KEYS[step_token]
        meta_ref = next(
            (ref for ref in substep_outputs if ref.endswith(meta_filename)),
            None,
        )
        if meta_ref is None:
            raise ValueError(
                f"pass step_result for {step_token} requires a substep output_ref ending in "
                f"{meta_filename}"
            )
        meta_path = repo_root / meta_ref
        if not meta_path.exists():
            raise ValueError(
                f"{meta_filename} not found at output_ref: {meta_ref}"
            )
        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{meta_filename} is not valid JSON: {meta_ref}"
            ) from exc
        if not isinstance(meta_data, dict):
            raise ValueError(f"{meta_filename} must be a JSON object: {meta_ref}")
        missing_keys = [k for k in required_meta_keys if k not in meta_data]
        if missing_keys:
            raise ValueError(
                f"{meta_filename} missing required keys: {missing_keys} (ref={meta_ref})"
            )

    missing_outputs = sorted(ref for ref in declared_outputs if ref not in substep_outputs)
    if missing_outputs:
        raise ValueError(
            "step_result.required_outputs must be satisfied by substep output_refs: "
            + ", ".join(missing_outputs)
        )


def parse_feature_list(raw: str) -> dict[str, bool]:
    features: dict[str, bool] = {}
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        enabled = parts[-1].lower()
        if enabled not in {"true", "false"}:
            continue
        feature_name = parts[0].strip()
        if feature_name:
            features[feature_name] = enabled == "true"
    return features


def _probe_codex_backend(
    backend_token: str,
    command: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[list[dict[str, Any]], dict[str, bool], bool, str]:
    """codex バックエンドのプローブを実行し (checks, features, multi_agent_enabled, agent_version) を返す。"""
    version_proc = runner([command, "--version"], text=True, capture_output=True, check=False)
    features_proc = runner([command, "features", "list"], text=True, capture_output=True, check=False)
    features: dict[str, bool] = {}
    features_list_available = features_proc.returncode == 0
    multi_agent_enabled = False
    if features_proc.returncode == 0:
        features = parse_feature_list(features_proc.stdout)
        multi_agent_enabled = features.get("multi_agent") is True
    features_list_detail = features_proc.stdout.strip() or features_proc.stderr.strip()
    checks = [
        {
            "name": f"{backend_token}_version_available",
            "pass": version_proc.returncode == 0,
            "detail": version_proc.stdout.strip() or version_proc.stderr.strip(),
        },
        {
            "name": f"{backend_token}_features_list_available",
            "pass": features_list_available,
            "detail": features_list_detail,
        },
        {
            "name": "multi_agent_enabled",
            "pass": multi_agent_enabled,
            "detail": f"multi_agent={features.get('multi_agent')}",
        },
    ]
    return checks, features, multi_agent_enabled, version_proc.stdout.strip()


def _pass_values_by_check_name(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """各 check の `pass` を名前で引けるようにする。`pass` は bool または None（未実行スキップ）。"""
    by_name: dict[str, Any] = {}
    for item in checks:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            by_name[name] = item.get("pass")
    return by_name


def _can_launch_from_help_fallback_checks(
    backend_token: str, checks: list[dict[str, Any]]
) -> bool:
    """cursor/claude 用。`features list` が無くても `--help` が通れば起動可能とみなす。"""
    passes = _pass_values_by_check_name(checks)
    version_ok = passes.get(f"{backend_token}_version_available") is True
    features_list_ok = passes.get(f"{backend_token}_features_list_available") is True
    help_pass = passes.get(f"{backend_token}_help_probe_available")
    multi_ok = passes.get("multi_agent_enabled") is True
    # `pass` が None のときは --help を実行していない（features list で multi_agent 確定済み）。
    # その場合は `features_list_ok` に委ね、`None` を黙って False 相当にしない。
    help_confirms_launch = help_pass is True
    return version_ok and multi_ok and (features_list_ok or help_confirms_launch)


def _all_strict_boolean_probe_checks_pass(checks: list[dict[str, Any]]) -> bool:
    """codex 等。`pass` キーは必須。値が None の check は未実行プローブとして評価から除外する。

    明示的に False の `pass` は不合格。少なくとも 1 件は None 以外の `pass` が存在し、
    それらがすべて True でなければならない。help fallback 由来の check 列を誤って渡した
    場合でも、`pass: None` のみを黙って不合格にしない。
    """
    evaluated_any = False
    for item in checks:
        if not isinstance(item, dict):
            return False
        if "pass" not in item:
            return False
        p = item["pass"]
        if p is None:
            continue
        evaluated_any = True
        if p is not True:
            return False
    return evaluated_any


def _probe_help_fallback_backend(
    backend_token: str,
    command: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[list[dict[str, Any]], dict[str, bool], bool, str]:
    """cursor/claude バックエンドのプローブを実行し (checks, features, multi_agent_enabled, agent_version) を返す。"""
    version_proc = runner([command, "--version"], text=True, capture_output=True, check=False)
    features_proc = runner([command, "features", "list"], text=True, capture_output=True, check=False)
    features: dict[str, bool] = {}
    features_list_available = features_proc.returncode == 0
    multi_agent_enabled = False
    features_list_detail = features_proc.stdout.strip() or features_proc.stderr.strip()
    help_proc: subprocess.CompletedProcess[str] | None = None
    if features_proc.returncode == 0:
        features = parse_feature_list(features_proc.stdout)
        multi_agent_enabled = features.get("multi_agent") is True
    if not multi_agent_enabled:
        # Cursor and Claude Code CLIs do not expose `features list` as a structured command.
        # Use --help as a best-effort launchability probe instead.
        # Launch-time live preflight in `record_launch` remains the fail-safe.
        help_proc = runner([command, "--help"], text=True, capture_output=True, check=False)
        if help_proc.returncode == 0:
            multi_agent_enabled = True
            features = {"multi_agent": True}
    if help_proc is None:
        # Do not record pass=true for a probe that was not executed; launchability
        # still uses features_list_ok in _can_launch_from_help_fallback_checks.
        help_probe_pass: bool | None = None
        help_probe_detail = (
            "skipped; multi_agent was already confirmed from features list output "
            "(no --help probe run)"
        )
    else:
        help_probe_pass = help_proc.returncode == 0
        help_detail = help_proc.stdout.strip() or help_proc.stderr.strip()
        if help_probe_pass:
            help_probe_detail = (
                f"{backend_token} backend multi_agent could not be confirmed from features list; "
                "fallback to --help succeeded"
            )
            if features_list_detail:
                help_probe_detail += f"\nfeatures list: {features_list_detail}"
            if help_detail:
                help_probe_detail += f"\n{help_detail}"
        else:
            help_probe_detail = help_detail or "(no stdout/stderr from --help)"

    checks = [
        {
            "name": f"{backend_token}_version_available",
            "pass": version_proc.returncode == 0,
            "detail": version_proc.stdout.strip() or version_proc.stderr.strip(),
        },
        {
            "name": f"{backend_token}_features_list_available",
            "pass": features_list_available,
            "detail": features_list_detail,
        },
        {
            "name": f"{backend_token}_help_probe_available",
            "pass": help_probe_pass,
            "detail": help_probe_detail,
        },
        {
            "name": "multi_agent_enabled",
            "pass": multi_agent_enabled,
            "detail": f"multi_agent={features.get('multi_agent')}",
        },
    ]
    return checks, features, multi_agent_enabled, version_proc.stdout.strip()


_BACKEND_PROBERS: dict[
    str,
    Callable[
        [str, str, Callable[..., subprocess.CompletedProcess[str]]],
        tuple[list[dict[str, Any]], dict[str, bool], bool, str],
    ],
] = {
    "codex": _probe_codex_backend,
    "cursor": _probe_help_fallback_backend,
    "claude": _probe_help_fallback_backend,
}


def probe_execution_platform(
    *,
    backend: str,
    agent_command: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    backend_token = backend.strip().lower()
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(f"unsupported backend: {backend}")

    default_command = DEFAULT_BACKEND_COMMANDS[backend_token]
    command = (
        agent_command.strip()
        if isinstance(agent_command, str) and agent_command.strip()
        else default_command
    )
    for known_backend, known_command in DEFAULT_BACKEND_COMMANDS.items():
        if command != known_command:
            continue
        if known_backend != backend_token:
            raise ValueError(
                f"agent_command/backend mismatch: backend={backend_token} requires "
                f"{DEFAULT_BACKEND_COMMANDS[backend_token]} (or custom command), got {command}"
            )
        break

    prober = _BACKEND_PROBERS[backend_token]
    checks, features, multi_agent_enabled, agent_version = prober(backend_token, command, runner)

    if backend_token in ("cursor", "claude"):
        can_launch_agents = _can_launch_from_help_fallback_checks(backend_token, checks)
    else:
        can_launch_agents = _all_strict_boolean_probe_checks_pass(checks)
    session_policy = {
        "allow_step_agent_launch": os.environ.get("CODEX_ALLOW_STEP_AGENT_LAUNCH", "1").strip().lower()
        not in {"0", "false", "no"},
        "allow_substep_agent_launch": os.environ.get(
            "CODEX_ALLOW_SUBSTEP_AGENT_LAUNCH", "1"
        ).strip().lower()
        not in {"0", "false", "no"},
    }
    return {
        "checked_at": _utc_now_iso(),
        "backend": backend_token,
        "probe_command": command,
        "agent_version": agent_version,
        "feature_states": features,
        "checks": checks,
        "can_launch_step_agents": can_launch_agents,
        "can_launch_substep_agents": can_launch_agents,
        "session_policy": session_policy,
        "session_policy_launchable": (
            bool(session_policy["allow_step_agent_launch"])
            and bool(session_policy["allow_substep_agent_launch"])
        ),
        "status": "pass" if can_launch_agents else "fail",
    }


def probe_codex_cli(
    codex_command: str = "codex",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    return probe_execution_platform(
        backend="codex",
        agent_command=codex_command,
        runner=runner,
    )


def init_orchestration(
    repo_root: Path,
    orchestration_id: str,
    *,
    spec_ref: str | None = None,
    dependency_ref: str | None = None,
    status: str = "running",
) -> dict[str, Any]:
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "launches").mkdir(parents=True, exist_ok=True)
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "steps").mkdir(parents=True, exist_ok=True)
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    init_phase_state_json(repo_root, orchestration_id, reason="init_orchestration")

    meta = {
        "orchestration_id": orchestration_id,
        "status": status,
        "started_at": _utc_now_iso(),
    }
    if spec_ref:
        meta["spec_ref"] = spec_ref
    if dependency_ref:
        meta["dependency_ref"] = dependency_ref
    _write_json(root / "orchestration_meta.json", meta)

    graph_path = root / "agent_graph.json"
    if not graph_path.exists():
        _write_json(graph_path, {"edges": []})

    runs_path = root / "agent_runs.jsonl"
    if not runs_path.exists():
        runs_path.write_text("", encoding="utf-8")

    return meta


def write_preflight(repo_root: Path, orchestration_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _validate_preflight_payload(payload)
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)

    stored = dict(payload)
    if not isinstance(stored.get("session_policy"), dict):
        allow_step = stored.get("can_launch_step_agents") is True
        allow_substep = stored.get("can_launch_substep_agents") is True
        stored["session_policy"] = {
            "allow_step_agent_launch": allow_step,
            "allow_substep_agent_launch": allow_substep,
        }
    if not isinstance(stored.get("session_policy_launchable"), bool):
        policy = stored.get("session_policy")
        allow_step = bool(policy.get("allow_step_agent_launch")) if isinstance(policy, dict) else True
        allow_substep = (
            bool(policy.get("allow_substep_agent_launch")) if isinstance(policy, dict) else True
        )
        stored["session_policy_launchable"] = allow_step and allow_substep
    if "probed_at" not in stored:
        stored["probed_at"] = stored.get("checked_at") or _utc_now_iso()

    _write_json(root / "preflight.json", stored)
    meta_path = root / "orchestration_meta.json"
    if meta_path.exists():
        meta = _read_json(meta_path)
        if isinstance(meta, dict) and not isinstance(meta.get("dependency_readiness"), dict):
            meta["dependency_readiness"] = {
                "direct_dependency_plan_readiness": True,
                "direct_dependency_execution_readiness": True,
                "detail": {
                    "plan_ref_verified": True,
                    "pipeline_ref_verified": True,
                    "aggregate_verdict_verified": True,
                },
            }
            _write_json(meta_path, meta)
    if _preflight_allows_agent_launch(stored):
        _transition_phase_state(
            repo_root,
            orchestration_id,
            new_state="preflight_passed",
            event="preflight_written",
        )
    else:
        _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
        _append_phase_state_log(
            repo_root,
            orchestration_id,
            {
                "ts": _utc_now_iso(),
                "event": "preflight_written_not_launchable",
                "from": None,
                "to": None,
            },
        )
    return stored


def _load_graph(graph_path: Path) -> dict[str, Any]:
    if graph_path.exists():
        graph = _read_json(graph_path)
        if isinstance(graph, dict) and isinstance(graph.get("edges"), list):
            return graph
    return {"edges": []}


def record_launch(
    repo_root: Path,
    orchestration_id: str,
    *,
    parent_agent_run_id: str,
    child_agent_run_id: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    relation_type: str = "launch",
) -> dict[str, Any]:
    preflight_payload: dict[str, Any] | None = None
    try:
        preflight_payload = _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=True,
        )
    except RuntimeError:
        # Launch gate failure must terminate orchestration immediately.
        try:
            update_orchestration_status(
                repo_root,
                orchestration_id,
                status="fail",
            )
        except Exception:
            pass
        raise
    step_raw = request_payload.get("step")
    node_key_raw = request_payload.get("node_key")
    if isinstance(step_raw, str) and step_raw.strip() and isinstance(node_key_raw, str) and node_key_raw.strip():
        required = _required_child_agent_kind(step_raw)
        backend = (
            str(preflight_payload.get("backend", "")).strip().lower()
            if isinstance(preflight_payload, dict)
            else ""
        )
        backend_token = backend if backend in SUPPORTED_BACKENDS else "codex"
        check = workflow_launch_check(
            repo_root,
            orchestration_id=orchestration_id,
            node_key=node_key_raw.strip(),
            step=step_raw.strip(),
            backend=backend_token,
            require_child_agent=required,
        )
        if check.get("status") == "fail_closed":
            reason_code = str(check.get("reason_code") or "child_agent_unavailable_on_execution_platform")
            try:
                update_orchestration_status(
                    repo_root,
                    orchestration_id,
                    status="fail_closed",
                    reason_code=reason_code,
                    reason_detail=str(check.get("reason_detail") or ""),
                    blocking_policy_scope=str(check.get("blocking_policy_scope") or ""),
                )
            except Exception:
                pass
            raise RuntimeError(
                "record-launch blocked by workflow-launch-check: "
                f"reason_code={reason_code}"
            )
    root = _orchestration_root(repo_root, orchestration_id)
    launches_root = root / "launches"
    launches_root.mkdir(parents=True, exist_ok=True)
    child_dialog_root = root / "agents" / child_agent_run_id / "dialogs"
    child_dialog_root.mkdir(parents=True, exist_ok=True)

    request_payload = dict(request_payload)
    request_payload.setdefault("orchestration_id", orchestration_id)
    request_payload.setdefault("agent_run_id", child_agent_run_id)
    request_payload.setdefault("parent_agent_run_id", parent_agent_run_id)
    request_payload = prepare_launch_request_payload(request_payload)
    response_payload = dict(response_payload)
    response_agent_session_id = _validate_response_agent_session_id(response_payload)
    response_payload.setdefault("agent_session_id", response_agent_session_id)

    _validate_launch_request_payload(request_payload)

    prompt_text = _extract_launch_prompt_text(request_payload)
    reply_text = _extract_launch_reply_text(response_payload)
    if not prompt_text.strip():
        raise ValueError("launch prompt text must be non-empty")
    if not reply_text.strip():
        raise ValueError("launch reply text must be non-empty")
    _validate_launch_prompt_text(request_payload, prompt_text)

    request_ref, response_ref = _launch_refs(orchestration_id, child_agent_run_id)
    prompt_ref, reply_ref = _launch_dialog_refs(orchestration_id, child_agent_run_id)
    child_request_ref, child_response_ref = _child_launch_refs(orchestration_id, child_agent_run_id)
    child_prompt_ref, child_reply_ref = _child_dialog_refs(orchestration_id, child_agent_run_id)
    request_payload.setdefault("launch_prompt_ref", prompt_ref)
    request_payload.setdefault("child_launch_request_ref", child_request_ref)
    request_payload.setdefault("child_launch_prompt_ref", child_prompt_ref)
    response_payload.setdefault("launch_reply_ref", reply_ref)
    response_payload.setdefault("child_launch_response_ref", child_response_ref)
    response_payload.setdefault("child_launch_reply_ref", child_reply_ref)

    request_path = launches_root / f"{child_agent_run_id}.request.json"
    response_path = launches_root / f"{child_agent_run_id}.response.json"
    prompt_path = launches_root / f"{child_agent_run_id}.prompt.txt"
    reply_path = launches_root / f"{child_agent_run_id}.reply.txt"
    child_request_path = child_dialog_root / "child.request.json"
    child_response_path = child_dialog_root / "child.response.json"
    child_prompt_path = child_dialog_root / "child.prompt.txt"
    child_reply_path = child_dialog_root / "child.reply.txt"
    _write_json(request_path, request_payload)
    _write_json(response_path, response_payload)
    _write_text(prompt_path, prompt_text)
    _write_text(reply_path, reply_text)
    _write_json(child_request_path, request_payload)
    _write_json(child_response_path, response_payload)
    _write_text(child_prompt_path, prompt_text)
    _write_text(child_reply_path, reply_text)

    graph_path = root / "agent_graph.json"
    graph = _load_graph(graph_path)
    edge = {
        "parent_agent_run_id": parent_agent_run_id,
        "child_agent_run_id": child_agent_run_id,
        "relation_type": relation_type,
    }
    if edge not in graph["edges"]:
        graph["edges"].append(edge)
    _write_json(graph_path, graph)

    nk = request_payload.get("node_key")
    st = request_payload.get("step")
    out_refs: dict[str, Any] = {
        "launch_request_ref": request_ref,
        "launch_response_ref": response_ref,
        "launch_prompt_ref": prompt_ref,
        "launch_reply_ref": reply_ref,
        "child_launch_request_ref": child_request_ref,
        "child_launch_response_ref": child_response_ref,
        "child_launch_prompt_ref": child_prompt_ref,
        "child_launch_reply_ref": child_reply_ref,
    }
    if isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip():
        _write_access_policy_for_launch(
            repo_root,
            orchestration_id,
            child_agent_run_id,
            request_payload,
        )
        cap_doc = _write_capability_for_launch(
            repo_root,
            orchestration_id,
            child_agent_run_id,
            request_payload,
        )
        cap_rel = f"workspace/orchestrations/{orchestration_id}/capabilities/{child_agent_run_id}.json"
        out_refs["capability_ref"] = cap_rel
        out_refs["capability_token"] = cap_doc.get("capability_token", "")
        step_tok = st.strip().lower()
        _transition_node_step_phase_state(
            repo_root,
            orchestration_id,
            node_key=nk.strip(),
            step=step_tok,
            new_state="launch_recorded",
            event="record_launch",
            agent_run_id=child_agent_run_id,
        )
        _transition_node_step_phase_state(
            repo_root,
            orchestration_id,
            node_key=nk.strip(),
            step=step_tok,
            new_state="child_running",
            event="child_launched",
            agent_run_id=child_agent_run_id,
        )

    return out_refs


def _read_existing_run_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    run_ids: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        item = json.loads(line)
        run_id = item.get("agent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            run_ids.add(run_id.strip())
    return run_ids


def _validate_skipped_by_checkpoint_payload(payload: dict[str, Any]) -> None:
    for key in ("node_key", "step", "skipped_step", "reason", "checkpoint_agent_run_id"):
        val = payload.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{key} must be non-empty string for skipped_by_checkpoint")
    status = payload.get("status")
    if not isinstance(status, str) or status.strip().lower() != "skipped":
        raise ValueError("skipped_by_checkpoint requires status=skipped")
    if payload["step"].strip().lower() != payload["skipped_step"].strip().lower():
        raise ValueError("skipped_step must match step for skipped_by_checkpoint")


def record_agent_run(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    runs_path = root / "agent_runs.jsonl"

    agent_run_id = payload.get("agent_run_id")
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("agent_run_id must be non-empty string")
    agent_run_id = agent_run_id.strip()

    role = payload.get("agent_role") or payload.get("agent_type") or payload.get("role")
    role_token = role.strip().lower() if isinstance(role, str) and role.strip() else None
    if role_token is None:
        raise ValueError("agent_role must be non-empty string")
    if role_token == "skipped_by_checkpoint":
        _validate_skipped_by_checkpoint_payload(payload)
    elif role_token in {"step", "substep"}:
        _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=False,
        )

    agent_backend = payload.get("agent_backend")
    if not isinstance(agent_backend, str) or not agent_backend.strip():
        raise ValueError("agent_backend must be non-empty string")
    backend_token = agent_backend.strip().lower()
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"agent_backend must be one of {sorted(SUPPORTED_BACKENDS)}; got {agent_backend!r}"
        )
    payload["agent_backend"] = backend_token

    existing = _read_existing_run_ids(runs_path)
    if agent_run_id in existing:
        raise ValueError(f"duplicate agent_run_id: {agent_run_id}")

    payload = dict(payload)
    payload["agent_run_id"] = agent_run_id
    payload["agent_role"] = role_token
    payload.setdefault("started_at", _utc_now_iso())

    if role_token in {"step", "substep"}:
        payload.setdefault("context_isolated", True)
        request_ref, response_ref = _launch_refs(orchestration_id, agent_run_id)
        prompt_ref, reply_ref = _launch_dialog_refs(orchestration_id, agent_run_id)
        payload.setdefault("launch_request_ref", request_ref)
        payload.setdefault("launch_response_ref", response_ref)
        payload.setdefault("launch_prompt_ref", prompt_ref)
        payload.setdefault("launch_reply_ref", reply_ref)
        _validate_step_or_substep_launch_refs(repo_root, payload)

    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in TERMINAL_STATUSES:
        payload.setdefault("finished_at", _utc_now_iso())

    if role_token in {"step", "substep"}:
        launch_response_path = repo_root / payload["launch_response_ref"]
        launch_response_payload = _read_json(launch_response_path)
        if not isinstance(launch_response_payload, dict):
            raise ValueError("launch response must be json object")
        response_agent_session_id = _validate_response_agent_session_id(launch_response_payload)
        payload_agent_session_id = payload.get("agent_session_id")
        if not isinstance(payload_agent_session_id, str) or not payload_agent_session_id.strip():
            raise ValueError("agent_session_id must be non-empty string")
        if payload_agent_session_id.strip() != response_agent_session_id:
            raise ValueError(
                "agent_session_id must match child agent identifier in launch response"
            )

    _validate_terminal_run_payload(repo_root, orchestration_id, payload)

    dialogs_root = root / "agents" / agent_run_id / "dialogs"
    dialogs_root.mkdir(parents=True, exist_ok=True)
    result_ref, summary_ref = _agent_result_refs(orchestration_id, agent_run_id)
    payload.setdefault("agent_result_ref", result_ref)
    payload.setdefault("agent_summary_ref", summary_ref)
    summary_text = _extract_agent_summary_text(payload)
    _validate_agent_summary_text(payload, summary_text)
    _write_json(dialogs_root / "agent.result.json", payload)
    _write_text(dialogs_root / "agent.summary.txt", summary_text)

    with runs_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    if role_token in {"step", "substep"}:
        status_raw = payload.get("status")
        status_lower = status_raw.strip().lower() if isinstance(status_raw, str) else ""
        if status_lower in TERMINAL_STATUSES:
            nk_done = payload.get("node_key")
            st_done = payload.get("step")
            if isinstance(nk_done, str) and nk_done.strip() and isinstance(st_done, str) and st_done.strip():
                _transition_node_step_phase_state(
                    repo_root,
                    orchestration_id,
                    node_key=nk_done.strip(),
                    step=st_done.strip().lower(),
                    new_state="child_finished",
                    event="record_agent_run_terminal",
                    agent_run_id=agent_run_id,
                )

    return payload


def write_step_result(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    _require_preflight_launchable(
        repo_root,
        orchestration_id,
        enforce_live_probe=False,
    )
    _phase_state_allows_write_step_result(
        repo_root,
        orchestration_id,
        node_key=node_key,
        step=step,
    )
    node_safe = _node_key_to_safe(node_key)
    step_token = step.strip().lower()
    root = _orchestration_root(repo_root, orchestration_id)
    result_path = root / "steps" / node_safe / step_token / agent_run_id / "step_result.json"

    result = dict(payload)
    result.setdefault("executor_agent_run_id", agent_run_id)
    result.setdefault("required_outputs", [])
    result.setdefault("failed_substeps", [])

    _validate_step_result_payload(
        repo_root,
        orchestration_id,
        node_key=node_key,
        step=step,
        agent_run_id=agent_run_id,
        payload=result,
    )

    _write_json(result_path, result)

    _transition_node_step_phase_state(
        repo_root,
        orchestration_id,
        node_key=node_key,
        step=step_token,
        new_state="step_result_written",
        event="write_step_result",
        agent_run_id=agent_run_id,
    )

    if result.get("status", "").strip().lower() == "pass":
        try:
            update_checkpoint(
                repo_root,
                orchestration_id,
                node_key=node_key,
                step=step,
                agent_run_id=agent_run_id,
                result=result,
            )
        except Exception:
            print(
                f"[WARN] checkpoint update failed for {node_key}/{step}: "
                + traceback.format_exc(),
                file=sys.stderr,
            )

    return result


def update_orchestration_status(
    repo_root: Path,
    orchestration_id: str,
    *,
    status: str,
    reason_code: str | None = None,
    reason_detail: str | None = None,
    blocking_policy_scope: str | None = None,
) -> dict[str, Any]:
    if status == "pass":
        _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=False,
        )
        _validate_orchestration_completion_for_pass(repo_root, orchestration_id)
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(meta_path)
    meta = _read_json(meta_path)
    if not isinstance(meta, dict):
        raise ValueError(f"invalid orchestration_meta.json: {meta_path}")
    if status == "fail_closed":
        if not isinstance(reason_code, str) or not reason_code.strip():
            raise ValueError("set-status fail_closed requires non-empty reason_code")
        if reason_code.strip() not in FAIL_CLOSED_REASON_CODES:
            raise ValueError(
                "set-status fail_closed reason_code must be one of "
                f"{sorted(FAIL_CLOSED_REASON_CODES)}"
            )
    meta["status"] = status
    if isinstance(reason_code, str) and reason_code.strip():
        meta["reason_code"] = reason_code.strip()
    if isinstance(reason_detail, str) and reason_detail.strip():
        meta["reason_detail"] = reason_detail.strip()
    if isinstance(blocking_policy_scope, str) and blocking_policy_scope.strip():
        meta["blocking_policy_scope"] = blocking_policy_scope.strip()
    if status == "fail_closed":
        meta["detected_at"] = _utc_now_iso()
    if status in TERMINAL_STATUSES:
        meta["finished_at"] = _utc_now_iso()
    if status == "fail_closed":
        meta["finished_at"] = _utc_now_iso()
    _write_json(meta_path, meta)
    _append_phase_state_log(
        repo_root,
        orchestration_id,
        {
            "ts": _utc_now_iso(),
            "event": "set_status",
            "to": status,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "blocking_policy_scope": blocking_policy_scope,
            "detected_at": _utc_now_iso() if status == "fail_closed" else None,
        },
    )
    return meta


def reserve_phase_root(
    repo_root: Path,
    *,
    orchestration_id: str,
    node_key: str,
    step: str,
    reserved_id: str,
    reserved_by_agent_run_id: str,
) -> dict[str, Any]:
    step_key = step.strip().lower()
    _required_child_agent_kind(step_key)
    node_safe = _node_key_to_safe(node_key.strip())
    out = (
        _orchestration_root(repo_root, orchestration_id)
        / "reservations"
        / node_safe
        / f"{step_key}.json"
    )
    payload = {
        "node_key": node_key.strip(),
        "step": step_key,
        "reserved_plan_id": reserved_id.strip(),
        "reserved_by_agent_run_id": reserved_by_agent_run_id.strip(),
        "status": "reserved",
        "reserved_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return payload


def _json_arg(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("json payload must be object")
    return value


def _json_string_list_arg(raw: str) -> list[str]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise argparse.ArgumentTypeError("json payload must be array of strings")
    return [x for x in value if x.strip()]


def _extract_patch_target_paths(patch_text: str) -> list[str]:
    targets: list[str] = []
    for raw in patch_text.splitlines():
        line = raw.strip()
        if not line.startswith("+++ "):
            continue
        token = line[4:].strip()
        if token == "/dev/null":
            continue
        if token.startswith("b/"):
            token = token[2:]
        norm = _normalize_rel_posix(token)
        if norm:
            targets.append(norm)
    return sorted(set(targets))


def guarded_apply_patch(
    repo_root: Path,
    *,
    orchestration_id: str,
    actor_role: str,
    agent_run_id: str,
    changed_paths: Sequence[str],
    patch_text: str,
    capability_token: str,
) -> dict[str, Any]:
    if not patch_text.strip():
        raise ValueError("patch_text must be non-empty")
    normalized_paths = [_normalize_rel_posix(p) for p in changed_paths if str(p).strip()]
    if not normalized_paths:
        raise ValueError("changed_paths must be non-empty")

    patch_targets = _extract_patch_target_paths(patch_text)
    if not patch_targets:
        raise ValueError("patch_text must include at least one '+++ <path>' target")
    not_covered = [
        p for p in patch_targets if not any(_repo_path_under_prefix(p, cp) for cp in normalized_paths)
    ]
    if not_covered:
        raise RuntimeError(
            "guarded-apply-patch: patch targets are not covered by changed_paths: "
            + ", ".join(not_covered)
        )

    gate_out = run_gate(
        repo_root,
        orchestration_id=orchestration_id,
        gate_name="apply_patch_writes",
        agent_run_id=agent_run_id,
        args_json={"actor_role": actor_role, "changed_paths": normalized_paths},
        capability_token=capability_token,
    )
    proc = subprocess.run(
        ["git", "apply", "--recount", "--whitespace=nowarn", "-"],
        cwd=str(repo_root),
        text=True,
        input=patch_text,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"guarded-apply-patch: git apply failed: {msg}")

    return {
        "applied": True,
        "changed_paths": normalized_paths,
        "patch_targets": patch_targets,
        "gate_result_ref": gate_out.get("gate_result_ref", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--repo-root", required=True)
    init_parser.add_argument("--orchestration-id", required=True)
    init_parser.add_argument("--spec-ref")
    init_parser.add_argument("--dependency-ref")
    init_parser.add_argument("--status", default="running")
    init_parser.add_argument(
        "--resume-from-checkpoint",
        action="store_true",
        help="Enable checkpoint resume on an existing orchestration (sets resume_enabled).",
    )

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--repo-root", required=True)
    preflight_parser.add_argument("--orchestration-id", required=True)
    preflight_parser.add_argument("--backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
    preflight_parser.add_argument("--agent-command")
    preflight_parser.add_argument("--codex-command", default="codex")
    preflight_parser.add_argument("--claude-command", default="claude")

    preflight_status_parser = subparsers.add_parser("preflight-status")
    preflight_status_parser.add_argument("--repo-root", required=True)
    preflight_status_parser.add_argument("--orchestration-id", required=True)

    launch_parser = subparsers.add_parser("record-launch")
    launch_parser.add_argument("--repo-root", required=True)
    launch_parser.add_argument("--orchestration-id", required=True)
    launch_parser.add_argument("--parent-agent-run-id", required=True)
    launch_parser.add_argument("--child-agent-run-id", required=True)
    launch_parser.add_argument("--request-json", required=True, type=_json_arg)
    launch_parser.add_argument("--response-json", required=True, type=_json_arg)
    launch_parser.add_argument("--relation-type", default="launch")

    orch_read_parser = subparsers.add_parser("orchestration-read")
    orch_read_parser.add_argument("--repo-root", required=True)
    orch_read_parser.add_argument("--orchestration-id", required=True)
    orch_read_parser.add_argument("--agent-run-id", required=True)
    orch_read_parser.add_argument("--read-path", required=True)
    orch_read_parser.add_argument("--capability-token", required=True)

    patch_gate_parser = subparsers.add_parser("apply-patch-gate")
    patch_gate_parser.add_argument("--repo-root", required=True)
    patch_gate_parser.add_argument("--orchestration-id", required=True)
    patch_gate_parser.add_argument("--actor-role", required=True)
    patch_gate_parser.add_argument("--agent-run-id", required=True)
    patch_gate_parser.add_argument("--paths-json", required=True, type=_json_string_list_arg)
    patch_gate_parser.add_argument("--capability-token", required=True)

    guarded_patch_parser = subparsers.add_parser("guarded-apply-patch")
    guarded_patch_parser.add_argument("--repo-root", required=True)
    guarded_patch_parser.add_argument("--orchestration-id", required=True)
    guarded_patch_parser.add_argument("--actor-role", required=True)
    guarded_patch_parser.add_argument("--agent-run-id", required=True)
    guarded_patch_parser.add_argument("--paths-json", required=True, type=_json_string_list_arg)
    guarded_patch_parser.add_argument("--patch-text", required=True)
    guarded_patch_parser.add_argument("--capability-token", required=True)

    gate_parser = subparsers.add_parser("run-gate")
    gate_parser.add_argument("--repo-root", required=True)
    gate_parser.add_argument("--orchestration-id", required=True)
    gate_parser.add_argument("--gate", required=True)
    gate_parser.add_argument("--agent-run-id", required=True)
    gate_parser.add_argument("--args-json", required=True, type=_json_arg)
    gate_parser.add_argument("--capability-token", required=True)

    run_parser = subparsers.add_parser("record-agent-run")
    run_parser.add_argument("--repo-root", required=True)
    run_parser.add_argument("--orchestration-id", required=True)
    run_parser.add_argument("--agent-run-json", required=True, type=_json_arg)

    step_parser = subparsers.add_parser("write-step-result")
    step_parser.add_argument("--repo-root", required=True)
    step_parser.add_argument("--orchestration-id", required=True)
    step_parser.add_argument("--node-key", required=True)
    step_parser.add_argument("--step", required=True)
    step_parser.add_argument("--agent-run-id", required=True)
    step_parser.add_argument("--result-json", required=True, type=_json_arg)

    status_parser = subparsers.add_parser("set-status")
    status_parser.add_argument("--repo-root", required=True)
    status_parser.add_argument("--orchestration-id", required=True)
    status_parser.add_argument("--status", required=True)
    status_parser.add_argument("--reason-code")
    status_parser.add_argument("--reason-detail")
    status_parser.add_argument("--blocking-policy-scope")

    launch_check_parser = subparsers.add_parser("workflow-launch-check")
    launch_check_parser.add_argument("--repo-root", required=True)
    launch_check_parser.add_argument("--orchestration-id", required=True)
    launch_check_parser.add_argument("--node-key", required=True)
    launch_check_parser.add_argument("--step", required=True)
    launch_check_parser.add_argument("--backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
    launch_check_parser.add_argument("--require-child-agent", required=True, choices=("step", "substep"))

    reserve_root_parser = subparsers.add_parser("reserve-phase-root")
    reserve_root_parser.add_argument("--repo-root", required=True)
    reserve_root_parser.add_argument("--orchestration-id", required=True)
    reserve_root_parser.add_argument("--node-key", required=True)
    reserve_root_parser.add_argument("--step", required=True)
    reserve_root_parser.add_argument("--reserved-id", required=True)
    reserve_root_parser.add_argument("--reserved-by-agent-run-id", required=True)

    read_cp_parser = subparsers.add_parser("read-checkpoint")
    read_cp_parser.add_argument("--repo-root", required=True)
    read_cp_parser.add_argument("--orchestration-id", required=True)

    verify_cp_parser = subparsers.add_parser("verify-checkpoint-integrity")
    verify_cp_parser.add_argument("--repo-root", required=True)
    verify_cp_parser.add_argument("--orchestration-id", required=True)

    check_step_parser = subparsers.add_parser("check-step-completed")
    check_step_parser.add_argument("--repo-root", required=True)
    check_step_parser.add_argument("--orchestration-id", required=True)
    check_step_parser.add_argument("--node-key", required=True)
    check_step_parser.add_argument("--step", required=True)
    check_step_parser.add_argument(
        "--skip-integrity-check",
        action="store_true",
        help="Skip artifact hash verification (testing only).",
    )

    args = parser.parse_args(argv)
    repo_root = Path(getattr(args, "repo_root")).resolve()

    if args.command == "init":
        if getattr(args, "resume_from_checkpoint", False):
            result = enable_checkpoint_resume(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
            )
        else:
            result = init_orchestration(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                spec_ref=args.spec_ref,
                dependency_ref=args.dependency_ref,
                status=args.status,
            )
    elif args.command == "preflight":
        agent_command = args.agent_command
        if not isinstance(agent_command, str) or not agent_command.strip():
            if args.backend == "codex":
                # Keep backward compatibility only for codex backend.
                agent_command = args.codex_command
            elif args.backend == "claude":
                agent_command = args.claude_command
        result = write_preflight(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            payload=probe_execution_platform(
                backend=args.backend,
                agent_command=agent_command,
            ),
        )
    elif args.command == "preflight-status":
        result = get_preflight_ttl_status(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
        )
    elif args.command == "record-launch":
        result = record_launch(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            parent_agent_run_id=args.parent_agent_run_id,
            child_agent_run_id=args.child_agent_run_id,
            request_payload=args.request_json,
            response_payload=args.response_json,
            relation_type=args.relation_type,
        )
    elif args.command == "orchestration-read":
        try:
            gate_out = run_gate(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                gate_name="orchestration_read",
                agent_run_id=args.agent_run_id,
                args_json={"read_path": args.read_path},
                capability_token=args.capability_token,
            )
            result = gate_out.get("result", {})
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "apply-patch-gate":
        try:
            gate_out = run_gate(
                repo_root,
                orchestration_id=args.orchestration_id,
                gate_name="apply_patch_writes",
                agent_run_id=args.agent_run_id,
                args_json={
                    "actor_role": args.actor_role,
                    "changed_paths": args.paths_json,
                },
                capability_token=args.capability_token,
            )
            result = gate_out.get("result", {})
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "guarded-apply-patch":
        try:
            result = guarded_apply_patch(
                repo_root,
                orchestration_id=args.orchestration_id,
                actor_role=args.actor_role,
                agent_run_id=args.agent_run_id,
                changed_paths=args.paths_json,
                patch_text=args.patch_text,
                capability_token=args.capability_token,
            )
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "run-gate":
        try:
            result = run_gate(
                repo_root,
                orchestration_id=args.orchestration_id,
                gate_name=args.gate,
                agent_run_id=args.agent_run_id,
                args_json=args.args_json,
                capability_token=args.capability_token,
            )
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "record-agent-run":
        result = record_agent_run(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            payload=args.agent_run_json,
        )
    elif args.command == "write-step-result":
        result = write_step_result(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            agent_run_id=args.agent_run_id,
            payload=args.result_json,
        )
    elif args.command == "read-checkpoint":
        loaded = read_checkpoint(repo_root=repo_root, orchestration_id=args.orchestration_id)
        result = (
            loaded
            if loaded is not None
            else {"orchestration_id": args.orchestration_id, "completed_steps": []}
        )
    elif args.command == "verify-checkpoint-integrity":
        result = verify_checkpoint_integrity(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
        )
    elif args.command == "check-step-completed":
        info = check_step_completed(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            verify_integrity=not args.skip_integrity_check,
        )
        if info:
            result = {"completed": True, **info}
        else:
            result = {
                "completed": False,
                "node_key": args.node_key,
                "step": args.step.strip().lower(),
            }
    elif args.command == "set-status":
        result = update_orchestration_status(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            status=args.status,
            reason_code=args.reason_code,
            reason_detail=args.reason_detail,
            blocking_policy_scope=args.blocking_policy_scope,
        )
    elif args.command == "workflow-launch-check":
        result = workflow_launch_check(
            repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            backend=args.backend,
            require_child_agent=args.require_child_agent,
        )
    elif args.command == "reserve-phase-root":
        result = reserve_phase_root(
            repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            reserved_id=args.reserved_id,
            reserved_by_agent_run_id=args.reserved_by_agent_run_id,
        )
    else:
        raise RuntimeError(f"unhandled command: {args.command}")

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
