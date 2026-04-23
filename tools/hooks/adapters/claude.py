#!/usr/bin/env python3
"""Claude Code hook adapter skeleton."""

from __future__ import annotations

import json

from tools.hooks.common import (
    HookBackendAdapter,
    HookDecision,
    HookDecisionAction,
    HookEventName,
    HookInput,
)


class ClaudeHookAdapter(HookBackendAdapter):
    """Placeholder adapter for future Claude Code hook integration."""

    def supported_events(self) -> set[HookEventName]:
        # Claude integration is not implemented yet in this repository.
        return set()

    def decode_event(self, event_name: str, payload: dict[str, object]) -> HookInput:
        raise NotImplementedError(
            "Claude hook adapter is a skeleton only. "
            "Use runtime gates until Claude hook events are wired."
        )

    def encode_decision(self, decision: HookDecision) -> tuple[int, str]:
        if decision.action == HookDecisionAction.BLOCK:
            body = {
                "decision": "block",
                "reason": decision.reason or "blocked by policy",
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

        body = {"decision": "allow", "continue_processing": bool(decision.continue_processing)}
        return 0, json.dumps(body, ensure_ascii=False)
