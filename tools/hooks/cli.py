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


def _inner_payload(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else {}


def _payload_value(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is not None:
        return value
    return _inner_payload(payload).get(key)


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = _payload_value(payload, "tool_input")
    return value if isinstance(value, dict) else {}


def _redact_sensitive_text(text: str) -> str:
    redacted = re.sub(r"(--capability-token(?:=|\s+))\S+", r"\1<redacted>", text)
    redacted = re.sub(
        r'("capability_token"\s*:\s*")([^"]+)(")',
        r'\1<redacted>\3',
        redacted,
    )
    return redacted


def _trim_audit_text(text: str, *, limit: int = 500) -> str:
    safe = _redact_sensitive_text(text)
    if len(safe) <= limit:
        return safe
    return safe[:limit] + f"...<truncated {len(safe) - limit} chars>"


def _extract_apply_patch_paths(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        for prefix in (
            "*** Add File: ",
            "*** Update File: ",
            "*** Delete File: ",
            "*** Move to: ",
        ):
            if line.startswith(prefix):
                token = line[len(prefix):].strip()
                if token:
                    paths.append(token)
                break
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        deduped.append(path)
        seen.add(path)
    return deduped


def _sanitize_audit_detail(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if key_s.lower() in {"capability_token", "token", "secret"}:
                sanitized[key_s] = "<redacted>"
            else:
                sanitized[key_s] = _sanitize_audit_detail(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_audit_detail(item) for item in value]
    if isinstance(value, str):
        return _trim_audit_text(value)
    return value


def _audit_payload_summary(payload: dict[str, Any], tool_name: str | None) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    session_id = _payload_value(payload, "session_id")
    if isinstance(session_id, str) and session_id.strip():
        summary["session_id"] = session_id.strip()
    agent_session_id = _payload_value(payload, "agent_session_id")
    if isinstance(agent_session_id, str) and agent_session_id.strip():
        summary["agent_session_id"] = agent_session_id.strip()

    tool_input = _tool_input(payload)
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        summary["file_path"] = file_path.strip()

    command = _payload_value(payload, "command")
    if not isinstance(command, str) or not command.strip():
        candidate = tool_input.get("command")
        command = candidate if isinstance(candidate, str) and candidate.strip() else None
    if isinstance(command, str) and command.strip():
        summary["command"] = _trim_audit_text(command.strip())

    if (tool_name or "").strip() == "apply_patch":
        patch_value = tool_input.get("patch")
        if not isinstance(patch_value, str):
            patch_value = tool_input.get("patch_text")
        if isinstance(patch_value, str):
            summary["apply_patch_paths"] = _extract_apply_patch_paths(patch_value)
            summary["patch_line_count"] = len(patch_value.splitlines())
    return summary


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
    inner_payload = _inner_payload(payload)
    inner_tool_name = inner_payload.get("tool_name")
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
        "tool_name": tool_name,
    }
    payload_summary = _audit_payload_summary(payload, tool_name if isinstance(tool_name, str) else None)
    if payload_summary:
        entry["payload_summary"] = payload_summary
    if decision.audit_detail is not None:
        entry["audit_detail"] = _sanitize_audit_detail(decision.audit_detail)
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
_BASH_REDIRECT_RE = re.compile(r"(?:>>?)\s+([^\s;&|<>)]+)")
# tee: tee [-opts] path
_BASH_TEE_RE = re.compile(r"\btee\b(?:\s+-\w+)*\s+([^\n;&|<>]+)")
_REDIRECT_SKIP = frozenset({
    "/dev/null", "/dev/stderr", "/dev/stdout", "/dev/stdin", "1", "2",
})
# Patterns for stripping quoted string content before redirect detection.
# Prevents false positives when CLI arguments like --reply-text "... > 0 ..." are scanned.
_DOUBLE_QUOTED_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_SINGLE_QUOTED_RE = re.compile(r"'[^']*'", re.DOTALL)


def _strip_quoted_strings(cmd: str) -> str:
    """Replace the content inside shell quotes with spaces to prevent redirect false-positives."""
    cmd = _DOUBLE_QUOTED_RE.sub(lambda m: '"' + " " * (len(m.group()) - 2) + '"', cmd)
    cmd = _SINGLE_QUOTED_RE.sub(lambda m: "'" + " " * (len(m.group()) - 2) + "'", cmd)
    return cmd


def _skip_single_quoted(s: str, i: int) -> int:
    """Advance past a single-quoted string starting after the opening quote."""
    n = len(s)
    while i < n and s[i] != "'":
        i += 1
    return i + 1 if i < n else i


def _skip_double_quoted(s: str, i: int) -> int:
    """Advance past a double-quoted string starting after the opening quote.
    Does NOT recurse into $() / backticks — callers handle those separately.
    """
    n = len(s)
    while i < n and s[i] != '"':
        if s[i] == "\\" and i + 1 < n:
            i += 2
        else:
            i += 1
    return i + 1 if i < n else i


def _scan_backtick(s: str, start: int) -> tuple[str, int]:
    """Return (body, index_after_closing_backtick) for a `...` substitution."""
    i = start
    n = len(s)
    while i < n and s[i] != "`":
        if s[i] == "\\" and i + 1 < n:
            i += 2
        else:
            i += 1
    return s[start:i], (i + 1 if i < n else i)


def _scan_arithmetic_expansion(s: str, start: int) -> tuple[list[str], int]:
    """Advance past $((...)) starting after the two opening '((' chars.

    $((…)) is arithmetic expansion — bare '>' inside is comparison, not redirect.
    However nested $(...) and backtick substitutions inside arithmetic DO execute,
    so their bodies are collected and returned for recursive write-target scanning.

    Returns (nested_bodies, index_after_closing_'))')
    """
    depth = 2  # two open parens from '$((' to close
    i = start
    n = len(s)
    bodies: list[str] = []
    while i < n and depth > 0:
        c = s[i]
        if c == "\\" and i + 1 < n:
            i += 2
        elif c == "'":
            i = _skip_single_quoted(s, i + 1)
        elif s[i : i + 3] == "$((":
            # nested arithmetic — recurse to capture its inner bodies
            nested, i = _scan_arithmetic_expansion(s, i + 3)
            bodies.extend(nested)
        elif s[i : i + 2] == "$(":
            # nested command substitution inside arithmetic — executes
            body, i = _scan_subshell(s, i + 2)
            bodies.append(body)
        elif c == "`":
            body, i = _scan_backtick(s, i + 1)
            bodies.append(body)
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            i += 1
        else:
            i += 1
    return bodies, i


def _scan_subshell(s: str, start: int) -> tuple[str, int]:
    """Return (body, index_after_closing_paren) for a $(...) starting after '('.

    Tracks depth while respecting single/double-quoted strings and escape sequences,
    so characters like '(' inside single quotes do not affect nesting depth.
    """
    depth = 1
    i = start
    n = len(s)
    while i < n and depth > 0:
        c = s[i]
        if c == "\\" and i + 1 < n:
            i += 2
        elif c == "'":
            i = _skip_single_quoted(s, i + 1)
        elif c == '"':
            i = _skip_double_quoted(s, i + 1)
        elif c == "`":
            _, i = _scan_backtick(s, i + 1)
        elif s[i : i + 2] == "$(":
            depth += 1
            i += 2
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
            i += 1
        else:
            i += 1
    return s[start:i], i


def _extract_command_substitution_bodies(command: str) -> list[str]:
    """Return bodies of every $(...) and `...` substitution, respecting shell quoting.

    Double-quoted strings do NOT suppress $() or backtick substitutions in bash,
    so these must be scanned for write targets even after the outer quotes are stripped.
    Parentheses inside single/double quotes are ignored for depth counting.
    """
    bodies: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            i += 2
        elif c == "'":
            i = _skip_single_quoted(command, i + 1)
        elif c == '"':
            # Inside double-quotes $() and backticks still expand — scan for them.
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    i += 2
                elif command[i : i + 3] == "$((":
                    nested, i = _scan_arithmetic_expansion(command, i + 3)
                    for b in nested:
                        bodies.append(b)
                        bodies.extend(_extract_command_substitution_bodies(b))
                elif command[i : i + 2] == "$(":
                    body, i = _scan_subshell(command, i + 2)
                    bodies.append(body)
                    bodies.extend(_extract_command_substitution_bodies(body))
                elif command[i] == "`":
                    body, i = _scan_backtick(command, i + 1)
                    bodies.append(body)
                    bodies.extend(_extract_command_substitution_bodies(body))
                else:
                    i += 1
            if i < n:
                i += 1  # skip closing "
        elif c == "`":
            body, i = _scan_backtick(command, i + 1)
            bodies.append(body)
            bodies.extend(_extract_command_substitution_bodies(body))
        elif command[i : i + 3] == "$((":
            nested, i = _scan_arithmetic_expansion(command, i + 3)
            for b in nested:
                bodies.append(b)
                bodies.extend(_extract_command_substitution_bodies(b))
        elif command[i : i + 2] == "$(":
            body, i = _scan_subshell(command, i + 2)
            bodies.append(body)
            bodies.extend(_extract_command_substitution_bodies(body))
        else:
            i += 1
    return bodies


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
    # Recurse into $(...) and `...` bodies first.  Double-quoted strings do NOT
    # suppress command substitutions in bash, so a redirect inside "$(cmd > path)"
    # is real even though the outer quotes would otherwise be stripped below.
    for body in _extract_command_substitution_bodies(command):
        targets.extend(_detect_bash_write_targets(body))
    # Strip quoted string content before redirect/tee scanning to avoid false-positives
    # caused by `>` or `tee` appearing as literal text inside CLI argument values
    # (e.g. --reply-text "exit code > 0").
    # _detect_sed_inplace_targets uses shlex.split internally and is not affected.
    scanned = _strip_quoted_strings(command)
    for m in _BASH_REDIRECT_RE.finditer(scanned):
        path = m.group(1)
        if path not in _REDIRECT_SKIP and not path.startswith("&"):
            targets.append(path)
    for m in _BASH_TEE_RE.finditer(scanned):
        # Match on scanned to skip `tee` inside quoted strings, but recover the
        # original blob (same span) so quoted paths like tee "out.log" are preserved.
        # _strip_quoted_strings is length-preserving, so span indices stay aligned.
        start, end = m.span(1)
        blob = command[start:end]
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
) -> tuple[str | None, int]:
    tokens = {
        value.strip()
        for value in (session_id, agent_session_id)
        if isinstance(value, str) and value.strip()
    }
    if not tokens:
        return None, 0
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    index_path = orch_root / "session_run_index.json"
    if index_path.is_file():
        try:
            index_doc = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            index_doc = None
        if isinstance(index_doc, dict):
            entries_obj = index_doc.get("entries")
            if isinstance(entries_obj, list):
                matched_from_index: set[str] = set()
                for item in entries_obj:
                    if not isinstance(item, dict):
                        continue
                    run_id_obj = item.get("agent_run_id")
                    if not isinstance(run_id_obj, str) or not run_id_obj.strip():
                        continue
                    candidate_tokens: set[str] = set()
                    for key in ("agent_session_id", "context_id", "session_id"):
                        value_obj = item.get(key)
                        if isinstance(value_obj, str) and value_obj.strip():
                            candidate_tokens.add(value_obj.strip())
                    if tokens.isdisjoint(candidate_tokens):
                        continue
                    matched_from_index.add(run_id_obj.strip())
                if len(matched_from_index) == 1:
                    return next(iter(matched_from_index)), 1
                if len(matched_from_index) > 1:
                    return None, len(matched_from_index)
    runs_path = orch_root / "agent_runs.jsonl"
    if not runs_path.is_file():
        return None, 0
    session_match_ids: set[str] = set()
    context_match_ids: set[str] = set()
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
            run_id = item.get("agent_run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                continue
            normalized_run_id = run_id.strip()
            entry_session = str(item.get("agent_session_id", "")).strip()
            if entry_session in tokens:
                session_match_ids.add(normalized_run_id)
                continue
            entry_context = str(item.get("context_id", "")).strip()
            if entry_context in tokens:
                context_match_ids.add(normalized_run_id)
    if len(session_match_ids) == 1:
        return next(iter(session_match_ids)), 1
    if len(session_match_ids) > 1:
        return None, len(session_match_ids)
    if len(context_match_ids) == 1:
        return next(iter(context_match_ids)), 1
    if len(context_match_ids) > 1:
        return None, len(context_match_ids)
    return None, 0


def _get_agent_role_from_capability(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> str | None:
    """Return the agent_role for `agent_run_id`.

    Resolution order:
    1. `capabilities/<agent_run_id>.json` (step/substep agents, written at
       record-launch time).
    2. `orchestration_meta.json` — if `agent_run_id` matches
       `orchestration_agent_run_id`, the role is "orchestration". The
       orchestration agent has no capability file because it is initialized
       directly by `init_orchestration` rather than launched as a child.
    Returns None if neither source identifies the agent.
    """
    cap_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "capabilities"
        / f"{agent_run_id}.json"
    )
    if cap_path.is_file():
        try:
            doc = json.loads(cap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = None
        if isinstance(doc, dict):
            role = doc.get("agent_role")
            if isinstance(role, str) and role.strip():
                return role.strip().lower()

    # Fallback: orchestration agent has no capability file. Match the run id
    # against orchestration_meta.json.
    orch_run_id = _get_orchestration_agent_run_id(repo_root, orchestration_id)
    if orch_run_id and orch_run_id.strip() == agent_run_id.strip():
        return "orchestration"
    return None


def _hint_for_file_tool(tool_name: str) -> str:
    return READ_HINT if tool_name == "Read" else WRITE_HINT


def _resolve_agent_run_id_for_file_tool(
    *,
    backend: str,
    repo_root: Path,
    orchestration_id: str,
    session_id: str | None,
    agent_session_id: str | None,
    tool_name: str,
) -> tuple[str | None, HookDecision | None]:
    if backend == "claude":
        active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
        if active_path.exists():
            active_agent_run_id = active_path.read_text(encoding="utf-8").strip()
            if not active_agent_run_id:
                hint = _hint_for_file_tool(tool_name)
                return None, HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        "active child agent_run_id is empty for Claude backend. "
                        f"{hint}"
                    ),
                    continue_processing=False,
                )
            return active_agent_run_id, None
        orch_agent_run_id = _get_orchestration_agent_run_id(repo_root, orchestration_id)
        if not orch_agent_run_id:
            hint = _hint_for_file_tool(tool_name)
            return None, HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "no orchestration_agent_run_id found in orchestration_meta.json. "
                    f"{hint}"
                ),
                continue_processing=False,
            )
        return orch_agent_run_id, None
    mapped_agent_run_id, match_count = _resolve_codex_agent_run_id_from_session(
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        session_id=session_id,
        agent_session_id=agent_session_id,
    )
    if not mapped_agent_run_id:
        hint = _hint_for_file_tool(tool_name)
        suffix = (
            f" (ambiguous candidates={match_count})"
            if isinstance(match_count, int) and match_count > 1
            else ""
        )
        return None, HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=f"session-to-run mapping not found{suffix}. {hint}",
            continue_processing=False,
        )
    return mapped_agent_run_id, None


