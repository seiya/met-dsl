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
    _lookup_payload_field,
    format_block_reason_with_hint,
    normalize_hook_event_name,
)


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
        tool_input = _lookup_payload_field(payload, "tool_input")
        if isinstance(tool_input, dict):
            raw_cmd = tool_input.get("command")
            if isinstance(raw_cmd, str) and raw_cmd.strip():
                command = raw_cmd.strip()
        if command is None:
            raw_cmd = _lookup_payload_field(payload, "command")
            if isinstance(raw_cmd, str) and raw_cmd.strip():
                command = raw_cmd.strip()
        prompt = _lookup_payload_field(payload, "prompt")
        tool_name = _lookup_payload_field(payload, "tool_name")
        file_path: str | None = None
        if isinstance(tool_input, dict):
            fp = tool_input.get("file_path")
            if isinstance(fp, str) and fp.strip():
                file_path = fp.strip()
        return HookInput(
            event_name=normalized,
            backend="claude",
            payload=payload,
            command=command,
            prompt=prompt if isinstance(prompt, str) else None,
            tool_name=tool_name if isinstance(tool_name, str) else None,
            file_path=file_path,
        )

    def encode_decision(self, decision: HookDecision) -> tuple[int, str]:
        # Claude Code hook protocol: exit code signals allow/block; stdout is
        # the message shown to the user. continue_processing is Codex-specific
        # and is intentionally omitted here.
        if decision.action == HookDecisionAction.BLOCK:
            body = {
                "decision": "block",
                "reason": format_block_reason_with_hint(decision),
            }
            return 2, json.dumps(body, ensure_ascii=False)

        if decision.action == HookDecisionAction.ALLOW_AUTO_APPROVE:
            # hookSpecificOutput.permissionDecision="allow" bypasses the harness's
            # permission prompt and continues execution without operator approval.
            # Premise: ALLOW_AUTO_APPROVE is issued only in the Write/Edit branch
            # (PreToolUse event) of cli.py. If mixed into another event, the
            # hookEventName becomes inconsistent, so when adding a new issuing
            # site, guarantee that it is PreToolUse.
            audit = decision.audit_detail or {}
            if decision.reason:
                # Issuing site provided an explicit reason (e.g. the Bash
                # read-only auto-approve path); use it verbatim.
                reason = decision.reason
            else:
                tool_name = audit.get("tool_name") or "tool"
                file_path = audit.get("file_path") or ""
                agent_run_id = audit.get("agent_run_id") or ""
                reason = f"{tool_name} to {file_path} matched output_manifest.allowed_file_tool_paths"
                if agent_run_id:
                    reason += f" (agent_run_id={agent_run_id})"
            body = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": reason,
                }
            }
            return 0, json.dumps(body, ensure_ascii=False)

        if decision.action == HookDecisionAction.CONTINUE_WITH_MESSAGE:
            # Claude Code expects plain text (or empty) on the allow path;
            # "continue" is not a recognised JSON decision value.
            return 0, decision.additional_context or ""

        # Exit 0 with empty stdout = allow. Claude Code does not recognise
        # "allow" as a valid JSON decision value and raises a validation error.
        return 0, ""
