#!/usr/bin/env python3
"""Helpers for workflow orchestration artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from tools.hooks.common import (
        _normalize_rel_posix,
        _utc_now_iso,
        _ALLOWED_BYPRODUCT_EXTENSIONS,
        _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES,
        _COMPILER_BYPRODUCT_EXTENSIONS,
        validate_pipeline_semantics_stage,
    )
    from tools.meta_contracts import (
        STAGE_META_FILENAME_BY_STEP,
        missing_required_meta_keys,
    )
except ModuleNotFoundError:  # pragma: no cover - import bootstrap for direct CLI execution
    _THIS_FILE = Path(__file__).resolve()
    _REPO_ROOT = _THIS_FILE.parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from tools.hooks.common import (
        _normalize_rel_posix,
        _utc_now_iso,
        _ALLOWED_BYPRODUCT_EXTENSIONS,
        _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES,
        _COMPILER_BYPRODUCT_EXTENSIONS,
        validate_pipeline_semantics_stage,
    )
    from tools.meta_contracts import (
        STAGE_META_FILENAME_BY_STEP,
        missing_required_meta_keys,
    )

TERMINAL_STATUSES = {"pass", "fail", "blocked", "timeout", "cancel"}
# Judge の pre_phase_complete 検証で semantic_review を要求しない終了理由（未完了扱い）。
JUDGE_SEMANTIC_REVIEW_SKIPPED_STATUSES = frozenset({"timeout", "cancel"})
SUPPORTED_BACKENDS = {"codex", "cursor", "claude"}
PREFLIGHT_TTL_DEFAULT_SECONDS: int = 1800
VALID_REPAIR_STRATEGIES = frozenset({"none", "reuse", "restart"})
VALID_ISSUE_SEVERITIES = frozenset({"none", "minor", "major", "critical"})

# Must match tools/validate_workspace_root.py (canonical pipeline/plan id directory naming).
_NODE_KEY_SAFE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$"
)
_SLUG_DATE_SEQ3_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$")
# Safe agent_run_id characters: alphanumerics, hyphens, underscores.
# Rejects path separators (/, \), dots (..), null bytes, and other traversal vectors.
_AGENT_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
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


def _active_child_agent_run_id_path(repo_root: Path, orchestration_id: str) -> Path:
    """Claude backend 専用の active child `agent_run_id` 管理ファイル。"""
    return _orchestration_root(repo_root, orchestration_id) / "active_child_agent_run_id.txt"


def _session_run_index_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "session_run_index.json"


def _read_session_run_index(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    path = _session_run_index_path(repo_root, orchestration_id)
    if not path.is_file():
        return {"entries": []}
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"entries": []}
    if not isinstance(payload, dict):
        return {"entries": []}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        payload["entries"] = []
    return payload


def _append_session_run_index_entry(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    agent_session_id: str,
    context_id: str | None,
    agent_role: str,
    status: str,
) -> None:
    doc = _read_session_run_index(repo_root, orchestration_id)
    entries_obj = doc.get("entries")
    entries = entries_obj if isinstance(entries_obj, list) else []
    normalized_run_id = agent_run_id.strip()
    normalized_session_id = agent_session_id.strip()
    normalized_context_id = context_id.strip() if isinstance(context_id, str) and context_id.strip() else None
    normalized_role = agent_role.strip().lower()
    normalized_status = status.strip().lower()
    for item in entries:
        if not isinstance(item, dict):
            continue
        if str(item.get("agent_run_id", "")).strip() != normalized_run_id:
            continue
        item["agent_session_id"] = normalized_session_id
        item["session_id"] = normalized_session_id
        item["context_id"] = normalized_context_id
        item["agent_role"] = normalized_role
        item["status"] = normalized_status
        item["updated_at"] = _utc_now_iso()
        _write_json(_session_run_index_path(repo_root, orchestration_id), doc)
        return
    entries.append(
        {
            "agent_run_id": normalized_run_id,
            "agent_session_id": normalized_session_id,
            "session_id": normalized_session_id,
            "context_id": normalized_context_id,
            "agent_role": normalized_role,
            "status": normalized_status,
            "recorded_at": _utc_now_iso(),
        }
    )
    doc["entries"] = entries
    _write_json(_session_run_index_path(repo_root, orchestration_id), doc)


# --- Phase 1: access policy / phase state artifact layout (Item 10) ---

DEFAULT_ALLOWED_GATE_SERVICES: tuple[str, ...] = (
    "validate_pipeline_semantics",
    "check_artifact_syntax",
    "validate_workspace_root",
    "orchestration_read",
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
    "downstream_artifact_not_ready",
    "checkpoint_read_forbidden_without_resume",
    "post_phase_complete_violation",
    "parallel_nodes_not_explicitly_allowed",
    "sandbox_enforcement_violation",
}

PARALLEL_NODES_ENV_VAR = "METDSL_ALLOW_PARALLEL_NODES"

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


def _output_manifests_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "output_manifests"


def _read_manifests_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "read_manifests"


def _sandbox_profiles_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "sandbox_profiles"


def _hooks_log_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "hooks" / "workflow_hooks.jsonl"


def _append_workflow_hook_log(
    repo_root: Path,
    orchestration_id: str,
    *,
    hook_name: str,
    status: str,
    detail: dict[str, Any],
) -> None:
    path = _hooks_log_path(repo_root, orchestration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {"ts": _utc_now_iso(), "hook": hook_name, "status": status, **detail}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _phase_state_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "phase_state.json"


def _phase_state_log_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "phase_state_log.jsonl"


def _ensure_orchestration_audit_dirs(repo_root: Path, orchestration_id: str) -> None:
    root = _orchestration_root(repo_root, orchestration_id)
    for sub in ("access_policies", "access_logs", "violations", "capabilities", "sandbox_profiles"):
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
        # pipeline_ref contains the unique pipeline_id (reserved by reserve-phase-root),
        # so lineage.json is exclusive to this run — no concurrent agent shares this path.
        # bwrap binds lineage.json's parent directory (not the file) so the agent can create
        # it; the file must not be pre-created before the agent writes it.
        return [
            _with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/generate")),
            _normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/lineage.json"),
        ]
    if st == "build":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/build"))]
    if st == "execute":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/execute"))]
    if st == "judge":
        # Judge artifacts are written under execute/<execution_id>/<node_key_safe>/.
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/execute"))]
    if st == "tune":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/tune"))]
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


def _write_sandbox_enforcement_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.sandbox_enforcement_violation.json"
    payload: dict[str, Any] = {
        "kind": "sandbox_enforcement_violation",
        "agent_run_id": agent_run_id,
        "reason": reason,
        "evaluated_at": _utc_now_iso(),
    }
    if isinstance(detail, dict):
        payload["detail"] = detail
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
    try:
        update_orchestration_status(
            repo_root,
            orchestration_id,
            status="fail_closed",
            reason_code="noncanonical_phase_write_attempt",
            reason_detail="; ".join(attempted_paths),
            blocking_policy_scope="apply_patch_writes",
        )
    except Exception:
        pass
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


def _resolve_judge_execution_dir(
    repo_root: Path,
    *,
    pipeline_ref: str,
    node_key: str,
    launch_request: dict[str, Any],
) -> tuple[Path | None, str | None]:
    """`Judge` 入力となる `execute/<execution_id>/<node_key_safe>/` を返す。失敗時は (None, reason)。"""
    rel = _normalize_rel_posix(pipeline_ref)
    pr_abs = repo_root / rel
    if not pr_abs.is_dir():
        return None, "pipeline_missing"
    nk_safe = _node_key_to_safe(node_key)
    ex_id = launch_request.get("execution_id")
    if isinstance(ex_id, str) and ex_id.strip():
        cand = pr_abs / "execute" / ex_id.strip() / nk_safe
        if cand.is_dir():
            return cand, None
        return None, "judge_execution_path_missing"
    exec_root = pr_abs / "execute"
    candidates: list[Path] = []
    if exec_root.is_dir():
        for eid_dir in sorted(exec_root.iterdir()):
            if not eid_dir.is_dir():
                continue
            cand = eid_dir / nk_safe
            if cand.is_dir():
                candidates.append(cand)
    if len(candidates) != 1:
        return None, "judge_execution_id_unresolved_or_ambiguous"
    return candidates[0], None


def _downstream_phase_launch_gate(
    repo_root: Path,
    *,
    node_key: str,
    step: str,
    pipeline_ref: str,
    launch_request: dict[str, Any],
) -> tuple[bool, str | None]:
    """`pipeline_ref` がディスク上に存在する場合のみ下流 phase 開始条件を検査する。"""
    rel = _normalize_rel_posix(pipeline_ref)
    pr_abs = repo_root / rel
    if not pr_abs.is_dir():
        return True, None
    st = step.strip().lower()
    if st == "build":
        gen_root = pr_abs / "generate"
        if not gen_root.is_dir():
            return False, "downstream:generate_dir_missing"
        for gen_dir in sorted(gen_root.iterdir()):
            if not gen_dir.is_dir():
                continue
            meta = gen_dir / "generate_meta.json"
            if not meta.is_file():
                continue
            try:
                data = _read_json(meta)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and str(data.get("verification_status", "")).strip().lower() == "pass":
                return True, None
        return False, "downstream:generate_meta_verification_status_not_pass"
    if st == "execute":
        build_root = pr_abs / "build"
        if not build_root.is_dir():
            return False, "downstream:build_dir_missing"
        for bdir in sorted(build_root.iterdir()):
            if not bdir.is_dir():
                continue
            bin_dir = bdir / "bin"
            if bin_dir.is_dir() and any(bin_dir.iterdir()):
                return True, None
        return False, "downstream:build_bin_dir_missing"
    if st == "judge":
        base, err = _resolve_judge_execution_dir(
            repo_root,
            pipeline_ref=pipeline_ref,
            node_key=node_key,
            launch_request=launch_request,
        )
        if base is None:
            return False, f"downstream:{err or 'judge_path'}"
        for name in ("diagnostics.json", "perf.json"):
            if not (base / name).is_file():
                return False, f"downstream:judge_missing:{name}"
        raw_dir = base / "raw"
        if not raw_dir.is_dir():
            return False, "downstream:judge_raw_dir_missing"
        exec_ok = (base / "mcp_command_log.jsonl").is_file() or (
            (base / "stdout.log").is_file() and (base / "stderr.log").is_file()
        )
        if not exec_ok:
            return False, "downstream:judge_execution_record_missing"
        return True, None
    return True, None


def pre_phase_launch(
    repo_root: Path,
    *,
    orchestration_id: str,
    node_key: str,
    step: str,
    backend: str,
    require_child_agent: str,
    launch_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`workflow_launch_check` と下流 artifact 開始条件（`pipeline_ref` がディスク上に存在する場合）をまとめる hook。"""
    base = workflow_launch_check(
        repo_root,
        orchestration_id=orchestration_id,
        node_key=node_key,
        step=step,
        backend=backend,
        require_child_agent=require_child_agent,
    )
    merged: dict[str, Any] = dict(base)
    merged["hook"] = "pre_phase_launch"
    if merged.get("status") != "pass":
        _append_workflow_hook_log(
            repo_root,
            orchestration_id,
            hook_name="pre_phase_launch",
            status="deny",
            detail={"reason": merged.get("reason_code"), "detail": merged.get("reason_detail")},
        )
        return merged
    if launch_request:
        pr = launch_request.get("pipeline_ref")
        if isinstance(pr, str) and pr.strip():
            ok, reason = _downstream_phase_launch_gate(
                repo_root,
                node_key=node_key,
                step=step,
                pipeline_ref=pr.strip(),
                launch_request=launch_request,
            )
            if not ok:
                merged["status"] = "fail_closed"
                merged["reason_code"] = "downstream_artifact_not_ready"
                merged["reason_detail"] = reason
                merged["next_action"] = "stop_before_phase_body"
                merged["blocking_policy_scope"] = "downstream_artifacts"
                _append_workflow_hook_log(
                    repo_root,
                    orchestration_id,
                    hook_name="pre_phase_launch",
                    status="deny",
                    detail={"reason": "downstream_artifact_not_ready", "detail": reason},
                )
                return merged
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_phase_launch",
        status="allow",
        detail={"node_key": node_key, "step": step.strip().lower()},
    )
    return merged


def pre_orchestration_start(
    repo_root: Path,
    orchestration_id: str,
    *,
    event: str,
) -> dict[str, Any]:
    """`init` / `preflight` 入口で冪等に適用する workflow 開始前 hook。"""
    ws = repo_root / "workspace"
    created_ws: str | None = None
    if not ws.exists():
        ws.mkdir(parents=True, exist_ok=True)
        created_ws = "created_workspace_root"
    parallel_explicit = os.environ.get(PARALLEL_NODES_ENV_VAR, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    orch_root = _orchestration_root(repo_root, orchestration_id)
    orch_root.mkdir(parents=True, exist_ok=True)
    meta_path = orch_root / "orchestration_meta.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = _read_json(meta_path)
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            meta = loaded
    meta.setdefault("parallel_nodes_explicit", parallel_explicit)
    meta.setdefault("parallel_nodes_policy", "sequential_default")
    parallel_nodes_explicit_persisted = meta["parallel_nodes_explicit"]
    _write_json(meta_path, meta)
    detail = {
        "event": event,
        "workspace_bootstrap": created_ws,
        "parallel_nodes_explicit": parallel_nodes_explicit_persisted,
    }
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_orchestration_start",
        status="allow",
        detail=detail,
    )
    return {"status": "pass", "hook": "pre_orchestration_start", **detail}


def _load_write_roots_from_cap(roots_obj: Any) -> list[str]:
    """Normalize write_roots from capability JSON at load time.

    Trailing-slash entries are directory roots. All other entries are file pins (exact match),
    including extensionless files like Makefile or LICENSE.
    """
    result: list[str] = []
    for item in (roots_obj if isinstance(roots_obj, list) else []):
        if not isinstance(item, str) or not item.strip():
            continue
        raw = item.strip()
        if raw.endswith("/"):
            result.append(_normalize_rel_posix(raw) + "/")
        else:
            result.append(_normalize_rel_posix(raw))  # file pin: exact match
    return result


# _ALLOWED_BYPRODUCT_EXTENSIONS and _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES are imported
# from tools.hooks.common — the single source of truth for pre-write and terminal policy.


def _path_under_any_write_root(rel_posix: str, write_roots: list[str]) -> bool:
    """Check whether rel_posix is authorized by any write_roots entry.

    write_roots must be pre-normalized by _load_write_roots_from_cap so that
    directory entries have trailing '/' and file pins are exact paths (with or without extension).
    """
    p = _normalize_rel_posix(rel_posix)
    for root in write_roots:
        if not isinstance(root, str) or not root.strip():
            continue
        normalized_root = _normalize_rel_posix(root)
        if not normalized_root:
            continue
        if root.strip().endswith("/"):
            # Directory entry: prefix match
            if _repo_path_under_prefix(p, normalized_root):
                return True
        else:
            # File pin: exact match only
            if p == normalized_root:
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
        roots = _load_write_roots_from_cap(roots_obj)
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
    mcp_args: dict[str, Any] | None = None,
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

    args_obj = mcp_args if isinstance(mcp_args, dict) else {}
    if tool_name == "run_program" and step_key == "execute":
        cmd = args_obj.get("command")
        if not isinstance(cmd, list) or not cmd:
            raise RuntimeError("MCP phase gate: run_program requires non-empty command array")
        joined = " ".join(str(x) for x in cmd)
        if "case.resolved.yaml" not in joined:
            raise RuntimeError(
                "MCP phase gate: Execute run_program command must reference case.resolved.yaml"
            )
    if tool_name in {"compile_project", "run_quality_checks"}:
        plan_ref = _launch_plan_ref_for_agent(repo_root, orchestration_id, agent_run_id)
        if plan_ref:
            bs = _impl_resolved_build_system(repo_root, plan_ref)
            if bs == "make":
                if tool_name == "compile_project":
                    req_bs = str(args_obj.get("build_system", "")).strip().lower()
                    if req_bs and req_bs != "make":
                        raise RuntimeError(
                            "MCP phase gate: toolchain.build_system=make requires compile_project "
                            f"build_system make (got {req_bs!r})"
                        )
                if tool_name == "run_quality_checks":
                    preset = str(args_obj.get("preset", "")).strip().lower()
                    if preset not in {"make_test", "make_check"}:
                        raise RuntimeError(
                            "MCP phase gate: toolchain.build_system=make requires run_quality_checks "
                            f"preset make_test or make_check (got {preset!r})"
                        )

    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_command_execute",
        status="allow",
        detail={"mcp_tool": tool_name, "step": step_key},
    )