def _validate_write_targets(
    *,
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    targets: list[str],
    tool_name: str,
    bash_command: str | None = None,
) -> HookDecision:
    for target in targets:
        cli_guard = check_cli_managed_path(repo_root, target)
        candidate = cli_guard if cli_guard is not None else validate_write_access(
            repo_root,
            orchestration_id,
            agent_run_id,
            target,
            tool_name=tool_name,
            bash_command=bash_command,
        )
        if candidate.action == HookDecisionAction.BLOCK:
            return candidate
    return HookDecision(action=HookDecisionAction.ALLOW)


def _evaluate_pre_command_file_access_policy(
    *,
    decoded: Any,
    repo_root: Path,
    orchestration_id: str,
    backend: str,
) -> HookDecision | None:
    tool_name = (decoded.tool_name or "").strip()
    if decoded.event_name != HookEventName.PRE_COMMAND_EXECUTE:
        return None
    workflow_mode = os.environ.get("METDSL_WORKFLOW_MODE", "0").strip()

    # step 1: apply_patch write guard
    if tool_name == "apply_patch":
        if workflow_mode != "1":
            return None
        patch_text = ""
        decoded_tool_input = _tool_input(decoded.payload)
        patch_value = decoded_tool_input.get("patch")
        if not isinstance(patch_value, str):
            patch_value = decoded_tool_input.get("patch_text")
        if isinstance(patch_value, str):
            patch_text = patch_value
        apply_patch_paths = _extract_apply_patch_paths(patch_text)
        resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
            backend=backend,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            session_id=decoded.session_id,
            agent_session_id=decoded.agent_session_id,
            tool_name=tool_name,
        )
        if resolution_error is not None:
            return resolution_error
        if resolved_run_id is None:
            return HookDecision(action=HookDecisionAction.ALLOW)
        return _validate_write_targets(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=resolved_run_id,
            targets=apply_patch_paths,
            tool_name=tool_name,
        )

    # step 2: Write / Edit / Read file tool guard
    if tool_name in {"Write", "Edit", "Read"}:
        if workflow_mode != "1" or not decoded.file_path:
            return HookDecision(action=HookDecisionAction.ALLOW)
        resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
            backend=backend,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            session_id=decoded.session_id,
            agent_session_id=decoded.agent_session_id,
            tool_name=tool_name,
        )
        if resolution_error is not None:
            return resolution_error
        if resolved_run_id is None:
            return HookDecision(action=HookDecisionAction.ALLOW)
        if tool_name == "Read":
            agent_role = _get_agent_role_from_capability(repo_root, orchestration_id, resolved_run_id)
            return validate_read_access(
                repo_root,
                orchestration_id,
                resolved_run_id,
                decoded.file_path,
                agent_role=agent_role,
            )
        return _validate_write_targets(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=resolved_run_id,
            targets=[decoded.file_path],
            tool_name=tool_name,
        )

    # step 3: Bash read/write guard
    if tool_name == "Bash":
        common_decision = evaluate_common_policy(decoded)
        if common_decision.action == HookDecisionAction.BLOCK:
            return common_decision
        if workflow_mode != "1":
            return common_decision
        write_targets = _detect_bash_write_targets(decoded.command)
        if not write_targets:
            return common_decision
        resolved_run_id, resolution_error = _resolve_agent_run_id_for_file_tool(
            backend=backend,
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            session_id=decoded.session_id,
            agent_session_id=decoded.agent_session_id,
            tool_name=tool_name,
        )
        if resolution_error is not None:
            return resolution_error
        if resolved_run_id is None:
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=f"session-to-run mapping not found. {WRITE_HINT}",
                continue_processing=False,
            )
        write_decision = _validate_write_targets(
            repo_root=repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=resolved_run_id,
            targets=write_targets,
            tool_name=tool_name,
            bash_command=decoded.command,
        )
        if write_decision.action == HookDecisionAction.BLOCK:
            return write_decision
        return common_decision
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
            decision = _evaluate_pre_command_file_access_policy(
                decoded=decoded,
                repo_root=repo_root,
                orchestration_id=orchestration_id,
                backend=args.backend,
            )
            if decision is None:
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
