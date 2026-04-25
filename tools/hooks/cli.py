#!/usr/bin/env python3
"""Unified backend hook entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.hooks.adapters import ClaudeHookAdapter, CodexHookAdapter
from tools.hooks.codex_feature import codex_hooks_feature_enabled
from tools.hooks.common import (
    HookDecision,
    HookDecisionAction,
    HookEventName,
    evaluate_common_policy,
    normalize_hook_event_name,
)


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input_json:
        raw = args.input_json
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        return {}
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("hook payload must be a JSON object")
    return loaded


def _resolve_event_name(args: argparse.Namespace, payload: dict[str, Any]) -> HookEventName:
    if args.event:
        return normalize_hook_event_name(args.event)
    for key in ("event_name", "event", "hook_event", "hook_event_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_hook_event_name(value)
    raise ValueError("hook event name is required (--event or payload.event_name)")


def _adapter_for_backend(backend: str):
    token = backend.strip().lower()
    if token == "codex":
        return CodexHookAdapter()
    if token == "claude":
        return ClaudeHookAdapter()
    raise ValueError(f"unsupported backend: {backend!r}")


def _decision_error(message: str) -> HookDecision:
    return HookDecision(
        action=HookDecisionAction.BLOCK,
        reason=message,
        continue_processing=False,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append_hook_audit(
    *,
    backend: str,
    event_name: HookEventName,
    payload: dict[str, Any],
    decision: HookDecision,
    orchestration_id_override: str | None = None,
) -> None:
    orchestration_id = orchestration_id_override
    if not isinstance(orchestration_id, str) or not orchestration_id.strip():
        orchestration_id = payload.get("orchestration_id")
        if not isinstance(orchestration_id, str) or not orchestration_id.strip():
            inner = payload.get("payload")
            if isinstance(inner, dict):
                orchestration_id = inner.get("orchestration_id")
    if not isinstance(orchestration_id, str) or not orchestration_id.strip():
        return
    repo_root_raw = payload.get("repo_root")
    repo_root = (
        Path(repo_root_raw).resolve()
        if isinstance(repo_root_raw, str) and repo_root_raw.strip()
        else Path.cwd()
    )
    path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id.strip()
        / "hooks"
        / "native_hook_events.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _utc_now_iso(),
        "backend": backend,
        "event": event_name.value,
        "action": decision.action.value,
        "reason": decision.reason,
        "continue_processing": decision.continue_processing,
        "tool_name": payload.get("tool_name"),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _resolve_repo_root(payload: dict[str, Any], backend: str = "") -> Path:
    del backend
    env_repo_root = os.environ.get("METDSL_HOOK_REPO_ROOT", "").strip()
    if env_repo_root:
        return Path(env_repo_root).resolve()
    repo_root_raw = payload.get("repo_root")
    return (
        Path(repo_root_raw).resolve()
        if isinstance(repo_root_raw, str) and repo_root_raw.strip()
        else Path.cwd()
    )


def _codex_feature_cache_path(
    *,
    repo_root: Path,
    orchestration_id: str,
) -> Path:
    return (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "hooks"
        / "codex_feature_check.json"
    )


def _read_codex_feature_cache(
    *,
    repo_root: Path,
    orchestration_id: str,
) -> tuple[bool, str, str, str] | None:
    path = _codex_feature_cache_path(repo_root=repo_root, orchestration_id=orchestration_id)
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


def _write_codex_feature_cache(
    *,
    repo_root: Path,
    orchestration_id: str,
    enabled: bool,
    detail: str,
    status_kind: str,
) -> None:
    path = _codex_feature_cache_path(repo_root=repo_root, orchestration_id=orchestration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "checked_at": _utc_now_iso(),
        "enabled": enabled,
        "detail": detail,
        "status_kind": status_kind,
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_retryable_probe_error(detail: str) -> bool:
    return detail.startswith("codex features list failed:") or detail.startswith(
        "codex features list timed out"
    )


def _is_recent_iso_timestamp(ts: str, ttl_seconds: int) -> bool:
    try:
        checked = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    return (now - checked).total_seconds() <= float(ttl_seconds)


def _safe_retry_ttl_seconds() -> int:
    raw = os.environ.get("METDSL_HOOK_FEATURE_RETRY_TTL_SECONDS", "30").strip()
    try:
        ttl = int(raw or "30")
    except ValueError:
        ttl = 30
    if ttl < 0:
        return 0
    return ttl


def _env_flag_true(name: str, default: str = "0") -> bool:
    raw = os.environ.get(name, default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _extract_command_for_policy(payload: dict[str, Any]) -> str | None:
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        inner = tool_input.get("command")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    inner_payload = payload.get("payload")
    if isinstance(inner_payload, dict):
        command = inner_payload.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
        tool_input = inner_payload.get("tool_input")
        if isinstance(tool_input, dict):
            inner = tool_input.get("command")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _extract_orchestration_id(payload: dict[str, Any]) -> str | None:
    orchestration_id = payload.get("orchestration_id")
    if isinstance(orchestration_id, str) and orchestration_id.strip():
        return orchestration_id.strip()
    inner = payload.get("payload")
    if isinstance(inner, dict):
        inner_id = inner.get("orchestration_id")
        if isinstance(inner_id, str) and inner_id.strip():
            return inner_id.strip()
    env_value = os.environ.get("METDSL_ORCHESTRATION_ID")
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip()
    return None


def _emit_hook_response(
    exit_code: int,
    stdout_text: str,
    *,
    event_name: HookEventName | None = None,
) -> int:
    suppress_stdout = event_name == HookEventName.STOP and exit_code == 0
    if stdout_text and not suppress_stdout:
        sys.stdout.write(stdout_text + "\n")
    if exit_code != 0:
        message = "hook failed"
        if stdout_text:
            try:
                body = json.loads(stdout_text)
                if isinstance(body, dict):
                    reason = body.get("reason")
                    if isinstance(reason, str) and reason.strip():
                        message = reason.strip()
                    else:
                        decision = body.get("decision")
                        if isinstance(decision, str) and decision.strip():
                            message = f"hook decision={decision.strip()}"
            except json.JSONDecodeError:
                message = stdout_text.strip() or message
        sys.stderr.write(message + "\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True, choices=["codex", "claude"])
    parser.add_argument("--event")
    parser.add_argument("--input-json")
    parser.add_argument("--repo-root")
    args = parser.parse_args(argv)

    payload: dict[str, Any] = {}
    event_name: HookEventName = HookEventName.STOP
    try:
        payload = _load_payload(args)
        if args.repo_root:
            payload = dict(payload)
            payload["repo_root"] = args.repo_root
        event_name = _resolve_event_name(args, payload)
        adapter = _adapter_for_backend(args.backend)
        orchestration_id = _extract_orchestration_id(payload)
        repo_root = _resolve_repo_root(payload, backend=args.backend)
        missing_id_policy = os.environ.get(
            "METDSL_MISSING_ORCHESTRATION_ID_POLICY", ""
        ).strip().lower()
        if orchestration_id is None:
            if missing_id_policy == "strict":
                decision = _decision_error(
                    "orchestration_id is required for hook execution"
                )
                _append_hook_audit(
                    backend=args.backend,
                    event_name=event_name,
                    payload=payload,
                    decision=decision,
                    orchestration_id_override="_global",
                )
                exit_code, stdout_text = adapter.encode_decision(decision)
                return _emit_hook_response(exit_code, stdout_text, event_name=event_name)
            else:
                orchestration_id = "_global"

        if args.backend == "codex":
            require_flag = os.environ.get("METDSL_REQUIRE_CODEX_HOOKS_FEATURE", "1").strip().lower()
            if require_flag not in {"0", "false", "no"}:
                cached = _read_codex_feature_cache(
                    repo_root=repo_root,
                    orchestration_id=orchestration_id,
                )
                recache = False
                if cached is None:
                    recache = True
                else:
                    cached_enabled, cached_detail, cached_kind, cached_checked_at = cached
                    if (
                        cached_enabled is False
                        and cached_kind == "probe_error"
                        and isinstance(cached_checked_at, str)
                    ):
                        retry_ttl = _safe_retry_ttl_seconds()
                        if not _is_recent_iso_timestamp(cached_checked_at, retry_ttl):
                            recache = True
                if recache:
                    enabled, detail = codex_hooks_feature_enabled()
                    status_kind = "ok" if enabled or not _is_retryable_probe_error(detail) else "probe_error"
                    _write_codex_feature_cache(
                        repo_root=repo_root,
                        orchestration_id=orchestration_id,
                        enabled=enabled,
                        detail=detail,
                        status_kind=status_kind,
                    )
                else:
                    enabled, detail = cached_enabled, cached_detail
                if not enabled:
                    decision = _decision_error(
                        "codex_hooks feature is required but not enabled: " + detail
                    )
                    _append_hook_audit(
                        backend=args.backend,
                        event_name=event_name,
                        payload=payload,
                        decision=decision,
                        orchestration_id_override=orchestration_id,
                    )
                    exit_code, stdout_text = adapter.encode_decision(decision)
                    return _emit_hook_response(exit_code, stdout_text, event_name=event_name)

        if event_name not in adapter.supported_events():
            decision = _decision_error(
                f"backend={args.backend} does not support event={event_name.value}"
            )
        else:
            decoded = adapter.decode_event(event_name.value, payload)
            decision = evaluate_common_policy(decoded)
        _append_hook_audit(
            backend=args.backend,
            event_name=event_name,
            payload=payload,
            decision=decision,
            orchestration_id_override=orchestration_id,
        )
        exit_code, stdout_text = adapter.encode_decision(decision)
    except Exception as exc:
        fallback_adapter = _adapter_for_backend(args.backend)
        decision = _decision_error(f"hook entrypoint failure: {exc}")
        fallback_orchestration_id = _extract_orchestration_id(payload) or "_global"
        _append_hook_audit(
            backend=args.backend,
            event_name=event_name,
            payload=payload,
            decision=decision,
            orchestration_id_override=fallback_orchestration_id,
        )
        exit_code, stdout_text = fallback_adapter.encode_decision(decision)
    return _emit_hook_response(exit_code, stdout_text, event_name=event_name)


if __name__ == "__main__":
    raise SystemExit(main())