def _launch_plan_ref_for_agent(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> str | None:
    req_path = _orchestration_root(repo_root, orchestration_id) / "launches" / f"{agent_run_id.strip()}.request.json"
    if not req_path.is_file():
        return None
    try:
        doc = _read_json(req_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    pr = doc.get("plan_ref")
    return pr.strip() if isinstance(pr, str) and pr.strip() else None


def _impl_resolved_build_system(repo_root: Path, plan_ref: str) -> str | None:
    path = repo_root / _normalize_rel_posix(plan_ref) / "impl.resolved.yaml"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        if "build_system" not in key.strip().lower():
            continue
        val = rest.strip().strip("\"'")
        return val.lower() or None
    return None


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


_CHECK_ARTIFACT_SYNTAX_EXPECT_TOP_ALLOWED = frozenset({"object", "array"})


def _validate_check_artifact_syntax_args(args_json: dict[str, Any]) -> None:
    paths_value = args_json.get("paths")
    if "path" in args_json:
        raise ValueError(
            "check_artifact_syntax args-json requires 'paths' (list[str]); "
            "single 'path' is unsupported"
        )
    if not isinstance(paths_value, list):
        raise ValueError("check_artifact_syntax args-json requires key 'paths' as list[str]")
    if not paths_value:
        raise ValueError("check_artifact_syntax args-json paths must be a non-empty list")
    for idx, item in enumerate(paths_value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"check_artifact_syntax args-json paths[{idx}] must be non-empty string"
            )

    expect_top = args_json.get("expect_top")
    if expect_top is None:
        return
    if not isinstance(expect_top, str) or expect_top.strip() not in _CHECK_ARTIFACT_SYNTAX_EXPECT_TOP_ALLOWED:
        raise ValueError(
            "check_artifact_syntax args-json expect_top must be one of "
            f"{sorted(_CHECK_ARTIFACT_SYNTAX_EXPECT_TOP_ALLOWED)!r}"
        )


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
    raise ValueError(f"unsupported inline gate name: {gate_name!r}")


def _gate_python_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["PYTHONPYCACHEPREFIX"] = str((repo_root / "workspace" / ".pycache").resolve())
    return env


def _pre_command_execute_validate_pipeline_semantics(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    args_json: dict[str, Any],
) -> None:
    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    cap = _read_json(cap_path)
    if not isinstance(cap, dict):
        return
    step_key = str(cap.get("step", "")).strip().lower()
    stage_l = validate_pipeline_semantics_stage(step_key=step_key, args_json=args_json)
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_command_execute",
        status="allow",
        detail={"gate": "validate_pipeline_semantics", "stage": stage_l, "step": step_key},
    )


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
    if gate == "validate_pipeline_semantics":
        _pre_command_execute_validate_pipeline_semantics(
            repo_root,
            orchestration_id,
            agent_run_id,
            args_json,
        )

    arg_validation_error: str | None = None
    if gate == "check_artifact_syntax":
        try:
            _validate_check_artifact_syntax_args(args_json)
        except ValueError as exc:
            arg_validation_error = str(exc)

    inline_result: dict[str, Any] | None = None
    if arg_validation_error is not None:
        violations = [f"args-json validation failed: {arg_validation_error}"]
        status = "fail"
        exit_code = 2
    elif gate == "orchestration_read":
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
        gate_env = _gate_python_env(repo_root)
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
    if arg_validation_error is not None:
        gate_doc["arg_validation_error"] = arg_validation_error
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


def _write_apply_patch_gate_evidence(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    actor_role: str,
    changed_paths: Sequence[str],
    result_payload: dict[str, Any],
) -> str:
    gate = "apply_patch_writes"
    latest_changed_paths = _normalize_rel_path_list([str(p) for p in changed_paths if str(p).strip()])
    cumulative_changed_paths = _update_cumulative_gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
        changed_paths=latest_changed_paths,
    )
    gate_doc: dict[str, Any] = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "gate": gate,
        "args_json": {
            "actor_role": actor_role,
            "changed_paths": cumulative_changed_paths,
            "latest_changed_paths": latest_changed_paths,
        },
        "status": "pass",
        "exit_code": 0,
        "violations": [],
        "evaluated_at": _utc_now_iso(),
        "result": result_payload,
    }
    out_path = _gates_dir(repo_root, orchestration_id) / agent_run_id.strip() / f"{gate}.json"
    _write_json(out_path, gate_doc)
    return f"workspace/orchestrations/{orchestration_id}/gates/{agent_run_id.strip()}/{gate}.json"


def _allowed_output_manifest_path(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
) -> Path:
    return _output_manifests_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"


def _write_allowed_output_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    allowed_output_paths: Sequence[str],
    allowed_file_tool_paths: Sequence[str] | None = None,
    agent_role: str | None = None,
    allowed_tmp_root: str | None = None,
) -> str:
    normalized = []
    for p in allowed_output_paths:
        if not isinstance(p, str) or not p.strip():
            continue
        raw = p.strip()
        if raw.endswith("/"):
            normalized.append(_normalize_rel_posix(raw) + "/")
        else:
            normalized.append(_normalize_rel_posix(raw))
    file_tool_normalized = [
        _normalize_rel_posix(p)
        for p in (allowed_file_tool_paths or [])
        if isinstance(p, str) and p.strip()
    ]
    payload = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id.strip(),
        "allowed_output_paths": sorted(set(normalized)),
        "allowed_file_tool_paths": sorted(set(file_tool_normalized)),
        "generated_at": _utc_now_iso(),
    }
    if isinstance(allowed_tmp_root, str) and allowed_tmp_root.strip():
        payload["allowed_tmp_root"] = _normalize_rel_posix(allowed_tmp_root.strip())
    if isinstance(agent_role, str) and agent_role.strip():
        payload["agent_role"] = agent_role.strip()
    out_path = _allowed_output_manifest_path(repo_root, orchestration_id, agent_run_id)
    _write_json(out_path, payload)
    return f"workspace/orchestrations/{orchestration_id}/output_manifests/{agent_run_id.strip()}.json"


def _load_allowed_output_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> dict[str, Any]:
    path = _allowed_output_manifest_path(repo_root, orchestration_id, agent_run_id)
    if not path.exists():
        raise ValueError(f"allowed_output_paths manifest not found: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"allowed_output_paths manifest must be object: {path}")
    return payload


def _validate_paths_against_allowed_output_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    paths: Sequence[str],
) -> None:
    manifest = _load_allowed_output_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
    )
    allowed_obj = manifest.get("allowed_output_paths")
    if not isinstance(allowed_obj, list) or not all(isinstance(x, str) for x in allowed_obj):
        raise ValueError("allowed_output_paths manifest must include string array allowed_output_paths")
    allowed_files: set[str] = set()
    allowed_dirs: list[str] = []
    for p in allowed_obj:
        if not isinstance(p, str) or not p.strip():
            continue
        raw_p = p.strip()
        if raw_p.endswith("/"):
            allowed_dirs.append(_normalize_rel_posix(raw_p))
        else:
            allowed_files.add(_normalize_rel_posix(raw_p))
    if not allowed_files and not allowed_dirs:
        raise ValueError("allowed_output_paths manifest must include non-empty allowed_output_paths")
    tmp_root_raw = manifest.get("allowed_tmp_root", "")
    tmp_norm = ""
    tmp_prefix = ""
    if isinstance(tmp_root_raw, str) and tmp_root_raw.strip():
        tmp_norm = _normalize_rel_posix(tmp_root_raw.strip())
        tmp_prefix = tmp_norm + "/"
    denied: list[str] = []
    invalid_paths: list[str] = []
    for raw in paths:
        rel = _normalize_rel_posix(str(raw))
        if not rel:
            invalid_paths.append(str(raw))
            continue
        if rel in allowed_files:
            continue
        if allowed_dirs and any(_repo_path_under_prefix(rel, d) for d in allowed_dirs):
            # Apply same extension policy as terminal validation — fail before mutation.
            ext = os.path.splitext(rel)[1].lower()
            if ext in _ALLOWED_BYPRODUCT_EXTENSIONS:
                continue
            if ext == "" and os.path.basename(rel).lower() in _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES:
                continue
            denied.append(rel)
            continue
        if tmp_prefix and (rel == tmp_norm or rel.startswith(tmp_prefix)):
            continue
        denied.append(rel)
    if denied or invalid_paths:
        details = [*denied, *[f"<invalid:{token}>" for token in invalid_paths]]
        raise ValueError("allowed_output_paths manifest violation: " + ", ".join(details))


def _allowed_output_paths_for_launch(
    *,
    request_payload: dict[str, Any],
    write_roots: Sequence[str],
) -> list[str]:
    role = str(request_payload.get("agent_role") or "").strip().lower()
    if role not in {"step", "substep"}:
        return [
            _normalize_rel_posix(item)
            for item in write_roots
            if isinstance(item, str) and item.strip()
        ]
    raw_candidates = (
        request_payload.get("allowed_output_paths")
        or request_payload.get("required_outputs")
        or request_payload.get("output_refs")
    )
    if not isinstance(raw_candidates, list):
        raise ValueError(
            "record-launch requires explicit allowed_output_paths list for step/substep agents"
        )
    allowed: list[str] = []
    for idx, item in enumerate(raw_candidates):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"allowed_output_paths[{idx}] must be non-empty string")
        token_raw = item.strip().replace("\\", "/")
        if token_raw.endswith("/"):
            # Directory allowlist entry: stored with trailing slash preserved
            token = _normalize_rel_posix(token_raw.rstrip("/")) + "/"
        else:
            token = _normalize_rel_posix(token_raw)
        if not token or token == "/":
            raise ValueError(f"allowed_output_paths[{idx}] must be valid relative path")
        allowed.append(token)
    normalized_roots = [
        _normalize_rel_posix(root)
        for root in write_roots
        if isinstance(root, str) and str(root).strip()
    ]
    step_token = str(request_payload.get("step") or "").strip().lower()
    plan_ref = _normalize_rel_posix(str(request_payload.get("plan_ref") or ""))
    pipeline_ref = _normalize_rel_posix(str(request_payload.get("pipeline_ref") or ""))
    node_key = str(request_payload.get("node_key") or "").strip()
    node_safe = _node_key_to_safe(node_key) if node_key else ""
    plan_required = {
        f"{plan_ref}/case.resolved.yaml",
        f"{plan_ref}/algorithm.resolved.yaml",
        f"{plan_ref}/impl.resolved.yaml",
        f"{plan_ref}/dependency.resolved.yaml",
        f"{plan_ref}/derived_contract.json",
        f"{plan_ref}/algorithm.summary.md",
        f"{plan_ref}/plan_meta.json",
    } if plan_ref else set()
    generate_prefix = f"{pipeline_ref}/generate/" if pipeline_ref else ""
    build_prefix = f"{pipeline_ref}/build/" if pipeline_ref else ""
    execute_prefix = f"{pipeline_ref}/execute/" if pipeline_ref else ""
    tune_prefix = f"{pipeline_ref}/tune/" if pipeline_ref else ""

    def _matches_phase_contract(path: str) -> bool:
        # Directory allowlist entries (trailing slash): validate the directory itself is permitted.
        if path.endswith("/"):
            dir_path = path.rstrip("/")
            if step_token == "generate":
                if generate_prefix and dir_path.startswith(generate_prefix):
                    # Allow only <gen_id>/src and its subdirectories; the generate root itself is forbidden
                    tail = dir_path[len(generate_prefix):]
                    parts = [seg for seg in tail.split("/") if seg]
                    if len(parts) >= 2 and parts[1] == "src":
                        return True
            return False
        if step_token == "plan":
            return path in plan_required
        if step_token == "generate":
            if pipeline_ref and path == f"{pipeline_ref}/lineage.json":
                return True
            if generate_prefix and path.startswith(generate_prefix):
                if "/src/" in path:
                    return True
                if path.endswith("/generate_meta.json"):
                    return True
            return False
        if step_token == "build":
            if build_prefix and path.startswith(build_prefix):
                return "/bin/" in path or path.endswith("/build_meta.json")
            return False
        if step_token == "execute":
            if not execute_prefix or not node_safe:
                return False
            if not path.startswith(execute_prefix):
                return False
            tail = path[len(execute_prefix):]
            tail_parts = [part for part in tail.split("/") if part]
            # execute contract must be under execute/<execution_id>/<node_safe>/...
            if len(tail_parts) < 3 or tail_parts[1] != node_safe:
                return False
            rel_under_node = "/".join(tail_parts[2:])
            allowed_files = {
                "diagnostics.json",
                "perf.json",
                "quality_check.json",
                "verdict.json",
                "aggregate_verdict.json",
                "summary.json",
                "semantic_review.json",
                "trial_meta.json",
                "stdout.log",
                "stderr.log",
                "metrics_basis.json",
                "execution_trace.json",
            }
            return rel_under_node in allowed_files or rel_under_node.startswith("raw/")
        if step_token == "judge":
            if not execute_prefix or not node_safe:
                return False
            if not path.startswith(execute_prefix):
                return False
            tail = path[len(execute_prefix):]
            tail_parts = [part for part in tail.split("/") if part]
            # judge contract must be under execute/<execution_id>/<node_safe>/...
            if len(tail_parts) < 3 or tail_parts[1] != node_safe:
                return False
            rel_under_node = "/".join(tail_parts[2:])
            allowed_files = {
                "semantic_review.json",
                "verdict.json",
                "aggregate_verdict.json",
                "summary.json",
                "trial_meta.json",
            }
            return rel_under_node in allowed_files
        if step_token == "tune":
            if not tune_prefix or not path.startswith(tune_prefix):
                return False
            rel_under_tune = path[len(tune_prefix):]
            rel_parts = [part for part in rel_under_tune.split("/") if part]
            # tune contract must be tune/<trial_id>/<artifact>; deeper nesting is forbidden.
            if len(rel_parts) != 2:
                return False
            allowed_files = {
                "impl.resolved.yaml",
                "diagnostics.json",
                "perf.json",
                "verdict.json",
                "trial_meta.json",
                "evaluation.json",
                "tune_meta.json",
            }
            base = rel_parts[1]
            return (
                base in allowed_files
                or base.endswith("_meta.json")
            )
        return False

    for idx, path in enumerate(allowed):
        if normalized_roots and not any(_repo_path_under_prefix(path, root) for root in normalized_roots):
            raise ValueError(
                f"allowed_output_paths[{idx}] must be under capability write_roots: {path!r}"
            )
        if not _matches_phase_contract(path):
            raise ValueError(
                f"allowed_output_paths[{idx}] is outside phase contract outputs for step={step_token!r}: {path!r}"
            )
    deduped: list[str] = []
    seen: set[str] = set()
    for path in allowed:
        if path in seen:
            continue
        deduped.append(path)
        seen.add(path)
    if not deduped:
        raise ValueError("allowed_output_paths must be non-empty for step/substep agents")
    return deduped


# Extension classification for write-path policy:
# `.json` / `.txt` outputs must go through `guarded-apply-patch` CLI for
# audit/integrity, while other artifact extensions (yaml, md, source code,
# etc.) are written via the LLM `Edit` / `Write` tools directly.
CLI_MANAGED_EXTENSIONS: frozenset[str] = frozenset({".json", ".txt"})


def _is_direct_write_path(rel_posix: str) -> bool:
    """Return True when the path may be written via direct Edit/Write tools.

    Paths whose extension belongs to ``CLI_MANAGED_EXTENSIONS`` (e.g. `.json`,
    `.txt`) are required to go through `guarded-apply-patch` and are therefore
    excluded from direct write.
    """
    token = _normalize_rel_posix(rel_posix)
    if not token:
        return False
    last = token.rsplit("/", 1)[-1]
    if "." not in last:
        return True
    ext = "." + last.rsplit(".", 1)[-1].lower()
    return ext not in CLI_MANAGED_EXTENSIONS


def _allowed_file_tool_paths_for_launch(
    *,
    request_payload: dict[str, Any],
    allowed_output_paths: Sequence[str],
) -> list[str]:
    raw = request_payload.get("allowed_file_tool_paths")
    # Exclude directory entries (trailing "/") from allowed_set: _normalize_rel_posix strips the
    # slash, which would make directory paths appear extension-free and pass _is_direct_write_path.
    allowed_set = {
        _normalize_rel_posix(str(item))
        for item in allowed_output_paths
        if isinstance(item, str) and item.strip() and not item.strip().endswith("/")
    }
    if raw is None:
        # Auto-derive: every output path whose extension is not CLI-managed
        # is permitted to be written via direct Edit/Write tools.
        derived = {path for path in allowed_set if path and _is_direct_write_path(path)}
        return sorted(derived)
    if not isinstance(raw, list):
        raise ValueError("allowed_file_tool_paths must be a list when provided")
    normalized: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"allowed_file_tool_paths[{idx}] must be non-empty string")
        item_token = item.strip().replace("\\", "/")
        if item_token.endswith("/"):
            raise ValueError(f"allowed_file_tool_paths[{idx}] must be file path: {item!r}")
        path = _normalize_rel_posix(item_token)
        if not _is_direct_write_path(path):
            raise ValueError(
                f"allowed_file_tool_paths[{idx}] must not include CLI-managed extensions "
                f"{sorted(CLI_MANAGED_EXTENSIONS)!r}: {path!r}"
            )
        if path not in allowed_set:
            raise ValueError(
                f"allowed_file_tool_paths[{idx}] must be included in allowed_output_paths: {path!r}"
            )
        normalized.append(path)
    return sorted(set(normalized))


def _validate_child_write_contract_preflight(
    *,
    request_payload: dict[str, Any],
    capability_doc: dict[str, Any],
    allowed_output_paths: Sequence[str],
) -> None:
    role = str(request_payload.get("agent_role") or "").strip().lower()
    if role not in {"step", "substep"}:
        return
    cap_token = str(capability_doc.get("capability_token") or "").strip()
    if not cap_token:
        raise ValueError("child_write_contract_preflight: capability_token must be non-empty")
    roots_obj = capability_doc.get("write_roots")
    if not isinstance(roots_obj, list):
        raise ValueError("child_write_contract_preflight: capability write_roots must be list")
    roots = [_normalize_rel_posix(str(item)) for item in roots_obj if isinstance(item, str) and item.strip()]
    allowed = [_normalize_rel_posix(str(item)) for item in allowed_output_paths if isinstance(item, str) and item.strip()]
    if not allowed:
        raise ValueError("child_write_contract_preflight: allowed_output_paths must be non-empty")
    for idx, path in enumerate(allowed):
        if path.endswith("/"):
            # Directory allowlist entry — check it is under a write root (using the dir path itself).
            if roots and not any(_repo_path_under_prefix(path, root) for root in roots):
                raise ValueError(
                    "child_write_contract_preflight: allowed_output_paths directory entry must be under "
                    f"capability write_roots: {path!r}"
                )
            continue
        if roots and not any(_repo_path_under_prefix(path, root) for root in roots):
            raise ValueError(
                "child_write_contract_preflight: allowed_output_path must be under capability write_roots: "
                f"{path!r}"
            )



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
        _with_trailing_slash(_normalize_rel_posix(f"workspace/tmp/{agent_run_id.strip()}")),
        _with_trailing_slash(_normalize_rel_posix(plan_ref)),
        _with_trailing_slash(_normalize_rel_posix(pipeline_ref)),
    ]
    skill_must_read_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))
    skill_ref = request_payload.get("skill_ref")
    if isinstance(skill_ref, str) and skill_ref.strip():
        skill_must_read_refs = _merge_unique_refs([skill_ref.strip()], skill_must_read_refs)
    skill_allowed_roots = [
        _with_trailing_slash(_normalize_rel_posix(ref))
        for ref in skill_must_read_refs
        if isinstance(ref, str) and ref.strip()
    ]
    allowed_read_roots = _merge_unique_refs(allowed_read_roots, skill_allowed_roots)
    orchestration_id_val = str(request_payload.get("orchestration_id", "")).strip()
    if orchestration_id_val:
        cap_file = (
            f"workspace/orchestrations/{orchestration_id_val}"
            f"/capabilities/{agent_run_id}.json"
        )
        allowed_read_roots = _merge_unique_refs(allowed_read_roots, [cap_file])
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


