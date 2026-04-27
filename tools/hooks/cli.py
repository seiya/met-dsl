#!/usr/bin/env python3
"""Unified backend hook entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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
    _utc_now_iso,
    check_cli_managed_path,
    evaluate_common_policy,
    normalize_hook_event_name,
    READ_HINT,
    WRITE_HINT,
    validate_read_access,
    validate_write_access,
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
    normalized_orch = orchestration_id.strip()
    inner_payload = payload.get("payload")
    inner_tool_name = inner_payload.get("tool_name") if isinstance(inner_payload, dict) else None
    tool_name_raw = payload.get("tool_name")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) and tool_name_raw.strip() else inner_tool_name
    workflow_mode = os.environ.get("METDSL_WORKFLOW_MODE", "").strip().lower()
    if (
        normalized_orch == "_global"
        and isinstance(tool_name, str)
        and tool_name.strip().lower() == "shell"
        and workflow_mode not in {"1", "true", "yes"}
    ):
        return
    payload_has_repo_root = isinstance(payload.get("repo_root"), str) and bool(
        str(payload.get("repo_root")).strip()
    )
    inner_has_repo_root = isinstance(inner_payload, dict) and isinstance(
        inner_payload.get("repo_root"), str
    ) and bool(str(inner_payload.get("repo_root")).strip())
    env_repo_root = os.environ.get("METDSL_HOOK_REPO_ROOT", "").strip()
    if (
        normalized_orch == "_global"
        and workflow_mode not in {"1", "true", "yes"}
        and not payload_has_repo_root
        and not inner_has_repo_root
        and not env_repo_root
    ):
        return
    repo_root_raw = payload.get("repo_root")
    if not (isinstance(repo_root_raw, str) and repo_root_raw.strip()):
        if isinstance(inner_payload, dict):
            inner_repo_root = inner_payload.get("repo_root")
            if isinstance(inner_repo_root, str) and inner_repo_root.strip():
                repo_root_raw = inner_repo_root

    # `repo_root` が未指定の ambient hook 呼び出しでは実 workspace を汚染しない。
    # 監査ログを永続化する場合は、明示的に `repo_root`（または env 経由の
    # `METDSL_HOOK_REPO_ROOT`）を与える。
    if not (isinstance(repo_root_raw, str) and repo_root_raw.strip()):
        if env_repo_root:
            repo_root = Path(env_repo_root).resolve()
        else:
            return
    else:
        repo_root = Path(repo_root_raw).resolve()
    path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / normalized_orch
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


def _active_child_agent_run_id_path(repo_root: Path, orchestration_id: str) -> Path:
    return (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "active_child_agent_run_id.txt"
    )


def _get_orchestration_agent_run_id(repo_root: Path, orchestration_id: str) -> str | None:
    """orchestration_meta.json から orchestration_agent_run_id を取得する。"""
    meta_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "orchestration_meta.json"
    )
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    run_id = meta.get("orchestration_agent_run_id")
    return run_id.strip() if isinstance(run_id, str) and run_id.strip() else None


# shell redirection: cmd > path, cmd >> path
_BASH_REDIRECT_RE = re.compile(r"(?:>>?)\s+([^\s;&|<>]+)")
# tee: tee [-opts] path
_BASH_TEE_RE = re.compile(r"\btee\b(?:\s+-\w+)*\s+([^\n;&|<>]+)")
_REDIRECT_SKIP = frozenset({
    "/dev/null", "/dev/stderr", "/dev/stdout", "/dev/stdin", "1", "2",
})
_SHELL_CONTROL_TOKENS = frozenset({"|", "||", "&&", ";"})


def _looks_like_sed_script(token: str) -> bool:
    if not token:
        return False
    lowered = token.lower()
    if lowered.startswith(("s/", "y/", "c\\", "i\\", "a\\")):
        return True
    return "=" in token and lowered.split("=", 1)[0] in {"s", "y"}


def _detect_sed_inplace_targets(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    targets: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.split("/")[-1] != "sed":
            i += 1
            continue
        j = i + 1
        segment: list[str] = []
        while j < len(tokens) and tokens[j] not in _SHELL_CONTROL_TOKENS:
            segment.append(tokens[j])
            j += 1
        k = 0
        while k < len(segment):
            arg = segment[k]
            if arg == "-i" or arg.startswith("-i"):
                candidate_idx = k + 1
                if candidate_idx >= len(segment):
                    k += 1
                    continue
                candidate = segment[candidate_idx]
                if _looks_like_sed_script(candidate) and candidate_idx + 1 < len(segment):
                    candidate = segment[candidate_idx + 1]
                if candidate and not candidate.startswith("-"):
                    targets.append(candidate)
            k += 1
        i = j + 1 if j < len(tokens) else j
    return targets


def _detect_bash_write_targets(command: str | None) -> list[str]:
    """Bash コマンドから書き込み先パスを抽出する。"""
    if not command:
        return []
    targets: list[str] = []
    for m in _BASH_REDIRECT_RE.finditer(command):
        path = m.group(1)
        if path not in _REDIRECT_SKIP and not path.startswith("&"):
            targets.append(path)
    for m in _BASH_TEE_RE.finditer(command):
        blob = m.group(1)
        try:
            tee_args = shlex.split(blob)
        except ValueError:
            tee_args = blob.split()
        for arg in tee_args:
            if arg.startswith("-"):
                continue
            if arg in {"|", "||", "&&", ";"}:
                break
            targets.append(arg)
    targets.extend(_detect_sed_inplace_targets(command))
    return targets


def _resolve_codex_agent_run_id_from_session(
    *,
    repo_root: Path,
    orchestration_id: str,
    session_id: str | None,
    agent_session_id: str | None,
) -> str | None:
    tokens = {
        value.strip()
        for value in (session_id, agent_session_id)
        if isinstance(value, str) and value.strip()
    }
    if not tokens:
        return None
    runs_path = repo_root / "workspace" / "orchestrations" / orchestration_id / "agent_runs.jsonl"
    if not runs_path.is_file():
        return None
    resolved: str | None = None
    with runs_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            backend = str(item.get("agent_backend", "")).strip().lower()
            if backend != "codex":
                continue
            entry_session = str(item.get("agent_session_id", "")).strip()
            if entry_session not in tokens:
                continue
            run_id = item.get("agent_run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                continue
            if resolved is not None and resolved != run_id.strip():
                return None
            resolved = run_id.strip()
    return resolved


def _hint_for_file_tool(tool_name: str) -> str:
    return READ_HINT if tool_name == "Read" else WRITE_HINT


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
        if orchestration_id is None:
            orchestration_id = "_global"
        if orchestration_id == "_global":
            exit_code, stdout_text = adapter.encode_decision(
                HookDecision(action=HookDecisionAction.ALLOW)
            )
            return _emit_hook_response(exit_code, stdout_text, event_name=event_name)

        repo_root = _resolve_repo_root(payload, backend=args.backend)

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
            tool_name = (decoded.tool_name or "").strip()
            if (
                event_name == HookEventName.PRE_COMMAND_EXECUTE
                and os.environ.get("METDSL_WORKFLOW_MODE", "0").strip() == "1"
                and tool_name == "apply_patch"
            ):
                decision = HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        "raw apply_patch tool is forbidden in workflow mode. "
                        "Use guarded-apply-patch instead: "
                        "python3 tools/orchestration_runtime.py guarded-apply-patch ..."
                    ),
                    continue_processing=False,
                    audit_detail={"policy": "forbid_raw_apply_patch_in_workflow_mode"},
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
            is_file_tool_pre = (
                event_name == HookEventName.PRE_COMMAND_EXECUTE
                and tool_name in {"Write", "Edit", "Read"}
            )
            if not is_file_tool_pre:
                decision = evaluate_common_policy(decoded)
                if (
                    decision.action == HookDecisionAction.ALLOW
                    and event_name == HookEventName.PRE_COMMAND_EXECUTE
                    and tool_name == "Bash"
                    and os.environ.get("METDSL_WORKFLOW_MODE", "0").strip() == "1"
                    and args.backend == "claude"
                ):
                    active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
                    if active_path.exists():
                        active_id = active_path.read_text(encoding="utf-8").strip()
                        if active_id:
                            for target in _detect_bash_write_targets(decoded.command):
                                cli_guard = check_cli_managed_path(repo_root, target)
                                if cli_guard is not None:
                                    decision = cli_guard
                                    break
                                candidate = validate_write_access(
                                    repo_root, orchestration_id, active_id, target, tool_name=tool_name
                                )
                                if candidate.action == HookDecisionAction.BLOCK:
                                    decision = candidate
                                    break
            else:
                workflow_mode = os.environ.get("METDSL_WORKFLOW_MODE", "0").strip()
                if workflow_mode != "1":
                    decision = HookDecision(action=HookDecisionAction.ALLOW)
                elif not decoded.file_path:
                    decision = HookDecision(action=HookDecisionAction.ALLOW)
                elif args.backend == "claude":
                    active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
                    if not active_path.exists():
                        orch_agent_run_id = _get_orchestration_agent_run_id(repo_root, orchestration_id)
                        if orch_agent_run_id and tool_name == "Read":
                            decision = validate_read_access(
                                repo_root,
                                orchestration_id,
                                orch_agent_run_id,
                                decoded.file_path,
                            )
                        elif orch_agent_run_id and tool_name in {"Write", "Edit"}:
                            decision = HookDecision(
                                action=HookDecisionAction.BLOCK,
                                reason=(
                                    "orchestration agent must not use Write/Edit tools directly. "
                                    "Use guarded-apply-patch instead: "
                                    "python3 tools/orchestration_runtime.py guarded-apply-patch ..."
                                ),
                                continue_processing=False,
                            )
                        else:
                            hint = _hint_for_file_tool(tool_name)
                            decision = HookDecision(
                                action=HookDecisionAction.BLOCK,
                                reason=(
                                    "no orchestration_agent_run_id found in orchestration_meta.json. "
                                    f"{hint}"
                                ),
                                continue_processing=False,
                            )
                    else:
                        active_agent_run_id = active_path.read_text(encoding="utf-8").strip()
                        if not active_agent_run_id:
                            hint = _hint_for_file_tool(tool_name)
                            decision = HookDecision(
                                action=HookDecisionAction.BLOCK,
                                reason=(
                                    "active child agent_run_id is empty for Claude backend. "
                                    f"{hint}"
                                ),
                                continue_processing=False,
                            )
                        elif tool_name == "Read":
                            decision = validate_read_access(
                                repo_root,
                                orchestration_id,
                                active_agent_run_id,
                                decoded.file_path,
                            )
                        else:
                            cli_guard = check_cli_managed_path(repo_root, decoded.file_path)
                            decision = cli_guard if cli_guard is not None else validate_write_access(
                                repo_root,
                                orchestration_id,
                                active_agent_run_id,
                                decoded.file_path,
                                tool_name=tool_name,
                            )
                else:
                    mapped_agent_run_id = _resolve_codex_agent_run_id_from_session(
                        repo_root=repo_root,
                        orchestration_id=orchestration_id,
                        session_id=decoded.session_id,
                        agent_session_id=decoded.agent_session_id,
                    )
                    if not mapped_agent_run_id:
                        hint = _hint_for_file_tool(tool_name)
                        decision = HookDecision(
                            action=HookDecisionAction.BLOCK,
                            reason=f"session-to-run mapping not found. {hint}",
                            continue_processing=False,
                        )
                    elif tool_name == "Read":
                        decision = validate_read_access(
                            repo_root,
                            orchestration_id,
                            mapped_agent_run_id,
                            decoded.file_path,
                        )
                    else:
                        cli_guard = check_cli_managed_path(repo_root, decoded.file_path)
                        decision = cli_guard if cli_guard is not None else validate_write_access(
                            repo_root,
                            orchestration_id,
                            mapped_agent_run_id,
                            decoded.file_path,
                            tool_name=tool_name,
                        )
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
