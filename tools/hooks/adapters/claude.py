#!/usr/bin/env python3
"""Claude Code hook adapter."""

from __future__ import annotations

import json
from typing import Any

from tools.hooks.common import (
    HookBackendAdapter,
    HookDecision,
    HookDecisionAction,
    HookEventName,
    HookInput,
    normalize_hook_event_name,
)


def _payload_value(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is not None:
        return value
    # Claude Code does not nest its payload under a "payload" key, but this
    # defensive fallback mirrors the Codex adapter for future compatibility.
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return inner.get(key)
    return None


class ClaudeHookAdapter(HookBackendAdapter):
    def supported_events(self) -> set[HookEventName]:
        return {
            HookEventName.USER_PROMPT_SUBMIT,
            HookEventName.PRE_COMMAND_EXECUTE,
            HookEventName.POST_COMMAND_EXECUTE,
            HookEventName.STOP,
        }

    def decode_event(self, event_name: str, payload: dict[str, Any]) -> HookInput:
        normalized = normalize_hook_event_name(event_name)
        command: str | None = None
        # Claude Code wraps the command inside tool_input; check there first.
        # (Codex checks the top-level command field first — the opposite order.)
        tool_input = _payload_value(payload, "tool_input")
        if isinstance(tool_input, dict):
            raw_cmd = tool_input.get("command")
            if isinstance(raw_cmd, str) and raw_cmd.strip():
                command = raw_cmd.strip()
        if command is None:
            raw_cmd = _payload_value(payload, "command")
            if isinstance(raw_cmd, str) and raw_cmd.strip():
                command = raw_cmd.strip()
        prompt = _payload_value(payload, "prompt")
        tool_name = _payload_value(payload, "tool_name")
        return HookInput(
            event_name=normalized,
            backend="claude",
            payload=payload,
            command=command,
            prompt=prompt if isinstance(prompt, str) else None,
            tool_name=tool_name if isinstance(tool_name, str) else None,
        )

    def encode_decision(self, decision: HookDecision) -> tuple[int, str]:
        # Claude Code hook protocol: exit code signals allow/block; stdout is
        # the message shown to the user. continue_processing is Codex-specific
        # and is intentionally omitted here.
        if decision.action == HookDecisionAction.BLOCK:
            body = {
                "decision": "block",
                "reason": decision.reason or "blocked by policy",
            }
            return 2, json.dumps(body, ensure_ascii=False)

        if decision.action == HookDecisionAction.CONTINUE_WITH_MESSAGE:
            body = {
                "decision": "continue",
                "message": decision.additional_context or "",
            }
            return 0, json.dumps(body, ensure_ascii=False)

        return 0, json.dumps({"decision": "allow"}, ensure_ascii=False)