def _read_access_manifest_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> Path:
    return _read_manifests_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"


def _write_read_access_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    allowed_read_roots: Sequence[str],
    denied_read_roots: Sequence[str],
) -> str:
    payload = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id.strip(),
        "allowed_read_roots": [
            _with_trailing_slash(_normalize_rel_posix(p))
            for p in allowed_read_roots
            if isinstance(p, str) and p.strip()
        ],
        "denied_read_roots": [
            _with_trailing_slash(_normalize_rel_posix(p))
            for p in denied_read_roots
            if isinstance(p, str) and p.strip()
        ],
        "generated_at": _utc_now_iso(),
    }
    out = _read_access_manifest_path(repo_root, orchestration_id, agent_run_id=agent_run_id)
    _write_json(out, payload)
    return f"workspace/orchestrations/{orchestration_id}/read_manifests/{agent_run_id.strip()}.json"


def _load_read_access_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> dict[str, Any]:
    path = _read_access_manifest_path(repo_root, orchestration_id, agent_run_id=agent_run_id)
    if not path.exists():
        raise FileNotFoundError(f"read access manifest not found: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"read access manifest must be object: {path}")
    return payload


def _runtime_ro_bind_paths() -> list[str]:
    runtime_paths = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
    return [p for p in runtime_paths if Path(p).exists()]


def _safe_host_env_for_child() -> dict[str, str]:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME")
    body: dict[str, str] = {}
    for key in allowed:
        value = os.environ.get(key)
        if isinstance(value, str) and value:
            body[key] = value
    body.setdefault("PATH", "/usr/bin:/bin")
    return body


def build_bwrap_profile(
    *,
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    backend_command: str,
) -> dict[str, Any]:
    read_manifest = _load_read_access_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
    )
    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id}.json"
    if not cap_path.exists():
        raise ValueError(f"capability file not found: {cap_path}")
    cap_payload = _read_json(cap_path)
    if not isinstance(cap_payload, dict):
        raise ValueError(f"capability file must be object: {cap_path}")
    reads_obj = read_manifest.get("allowed_read_roots")
    if not isinstance(reads_obj, list):
        raise ValueError("read manifest must include allowed_read_roots list")
    writes_obj = cap_payload.get("write_roots")
    if not isinstance(writes_obj, list):
        raise ValueError("capability must include write_roots list")
    read_roots = sorted(
        {
            _normalize_rel_posix(str(p))
            for p in reads_obj
            if isinstance(p, str) and _normalize_rel_posix(str(p))
        }
    )
    write_roots = _load_write_roots_from_cap(writes_obj)
    resolved_repo_root = repo_root.resolve()
    created_file_pin_stubs: list[dict[str, Any]] = []
    for root_entry in write_roots:
        if root_entry.endswith("/"):
            candidate = (repo_root / root_entry.rstrip("/")).resolve()
            try:
                candidate.relative_to(resolved_repo_root)
            except ValueError:
                raise ValueError(
                    f"write_roots entry {root_entry!r} resolves outside repo_root "
                    f"({candidate} is not under {resolved_repo_root})"
                )
            candidate.mkdir(parents=True, exist_ok=True)
        else:
            # File pin: pre-create so bwrap can --bind it at file granularity.
            # File-level bind ensures bwrap cannot write to sibling files/directories —
            # the sandbox boundary is exactly the declared pin, nothing broader.
            # The stub is created empty here; _cleanup_empty_file_pin_stubs removes it
            # if the agent terminates without writing to it.
            pin_path = (repo_root / root_entry).resolve()
            try:
                pin_path.relative_to(resolved_repo_root)
            except ValueError:
                raise ValueError(
                    f"write_roots entry {root_entry!r} resolves outside repo_root "
                    f"({pin_path} is not under {resolved_repo_root})"
                )
            pin_path.parent.mkdir(parents=True, exist_ok=True)
            # Check the original (unresolved) path for symlinks — resolve() follows
            # symlinks so is_symlink() on the resolved path is always False.
            orig_pin_path = repo_root / _normalize_rel_posix(root_entry)
            if orig_pin_path.is_symlink():
                raise ValueError(
                    f"write_roots file pin {root_entry!r} is a symlink ({orig_pin_path}); "
                    f"only regular files are permitted as file pins"
                )
            if pin_path.exists():
                # Reject if the resolved path is a directory — binding it via bwrap
                # would expose the entire subtree as writable.
                if pin_path.is_dir():
                    raise ValueError(
                        f"write_roots file pin {root_entry!r} resolves to a directory ({pin_path}); "
                        f"add a trailing '/' to declare a directory write root instead"
                    )
            else:
                pin_path.touch()
                # Record path + mtime_ns so cleanup can distinguish an untouched stub
                # from a legitimately empty file written by a subprocess after touch().
                created_file_pin_stubs.append({
                    "path": _normalize_rel_posix(root_entry),
                    "mtime_ns": pin_path.stat().st_mtime_ns,
                })
    sandbox_root = _orchestration_root(repo_root, orchestration_id) / "sandboxes" / agent_run_id
    tmp_root = sandbox_root / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workspace_tmp_host = (repo_root / "workspace" / "tmp" / agent_run_id).resolve()
    workspace_tmp_host.mkdir(parents=True, exist_ok=True)
    child_env = _safe_host_env_for_child()
    child_env["TMPDIR"] = str(workspace_tmp_host)
    return {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "sandbox_runtime": "bwrap",
        "backend_command": backend_command.strip(),
        "repo_root": str(repo_root),
        "read_roots": read_roots,
        "write_roots": write_roots,
        "runtime_ro_bind_paths": _runtime_ro_bind_paths(),
        "tmp_dir": str(tmp_root),
        "workspace_tmp_rw_abs": str(workspace_tmp_host),
        "workdir": str(repo_root),
        "env": child_env,
        "generated_at": _utc_now_iso(),
        "created_file_pin_stubs": created_file_pin_stubs,
    }


def render_bwrap_command(
    *,
    profile: dict[str, Any],
    command_argv: Sequence[str],
) -> list[str]:
    if not command_argv:
        raise ValueError("command_argv must be non-empty")
    repo_root = str(profile.get("repo_root") or "").strip()
    tmp_dir = str(profile.get("tmp_dir") or "").strip()
    if not repo_root or not tmp_dir:
        raise ValueError("profile must include repo_root and tmp_dir")
    cmd: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        repo_root,
    ]
    for item in profile.get("runtime_ro_bind_paths", []):
        if isinstance(item, str) and item.strip():
            cmd.extend(["--ro-bind", item.strip(), item.strip()])
    cmd.extend(["--ro-bind", repo_root, repo_root])
    for rel in profile.get("read_roots", []):
        if not isinstance(rel, str) or not rel.strip():
            continue
        abs_path = (Path(repo_root) / _normalize_rel_posix(rel)).resolve()
        if abs_path.exists():
            abs_token = str(abs_path)
            cmd.extend(["--ro-bind", abs_token, abs_token])
    for rel in profile.get("write_roots", []):
        if not isinstance(rel, str) or not rel.strip():
            continue
        abs_path = (Path(repo_root) / _normalize_rel_posix(rel)).resolve()
        if not abs_path.exists():
            # File pins must be pre-created by build_bwrap_profile before render.
            if not rel.strip().endswith("/"):
                raise ValueError(
                    f"write_roots file pin {rel!r} does not exist; "
                    f"build_bwrap_profile must pre-create it before render_bwrap_command is called"
                )
            continue
        # File pins must be plain regular files — not directories or symlinks.
        # Binding a directory would make the entire subtree writable, not just one file.
        # Check the original (unresolved) path for symlinks: resolve() follows symlinks
        # so is_symlink() on abs_path (resolved) is always False.
        if not rel.strip().endswith("/"):
            orig_path = Path(repo_root) / _normalize_rel_posix(rel)
            if orig_path.is_symlink():
                raise ValueError(
                    f"write_roots file pin {rel!r} is a symlink ({orig_path}); "
                    f"only regular files are permitted as file pins"
                )
            if not abs_path.is_file():
                raise ValueError(
                    f"write_roots file pin {rel!r} resolves to a non-file ({abs_path}); "
                    f"add a trailing '/' to declare a directory write root instead"
                )
        abs_token = str(abs_path)
        cmd.extend(["--bind", abs_token, abs_token])
    ws_rw = str(profile.get("workspace_tmp_rw_abs") or "").strip()
    if not ws_rw:
        raise ValueError("profile must include workspace_tmp_rw_abs")
    ws_path = Path(ws_rw)
    if not ws_path.is_dir():
        raise ValueError(f"workspace_tmp_rw_abs must be existing directory: {ws_rw}")
    ws_abs = str(ws_path.resolve())
    cmd.extend(["--bind", ws_abs, ws_abs])
    cmd.extend(["--setenv", "TMPDIR", ws_abs])
    cmd.extend(["--bind", tmp_dir, tmp_dir])
    cmd.append("--")
    cmd.extend([str(part) for part in command_argv])
    return cmd


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
    manifest = _load_read_access_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
    )
    denied = manifest.get("denied_read_roots")
    if not isinstance(denied, list):
        denied = []
    allowed = manifest.get("allowed_read_roots")
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


def _run_write_baseline_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> Path:
    if agent_run_id is not None and agent_run_id.strip():
        return (
            _orchestration_root(repo_root, orchestration_id)
            / "agents"
            / agent_run_id.strip()
            / "run_write_baseline.json"
        )
    return _orchestration_root(repo_root, orchestration_id) / "orchestration_run_write_baseline.json"


def _should_ignore_runtime_snapshot_path(
    rel_posix: str,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> bool:
    token = _normalize_rel_posix(rel_posix)
    if not token or token.startswith(".git/"):
        return True
    # Ignore Claude local/runtime settings mutated by system-level hooks.
    if token.startswith(".claude/"):
        return True
    orch_root = _normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}")
    runtime_prefixes = (
        f"{orch_root}/access_logs/",
        f"{orch_root}/access_policies/",
        f"{orch_root}/agents/",
        f"{orch_root}/capabilities/",
        f"{orch_root}/gates/",
        f"{orch_root}/hooks/",
        f"{orch_root}/launches/",
        f"{orch_root}/output_manifests/",
        f"{orch_root}/read_manifests/",
        f"{orch_root}/sandbox_profiles/",
        f"{orch_root}/sandboxes/",
        f"{orch_root}/violations/",
        f"{orch_root}/steps/",
        f"{orch_root}/reservations/",
    )
    if any(token.startswith(prefix) for prefix in runtime_prefixes):
        return True
    runtime_files = {
        f"{orch_root}/agent_graph.json",
        f"{orch_root}/agent_runs.jsonl",
        f"{orch_root}/orchestration_meta.json",
        f"{orch_root}/orchestration_checkpoint.json",
        f"{orch_root}/active_child_agent_run_id.txt",
        f"{orch_root}/phase_state.json",
        f"{orch_root}/phase_state_log.jsonl",
        f"{orch_root}/preflight.json",
        f"{orch_root}/orchestration_run_write_baseline.json",
        f"{orch_root}/session_run_index.json",
    }
    return token in runtime_files


