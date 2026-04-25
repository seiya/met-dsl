#!/usr/bin/env python3
"""Tests for shared hook validation and adapters."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tools.hooks.adapters.claude import ClaudeHookAdapter
from tools.hooks.adapters.codex import CodexHookAdapter
from tools.hooks.common import (
    HookDecision,
    HookDecisionAction,
    HookEventName,
    HookInput,
    evaluate_common_policy,
    validate_pipeline_semantics_stage,
)


class HookCommonTests(unittest.TestCase):
    def test_validate_pipeline_semantics_stage_accepts_allowed_stage(self) -> None:
        out = validate_pipeline_semantics_stage(
            step_key="execute",
            args_json={"stage": "post_execute"},
        )
        self.assertEqual(out, "post_execute")

    def test_validate_pipeline_semantics_stage_rejects_forbidden_stage(self) -> None:
        with self.assertRaisesRegex(ValueError, "not permitted"):
            validate_pipeline_semantics_stage(
                step_key="judge",
                args_json={"stage": "post_build"},
            )

    def test_validate_pipeline_semantics_stage_rejects_pre_judge_allow_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "pre_judge forbids"):
            validate_pipeline_semantics_stage(
                step_key="judge",
                args_json={"stage": "pre_judge", "allow_missing_orchestration": True},
            )

    def test_evaluate_common_policy_blocks_git_reset_hard(self) -> None:
        decision = evaluate_common_policy(
            HookInput(
                event_name=HookEventName.PRE_COMMAND_EXECUTE,
                backend="codex",
                payload={"command": "git reset --hard HEAD~1"},
                command="git reset --hard HEAD~1",
            )
        )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_evaluate_common_policy_treats_unset_workflow_mode_as_dev(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="codex",
                    payload={
                        "command": (
                            "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                            "--allow-missing-orchestration"
                        )
                    },
                    command=(
                        "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                        "--allow-missing-orchestration"
                    ),
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_evaluate_common_policy_blocks_direct_tools_read_via_cat_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "cat tools/hooks/cli.py"},
                    command="cat tools/hooks/cli.py",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertIn("direct read from tools/ via Bash is forbidden", decision.reason or "")

    def test_evaluate_common_policy_allows_non_repo_tools_path_in_workflow_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
                decision = evaluate_common_policy(
                    HookInput(
                        event_name=HookEventName.PRE_COMMAND_EXECUTE,
                        backend="claude",
                        payload={
                            "repo_root": tmp,
                            "command": "cat /usr/local/tools/config.yaml",
                        },
                        command="cat /usr/local/tools/config.yaml",
                    )
                )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)

    def test_evaluate_common_policy_blocks_python_inline_open_write(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "python3 -c \"open('workspace/a.txt', 'w').write('x')\""},
                    command="python3 -c \"open('workspace/a.txt', 'w').write('x')\"",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertIn("python -c with file write", decision.reason or "")

    def test_evaluate_common_policy_allows_python_inline_open_write_outside_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "0"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "python3 -c \"open('workspace/a.txt', 'w').write('x')\""},
                    command="python3 -c \"open('workspace/a.txt', 'w').write('x')\"",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)

    def test_codex_adapter_roundtrip(self) -> None:
        adapter = CodexHookAdapter()
        decoded = adapter.decode_event(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        )
        self.assertEqual(decoded.event_name, HookEventName.PRE_COMMAND_EXECUTE)
        self.assertEqual(decoded.command, "echo hi")
        code, stdout_text = adapter.encode_decision(
            HookDecision(action=HookDecisionAction.ALLOW)
        )
        self.assertEqual(code, 0)
        loaded = json.loads(stdout_text)
        self.assertEqual(loaded.get("decision"), "allow")

    def test_claude_adapter_supported_events(self) -> None:
        adapter = ClaudeHookAdapter()
        events = adapter.supported_events()
        self.assertIn(HookEventName.USER_PROMPT_SUBMIT, events)
        self.assertIn(HookEventName.PRE_COMMAND_EXECUTE, events)
        self.assertIn(HookEventName.POST_COMMAND_EXECUTE, events)
        self.assertIn(HookEventName.STOP, events)
        self.assertNotIn(HookEventName.SESSION_START, events)
        self.assertNotIn(HookEventName.PERMISSION_REQUEST, events)

    def test_claude_adapter_decode_event_extracts_command(self) -> None:
        adapter = ClaudeHookAdapter()
        decoded = adapter.decode_event(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "echo hello"}},
        )
        self.assertEqual(decoded.event_name, HookEventName.PRE_COMMAND_EXECUTE)
        self.assertEqual(decoded.tool_name, "Bash")
        self.assertEqual(decoded.command, "echo hello")
        self.assertEqual(decoded.backend, "claude")

    def test_claude_adapter_decode_event_extracts_prompt(self) -> None:
        adapter = ClaudeHookAdapter()
        decoded = adapter.decode_event("UserPromptSubmit", {"prompt": "do something"})
        self.assertEqual(decoded.event_name, HookEventName.USER_PROMPT_SUBMIT)
        self.assertEqual(decoded.prompt, "do something")

    def test_claude_adapter_decode_event_stop(self) -> None:
        adapter = ClaudeHookAdapter()
        decoded = adapter.decode_event("Stop", {"stop_reason": "end_turn"})
        self.assertEqual(decoded.event_name, HookEventName.STOP)

    def test_claude_adapter_common_policy_blocks_git_reset_hard(self) -> None:
        adapter = ClaudeHookAdapter()
        decoded = adapter.decode_event(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD~1"}},
        )
        decision = evaluate_common_policy(decoded)
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_claude_adapter_encode_decision_block_uses_nonzero_exit(self) -> None:
        adapter = ClaudeHookAdapter()
        code, stdout_text = adapter.encode_decision(
            HookDecision(action=HookDecisionAction.BLOCK, reason="denied")
        )
        self.assertEqual(code, 2)
        loaded = json.loads(stdout_text)
        self.assertEqual(loaded.get("decision"), "block")
        self.assertEqual(loaded.get("reason"), "denied")

    def test_claude_adapter_encode_decision_allow_omits_continue_processing(self) -> None:
        adapter = ClaudeHookAdapter()
        code, stdout_text = adapter.encode_decision(HookDecision(action=HookDecisionAction.ALLOW))
        self.assertEqual(code, 0)
        body = json.loads(stdout_text)
        self.assertEqual(body.get("decision"), "allow")
        self.assertNotIn("continue_processing", body)

    def test_claude_adapter_encode_decision_block_omits_continue_processing(self) -> None:
        adapter = ClaudeHookAdapter()
        code, stdout_text = adapter.encode_decision(
            HookDecision(action=HookDecisionAction.BLOCK, reason="denied")
        )
        self.assertEqual(code, 2)
        body = json.loads(stdout_text)
        self.assertEqual(body.get("decision"), "block")
        self.assertNotIn("continue_processing", body)


if __name__ == "__main__":
    unittest.main()
