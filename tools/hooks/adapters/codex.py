#!/usr/bin/env python3
"""Codex hook adapter."""

from __future__ import annotations

import json
from typing import Any

from tools.hooks.common import (
    HookBackendAdapter,
    HookDecision,
    HookDecisionAction,
    HookEventName,
    HookInput,
    _lookup_payload_field,
    format_block_reason_with_hint,
    normalize_hook_event_name,
)


class CodexHookAdapter(HookBackendAdapter):
    def supported_events(self) -> set[HookEventName]:
        return {
            HookEventName.SESSION_START,
            HookEventName.USER_PROMPT_SUBMIT,
            HookEventName.PRE_COMMAND_EXECUTE,
            HookEventName.PERMISSION_REQUEST,
            HookEventName.POST_COMMAND_EXECUTE,
            HookEventName.STOP,
        }

    def decode_event(self, event_name: str, payload: dict[str, Any]) -> HookInput:
        normalized = normalize_hook_event_name(event_name)
        command = _lookup_payload_field(payload, "command")
        tool_input = _lookup_payload_field(payload, "tool_input")
        if not isinstance(command, str) or not command.strip():
            if isinstance(tool_input, dict):
                ti_command = tool_input.get("command")
                if isinstance(ti_command, str) and ti_command.strip():
                    command = ti_command.strip()
                else:
                    command = None
            else:
                command = None
        prompt = _lookup_payload_field(payload, "prompt")
        tool_name = _lookup_payload_field(payload, "tool_name")
        file_path: str | None = None
        if isinstance(tool_input, dict):
            fp = tool_input.get("file_path")
            if isinstance(fp, str) and fp.strip():
                file_path = fp.strip()
        session_id = _lookup_payload_field(payload, "session_id")
        agent_session_id = _lookup_payload_field(payload, "agent_session_id")
        return HookInput(
            event_name=normalized,
            backend="codex",
            payload=payload,
            command=command if isinstance(command, str) else None,
            prompt=prompt if isinstance(prompt, str) else None,
            tool_name=tool_name if isinstance(tool_name, str) else None,
            file_path=file_path,
            session_id=session_id if isinstance(session_id, str) and session_id.strip() else None,
            agent_session_id=(
                agent_session_id
                if isinstance(agent_session_id, str) and agent_session_id.strip()
                else None
            ),
        )

    def encode_decision(self, decision: HookDecision) -> tuple[int, str]:
        if decision.action == HookDecisionAction.BLOCK:
            body = {
                "decision": "block",
                "reason": format_block_reason_with_hint(decision),
                "continue_processing": bool(decision.continue_processing),
            }
            return 2, json.dumps(body, ensure_ascii=False)

        if decision.action == HookDecisionAction.CONTINUE_WITH_MESSAGE:
            body = {
                "decision": "continue",
                "message": decision.additional_context or "",
                "continue_processing": bool(decision.continue_processing),
            }
            return 0, json.dumps(body, ensure_ascii=False)

        # ALLOW_AUTO_APPROVE は Claude Code 固有の permissionDecision bypass を
        # 表現する。Codex は permission prompt を持たず、明示的な auto-approve
        # 表現も不要なので ALLOW と同じく空 stdout を返す。
        # Codex hook runtime does not require payload on allow-path and some events
        # reject non-empty JSON outputs. Return empty stdout for compatibility.
        return 0, ""