def _snapshot_repo_files(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = _normalize_rel_posix(path.relative_to(repo_root).as_posix())
        if _should_ignore_runtime_snapshot_path(
            rel,
            orchestration_id=orchestration_id,
            agent_run_id=agent_run_id,
        ):
            continue
        snapshot[rel] = _compute_sha256(path)
    return snapshot


def _write_run_write_baseline(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    run_id = agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else "orchestration"
    payload = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else None,
        "created_at": _utc_now_iso(),
        "files": _snapshot_repo_files(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=run_id,
        ),
    }
    _write_json(
        _run_write_baseline_path(repo_root, orchestration_id, agent_run_id=agent_run_id),
        payload,
    )
    return payload


def _load_run_write_baseline(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    path = _run_write_baseline_path(repo_root, orchestration_id, agent_run_id=agent_run_id)
    if not path.exists():
        who = agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else "orchestration"
        raise ValueError(f"run write baseline missing for {who}: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"run write baseline must be object: {path}")
    files = payload.get("files")
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ValueError(f"run write baseline files must be string map: {path}")
    return payload


def _actual_changed_paths_since_baseline(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> list[str]:
    baseline = _load_run_write_baseline(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    run_id = agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else "orchestration"
    before = {
        _normalize_rel_posix(str(path)): str(digest)
        for path, digest in dict(baseline.get("files", {})).items()
    }
    after = _snapshot_repo_files(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=run_id,
    )
    changed = {
        rel
        for rel in set(before) | set(after)
        if before.get(rel) != after.get(rel)
    }
    return sorted(changed)


def _normalize_rel_path_list(paths: Sequence[str]) -> list[str]:
    return sorted(
        {
            _normalize_rel_posix(str(path))
            for path in paths
            if isinstance(path, str) and path.strip()
        }
    )


def _gate_changed_paths_store_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> Path:
    return (
        _orchestration_root(repo_root, orchestration_id)
        / "agents"
        / agent_run_id.strip()
        / "gate_changed_paths.json"
    )


def _load_cumulative_gate_changed_paths_for_run(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> list[str]:
    path = _gate_changed_paths_store_path(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    if not path.exists():
        return []
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    paths_obj = payload.get("gate_changed_paths")
    if not isinstance(paths_obj, list):
        return []
    return _normalize_rel_path_list([str(item) for item in paths_obj if isinstance(item, str)])


def _update_cumulative_gate_changed_paths_for_run(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    changed_paths: Sequence[str],
) -> list[str]:
    current = _load_cumulative_gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    incoming = _normalize_rel_path_list(changed_paths)
    merged = sorted(set(current) | set(incoming))
    path = _gate_changed_paths_store_path(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    _write_json(
        path,
        {
            "orchestration_id": orchestration_id,
            "agent_run_id": agent_run_id.strip(),
            "gate_changed_paths": merged,
            "updated_at": _utc_now_iso(),
        },
    )
    return merged


def _gate_changed_paths_for_run(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> list[str]:
    cumulative = _load_cumulative_gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    if cumulative:
        return cumulative
    gate_path = _gates_dir(repo_root, orchestration_id) / agent_run_id.strip() / "apply_patch_writes.json"
    if not gate_path.exists():
        return []
    gate_doc = _read_json(gate_path)
    if not isinstance(gate_doc, dict):
        return []
    if str(gate_doc.get("status", "")).strip().lower() != "pass":
        return []
    args_json = gate_doc.get("args_json")
    if not isinstance(args_json, dict):
        return []
    changed_paths = args_json.get("changed_paths")
    if not isinstance(changed_paths, list):
        return []
    return _normalize_rel_path_list([str(item) for item in changed_paths if isinstance(item, str)])


def _declared_output_refs(payload: dict[str, Any]) -> list[str]:
    output_refs_obj = payload.get("output_refs")
    if not isinstance(output_refs_obj, list):
        return []
    return [
        _normalize_rel_posix(item)
        for item in output_refs_obj
        if isinstance(item, str) and item.strip()
    ]


def _orchestration_allowed_write_roots(orchestration_id: str) -> list[str]:
    return [
        _with_trailing_slash(_normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}")),
        _with_trailing_slash(_normalize_rel_posix(f"workspace/.pycache/{orchestration_id}")),
    ]


def _is_runtime_audit_artifact_path(orchestration_id: str, rel_path: str) -> bool:
    orch_root = _normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}")
    rel = _normalize_rel_posix(rel_path)
    prefixes: tuple[str, ...] = ()
    return any(_repo_path_under_prefix(rel, prefix.rstrip("/")) for prefix in prefixes)


def _declared_child_managed_paths(
    repo_root: Path,
    orchestration_id: str,
    *,
    current_agent_run_id: str,
) -> list[str]:
    declared: set[str] = set()
    records = _load_run_records(_orchestration_root(repo_root, orchestration_id))
    for run_id, record in records.items():
        if run_id == current_agent_run_id.strip():
            continue
        role = str(record.get("agent_role") or "").strip().lower()
        if role not in {"step", "substep"}:
            continue
        declared.update(_declared_output_refs(record))
        declared.update(
            _gate_changed_paths_for_run(
                repo_root,
                orchestration_id,
                agent_run_id=run_id,
            )
        )
    return sorted(declared)


def _managed_write_snapshot_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> Path:
    return (
        _orchestration_root(repo_root, orchestration_id)
        / "agents"
        / agent_run_id.strip()
        / "managed_write_snapshot.json"
    )


def _write_managed_write_snapshot(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    declared_paths: Sequence[str],
    actual_changed_paths: Sequence[str],
) -> None:
    normalized = sorted({_normalize_rel_posix(path) for path in declared_paths if str(path).strip()})
    if not normalized:
        return
    actual_paths = sorted(
        {
            _normalize_rel_posix(path)
            for path in actual_changed_paths
            if str(path).strip()
        }
    )
    tracked_paths = sorted(
        {
            path
            for path in actual_paths
            if any(_repo_path_under_prefix(path, decl) for decl in normalized)
        }
    )
    if not tracked_paths:
        return
    files: dict[str, str] = {}
    for path in tracked_paths:
        abs_path = repo_root / path
        if abs_path.exists():
            files[path] = _compute_sha256(abs_path)
        else:
            files[path] = "__MISSING__"
    _write_json(
        _managed_write_snapshot_path(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id,
        ),
        {
            "agent_run_id": agent_run_id.strip(),
            "recorded_at": _utc_now_iso(),
            "files": files,
        },
    )


def _child_managed_paths_excludable_from_orchestration_diff(
    repo_root: Path,
    orchestration_id: str,
    *,
    current_agent_run_id: str,
) -> set[str]:
    baseline = _load_run_write_baseline(repo_root, orchestration_id)
    baseline_files_obj = baseline.get("files")
    baseline_files = (
        {
            _normalize_rel_posix(str(path)): str(digest)
            for path, digest in baseline_files_obj.items()
            if isinstance(path, str) and path.strip() and isinstance(digest, str)
        }
        if isinstance(baseline_files_obj, dict)
        else {}
    )
    excludable: set[str] = set()
    records = _load_run_records(_orchestration_root(repo_root, orchestration_id))
    for run_id, record in records.items():
        if run_id == current_agent_run_id.strip():
            continue
        role = str(record.get("agent_role") or "").strip().lower()
        if role not in {"step", "substep"}:
            continue
        snap_path = _managed_write_snapshot_path(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
        )
        if not snap_path.exists():
            continue
        snap_doc = _read_json(snap_path)
        if not isinstance(snap_doc, dict):
            continue
        files_obj = snap_doc.get("files")
        if not isinstance(files_obj, dict):
            continue
        for path, digest in files_obj.items():
            if not isinstance(path, str) or not path.strip() or not isinstance(digest, str):
                continue
            rel = _normalize_rel_posix(path)
            current_path = repo_root / rel
            current_digest = "__MISSING__" if not current_path.exists() else _compute_sha256(current_path)
            if current_digest != digest:
                continue
            if baseline_files.get(rel) == current_digest:
                continue
            excludable.add(rel)
        manifest_path = _allowed_output_manifest_path(
            repo_root,
            orchestration_id,
            run_id,
        )
        manifest_rel = _normalize_rel_posix(str(manifest_path.relative_to(repo_root)))
        manifest_digest = (
            "__MISSING__" if not manifest_path.exists() else _compute_sha256(manifest_path)
        )
        if manifest_digest != "__MISSING__" and baseline_files.get(manifest_rel) != manifest_digest:
            excludable.add(manifest_rel)
    return excludable


def _cleanup_empty_file_pin_stubs(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> None:
    """Remove empty file-pin stubs created by this run's build_bwrap_profile.

    Only paths recorded in the sandbox profile's `created_file_pin_stubs` are
    candidates — pre-existing files are never touched. A stub is removed iff:
    - It is listed in created_file_pin_stubs (was created as empty stub by this run)
    - It currently exists and is zero bytes
    - It was not written by the agent via guarded-apply-patch (not in gate_changed_paths)
    """
    profile_path = _sandbox_profiles_dir(repo_root, orchestration_id) / f"{agent_run_id}.json"
    if not profile_path.exists():
        return
    try:
        profile_doc = _read_json(profile_path)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(profile_doc, dict):
        return
    stubs_obj = profile_doc.get("created_file_pin_stubs")
    if not isinstance(stubs_obj, list):
        return
    # Build path → recorded_mtime_ns mapping for stubs created by this run.
    # Each entry is {"path": str, "mtime_ns": int} recorded immediately after touch().
    candidate_stubs: dict[str, int] = {}
    for entry in stubs_obj:
        if isinstance(entry, dict):
            p = entry.get("path")
            m = entry.get("mtime_ns")
            if isinstance(p, str) and p.strip() and isinstance(m, int):
                candidate_stubs[_normalize_rel_posix(p)] = m
    if not candidate_stubs:
        return
    gate_changed = {
        _normalize_rel_posix(p)
        for p in _load_cumulative_gate_changed_paths_for_run(
            repo_root, orchestration_id, agent_run_id=agent_run_id
        )
        if p
    }
    for norm, recorded_mtime_ns in candidate_stubs.items():
        if norm in gate_changed:
            continue
        stub_path = repo_root / norm
        if not stub_path.exists():
            continue
        st = stub_path.stat()
        # Only delete if the file is still zero bytes AND its mtime is unchanged since
        # our touch() — a subprocess that writes (even zero bytes) updates the mtime.
        if st.st_size == 0 and st.st_mtime_ns == recorded_mtime_ns:
            try:
                stub_path.unlink()
            except OSError:
                pass


def _write_directory_authorized_paths(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    directory_authorized_paths: list[str],
    manifest_allowed_output_dirs: list[str],
) -> None:
    out = _violations_dir(repo_root, orchestration_id).parent / "audit" / f"{agent_run_id}.directory_authorized_paths.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json(out, {
        "kind": "directory_authorized_paths",
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "recorded_at": _utc_now_iso(),
        "manifest_allowed_output_dirs": manifest_allowed_output_dirs,
        "directory_authorized_paths": directory_authorized_paths,
    })


def _write_unauthorized_write_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    actor_role: str,
    actual_changed_paths: list[str],
    unauthorized_paths: list[str],
    output_refs: list[str],
    gate_changed_paths: list[str],
    missing_from_gate_changed_paths: list[str],
    write_roots: list[str],
    manifest_file_tool_paths: list[str] | None = None,
    directory_authorized_paths: list[str] | None = None,
) -> Path:
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.unauthorized_write_violation.json"
    record: dict[str, Any] = {
        "kind": "unauthorized_write_violation",
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "actor_role": actor_role,
        "detected_at": _utc_now_iso(),
        "actual_changed_paths": actual_changed_paths,
        "unauthorized_paths": unauthorized_paths,
        "output_refs": output_refs,
        "gate_changed_paths": gate_changed_paths,
        "missing_from_gate_changed_paths": missing_from_gate_changed_paths,
        "write_roots": write_roots,
    }
    if manifest_file_tool_paths is not None:
        record["manifest_file_tool_paths"] = manifest_file_tool_paths
    if directory_authorized_paths is not None:
        record["directory_authorized_paths"] = directory_authorized_paths
    _write_json(out, record)
    return out


def _validate_actual_write_paths(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
) -> None:
    role_obj = payload.get("agent_role")
    agent_run_id_obj = payload.get("agent_run_id")
    if not isinstance(role_obj, str) or not isinstance(agent_run_id_obj, str) or not agent_run_id_obj.strip():
        return
    actor_role = role_obj.strip().lower()
    if actor_role not in {"orchestration", "step", "substep"}:
        return
    status_obj = payload.get("status")
    if not isinstance(status_obj, str) or status_obj.strip().lower() not in TERMINAL_STATUSES:
        return

    run_id = agent_run_id_obj.strip()
    baseline_agent_run_id = run_id if actor_role in {"step", "substep"} else None
    actual_changed_paths = _actual_changed_paths_since_baseline(
        repo_root,
        orchestration_id,
        agent_run_id=baseline_agent_run_id,
    )
    output_refs = _declared_output_refs(payload)
    gate_changed_paths = _gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=run_id,
    )

    if actor_role == "orchestration":
        child_excludable = _child_managed_paths_excludable_from_orchestration_diff(
            repo_root,
            orchestration_id,
            current_agent_run_id=run_id,
        )
        actual_changed_paths = [
            path
            for path in actual_changed_paths
            if path not in child_excludable
        ]
        write_roots = _orchestration_allowed_write_roots(orchestration_id)
    else:
        cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{run_id}.json"
        if not cap_path.exists():
            raise ValueError(f"capability file not found for terminal write validation: {cap_path}")
        cap_doc = _read_json(cap_path)
        if not isinstance(cap_doc, dict):
            raise ValueError(f"capability must be object for terminal write validation: {cap_path}")
        roots_obj = cap_doc.get("write_roots")
        write_roots = _load_write_roots_from_cap(roots_obj)

    unauthorized: list[str] = []
    parent_tmp_root: str | None = None
    if actor_role in {"step", "substep"}:
        # Prefer the launch request file as the authoritative source for
        # parent_agent_run_id; fall back to payload for backward compatibility.
        _parent_run_id: str | None = None
        _launch_req = (
            repo_root
            / "workspace"
            / "orchestrations"
            / orchestration_id
            / "launches"
            / f"{run_id}.request.json"
        )
        if _launch_req.exists():
            try:
                _req_doc = _read_json(_launch_req)
                if isinstance(_req_doc, dict):
                    _raw = _req_doc.get("parent_agent_run_id")
                    if isinstance(_raw, str) and _raw.strip():
                        _parent_run_id = _raw.strip()
            except Exception:
                pass
        if _parent_run_id is None:
            _raw_payload = payload.get("parent_agent_run_id")
            if isinstance(_raw_payload, str) and _raw_payload.strip():
                _parent_run_id = _raw_payload.strip()
        if _parent_run_id:
            parent_tmp_root = _normalize_rel_posix(f"workspace/tmp/{_parent_run_id}")
    missing_from_gate_changed_paths = sorted(
        {
            path
            for path in actual_changed_paths
            if not any(_repo_path_under_prefix(path, gate_path) for gate_path in gate_changed_paths)
        }
    )
    manifest_file_tool_paths: set[str] = set()
    manifest_allowed_tmp_root: str | None = None
    manifest_allowed_output_dirs: list[str] = []
    if actor_role == "orchestration":
        declared_paths = sorted(set(output_refs) | set(gate_changed_paths))
        exact_declared_paths = declared_paths  # orchestration: no directory entries
    else:
        # step/substep: include manifest-permitted direct write paths so that
        # `.yaml` / `.md` / source code outputs written via Edit/Write are not
        # flagged as unauthorized writes.
        try:
            manifest_doc = _load_allowed_output_manifest(
                repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=run_id,
            )
        except ValueError:
            manifest_doc = None
        if isinstance(manifest_doc, dict):
            ftp_obj = manifest_doc.get("allowed_file_tool_paths")
            if isinstance(ftp_obj, list):
                manifest_file_tool_paths = {
                    _normalize_rel_posix(str(item))
                    for item in ftp_obj
                    if isinstance(item, str) and item.strip()
                }
            aop_obj = manifest_doc.get("allowed_output_paths")
            if isinstance(aop_obj, list):
                for item in aop_obj:
                    if isinstance(item, str) and item.strip().endswith("/"):
                        manifest_allowed_output_dirs.append(_normalize_rel_posix(item.strip()))
            _tmp_raw = manifest_doc.get("allowed_tmp_root", "")
            if isinstance(_tmp_raw, str) and _tmp_raw.strip():
                _tmp_norm = _normalize_rel_posix(_tmp_raw.strip())
                _expected_tmp = _normalize_rel_posix(f"workspace/tmp/{run_id}")
                if _tmp_norm != _expected_tmp:
                    raise ValueError(
                        f"allowed_tmp_root manifest value {_tmp_norm!r} does not match "
                        f"expected per-run root {_expected_tmp!r}"
                    )
                manifest_allowed_tmp_root = _tmp_norm
        exact_declared_paths = sorted(set(gate_changed_paths) | manifest_file_tool_paths)
        declared_paths = sorted(set(exact_declared_paths) | set(manifest_allowed_output_dirs))
    # Use a frozenset for O(1) exact-match lookup. exact_declared_paths contains
    # concrete file paths (gate_changed_paths + manifest_file_tool_paths); prefix
    # matching would allow a directory token that leaked in to bypass extension policy.
    _exact_declared_set: frozenset[str] = frozenset(exact_declared_paths)
    directory_authorized: list[str] = []
    for path in actual_changed_paths:
        if parent_tmp_root and _repo_path_under_prefix(path, parent_tmp_root):
            continue
        if manifest_allowed_tmp_root and _repo_path_under_prefix(path, manifest_allowed_tmp_root):
            continue
        if write_roots and not _path_under_any_write_root(path, write_roots):
            unauthorized.append(path)
            continue
        if _exact_declared_set and path in _exact_declared_set:
            continue
        if manifest_allowed_output_dirs and any(_repo_path_under_prefix(path, d) for d in manifest_allowed_output_dirs):
            # All writes under a directory allowlist must have gate provenance (guarded-apply-patch).
            # Compiler byproducts (.mod, .o, .a) are also unauthorized without provenance —
            # agents must clean them up before record-agent-run to prevent unaudited binary injection.
            unauthorized.append(path)
            continue
        if not declared_paths:
            unauthorized.append(path)
            continue
        unauthorized.append(path)

    if directory_authorized:
        _write_directory_authorized_paths(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
            directory_authorized_paths=directory_authorized,
            manifest_allowed_output_dirs=manifest_allowed_output_dirs,
        )
    if unauthorized:
        violation_path = _write_unauthorized_write_violation(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
            actor_role=actor_role,
            actual_changed_paths=actual_changed_paths,
            unauthorized_paths=unauthorized,
            output_refs=output_refs,
            gate_changed_paths=gate_changed_paths,
            missing_from_gate_changed_paths=missing_from_gate_changed_paths,
            write_roots=write_roots,
            manifest_file_tool_paths=sorted(manifest_file_tool_paths) if manifest_file_tool_paths else None,
            directory_authorized_paths=directory_authorized if directory_authorized else None,
        )
        if actor_role in {"step", "substep"}:
            # Cleanup runs AFTER violation is recorded so evidence is preserved for auditors.
            _cleanup_empty_file_pin_stubs(repo_root, orchestration_id, agent_run_id=run_id)
        raise ValueError(
            "terminal run has unauthorized write paths: "
            + ", ".join(unauthorized)
            + f" (violation: {violation_path})"
        )
    if actor_role in {"step", "substep"}:
        # Success path: clean up any stubs the agent never wrote to.
        _cleanup_empty_file_pin_stubs(repo_root, orchestration_id, agent_run_id=run_id)
        _write_managed_write_snapshot(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
            declared_paths=declared_paths,
            actual_changed_paths=actual_changed_paths,
        )


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
    backend_token = str(payload.get("backend", "")).strip().lower()
    codex_hooks = feature_states.get("codex_hooks")
    if backend_token == "codex" and codex_hooks is not True:
        return False

    checks = payload.get("checks")
    if not isinstance(checks, list):
        return False
    multi_agent_check_pass: bool | None = None
    codex_hooks_check_pass: bool | None = None
    codex_home_writable_check_pass: bool | None = None
    for item in checks:
        if not isinstance(item, dict):
            continue
        check_name = item.get("name")
        pass_value = item.get("pass")
        if check_name == "multi_agent_enabled" and isinstance(pass_value, bool):
            multi_agent_check_pass = pass_value
        if check_name == "codex_hooks_enabled" and isinstance(pass_value, bool):
            codex_hooks_check_pass = pass_value
        if check_name == "codex_home_writable" and isinstance(pass_value, bool):
            codex_home_writable_check_pass = pass_value

    launchable = (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
        and payload.get("sandbox_enforced") is True
        and multi_agent_check_pass is True
    )
    if backend_token == "codex":
        launchable = (
            launchable
            and codex_hooks_check_pass is True
            and codex_home_writable_check_pass is True
        )
    return launchable


def _validate_preflight_payload(payload: dict[str, Any]) -> None:
    if (
        payload.get("can_launch_step_agents") is True
        or payload.get("can_launch_substep_agents") is True
    ) and payload.get("status") != "pass":
        raise ValueError(
            "preflight status must be pass when can_launch_step_agents/can_launch_substep_agents is true"
        )

    feature_states = payload.get("feature_states")
    backend_token = str(payload.get("backend", "")).strip().lower()
    if isinstance(feature_states, dict):
        multi_agent = feature_states.get("multi_agent")
        if isinstance(multi_agent, bool) and not multi_agent:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "feature_states.multi_agent=false is incompatible with launchable preflight"
                )
        codex_hooks = feature_states.get("codex_hooks")
        if (
            backend_token == "codex"
            and codex_hooks is not True
            and (
                payload.get("can_launch_step_agents") is True
                or payload.get("can_launch_substep_agents") is True
            )
        ):
            raise ValueError(
                "feature_states.codex_hooks=true is required for codex launchable preflight"
            )

    checks = payload.get("checks")
    if isinstance(checks, list):
        multi_agent_check_pass: bool | None = None
        codex_hooks_check_pass: bool | None = None
        codex_home_writable_check_pass: bool | None = None
        for item in checks:
            if not isinstance(item, dict):
                continue
            check_name = item.get("name")
            pass_value = item.get("pass")
            if check_name == "multi_agent_enabled" and isinstance(pass_value, bool):
                multi_agent_check_pass = pass_value
            if check_name == "codex_hooks_enabled" and isinstance(pass_value, bool):
                codex_hooks_check_pass = pass_value
            if check_name == "codex_home_writable" and isinstance(pass_value, bool):
                codex_home_writable_check_pass = pass_value
        if multi_agent_check_pass is False:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "checks.multi_agent_enabled.pass=false is incompatible with launchable preflight"
                )
        if (
            backend_token == "codex"
            and codex_hooks_check_pass is not True
            and (
                payload.get("can_launch_step_agents") is True
                or payload.get("can_launch_substep_agents") is True
            )
        ):
            raise ValueError(
                "checks.codex_hooks_enabled.pass=true is required for codex launchable preflight"
            )
        if (
            backend_token == "codex"
            and codex_home_writable_check_pass is not True
            and (
                payload.get("can_launch_step_agents") is True
                or payload.get("can_launch_substep_agents") is True
            )
        ):
            raise ValueError(
                "checks.codex_home_writable.pass=true is required for codex launchable preflight"
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
        if backend_token == "codex" and feature_states.get("codex_hooks") is not True:
            raise ValueError(
                "feature_states.codex_hooks=true is required when codex preflight is launchable"
            )
        if payload.get("sandbox_enforced") is not True:
            raise ValueError("sandbox_enforced=true is required when preflight is launchable")


def _live_preflight_mode() -> str:
    """METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT の値から動作モードを返す。

    戻り値: 'never' | 'always' | 'ttl'
    - 'never' : プローブをスキップ
    - 'always': 毎回プローブ（TTL 無視、後方互換）
    - 'ttl'   : TTL キャッシュ付きプローブ（デフォルト）
    """
    raw = os.environ.get("METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", "").strip().lower()
    if raw in {"0", "false", "no"}:
        return "never"
    if raw == "1":
        return "always"
    return "ttl"


def _live_preflight_ttl_seconds() -> int:
    """METDSL_PREFLIGHT_TTL_SECONDS を読み非負整数を返す。

    未設定または無効値の場合は PREFLIGHT_TTL_DEFAULT_SECONDS を返す。
    """
    raw = os.environ.get("METDSL_PREFLIGHT_TTL_SECONDS", "").strip()
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
        "workflow_mode": str(request_payload.get("workflow_mode", "")),
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
    ]
    if step.strip().lower() == "generate":
        refs.append(f"{plan_root}/derived_contract.json")
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
    payload.setdefault("workflow_mode", os.environ.get("METDSL_WORKFLOW_EXEC_MODE", "dev"))
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
    # Backward compatibility: workflow_mode line is recommended but not mandatory
    # for manually provided legacy launch prompts.
    return [
        line
        for line in build_launch_prompt_text(request_payload).splitlines()
        if not line.strip().startswith("workflow_mode:")
    ]


def _required_launch_prompt_constraint_lines(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
    required_fragments = (
        "`run-gate --gate apply_patch_writes` と `apply-patch-gate`",
        "`output_manifests/",
        "/capabilities/",
        "`capability_token` が未取得または不一致の場合は処理を開始せず fail",
        "`.json` と `.txt` の出力は",
        "`.yaml` / `.yml` / `.md` および source code 等の上記以外の出力は",
    )
    return [
        line
        for line in render_launch_prompt_text(request_payload).splitlines()
        if any(fragment in line for fragment in required_fragments)
        and "読み取ってよい" not in line
    ]


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
    required_constraint_lines = _required_launch_prompt_constraint_lines(request_payload)
    missing_constraint_lines = [line for line in required_constraint_lines if line not in prompt_text]
    if missing_constraint_lines:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration shell-write constraints: "
            + ", ".join(missing_constraint_lines)
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


def _guard_checkpoint_read_requires_resume(repo_root: Path, orchestration_id: str) -> None:
    ck_path = _checkpoint_path(repo_root, orchestration_id)
    if not ck_path.is_file():
        return
    try:
        ck = _read_json(ck_path)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(ck, dict):
        return
    steps = ck.get("completed_steps")
    if not isinstance(steps, list) or not steps:
        return
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.is_file():
        return
    try:
        meta = _read_json(meta_path)
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(meta, dict) and meta.get("resume_enabled") is True:
        return
    raise RuntimeError(
        "read_checkpoint forbidden unless orchestration_meta.resume_enabled is true "
        f"(orchestration_id={orchestration_id!r})"
    )


def read_checkpoint(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any] | None:
    """orchestration_checkpoint.json を読んで返す。存在しない場合は None。"""
    _guard_checkpoint_read_requires_resume(repo_root, orchestration_id)
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
    step_val = str(request_payload.get("step", "")).strip().lower()
    if step_val == "plan" and isinstance(dependency_ref, str) and dependency_ref.strip():
        dep_norm = _normalize_rel_posix(dependency_ref.strip())
        if not (dep_norm.startswith("spec/") and dep_norm.endswith("/deps.yaml")):
            raise ValueError(
                "record-launch: Plan step dependency_ref must be spec/.../deps.yaml, "
                f"got {dependency_ref!r}. "
                "Both generate and verify substeps must receive the spec path, "
                "not workspace/plans/."
            )

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
    if not isinstance(role, str):
        return
    role_token = role.strip().lower()
    if role_token not in {"orchestration", "step", "substep"}:
        return
    _validate_actual_write_paths(repo_root, orchestration_id, payload)
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return

    output_refs = payload.get("output_refs")
    if role_token in {"step", "substep"}:
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError("pass status for step/substep requires non-empty output_refs")
        for idx, item in enumerate(output_refs):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"output_refs[{idx}] must be non-empty string")
        _validate_pass_output_refs_against_launch(repo_root, payload)
        _validate_paths_against_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=str(payload.get("agent_run_id") or ""),
            paths=[str(item) for item in output_refs if isinstance(item, str)],
        )
        _validate_apply_patch_gate_coverage(repo_root, orchestration_id, payload)
        return

    if not isinstance(output_refs, list) or not output_refs:
        return
    for idx, item in enumerate(output_refs):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"output_refs[{idx}] must be non-empty string")
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
    if actor_role not in {"orchestration", "step", "substep"}:
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

    # Direct-write extensions (e.g. .yaml / .md / source code) are written via
    # `Edit`/`Write` tools and are exempt from `apply_patch_writes` gate
    # coverage. Only `.json` / `.txt` outputs (CLI-managed extensions) require
    # gate evidence.
    cli_required_refs = [ref for ref in output_refs if not _is_direct_write_path(ref)]
    if not cli_required_refs:
        return

    gate_path = _gates_dir(repo_root, orchestration_id) / run_id / "apply_patch_writes.json"
    if not gate_path.exists():
        raise ValueError(
            f"pass status for {actor_role} requires apply_patch_writes gate evidence: "
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
    changed_paths = _gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=run_id,
    )
    if not changed_paths:
        raise ValueError(f"apply_patch_writes gate changed_paths must be non-empty: {gate_path}")

    uncovered: list[str] = []
    for output_ref in cli_required_refs:
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


_STEP_META_FILENAME = STAGE_META_FILENAME_BY_STEP

STEP_REQUIRED_VALIDATION_STAGES: dict[str, frozenset[str]] = {
    "generate": frozenset({"post_generate", "post_build", "full"}),
    "build": frozenset({"post_build", "full"}),
    "execute": frozenset({"post_execute", "pre_judge", "full"}),
    "judge": frozenset({"pre_judge", "full"}),
}

_RETRY_DECISION_REQUIRED_KEYS: tuple[str, ...] = (
    "issue_severity",
    "repair_strategy",
    "repair_target_agent_run_id",
    "new_agent_run_id",
    "repair_reason",
)


def _validate_lint_command_ref(meta_data: dict[str, Any], *, meta_filename: str, meta_ref: str) -> None:
    lint_command_ref = meta_data.get("lint_command_ref")
    if meta_filename != "generate_meta.json":
        return
    status = str(meta_data.get("verification_status", "")).strip().lower()
    if status != "pass":
        return
    if not isinstance(lint_command_ref, dict):
        raise ValueError(
            f"{meta_filename} missing lint_command_ref when verification_status=pass: {meta_ref}"
        )
    run_linter = lint_command_ref.get("run_linter")
    if not isinstance(run_linter, list) or not run_linter:
        raise ValueError(f"{meta_filename} lint_command_ref.run_linter must be non-empty list: {meta_ref}")
    for idx, item in enumerate(run_linter):
        if not isinstance(item, dict):
            raise ValueError(
                f"{meta_filename} lint_command_ref.run_linter[{idx}] must be object: {meta_ref}"
            )
        for key in ("command_id", "command_log_ref", "preset"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{meta_filename} lint_command_ref.run_linter[{idx}].{key} must be non-empty string: {meta_ref}"
                )


def _validate_step_meta_payload(meta_data: dict[str, Any], *, step_token: str, meta_ref: str) -> None:
    meta_filename = _STEP_META_FILENAME[step_token]
    missing_keys = missing_required_meta_keys(meta_data, step_token=step_token)
    if missing_keys:
        raise ValueError(
            f"{meta_filename} missing required keys: {missing_keys} "
            f"(phase={step_token} substep=verify ref={meta_ref})"
        )
    context_isolated = meta_data.get("context_isolated")
    if not isinstance(context_isolated, bool):
        raise ValueError(f"{meta_filename} context_isolated must be boolean: {meta_ref}")
    if not isinstance(meta_data.get("debug_mode"), bool):
        raise ValueError(f"{meta_filename} debug_mode must be boolean: {meta_ref}")
    if not isinstance(meta_data.get("attempt_count"), int):
        raise ValueError(f"{meta_filename} attempt_count must be integer: {meta_ref}")
    verification_status = meta_data.get("verification_status")
    if not isinstance(verification_status, str) or not verification_status.strip():
        raise ValueError(f"{meta_filename} verification_status must be non-empty string: {meta_ref}")
    last_fail_reason = meta_data.get("last_fail_reason")
    if last_fail_reason is not None and not isinstance(last_fail_reason, str):
        raise ValueError(f"{meta_filename} last_fail_reason must be string or null: {meta_ref}")
    if context_isolated is False:
        constraint_reason = meta_data.get("constraint_reason")
        if not isinstance(constraint_reason, str) or not constraint_reason.strip():
            raise ValueError(
                f"{meta_filename} requires non-empty constraint_reason when context_isolated=false: {meta_ref}"
            )
    _validate_lint_command_ref(meta_data, meta_filename=meta_filename, meta_ref=meta_ref)


def _effective_pass_substep_run_ids(
    payload: dict[str, Any],
    *,
    repo_root: Path,
    orchestration_id: str,
    run_records: dict[str, dict[str, Any]],
    node_key: str,
    step_token: str,
) -> tuple[list[str], dict[str, set[str]]]:
    substep_run_ids = payload.get("substep_agent_run_ids")
    if not isinstance(substep_run_ids, list) or not substep_run_ids:
        raise ValueError(f"pass step_result for {step_token} requires non-empty substep_agent_run_ids")

    listed_run_ids: list[str] = []
    listed_run_id_set: set[str] = set()
    for idx, substep_run_id in enumerate(substep_run_ids):
        if not isinstance(substep_run_id, str) or not substep_run_id.strip():
            raise ValueError(f"substep_agent_run_ids[{idx}] must be non-empty string")
        token = substep_run_id.strip()
        if token in listed_run_id_set:
            raise ValueError(f"substep_agent_run_ids must not contain duplicates: {token}")
        listed_run_ids.append(token)
        listed_run_id_set.add(token)

        substep_record = run_records.get(token)
        if not isinstance(substep_record, dict):
            raise ValueError(f"missing substep run record: {token}")
        role = str(substep_record.get("agent_role") or "").strip().lower()
        if role != "substep":
            raise ValueError(f"listed run must be substep role: {token}")
        record_node_key = str(substep_record.get("node_key") or "").strip()
        if record_node_key != node_key:
            raise ValueError(f"listed substep run node_key mismatch: {token}")
        record_step = str(substep_record.get("step") or "").strip().lower()
        if record_step != step_token:
            raise ValueError(f"listed substep run step mismatch: {token}")

    failed_substeps = payload.get("failed_substeps", [])
    if not isinstance(failed_substeps, list):
        raise ValueError("step_result.failed_substeps must be list")
    explicit_failed_run_ids: set[str] = set()
    for idx, failed_run_id in enumerate(failed_substeps):
        if not isinstance(failed_run_id, str) or not failed_run_id.strip():
            raise ValueError(f"failed_substeps[{idx}] must be non-empty string")
        token = failed_run_id.strip()
        if token not in listed_run_id_set:
            raise ValueError(f"failed_substeps[{idx}] must be listed in substep_agent_run_ids: {token}")
        failed_status = str(run_records[token].get("status") or "").strip().lower()
        if failed_status == "pass":
            raise ValueError(f"failed_substeps[{idx}] must reference actual non-pass run: {token}")
        explicit_failed_run_ids.add(token)

    retry_decisions = payload.get("retry_decisions", [])
    if retry_decisions is None:
        retry_decisions = []
    if not isinstance(retry_decisions, list):
        raise ValueError("step_result.retry_decisions must be list when provided")
    replaced_run_ids: set[str] = set()
    adopted_run_ids: set[str] = set()
    for idx, item in enumerate(retry_decisions):
        if not isinstance(item, dict):
            raise ValueError(f"retry_decisions[{idx}] must be object")
        missing_keys = [
            key for key in _RETRY_DECISION_REQUIRED_KEYS
            if not isinstance(item.get(key), str) or not str(item.get(key)).strip()
        ]
        if missing_keys:
            raise ValueError(
                f"retry_decisions[{idx}] missing required string keys: {missing_keys}"
            )
        repair_target = str(item["repair_target_agent_run_id"]).strip()
        new_run_id = str(item["new_agent_run_id"]).strip()
        repair_strategy = str(item.get("repair_strategy") or "").strip().lower()
        repair_reason = str(item.get("repair_reason") or "").strip().lower()
        if repair_target not in listed_run_id_set:
            raise ValueError(
                f"retry_decisions[{idx}].repair_target_agent_run_id must be listed in substep_agent_run_ids: {repair_target}"
            )
        if new_run_id not in listed_run_id_set:
            raise ValueError(
                f"retry_decisions[{idx}].new_agent_run_id must be listed in substep_agent_run_ids: {new_run_id}"
            )
        if repair_target == new_run_id:
            raise ValueError(f"retry_decisions[{idx}] must replace a different run_id")
        if repair_target in replaced_run_ids:
            raise ValueError(f"retry_decisions must not replace the same run twice: {repair_target}")
        repair_target_status = str(run_records[repair_target].get("status") or "").strip().lower()
        if repair_target_status == "pass":
            raise ValueError(
                f"retry_decisions[{idx}].repair_target_agent_run_id must reference actual non-pass run: {repair_target}"
            )
        violation_path = (
            _violations_dir(repo_root, orchestration_id)
            / f"{repair_target}.noncanonical_phase_write_attempt.json"
        )
        has_noncanonical_violation = violation_path.exists()
        if (has_noncanonical_violation or "noncanonical_phase_write_attempt" in repair_reason) and repair_strategy != "restart":
            raise ValueError(
                f"retry_decisions[{idx}] must use repair_strategy='restart' for noncanonical_phase_write_attempt"
            )
        replaced_run_ids.add(repair_target)
        adopted_run_ids.add(new_run_id)

    effective_run_ids: list[str] = []
    for run_id in listed_run_ids:
        if run_id in replaced_run_ids or run_id in explicit_failed_run_ids:
            continue
        substep_record = run_records[run_id]
        substep_status = str(substep_record.get("status") or "").strip().lower()
        if substep_status != "pass":
            raise ValueError(
                f"non-pass substep {run_id} must be excluded by failed_substeps or retry_decisions before step_result can pass"
            )
        effective_run_ids.append(run_id)

    for run_id in adopted_run_ids:
        substep_status = str(run_records[run_id].get("status") or "").strip().lower()
        if substep_status != "pass":
            raise ValueError(f"retry_decisions new_agent_run_id must be pass for step_result pass: {run_id}")
    if not effective_run_ids:
        raise ValueError(f"pass step_result for {step_token} requires at least one effective pass substep")

    return effective_run_ids, {
        "listed_run_ids": listed_run_id_set,
        "explicit_failed_run_ids": explicit_failed_run_ids,
        "replaced_run_ids": replaced_run_ids,
        "adopted_run_ids": adopted_run_ids,
    }


def _pre_phase_complete_judge_checks(
    repo_root: Path,
    *,
    node_key: str,
    status_token: str,
    payload: dict[str, Any],
) -> None:
    lr_ref = payload.get("launch_request_ref")
    if not isinstance(lr_ref, str) or not lr_ref.strip():
        raise ValueError("judge step_result requires launch_request_ref for pre_phase_complete hook")
    lr_path = repo_root / lr_ref.strip()
    if not lr_path.is_file():
        raise ValueError(f"judge launch_request_ref not found: {lr_ref}")
    try:
        lr = _read_json(lr_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge launch_request_ref invalid json: {lr_ref}") from exc
    if not isinstance(lr, dict):
        raise ValueError(f"judge launch_request must be object: {lr_ref}")
    pr = lr.get("pipeline_ref")
    if not isinstance(pr, str) or not pr.strip():
        raise ValueError("judge launch_request missing pipeline_ref")
    base, err = _resolve_judge_execution_dir(
        repo_root,
        pipeline_ref=pr.strip(),
        node_key=node_key,
        launch_request=lr,
    )
    if base is None:
        raise ValueError(f"judge execution directory not resolved: {err}")
    if status_token in JUDGE_SEMANTIC_REVIEW_SKIPPED_STATUSES:
        return
    sem = base / "semantic_review.json"
    if not sem.is_file():
        raise ValueError("pre_phase_complete: judge requires semantic_review.json")
    try:
        sdoc = _read_json(sem)
    except json.JSONDecodeError as exc:
        raise ValueError("semantic_review.json must be valid json") from exc
    if not isinstance(sdoc, dict):
        raise ValueError("semantic_review.json must be a json object")
    dec = sdoc.get("decision")
    if dec is None or (isinstance(dec, str) and not str(dec).strip()):
        raise ValueError("semantic_review.json decision missing (completion forbidden)")
    dec_norm = str(dec).strip().lower()
    if dec_norm == "fail" and status_token == "pass":
        raise ValueError("semantic_review.json decision=fail cannot accompany pass step_result")
    if dec_norm == "pass" and status_token in {"fail", "blocked"}:
        raise ValueError(
            "semantic_review.json decision=pass cannot accompany fail or blocked step_result"
        )
    if status_token == "blocked":
        for name in ("aggregate_verdict.json", "summary.json", "trial_meta.json"):
            if not (base / name).is_file():
                raise ValueError(f"blocked judge requires {name} under execution directory")


def post_phase_complete(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
) -> None:
    from tools.validate_workspace_root import _validate_write_scope_from_baseline

    step_token = step.strip().lower()
    violations: list[str] = []
    baseline_ref = payload.get("write_scope_baseline_ref")
    if isinstance(baseline_ref, str) and baseline_ref.strip():
        bp = repo_root / _normalize_rel_posix(baseline_ref.strip())
        if bp.is_file():
            violations.extend(
                _validate_write_scope_from_baseline(
                    repo_root=repo_root,
                    workspace_root="workspace/",
                    baseline_path=bp,
                )
            )
    orch_root = _orchestration_root(repo_root, orchestration_id)
    resp_path = orch_root / "launches" / f"{agent_run_id.strip()}.response.json"
    req_path = orch_root / "launches" / f"{agent_run_id.strip()}.request.json"
    if resp_path.is_file() and req_path.is_file():
        try:
            rsp = _read_json(resp_path)
            req = _read_json(req_path)
        except (OSError, json.JSONDecodeError):
            rsp = {}
            req = {}
        if isinstance(req, dict) and isinstance(rsp, dict):
            rq_sid = req.get("agent_session_id")
            rs_sid = rsp.get("agent_session_id")
            if (
                isinstance(rq_sid, str)
                and rq_sid.strip()
                and isinstance(rs_sid, str)
                and rs_sid.strip()
                and rq_sid.strip() != rs_sid.strip()
            ):
                violations.append(
                    "post_phase_complete: launch response agent_session_id mismatch vs request"
                )
    if violations:
        _append_workflow_hook_log(
            repo_root,
            orchestration_id,
            hook_name="post_phase_complete",
            status="deny",
            detail={"violations": violations, "step": step_token},
        )
        raise RuntimeError("post_phase_complete denied: " + "; ".join(violations))
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="post_phase_complete",
        status="allow",
        detail={"step": step_token, "agent_run_id": agent_run_id},
    )


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

    # validation_stage チェック（generate/build/execute/judge の terminal 時）
    if step_token in STEP_REQUIRED_VALIDATION_STAGES and status_token in TERMINAL_STATUSES:
        allowed = STEP_REQUIRED_VALIDATION_STAGES[step_token]
        validation_stage = payload.get("validation_stage")
        if not isinstance(validation_stage, str) or validation_stage.strip() not in allowed:
            raise ValueError(
                f"terminal step_result for {step_token} requires validation_stage in "
                f"{sorted(allowed)}; status={status_token!r} validation_stage={validation_stage!r}"
            )
        _append_workflow_hook_log(
            repo_root,
            orchestration_id,
            hook_name="pre_phase_complete",
            status="allow",
            detail={"step": step_token, "status": status_token, "validation_stage": validation_stage},
        )

    if step_token == "judge" and status_token in TERMINAL_STATUSES:
        _pre_phase_complete_judge_checks(
            repo_root,
            node_key=node_key,
            status_token=status_token,
            payload=payload,
        )

    # 以下は既存の substep 検証（plan/generate/tune のみ）
    if step_token not in {"plan", "generate", "tune"}:
        return
    if status_token != "pass":
        return

    run_records = _load_run_records(_orchestration_root(repo_root, orchestration_id))
    effective_run_ids, _ = _effective_pass_substep_run_ids(
        payload,
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        run_records=run_records,
        node_key=node_key,
        step_token=step_token,
    )
    required_outputs = payload.get("required_outputs")
    if not isinstance(required_outputs, list):
        raise ValueError("step_result.required_outputs must be list")
    declared_outputs = {item.strip() for item in required_outputs if isinstance(item, str) and item.strip()}

    substep_outputs: set[str] = set()
    for substep_run_id in effective_run_ids:
        substep_record = run_records[substep_run_id]
        output_refs = substep_record.get("output_refs")
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError(f"substep {substep_run_id} must publish non-empty output_refs")
        for output_ref in output_refs:
            if isinstance(output_ref, str) and output_ref.strip():
                substep_outputs.add(output_ref.strip())

    # meta ファイル検証（plan/generate の pass 時のみ）
    if step_token in _STEP_META_FILENAME:
        meta_filename = _STEP_META_FILENAME[step_token]
        meta_refs = [ref for ref in declared_outputs if ref.endswith(meta_filename)]
        if not meta_refs:
            raise ValueError(
                f"pass step_result for {step_token} requires required_outputs to include final {meta_filename}"
            )
        if len(meta_refs) != 1:
            raise ValueError(
                f"pass step_result for {step_token} requires exactly one final {meta_filename} in required_outputs"
            )
        meta_ref = meta_refs[0]
        if meta_ref not in substep_outputs:
            raise ValueError(
                f"step_result.required_outputs must reference final {meta_filename} from effective substep output_refs: {meta_ref}"
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
        _validate_step_meta_payload(
            meta_data,
            step_token=step_token,
            meta_ref=meta_ref,
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


def _probe_existing_directory_writable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path} does not exist"
    if not path.is_dir():
        return False, f"{path} is not a directory"
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(path),
            prefix=".codex-orchestration-preflight-",
            delete=True,
        ) as handle:
            handle.write(b"probe")
            handle.flush()
    except OSError as exc:
        return False, f"{path}: {exc}"
    return True, str(path)


def _probe_codex_home_writable() -> dict[str, Any]:
    raw = os.environ.get("METDSL_HOME")
    source = "env:METDSL_HOME" if isinstance(raw, str) and raw.strip() else "default:~/.codex"
    codex_home = (
        Path(raw).expanduser()
        if isinstance(raw, str) and raw.strip()
        else (Path.home() / ".codex")
    )
    if codex_home.exists():
        ok, detail = _probe_existing_directory_writable(codex_home)
        return {
            "name": "codex_home_writable",
            "pass": ok,
            "detail": f"{source} path={codex_home} detail={detail}",
        }
    parent = codex_home.parent
    ok, detail = _probe_existing_directory_writable(parent)
    detail_text = (
        f"{source} path={codex_home} parent={parent} "
        + ("parent writable; codex_home can be created" if ok else f"parent not writable: {detail}")
    )
    return {"name": "codex_home_writable", "pass": ok, "detail": detail_text}


def _probe_bwrap_sandbox() -> tuple[list[dict[str, Any]], bool]:
    checks: list[dict[str, Any]] = []
    assume = os.environ.get("METDSL_ORCHESTRATION_ASSUME_BWRAP", "").strip().lower()
    if assume in {"1", "true", "yes"}:
        checks.extend(
            [
                {"name": "sandbox_bwrap_available", "pass": True, "detail": "assumed via env override"},
                {"name": "sandbox_bwrap_userns", "pass": True, "detail": "assumed via env override"},
                {"name": "sandbox_bwrap_exec", "pass": True, "detail": "assumed via env override"},
            ]
        )
        return checks, True

    bwrap_path = shutil.which("bwrap")
    bwrap_available = bool(bwrap_path)
    checks.append(
        {
            "name": "sandbox_bwrap_available",
            "pass": bwrap_available,
            "detail": bwrap_path if bwrap_path else "bwrap not found in PATH",
        }
    )
    if not bwrap_available:
        checks.append(
            {
                "name": "sandbox_bwrap_userns",
                "pass": False,
                "detail": "skipped because bwrap is unavailable",
            }
        )
        checks.append(
            {
                "name": "sandbox_bwrap_exec",
                "pass": False,
                "detail": "skipped because bwrap is unavailable",
            }
        )
        return checks, False

    proc = subprocess.run(["bwrap", "--version"], text=True, capture_output=True, check=False)
    userns_ok = proc.returncode == 0
    checks.append(
        {
            "name": "sandbox_bwrap_userns",
            "pass": userns_ok,
            "detail": (proc.stdout.strip() or proc.stderr.strip() or f"exit={proc.returncode}"),
        }
    )
    dry_run = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--", "sh", "-lc", "true"],
        text=True,
        capture_output=True,
        check=False,
    )
    checks.append(
        {
            "name": "sandbox_bwrap_exec",
            "pass": dry_run.returncode == 0,
            "detail": (dry_run.stdout.strip() or dry_run.stderr.strip() or f"exit={dry_run.returncode}"),
        }
    )
    required_names = {"sandbox_bwrap_available", "sandbox_bwrap_userns", "sandbox_bwrap_exec"}
    by_name = {
        str(item.get("name")): item.get("pass")
        for item in checks
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    sandbox_enforced = all(by_name.get(name) is True for name in required_names)
    return checks, sandbox_enforced


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
        codex_hooks_enabled = features.get("codex_hooks") is True
        checks.append(
            {
                "name": "codex_hooks_enabled",
                "pass": codex_hooks_enabled,
                "detail": f"codex_hooks={features.get('codex_hooks')}",
            }
        )
        can_launch_agents = can_launch_agents and codex_hooks_enabled
        codex_home_check = _probe_codex_home_writable()
        checks.append(codex_home_check)
        can_launch_agents = can_launch_agents and (codex_home_check.get("pass") is True)
    sandbox_checks, sandbox_enforced = _probe_bwrap_sandbox()
    checks.extend(sandbox_checks)
    can_launch_agents = can_launch_agents and sandbox_enforced
    session_policy = {
        "allow_step_agent_launch": os.environ.get("METDSL_ALLOW_STEP_AGENT_LAUNCH", "1").strip().lower()
        not in {"0", "false", "no"},
        "allow_substep_agent_launch": os.environ.get(
            "METDSL_ALLOW_SUBSTEP_AGENT_LAUNCH", "1"
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
        "sandbox_runtime": "bwrap",
        "sandbox_enforced": sandbox_enforced,
        "codex_hooks_enabled": (features.get("codex_hooks") is True) if backend_token == "codex" else None,
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
    source_dependency_ref: str | None = None,
    status: str = "running",
    agent_backend: str = "codex",
) -> dict[str, Any]:
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    (repo_root / "workspace" / "tmp").mkdir(parents=True, exist_ok=True)
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
    if source_dependency_ref:
        meta["source_dependency_ref"] = source_dependency_ref
    meta_path = root / "orchestration_meta.json"
    orchestration_agent_run_id: str | None = None
    existing: dict[str, Any] | None = None
    if meta_path.is_file():
        try:
            existing = _read_json(meta_path)
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            for key in ("parallel_nodes_explicit", "parallel_nodes_policy"):
                if key in existing:
                    meta.setdefault(key, existing[key])
            existing_run_id = existing.get("orchestration_agent_run_id")
            if isinstance(existing_run_id, str) and existing_run_id.strip():
                orchestration_agent_run_id = existing_run_id.strip()
    if not orchestration_agent_run_id:
        orchestration_agent_run_id = str(uuid.uuid4())
    backend_token = str(agent_backend).strip().lower()
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"agent_backend must be one of {sorted(SUPPORTED_BACKENDS)}; got {agent_backend!r}"
        )
    meta["orchestration_agent_run_id"] = orchestration_agent_run_id
    _write_json(meta_path, meta)
    _write_read_access_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=orchestration_agent_run_id,
        allowed_read_roots=["docs/", "spec/", "skills/", "workspace/"],
        denied_read_roots=["tools/"],
    )
    _write_allowed_output_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=orchestration_agent_run_id,
        allowed_output_paths=[
            f"workspace/orchestrations/{orchestration_id}/failure_analysis.json",
        ],
        allowed_file_tool_paths=[
            f"workspace/orchestrations/{orchestration_id}/failure_analysis.json",
        ],
        agent_role="orchestration",
        allowed_tmp_root=f"workspace/tmp/{orchestration_agent_run_id}",
    )
    (repo_root / "workspace" / "tmp" / orchestration_agent_run_id).mkdir(parents=True, exist_ok=True)

    graph_path = root / "agent_graph.json"
    if not graph_path.exists():
        _write_json(graph_path, {"edges": []})

    runs_path = root / "agent_runs.jsonl"
    if not runs_path.exists():
        runs_path.write_text("", encoding="utf-8")
    has_orchestration_running_entry = False
    with runs_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("agent_run_id", "")).strip() == orchestration_agent_run_id
                and str(item.get("agent_role", "")).strip() == "orchestration"
                and str(item.get("status", "")).strip() == "running"
            ):
                has_orchestration_running_entry = True
                break
    if not has_orchestration_running_entry:
        with runs_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "agent_run_id": orchestration_agent_run_id,
                        "agent_role": "orchestration",
                        "agent_backend": backend_token,
                        "status": "running",
                        "started_at": _utc_now_iso(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    _append_session_run_index_entry(
        repo_root,
        orchestration_id,
        agent_run_id=orchestration_agent_run_id,
        agent_session_id=orchestration_agent_run_id,
        context_id=orchestration_agent_run_id,
        agent_role="orchestration",
        status="running",
    )

    pre_orchestration_start(repo_root, orchestration_id, event="init")
    _write_run_write_baseline(repo_root, orchestration_id)
    return meta


def write_preflight(repo_root: Path, orchestration_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _validate_preflight_payload(payload)
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    pre_orchestration_start(repo_root, orchestration_id, event="preflight")

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
    if not isinstance(parent_agent_run_id, str) or not parent_agent_run_id.strip():
        raise ValueError("record-launch requires non-empty parent_agent_run_id")
    parent_agent_run_id = parent_agent_run_id.strip()
    if not _AGENT_RUN_ID_RE.match(parent_agent_run_id):
        raise ValueError(
            f"record-launch: parent_agent_run_id contains invalid characters "
            f"(got {parent_agent_run_id!r}); only alphanumerics, hyphens, and underscores are allowed"
        )
    if not isinstance(child_agent_run_id, str) or not child_agent_run_id.strip():
        raise ValueError("record-launch requires non-empty child_agent_run_id")
    child_agent_run_id = child_agent_run_id.strip()
    if not _AGENT_RUN_ID_RE.match(child_agent_run_id):
        raise ValueError(
            f"record-launch: child_agent_run_id contains invalid characters "
            f"(got {child_agent_run_id!r}); only alphanumerics, hyphens, and underscores are allowed"
        )
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
    preflight_backend = (
        str(preflight_payload.get("backend", "")).strip().lower()
        if isinstance(preflight_payload, dict)
        else ""
    )
    backend_token = preflight_backend if preflight_backend in SUPPORTED_BACKENDS else "codex"

    # Claude backend は active file で sequential child launch を強制する。
    if backend_token == "claude":
        active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
        if active_path.exists():
            existing_id = active_path.read_text(encoding="utf-8").strip()
            try:
                update_orchestration_status(
                    repo_root,
                    orchestration_id,
                    status="fail_closed",
                    reason_code="parallel_nodes_not_explicitly_allowed",
                )
            except Exception:
                pass
            raise RuntimeError(
                "Claude backend sequential violation: "
                f"active child agent {existing_id!r} is still running. "
                "Parallel child agent launch is not permitted on Claude backend."
            )

    step_raw = request_payload.get("step")
    node_key_raw = request_payload.get("node_key")
    if isinstance(step_raw, str) and step_raw.strip() and isinstance(node_key_raw, str) and node_key_raw.strip():
        required = _required_child_agent_kind(step_raw)
        launch_ctx = dict(request_payload)
        check = pre_phase_launch(
            repo_root,
            orchestration_id=orchestration_id,
            node_key=node_key_raw.strip(),
            step=step_raw.strip(),
            backend=backend_token,
            require_child_agent=required,
            launch_request=launch_ctx,
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
                "record-launch blocked by pre_phase_launch / workflow-launch-check: "
                f"reason_code={reason_code}"
            )
    backend_command = "codex"
    if isinstance(preflight_payload, dict):
        probe_command = preflight_payload.get("probe_command")
        if isinstance(probe_command, str) and probe_command.strip():
            backend_command = probe_command.strip()
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
    launch_role_obj = request_payload.get("agent_role")
    if isinstance(launch_role_obj, str) and launch_role_obj.strip():
        launch_role = launch_role_obj.strip().lower()
    else:
        try:
            launch_role = _required_child_agent_kind(str(request_payload.get("step", "") or ""))
        except ValueError:
            launch_role = "unknown"
    context_obj = request_payload.get("context_id")
    launch_context_id = context_obj.strip() if isinstance(context_obj, str) and context_obj.strip() else None

    _validate_launch_request_payload(request_payload)
    _append_session_run_index_entry(
        repo_root,
        orchestration_id,
        agent_run_id=child_agent_run_id,
        agent_session_id=response_agent_session_id,
        context_id=launch_context_id,
        agent_role=launch_role,
        status="running",
    )

    prompt_text = _extract_launch_prompt_text(request_payload)
    reply_text = _extract_launch_reply_text(response_payload)
    if not prompt_text.strip():
        raise ValueError("launch prompt text must be non-empty")
    if not reply_text.strip():
        raise ValueError("launch reply text must be non-empty")
    _validate_launch_prompt_text(request_payload, prompt_text)
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_agent_launch",
        status="allow",
        detail={"child_agent_run_id": child_agent_run_id},
    )

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
    if not (isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip()):
        raise ValueError("record-launch requires non-empty node_key and step for sandbox-enforced launch")
    if isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip():
        _write_access_policy_for_launch(
            repo_root,
            orchestration_id,
            child_agent_run_id,
            request_payload,
        )
        policy_doc = _read_json(
            _access_policies_dir(repo_root, orchestration_id) / f"{child_agent_run_id}.json"
        )
        if not isinstance(policy_doc, dict):
            raise ValueError("access policy must be object for read manifest generation")
        allowed_read_roots_obj = policy_doc.get("allowed_read_roots")
        denied_read_roots_obj = policy_doc.get("denied_read_roots")
        allowed_read_roots = (
            [str(item) for item in allowed_read_roots_obj]
            if isinstance(allowed_read_roots_obj, list)
            else []
        )
        denied_read_roots = (
            [str(item) for item in denied_read_roots_obj]
            if isinstance(denied_read_roots_obj, list)
            else []
        )
        read_manifest_ref = _write_read_access_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=child_agent_run_id,
            allowed_read_roots=allowed_read_roots,
            denied_read_roots=denied_read_roots,
        )
        out_refs["read_access_manifest_ref"] = read_manifest_ref
        cap_doc = _write_capability_for_launch(
            repo_root,
            orchestration_id,
            child_agent_run_id,
            request_payload,
        )
        cap_rel = f"workspace/orchestrations/{orchestration_id}/capabilities/{child_agent_run_id}.json"
        out_refs["capability_ref"] = cap_rel
        out_refs["capability_token"] = cap_doc.get("capability_token", "")
        write_roots_obj = cap_doc.get("write_roots")
        write_roots = [str(item) for item in write_roots_obj] if isinstance(write_roots_obj, list) else []
        allowed_output_paths = _allowed_output_paths_for_launch(
            request_payload=request_payload,
            write_roots=write_roots,
        )
        allowed_file_tool_paths = _allowed_file_tool_paths_for_launch(
            request_payload=request_payload,
            allowed_output_paths=allowed_output_paths,
        )
        _validate_child_write_contract_preflight(
            request_payload=request_payload,
            capability_doc=cap_doc,
            allowed_output_paths=allowed_output_paths,
        )
        (repo_root / "workspace" / "tmp" / child_agent_run_id).mkdir(parents=True, exist_ok=True)
        manifest_ref = _write_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=child_agent_run_id,
            allowed_output_paths=allowed_output_paths,
            allowed_file_tool_paths=allowed_file_tool_paths,
            allowed_tmp_root=f"workspace/tmp/{child_agent_run_id}",
        )
        out_refs["allowed_output_manifest_ref"] = manifest_ref
        try:
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=child_agent_run_id,
                backend_command=backend_command,
            )
            command_argv = [backend_command]
            rendered = render_bwrap_command(profile=profile, command_argv=command_argv)
            profile["rendered_command"] = rendered
            profile_path = _sandbox_profiles_dir(
                repo_root,
                orchestration_id,
            ) / f"{child_agent_run_id}.json"
            _write_json(profile_path, profile)
            sandbox_ref = (
                f"workspace/orchestrations/{orchestration_id}/sandbox_profiles/{child_agent_run_id}.json"
            )
            out_refs["sandbox_profile_ref"] = sandbox_ref
            request_payload.setdefault("sandbox_profile_ref", sandbox_ref)
            response_payload.setdefault("sandbox_runtime", "bwrap")
            response_payload.setdefault("sandbox_enforced", True)
            response_payload.setdefault("sandbox_profile_ref", sandbox_ref)
            response_payload.setdefault("sandbox_command", rendered)
        except Exception as exc:
            _write_sandbox_enforcement_violation(
                repo_root,
                orchestration_id,
                agent_run_id=child_agent_run_id,
                reason="sandbox_profile_build_failed",
                detail={"error": str(exc)},
            )
            update_orchestration_status(
                repo_root,
                orchestration_id,
                status="fail_closed",
                reason_code="sandbox_enforcement_violation",
                reason_detail=str(exc),
                blocking_policy_scope="sandbox",
            )
            raise RuntimeError(f"record-launch sandbox enforcement failed: {exc}") from exc
    _write_json(request_path, request_payload)
    _write_json(response_path, response_payload)
    _write_text(prompt_path, prompt_text)
    _write_text(reply_path, reply_text)
    _write_json(child_request_path, request_payload)
    _write_json(child_response_path, response_payload)
    _write_text(child_prompt_path, prompt_text)
    _write_text(child_reply_path, reply_text)
    if isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip():
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
    _write_run_write_baseline(
        repo_root,
        orchestration_id,
        agent_run_id=child_agent_run_id,
    )
    if backend_token == "claude":
        _active_child_agent_run_id_path(repo_root, orchestration_id).write_text(
            child_agent_run_id,
            encoding="utf-8",
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
        sandbox_ref = launch_response_payload.get("sandbox_profile_ref")
        if launch_response_payload.get("sandbox_runtime") != "bwrap":
            _write_sandbox_enforcement_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id,
                reason="sandbox_runtime_not_bwrap",
                detail={"launch_response_ref": payload["launch_response_ref"]},
            )
            raise ValueError("launch response must record sandbox_runtime=bwrap")
        if launch_response_payload.get("sandbox_enforced") is not True:
            _write_sandbox_enforcement_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id,
                reason="sandbox_not_enforced",
                detail={"launch_response_ref": payload["launch_response_ref"]},
            )
            raise ValueError("launch response must record sandbox_enforced=true")
        if not isinstance(sandbox_ref, str) or not sandbox_ref.strip():
            _write_sandbox_enforcement_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id,
                reason="sandbox_profile_missing",
                detail={"launch_response_ref": payload["launch_response_ref"]},
            )
            raise ValueError("launch response must include sandbox_profile_ref")
        sandbox_path = repo_root / str(sandbox_ref).strip()
        if not sandbox_path.exists():
            _write_sandbox_enforcement_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id,
                reason="sandbox_profile_not_found",
                detail={"sandbox_profile_ref": sandbox_ref},
            )
            raise ValueError(f"sandbox_profile_ref target not found: {sandbox_ref}")
        payload.setdefault("sandbox_runtime", "bwrap")
        payload.setdefault("sandbox_enforced", True)
        payload.setdefault("sandbox_profile_ref", str(sandbox_ref).strip())

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

    status_token = str(payload.get("status", "")).strip().lower()
    if status_token in TERMINAL_STATUSES:
        session_obj = payload.get("agent_session_id")
        session_value = session_obj.strip() if isinstance(session_obj, str) and session_obj.strip() else agent_run_id
        context_obj = payload.get("context_id")
        context_value = context_obj.strip() if isinstance(context_obj, str) and context_obj.strip() else None
        _append_session_run_index_entry(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id,
            agent_session_id=session_value,
            context_id=context_value,
            agent_role=role_token,
            status=status_token,
        )

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
            if backend_token == "claude":
                _active_child_agent_run_id_path(repo_root, orchestration_id).unlink(missing_ok=True)

    agent_tmp = repo_root / "workspace" / "tmp" / agent_run_id
    if agent_tmp.exists():
        shutil.rmtree(agent_tmp, ignore_errors=True)

    return payload


def deactivate_child_agent(
    repo_root: Path,
    orchestration_id: str,
    *,
    child_run_id: str,
) -> dict[str, Any]:
    active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
    if not active_path.exists():
        return {
            "deactivated_child_run_id": child_run_id,
            "orchestration_id": orchestration_id,
            "deactivated_at": _utc_now_iso(),
            "already_inactive": True,
        }
    active_value = active_path.read_text(encoding="utf-8").strip()
    if active_value != child_run_id:
        raise ValueError(
            "active child run mismatch: "
            f"expected={child_run_id!r}, actual={active_value!r}"
        )
    active_path.unlink()
    return {
        "deactivated_child_run_id": child_run_id,
        "orchestration_id": orchestration_id,
        "deactivated_at": _utc_now_iso(),
        "already_inactive": False,
    }


def record_reply_text(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    reply_text: str,
) -> dict[str, Any]:
    if not isinstance(reply_text, str) or not reply_text.strip():
        raise ValueError("record-reply requires non-empty reply_text")
    _, reply_ref = _launch_dialog_refs(orchestration_id, agent_run_id)
    reply_path = repo_root / reply_ref
    _write_text(reply_path, reply_text)
    return {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "reply_ref": reply_ref,
        "recorded_at": _utc_now_iso(),
    }


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

    try:
        post_phase_complete(
            repo_root,
            orchestration_id,
            node_key=node_key,
            step=step,
            agent_run_id=agent_run_id,
            payload=result,
        )
    except RuntimeError:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

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


def _select_patch_strip(
    repo_root: Path,
    patch_text: str,
    normalized_paths: list[str],
) -> tuple[int, list[str]]:
    """Determine the correct -p<strip> level using changed_paths as the disambiguation oracle.

    Runs 'git apply --numstat' with strip=1 (standard git format) then strip=0 (bare paths),
    selecting the first level whose numstat targets are all covered by normalized_paths.

    Heuristic pattern-matching on 'diff --git a/<X> b/<X>' headers is fundamentally ambiguous:
    those headers are identical whether the patch is git-prefix format (target=<X>, strip=1)
    or a bare rename between real directories 'a/' and 'b/' (target='b/<X>', strip=0).
    Using changed_paths as the oracle resolves this without false assumptions.

    Returns (strip, numstat_targets). Raises RuntimeError if neither strip level produces
    targets covered by changed_paths (includes mixed-prefix and out-of-scope patches).
    """
    for strip in (1, 0):
        try:
            numstat = _numstat_targets(repo_root, patch_text, strip)
        except RuntimeError:
            continue
        if numstat and all(
            any(_repo_path_under_prefix(p, cp) for cp in normalized_paths) for p in numstat
        ):
            return strip, numstat
    raise RuntimeError(
        "guarded-apply-patch: cannot determine patch strip level — "
        "neither -p1 nor -p0 produces targets covered by declared changed_paths "
        "(patch may have mixed prefixes or targets outside changed_paths). "
        f"changed_paths={normalized_paths}"
    )


def _extract_patch_target_paths(patch_text: str, strip: int = 1) -> list[str]:
    targets: list[str] = []
    for raw in patch_text.splitlines():
        line = raw.strip()
        if not line.startswith("+++ "):
            continue
        token = line[4:].strip()
        if token == "/dev/null":
            continue
        if strip == 1 and token.startswith("b/"):
            token = token[2:]
        if ".." in token.split("/"):
            raise RuntimeError(
                f"guarded-apply-patch: patch path traversal detected: {token!r}"
            )
        norm = _normalize_rel_posix(token)
        if norm:
            targets.append(norm)
    return sorted(set(targets))


def _extract_rename_sources(patch_text: str) -> list[str]:
    """Return the source paths of all 'rename from' directives in the patch.

    Rename operations delete the source file, which is a destructive side-effect that
    must be authorized independently from the destination path.  'rename from' lines
    use raw repo-relative paths (no a/b/ prefix) regardless of the strip level, so no
    strip logic is needed here.  Copy sources are intentionally excluded: 'copy from'
    leaves the source intact and only creates a new destination file.
    """
    sources: list[str] = []
    for raw in patch_text.splitlines():
        line = raw.strip()
        if not line.startswith("rename from "):
            continue
        token = line[len("rename from "):].strip()
        if not token:
            continue
        norm = _normalize_rel_posix(token)
        if norm:
            sources.append(norm)
    return sorted(set(sources))


def _numstat_targets(repo_root: Path, patch_text: str, strip: int) -> list[str]:
    """Dry-run git apply --numstat -z to enumerate the paths git will actually touch.

    Uses -z so that git outputs NUL-terminated raw byte paths instead of quoting/escaping
    filenames that contain tabs, newlines, double-quotes, or backslashes.  With -z, each
    record is '<added>\\t<deleted>\\t<dest-path>\\0'.  For renames git outputs the
    destination path only (the file that will exist after apply), which is what we need.
    """
    proc = subprocess.run(
        ["git", "apply", "--numstat", "-z", "--check", f"-p{strip}", "-"],
        cwd=str(repo_root),
        input=patch_text.encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raw_err = proc.stderr or proc.stdout or b""
        if isinstance(raw_err, str):
            raw_err = raw_err.encode()
        msg = raw_err.decode(errors="replace").strip()
        raise RuntimeError(f"guarded-apply-patch: pre-apply numstat failed: {msg}")
    raw_out = proc.stdout if isinstance(proc.stdout, bytes) else (proc.stdout or "").encode()
    targets: list[str] = []
    for record in raw_out.split(b"\0"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) < 3:
            continue
        path_bytes = parts[2]
        try:
            path_str = path_bytes.decode()
        except UnicodeDecodeError:
            path_str = path_bytes.decode(errors="replace")
        norm = _normalize_rel_posix(path_str)
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

    strip, numstat_targets = _select_patch_strip(repo_root, patch_text, normalized_paths)
    # numstat_targets is the authoritative write set: already verified to be covered by
    # changed_paths inside _select_patch_strip.  It correctly handles mode-only patches
    # and pure renames that have no '+++ ' lines and would produce an empty patch_targets.
    patch_targets = _extract_patch_target_paths(patch_text, strip=strip)
    # Security check: if the +++ header parser claims a path that git's numstat does NOT
    # include, the patch text may be trying to deceive the gate.  We only reject for
    # parser-exclusive paths; numstat-exclusive paths (mode-only, renames) are fine.
    numstat_set = set(numstat_targets)
    parser_exclusive = [p for p in patch_targets if p not in numstat_set]
    if parser_exclusive:
        raise RuntimeError(
            "guarded-apply-patch: +++ headers declare paths absent from git-apply numstat "
            f"(strip={strip}); suspicious_paths={parser_exclusive} numstat={numstat_targets}"
        )
    # Defense-in-depth: re-verify all git-resolved targets are within declared changed_paths.
    not_covered = [
        p for p in numstat_targets
        if not any(_repo_path_under_prefix(p, cp) for cp in normalized_paths)
    ]
    if not_covered:
        raise RuntimeError(
            "guarded-apply-patch: patch targets are not covered by changed_paths: "
            + ", ".join(not_covered)
        )
    # Rename-source check: 'rename from' deletes the source file, which is a destructive
    # side-effect that must also be authorized.  numstat only reports the destination, so
    # we parse 'rename from' lines directly and require each source to be in changed_paths.
    rename_sources = _extract_rename_sources(patch_text)
    uncovered_sources = [
        p for p in rename_sources
        if not any(_repo_path_under_prefix(p, cp) for cp in normalized_paths)
    ]
    if uncovered_sources:
        raise RuntimeError(
            "guarded-apply-patch: rename source paths are not covered by changed_paths "
            "(rename deletes the source file, which must be explicitly authorized): "
            + ", ".join(uncovered_sources)
        )
    actor_role_token = actor_role.strip().lower()
    if actor_role_token in {"step", "substep"}:
        _validate_paths_against_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=agent_run_id,
            paths=normalized_paths,
        )

    gate_result = gate_apply_patch_writes(
        repo_root,
        orchestration_id=orchestration_id,
        actor_role=actor_role,
        changed_paths=normalized_paths,
        agent_run_id=agent_run_id,
        capability_token=capability_token,
    )
    proc = subprocess.run(
        ["git", "apply", "--recount", "--whitespace=nowarn", f"-p{strip}", "-"],
        cwd=str(repo_root),
        text=True,
        input=patch_text,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"guarded-apply-patch: git apply failed: {msg}")
    gate_ref = _write_apply_patch_gate_evidence(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
        actor_role=actor_role,
        changed_paths=normalized_paths,
        result_payload=gate_result,
    )

    return {
        "applied": True,
        "changed_paths": normalized_paths,
        "patch_targets": numstat_targets,
        "gate_result_ref": gate_ref,
    }


def _validate_record_launch_response_fields(payload: dict[str, Any]) -> None:
    """record-launch --response-json の必須フィールドを CLI dispatch 時点で検証する。"""
    label = "record-launch --response-json"
    for key in ("agent_run_id", "agent_session_id", "started_at", "backend"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"{label}: required field {key!r} is missing or empty. "
                f"For Claude Code use: {{\"agent_run_id\": \"<uuid>\", "
                f"\"agent_session_id\": \"<same uuid>\", "
                f"\"started_at\": \"<ISO8601>\", \"backend\": \"claude\"}}"
            )
    backend = payload["backend"].strip()
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"{label}: 'backend' must be one of {sorted(SUPPORTED_BACKENDS)}; got {backend!r}"
        )


def _validate_record_agent_run_fields(payload: dict[str, Any]) -> None:
    """record-agent-run --agent-run-json の必須フィールドを CLI dispatch 時点で検証する。"""
    label = "record-agent-run --agent-run-json"
    for key in ("agent_run_id", "agent_backend", "status"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}: required field {key!r} is missing or empty")
    role_raw = payload.get("agent_role") or payload.get("agent_type") or payload.get("role")
    if not isinstance(role_raw, str) or not role_raw.strip():
        raise ValueError(f"{label}: required field 'agent_role' is missing or empty")
    role_token = role_raw.strip().lower()
    if role_token in {"step", "substep"}:
        for key in ("node_key", "agent_session_id"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{label}: field {key!r} is required for agent_role={role_token!r}"
                )


def _validate_write_step_result_fields(payload: dict[str, Any], step: str) -> None:
    """write-step-result --result-json の必須フィールドを CLI dispatch 時点で検証する。"""
    label = "write-step-result --result-json"
    status_raw = payload.get("status")
    if not isinstance(status_raw, str) or not status_raw.strip():
        raise ValueError(f"{label}: required field 'status' is missing or empty")
    status_token = status_raw.strip().lower()
    substep_ids = payload.get("substep_agent_run_ids")
    if not isinstance(substep_ids, list):
        type_name = type(substep_ids).__name__ if substep_ids is not None else "missing"
        raise ValueError(
            f"{label}: required field 'substep_agent_run_ids' must be a list (got {type_name}). "
            "Use an empty list [] for step-only phases (build/execute/judge/promote)."
        )
    step_token = step.strip().lower()
    if step_token in STEP_REQUIRED_VALIDATION_STAGES and status_token in TERMINAL_STATUSES:
        allowed = STEP_REQUIRED_VALIDATION_STAGES[step_token]
        validation_stage = payload.get("validation_stage")
        if not isinstance(validation_stage, str) or validation_stage.strip() not in allowed:
            raise ValueError(
                f"{label}: step={step_token!r} with terminal status={status_token!r} requires "
                f"'validation_stage' to be one of {sorted(allowed)}; "
                f"got {validation_stage!r}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--repo-root", required=True)
    init_parser.add_argument("--orchestration-id", required=True)
    init_parser.add_argument("--spec-ref")
    init_parser.add_argument("--source-dependency-ref")
    init_parser.add_argument("--status", default="running")
    init_parser.add_argument("--agent-backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
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

    _NODE_KEY_HELP = (
        "node_key in '<spec_kind>/<spec_id>@<spec_version>' format "
        "(e.g. 'component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0'). "
        "Derived from deps.yaml (spec_kind, spec_id) and controlled_spec.md (spec_version). "
        "NOT a filesystem path."
    )
    _RECORD_LAUNCH_REQUEST_HELP = (
        "JSON object with launch parameters. Required fields: "
        "agent_role ('step'|'substep'), node_key (<spec_kind>/<spec_id>@<spec_version>), "
        "step, substep (for substep agents), orchestration_id, agent_run_id, "
        "parent_agent_run_id, workflow_mode ('dev'|'prod'), "
        "plan_ref (workspace/plans/<node_key_safe>/<plan_id>), "
        "pipeline_ref (workspace/pipelines/<node_key_safe>/<pipeline_id> -- required for ALL "
        "phases including Plan; reserve via reserve-phase-root --step generate if not yet created), "
        "dependency_ref (phase rule: Plan => spec/.../deps.yaml; Generate+ => workspace phase root), "
        "skill_name, skill_ref. "
        "For step/substep launch, one of allowed_output_paths|required_outputs|output_refs must be provided "
        "as file-path list; runtime validates each path against phase contract outputs and capability write_roots. "
        "allowed_file_tool_paths is optional and, when provided, must be a file-path list included in allowed_output_paths. "
        "plan_id/pipeline_id format: <slug>_<YYYYMMDD>_<seq3> where slug uses hyphens only "
        "(e.g. 'flux-rsn-p0_20260425_001'; underscores in slug are invalid)."
    )
    _RECORD_LAUNCH_RESPONSE_HELP = (
        "JSON object with child agent response. For Claude Code backend use: "
        '{"agent_run_id": "<uuid>", "agent_session_id": "<same uuid>", '
        '"started_at": "<ISO8601>", "backend": "claude"}. '
        "sandbox_runtime/sandbox_enforced/sandbox_profile_ref are added automatically. "
        "Call record-launch BEFORE Agent tool so capability_token is available to the child agent; "
        "then overwrite launches/<child_agent_run_id>.reply.txt with the actual Agent tool response."
    )
    _RUN_GATE_ARGS_HELP = (
        "JSON object for gate-specific arguments. Allowed gates and minimal args_json schema: "
        "orchestration_read => {'read_path': 'docs/...'}; "
        "validate_workspace_root => {'paths': ['workspace']} (optional, defaults to repo workspace); "
        "check_artifact_syntax => {'expect_top': 'object', 'paths': ['workspace/.../file.yaml', ...]}; "
        "validate_pipeline_semantics => {'stage': 'plan|post_generate|post_build|post_execute|pre_judge|full', "
        "'plan_ref': 'workspace/plans/...'(plan stage), "
        "'pipeline_root': 'workspace/pipelines/...' or ['workspace/pipelines/...', ...], "
        "'generation_id': '<id>' (optional)}. "
        "Keys are converted to CLI flags (e.g. pipeline_root -> --pipeline-root)."
    )
    _STEP_RESULT_HELP = (
        "JSON object for step_result. Required: status, required_outputs (list[str]), "
        "executor_agent_run_id, substep_agent_run_ids (list[str], empty list allowed for step-only phases), "
        "failed_substeps (list[str], optional), retry_decisions (list[object], optional). "
        "retry_decisions items require: issue_severity, repair_strategy, repair_target_agent_run_id, "
        "new_agent_run_id, repair_reason. "
        "When step in {generate,build,execute,judge} and status is terminal "
        "(pass/fail/blocked/timeout/cancel), validation_stage is required: "
        "generate=>post_generate|full, build=>post_build|full, execute=>post_execute|pre_judge|full, "
        "judge=>pre_judge|full. "
        "For plan/generate/tune pass, required_outputs must be covered by effective substep output_refs."
    )

    launch_parser = subparsers.add_parser(
        "record-launch",
        description=(
            "Record a child agent launch: runs live preflight, generates capability_token, "
            "sandbox profile, output/read manifests, and writes launches/<child_id>.* artifacts. "
            "For Claude Code: call this BEFORE Agent tool invocation so the child can read "
            "its capability_token from capabilities/<child_id>.json during execution."
        ),
    )
    launch_parser.add_argument("--repo-root", required=True)
    launch_parser.add_argument("--orchestration-id", required=True)
    launch_parser.add_argument("--parent-agent-run-id", required=True,
                               help="UUID of the orchestration (parent) agent.")
    launch_parser.add_argument("--child-agent-run-id", required=True,
                               help="UUID pre-generated for the child agent. "
                                    "For Claude Code this also becomes agent_session_id.")
    launch_parser.add_argument("--request-json", required=True, type=_json_arg,
                               help=_RECORD_LAUNCH_REQUEST_HELP)
    launch_parser.add_argument("--response-json", required=True, type=_json_arg,
                               help=_RECORD_LAUNCH_RESPONSE_HELP)
    launch_parser.add_argument("--relation-type", default="launch")

    orch_read_parser = subparsers.add_parser("orchestration-read")
    orch_read_parser.add_argument("--repo-root", required=True)
    orch_read_parser.add_argument("--orchestration-id", required=True)
    orch_read_parser.add_argument("--agent-run-id", required=True)
    orch_read_parser.add_argument("--read-path", required=True)
    orch_read_parser.add_argument("--capability-token", required=True)

    guarded_patch_parser = subparsers.add_parser("guarded-apply-patch")
    guarded_patch_parser.add_argument("--repo-root", required=True)
    guarded_patch_parser.add_argument("--orchestration-id", required=True)
    guarded_patch_parser.add_argument("--actor-role", required=True)
    guarded_patch_parser.add_argument("--agent-run-id", required=True)
    guarded_patch_parser.add_argument("--paths-json", required=True, type=_json_string_list_arg)
    guarded_patch_parser.add_argument("--patch-text", default=None)
    guarded_patch_parser.add_argument(
        "--patch-file",
        default=None,
        help="Path to a file containing the unified diff. Mutually exclusive with --patch-text. "
             "Use this to avoid OS ARG_MAX limits for large patches.",
    )
    guarded_patch_parser.add_argument("--capability-token", required=True)

    gate_parser = subparsers.add_parser(
        "run-gate",
        description=(
            "Execute a validator gate under orchestration policy. "
            "Use this as the canonical validator invocation path when capability-token/gate enforcement is required."
        ),
    )
    gate_parser.add_argument("--repo-root", required=True)
    gate_parser.add_argument("--orchestration-id", required=True)
    gate_parser.add_argument(
        "--gate",
        required=True,
        choices=sorted(DEFAULT_ALLOWED_GATE_SERVICES),
        help=(
            "Gate name. "
            "validate_pipeline_semantics | check_artifact_syntax | validate_workspace_root | orchestration_read"
        ),
    )
    gate_parser.add_argument("--agent-run-id", required=True)
    gate_parser.add_argument("--args-json", required=True, type=_json_arg, help=_RUN_GATE_ARGS_HELP)
    gate_parser.add_argument("--capability-token", required=True)

    run_parser = subparsers.add_parser(
        "record-agent-run",
        description=(
            "Append one agent run record to agent_runs.jsonl. "
            "For step/substep roles also writes agent.result.json and agent.summary.txt, "
            "and validates that output_refs lie within the capability write_roots."
        ),
    )
    run_parser.add_argument("--repo-root", required=True)
    run_parser.add_argument("--orchestration-id", required=True)
    run_parser.add_argument(
        "--agent-run-json", required=True, type=_json_arg,
        help=(
            "JSON object for the agent run record. "
            "Always required: agent_run_id (UUID), agent_role ('orchestration'|'step'|'substep'), "
            "agent_backend ('claude'|'codex'|'cursor'), status ('running'|'pass'|'fail'|...), "
            "started_at (ISO8601). "
            "Required for step/substep: agent_session_id (for Claude Code = agent_run_id), "
            "context_id (unique UUID per run), context_isolated (true), node_key "
            "(<spec_kind>/<spec_id>@<spec_version>). "
            "Required when status is a terminal state (pass/fail/blocked/timeout/cancel): "
            "finished_at (ISO8601). "
            "Required on pass: output_refs (list of written artifact paths)."
        ),
    )

    step_parser = subparsers.add_parser(
        "write-step-result",
        description=(
            "Write step_result.json for one step run and validate required fields, "
            "retry semantics, and required_outputs coverage."
        ),
    )
    step_parser.add_argument("--repo-root", required=True)
    step_parser.add_argument("--orchestration-id", required=True)
    step_parser.add_argument("--node-key", required=True)
    step_parser.add_argument("--step", required=True)
    step_parser.add_argument("--agent-run-id", required=True)
    step_parser.add_argument("--result-json", required=True, type=_json_arg, help=_STEP_RESULT_HELP)

    deactivate_child_parser = subparsers.add_parser("deactivate-child")
    deactivate_child_parser.add_argument("--repo-root", required=True)
    deactivate_child_parser.add_argument("--orchestration-id", required=True)
    deactivate_child_parser.add_argument("--child-run-id", required=True)

    record_reply_parser = subparsers.add_parser("record-reply")
    record_reply_parser.add_argument("--repo-root", required=True)
    record_reply_parser.add_argument("--orchestration-id", required=True)
    record_reply_parser.add_argument("--agent-run-id", required=True)
    record_reply_parser.add_argument("--reply-text")
    record_reply_parser.add_argument("--reply-from-stdin", action="store_true")

    status_parser = subparsers.add_parser("set-status")
    status_parser.add_argument("--repo-root", required=True)
    status_parser.add_argument("--orchestration-id", required=True)
    status_parser.add_argument("--status", required=True)
    status_parser.add_argument("--reason-code")
    status_parser.add_argument("--reason-detail")
    status_parser.add_argument("--blocking-policy-scope")

    launch_check_parser = subparsers.add_parser(
        "workflow-launch-check",
        description=(
            "Pre-phase gate: checks execution platform availability, session policy, "
            "dependency readiness, and required child agent kind. "
            "Returns JSON with status ('pass'|'fail_closed') and next_action. "
            "Run once before the first phase; fail_closed must stop the orchestration."
        ),
    )
    launch_check_parser.add_argument("--repo-root", required=True)
    launch_check_parser.add_argument("--orchestration-id", required=True)
    launch_check_parser.add_argument(
        "--node-key", required=True,
        help=_NODE_KEY_HELP,
    )
    launch_check_parser.add_argument("--step", required=True,
                                     help="Workflow step name: plan, generate, build, execute, judge, etc.")
    launch_check_parser.add_argument("--backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
    launch_check_parser.add_argument(
        "--require-child-agent", required=True, choices=("step", "substep"),
        help="Expected child agent kind. Plan/Generate/Tune require 'substep'; "
             "Build/Execute/Judge/Promote require 'step'.",
    )
    launch_check_parser.add_argument(
        "--launch-request-json",
        default=None,
        type=_json_arg,
        help="Optional launch request object for downstream artifact checks (pre_phase_launch).",
    )

    reserve_root_parser = subparsers.add_parser(
        "reserve-phase-root",
        description=(
            "Reserve a plan_id or pipeline_id before the child agent creates the directory. "
            "Writes a reservation marker only; does NOT create workspace/plans/ or "
            "workspace/pipelines/ directories. "
            "Use --step plan to reserve a plan_id; --step generate to reserve a pipeline_id. "
            "Both reservations are typically needed before launching Plan phase substeps, "
            "because record-launch requires a valid pipeline_ref even for Plan."
        ),
    )
    reserve_root_parser.add_argument("--repo-root", required=True)
    reserve_root_parser.add_argument("--orchestration-id", required=True)
    reserve_root_parser.add_argument(
        "--node-key", required=True,
        help=_NODE_KEY_HELP,
    )
    reserve_root_parser.add_argument("--step", required=True,
                                     help="'plan' to reserve a plan_id; 'generate' to reserve a pipeline_id.")
    reserve_root_parser.add_argument(
        "--reserved-id", required=True,
        help=(
            "The plan_id or pipeline_id to reserve. "
            "Format: <slug>_<YYYYMMDD>_<seq3> where slug is hyphen-separated lowercase alphanumeric "
            "(regex: ^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$). "
            "Example: 'flux-rsn-p0_20260425_001'. "
            "Underscores inside the slug are INVALID (use hyphens instead)."
        ),
    )
    reserve_root_parser.add_argument("--reserved-by-agent-run-id", required=True,
                                     help="UUID of the agent that will use this reserved ID.")

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
                source_dependency_ref=args.source_dependency_ref,
                status=args.status,
                agent_backend=args.agent_backend,
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
        try:
            _validate_record_launch_response_fields(args.response_json)
            result = record_launch(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                parent_agent_run_id=args.parent_agent_run_id,
                child_agent_run_id=args.child_agent_run_id,
                request_payload=args.request_json,
                response_payload=args.response_json,
                relation_type=args.relation_type,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
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
    elif args.command == "guarded-apply-patch":
        try:
            if args.patch_text is not None and args.patch_file is not None:
                print("error: --patch-text and --patch-file are mutually exclusive", file=sys.stderr)
                return 1
            if args.patch_text is None and args.patch_file is None:
                print("error: one of --patch-text or --patch-file is required", file=sys.stderr)
                return 1
            if args.patch_file is not None:
                import stat as _stat_mod
                _PATCH_FILE_MAX_BYTES = int(
                    os.environ.get("METDSL_PATCH_FILE_MAX_BYTES", 10 * 1024 * 1024)
                )
                _pf_fd = -1
                # Validate agent_run_id is a bare UUID before any path construction.
                # A traversal segment (e.g. "../..") in agent_run_id would expand the
                # allowed tmp root above the intended per-agent directory.
                _UUID_RE = re.compile(
                    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    re.IGNORECASE,
                )
                if not _UUID_RE.match(args.agent_run_id):
                    print(
                        f"error: --agent-run-id '{args.agent_run_id}' is not a valid UUID",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    _allowed_tmp = (
                        Path(repo_root) / "workspace" / "tmp" / args.agent_run_id
                    ).resolve()
                    # Pre-open confinement check using strict resolve (follows symlinks).
                    # This is the fallback path when /proc/self/fd is unavailable.
                    # O_NOFOLLOW (below) ensures the final path component cannot be a
                    # symlink, so the only remaining TOCTOU window is on directory
                    # components — which are within the agent-owned tmp root.
                    _pf_pre_resolved = Path(args.patch_file).resolve(strict=True)
                    if os.path.commonpath([_pf_pre_resolved, _allowed_tmp]) != str(_allowed_tmp):
                        print(
                            f"error: --patch-file '{args.patch_file}' is outside the agent's "
                            f"allowed tmp root '{_allowed_tmp}'. "
                            f"Use $TMPDIR to construct the path.",
                            file=sys.stderr,
                        )
                        return 1
                    # O_NOFOLLOW refuses to open if the final path component is a symlink,
                    # preventing leaf-level symlink swap between the pre-open check and open.
                    _o_nofollow = getattr(os, "O_NOFOLLOW", 0)
                    _pf_fd = os.open(args.patch_file, os.O_RDONLY | _o_nofollow)
                    # On Linux, re-verify confinement via /proc/self/fd/<fd> — this check
                    # operates on the already-open fd and is fully race-free. If /proc is
                    # unavailable (non-Linux, restricted container) the OSError is caught
                    # and we rely on the pre-open resolve check + O_NOFOLLOW above.
                    try:
                        _fd_real = Path(os.readlink(f"/proc/self/fd/{_pf_fd}")).resolve()
                        if os.path.commonpath([_fd_real, _allowed_tmp]) != str(_allowed_tmp):
                            print(
                                f"error: --patch-file '{args.patch_file}' is outside the agent's "
                                f"allowed tmp root '{_allowed_tmp}'. "
                                f"Use $TMPDIR to construct the path.",
                                file=sys.stderr,
                            )
                            return 1
                    except OSError:
                        pass  # /proc unavailable; pre-open check + O_NOFOLLOW suffice
                    # fstat operates on the open fd — not subject to path races.
                    _pf_stat = os.fstat(_pf_fd)
                    if not _stat_mod.S_ISREG(_pf_stat.st_mode):
                        print(
                            f"error: --patch-file '{args.patch_file}' is not a regular file "
                            f"(mode {_pf_stat.st_mode:#o})",
                            file=sys.stderr,
                        )
                        return 1
                    if _pf_stat.st_size > _PATCH_FILE_MAX_BYTES:
                        print(
                            f"error: --patch-file '{args.patch_file}' size {_pf_stat.st_size} bytes "
                            f"exceeds limit {_PATCH_FILE_MAX_BYTES} bytes",
                            file=sys.stderr,
                        )
                        return 1
                    # fdopen takes ownership of _pf_fd; mark sentinel before hand-off.
                    _pf_fobj = os.fdopen(_pf_fd, "r", encoding="utf-8")
                    _pf_fd = -1
                    with _pf_fobj:
                        _patch_text = _pf_fobj.read()
                except (OSError, UnicodeDecodeError) as exc:
                    print(f"error: cannot read --patch-file '{args.patch_file}': {exc}", file=sys.stderr)
                    return 1
                finally:
                    if _pf_fd >= 0:
                        os.close(_pf_fd)
            else:
                _patch_text = args.patch_text
            result = guarded_apply_patch(
                repo_root,
                orchestration_id=args.orchestration_id,
                actor_role=args.actor_role,
                agent_run_id=args.agent_run_id,
                changed_paths=args.paths_json,
                patch_text=_patch_text,
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
        try:
            _validate_record_agent_run_fields(args.agent_run_json)
            result = record_agent_run(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                payload=args.agent_run_json,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "write-step-result":
        try:
            _validate_write_step_result_fields(args.result_json, args.step)
            result = write_step_result(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                node_key=args.node_key,
                step=args.step,
                agent_run_id=args.agent_run_id,
                payload=args.result_json,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "deactivate-child":
        try:
            result = deactivate_child_agent(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                child_run_id=args.child_run_id,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "record-reply":
        if args.reply_from_stdin:
            reply_text = sys.stdin.read()
            if not isinstance(reply_text, str):
                reply_text = ""
        else:
            reply_text = args.reply_text
        if not isinstance(reply_text, str):
            print("record-reply requires --reply-text or --reply-from-stdin", file=sys.stderr)
            return 1
        if not reply_text.strip():
            print("record-reply requires non-empty reply_text", file=sys.stderr)
            return 1
        try:
            result = record_reply_text(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                agent_run_id=args.agent_run_id,
                reply_text=reply_text,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
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
        result = pre_phase_launch(
            repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            backend=args.backend,
            require_child_agent=args.require_child_agent,
            launch_request=getattr(args, "launch_request_json", None),
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
