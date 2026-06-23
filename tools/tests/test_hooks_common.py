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
    _detect_cli_help_invocation,
    _extract_read_targets,
    evaluate_common_policy,
    validate_pipeline_semantics_stage,
)


class HookCommonTests(unittest.TestCase):
    def test_extract_read_targets_sed_mixed_implicit_and_explicit_script_excludes_implicit_script(self) -> None:
        targets = _extract_read_targets(
            "sed",
            ["sed", "s/a/b/", "-e", "s/c/d/", "docs/WORKFLOW.md"],
        )
        self.assertEqual(targets, ["docs/WORKFLOW.md"])

    def test_validate_pipeline_semantics_stage_accepts_allowed_stage(self) -> None:
        out = validate_pipeline_semantics_stage(
            step_key="validate",
            args_json={"stage": "post_execute"},
        )
        self.assertEqual(out, "post_execute")

    def test_validate_pipeline_semantics_stage_rejects_forbidden_stage(self) -> None:
        with self.assertRaisesRegex(ValueError, "not permitted"):
            validate_pipeline_semantics_stage(
                step_key="validate",
                args_json={"stage": "post_build"},
            )

    def test_validate_pipeline_semantics_stage_rejects_pre_judge_allow_missing(self) -> None:
        with self.assertRaisesRegex(ValueError, "pre_judge forbids"):
            validate_pipeline_semantics_stage(
                step_key="validate",
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

    def test_evaluate_common_policy_blocks_direct_tools_read_via_sed_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "sed -n '1,40p' tools/orchestration_runtime.py"},
                    command="sed -n '1,40p' tools/orchestration_runtime.py",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_rg_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": 'rg -n "pattern" tools/run_workflow.py'},
                    command='rg -n "pattern" tools/run_workflow.py',
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_grep_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": 'grep -n "x" tools/hooks/cli.py'},
                    command='grep -n "x" tools/hooks/cli.py',
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_awk_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "awk '{print $1}' tools/file.txt"},
                    command="awk '{print $1}' tools/file.txt",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_allows_sed_non_tools_path_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "sed -n '1,40p' docs/WORKFLOW.md"},
                    command="sed -n '1,40p' docs/WORKFLOW.md",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)

    def test_evaluate_common_policy_allows_rg_pattern_only_tools_token_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": 'rg -n "tools/" docs/AGENT_SKILLS.md'},
                    command='rg -n "tools/" docs/AGENT_SKILLS.md',
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)

    def test_evaluate_common_policy_blocks_direct_tools_read_via_sed_f_script_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "sed -f tools/script.sed docs/WORKFLOW.md"},
                    command="sed -f tools/script.sed docs/WORKFLOW.md",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_rg_file_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": 'rg --file tools/patterns.txt "x" docs/WORKFLOW.md'},
                    command='rg --file tools/patterns.txt "x" docs/WORKFLOW.md',
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_grep_f_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "grep -f tools/patterns.txt docs/WORKFLOW.md"},
                    command="grep -f tools/patterns.txt docs/WORKFLOW.md",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_awk_f_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "awk -f tools/program.awk docs/WORKFLOW.md"},
                    command="awk -f tools/program.awk docs/WORKFLOW.md",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_sed_e_and_tools_input_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "sed -e 's/a/b/' tools/input.txt"},
                    command="sed -e 's/a/b/' tools/input.txt",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_awk_f_and_tools_input_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "awk -f docs/program.awk tools/input.txt"},
                    command="awk -f docs/program.awk tools/input.txt",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_sed_combined_f_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "sed -ftools/script.sed docs/WORKFLOW.md"},
                    command="sed -ftools/script.sed docs/WORKFLOW.md",
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_blocks_direct_tools_read_via_rg_combined_f_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": 'rg -ftools/patterns.txt "x" docs/WORKFLOW.md'},
                    command='rg -ftools/patterns.txt "x" docs/WORKFLOW.md',
                )
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_tools_direct_read")

    def test_evaluate_common_policy_allows_sed_mixed_implicit_and_explicit_script_without_tools_input(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            decision = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": "sed 's/a/b/' -e 's/c/d/' docs/WORKFLOW.md"},
                    command="sed 's/a/b/' -e 's/c/d/' docs/WORKFLOW.md",
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
        # Workflow-mode `python -c` is fail-closed; reason confirms the policy.
        self.assertIn("python -c inline execution is forbidden", decision.reason or "")

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
        self.assertEqual(stdout_text, "")

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

    def test_claude_adapter_encode_decision_allow_returns_empty_stdout(self) -> None:
        adapter = ClaudeHookAdapter()
        code, stdout_text = adapter.encode_decision(HookDecision(action=HookDecisionAction.ALLOW))
        self.assertEqual(code, 0)
        self.assertEqual(stdout_text, "")

    def test_claude_adapter_encode_decision_allow_auto_approve_emits_hook_specific_output(self) -> None:
        adapter = ClaudeHookAdapter()
        decision = HookDecision(
            action=HookDecisionAction.ALLOW_AUTO_APPROVE,
            audit_detail={
                "policy": "output_manifest_write_allow",
                "tool_name": "Write",
                "file_path": "workspace/ir/foo/spec.ir.yaml",
                "agent_run_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )
        code, stdout_text = adapter.encode_decision(decision)
        self.assertEqual(code, 0)
        body = json.loads(stdout_text)
        self.assertIn("hookSpecificOutput", body)
        hso = body["hookSpecificOutput"]
        self.assertEqual(hso.get("hookEventName"), "PreToolUse")
        self.assertEqual(hso.get("permissionDecision"), "allow")
        reason = hso.get("permissionDecisionReason") or ""
        self.assertIn("Write to workspace/ir/foo/spec.ir.yaml", reason)
        self.assertIn("agent_run_id=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", reason)

    def test_codex_adapter_encode_decision_allow_auto_approve_falls_back_to_empty_allow(self) -> None:
        adapter = CodexHookAdapter()
        decision = HookDecision(
            action=HookDecisionAction.ALLOW_AUTO_APPROVE,
            audit_detail={
                "policy": "output_manifest_write_allow",
                "tool_name": "Write",
                "file_path": "workspace/ir/foo/spec.ir.yaml",
                "agent_run_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            },
        )
        code, stdout_text = adapter.encode_decision(decision)
        self.assertEqual(code, 0)
        self.assertEqual(stdout_text, "")

    def test_hook_decision_action_allow_auto_approve_enum_value(self) -> None:
        self.assertEqual(HookDecisionAction.ALLOW_AUTO_APPROVE.value, "allow_auto_approve")

    def test_claude_adapter_encode_decision_block_omits_continue_processing(self) -> None:
        adapter = ClaudeHookAdapter()
        code, stdout_text = adapter.encode_decision(
            HookDecision(action=HookDecisionAction.BLOCK, reason="denied")
        )
        self.assertEqual(code, 2)
        body = json.loads(stdout_text)
        self.assertEqual(body.get("decision"), "block")
        self.assertNotIn("continue_processing", body)

    def test_claude_adapter_encode_decision_block_surfaces_fix_hint(self) -> None:
        adapter = ClaudeHookAdapter()
        decision = HookDecision(
            action=HookDecisionAction.BLOCK,
            reason="unauthorized write: foo",
            audit_detail={
                "policy": "output_manifest_write_guard",
                "fix_hint": {
                    "next_command": "python3 tools/orchestration_runtime.py ...",
                    "docs_ref": "docs/RUNBOOK.md#hook-recovery",
                    "note": "use the canonical CLI",
                },
            },
        )
        code, stdout_text = adapter.encode_decision(decision)
        self.assertEqual(code, 2)
        body = json.loads(stdout_text)
        reason = body.get("reason", "")
        self.assertIn("unauthorized write: foo", reason)
        self.assertIn("Fix: python3 tools/orchestration_runtime.py ...", reason)
        self.assertIn("Docs: docs/RUNBOOK.md#hook-recovery", reason)
        self.assertIn("Note: use the canonical CLI", reason)

    def test_claude_adapter_encode_decision_block_surfaces_write_under_hint(self) -> None:
        """Regression: the new `write_under` fix_hint field (introduced when Step 0
        was eliminated) must render as a `Write under: ...` line in the surfaced
        block reason. Without this, agents see no recovery hint for path-based
        guidance and can fall back to approval-blocking bootstrap Bash forms.
        """
        adapter = ClaudeHookAdapter()
        decision = HookDecision(
            action=HookDecisionAction.BLOCK,
            reason="unauthorized write: foo",
            audit_detail={
                "policy": "output_manifest_write_guard",
                "fix_hint": {
                    "write_under": "workspace/tmp/run123/...",
                    "docs_ref": "docs/AGENT_CONTRACT.md",
                    "note": "use literal allowed_tmp_root path",
                },
            },
        )
        code, stdout_text = adapter.encode_decision(decision)
        self.assertEqual(code, 2)
        body = json.loads(stdout_text)
        reason = body.get("reason", "")
        self.assertIn("unauthorized write: foo", reason)
        self.assertIn("Write under: workspace/tmp/run123/...", reason)
        self.assertIn("Docs: docs/AGENT_CONTRACT.md", reason)
        self.assertIn("Note: use literal allowed_tmp_root path", reason)

    def test_codex_adapter_encode_decision_block_surfaces_fix_hint(self) -> None:
        adapter = CodexHookAdapter()
        decision = HookDecision(
            action=HookDecisionAction.BLOCK,
            reason="forbidden inline write",
            audit_detail={
                "policy": "forbid_python_inline_write",
                "fix_hint": {
                    "next_command": "python3 tools/new_agent_run_id.py",
                    "docs_ref": "docs/RUNBOOK.md#hook-recovery",
                },
            },
        )
        code, stdout_text = adapter.encode_decision(decision)
        self.assertEqual(code, 2)
        body = json.loads(stdout_text)
        reason = body.get("reason", "")
        self.assertIn("forbidden inline write", reason)
        self.assertIn("Fix: python3 tools/new_agent_run_id.py", reason)
        self.assertIn("Docs: docs/RUNBOOK.md#hook-recovery", reason)

    def test_format_block_reason_with_hint_no_audit_detail_returns_base(self) -> None:
        from tools.hooks.common import format_block_reason_with_hint

        decision = HookDecision(action=HookDecisionAction.BLOCK, reason="denied")
        self.assertEqual(format_block_reason_with_hint(decision), "denied")

    def test_format_block_reason_with_hint_no_fix_hint_returns_base(self) -> None:
        from tools.hooks.common import format_block_reason_with_hint

        decision = HookDecision(
            action=HookDecisionAction.BLOCK,
            reason="denied",
            audit_detail={"policy": "x"},
        )
        self.assertEqual(format_block_reason_with_hint(decision), "denied")


class DetectCliHelpInvocationTests(unittest.TestCase):
    """`_detect_cli_help_invocation`: audit `python3 tools/<name>.py [<sub>] --help`.

    Because the CLI reference policy (the "Information-acquisition policy" section of docs/CLI_REFERENCE.md)
    treats reading argparse output via `--help` as a first-class path, the hook does not
    block but only attaches audit_detail and records the usage frequency.
    """

    def _shlex(self, command: str) -> list[str]:
        import shlex
        return shlex.split(command)

    def test_detects_orchestration_runtime_subcommand_help(self) -> None:
        cmd = "python3 tools/orchestration_runtime.py record-launch --help"
        result = _detect_cli_help_invocation(self._shlex(cmd), cmd)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["policy"], "cli_help_invocation_observed")
        self.assertEqual(result["tool"], "tools/orchestration_runtime.py")
        self.assertEqual(result["subcommand"], "record-launch")
        self.assertEqual(result["command"], cmd)

    def test_detects_root_help_with_null_subcommand(self) -> None:
        cmd = "python3 tools/orchestration_runtime.py --help"
        result = _detect_cli_help_invocation(self._shlex(cmd), cmd)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["tool"], "tools/orchestration_runtime.py")
        self.assertIsNone(result["subcommand"])

    def test_detects_run_workflow_help(self) -> None:
        cmd = "python3 tools/run_workflow.py --help"
        result = _detect_cli_help_invocation(self._shlex(cmd), cmd)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["tool"], "tools/run_workflow.py")
        self.assertIsNone(result["subcommand"])

    def test_detects_short_help_flag(self) -> None:
        cmd = "python3 tools/orchestration_runtime.py -h"
        result = _detect_cli_help_invocation(self._shlex(cmd), cmd)
        self.assertIsNotNone(result)

    def test_returns_none_for_normal_invocation(self) -> None:
        cmd = "python3 tools/orchestration_runtime.py record-launch --repo-root ."
        self.assertIsNone(_detect_cli_help_invocation(self._shlex(cmd), cmd))

    def test_returns_none_for_uuid_helper(self) -> None:
        cmd = "python3 tools/new_agent_run_id.py"
        self.assertIsNone(_detect_cli_help_invocation(self._shlex(cmd), cmd))

    def test_returns_none_for_non_tools_script(self) -> None:
        cmd = "python3 scripts/other.py --help"
        self.assertIsNone(_detect_cli_help_invocation(self._shlex(cmd), cmd))

    def test_returns_none_for_non_python_invocation(self) -> None:
        cmd = "cat tools/orchestration_runtime.py"
        self.assertIsNone(_detect_cli_help_invocation(self._shlex(cmd), cmd))

    def test_returns_none_for_module_form_invocation(self) -> None:
        """The `python -m tools.orchestration_runtime` form is not canonical and is out of detection scope."""
        cmd = "python3 -m tools.orchestration_runtime record-launch --help"
        self.assertIsNone(_detect_cli_help_invocation(self._shlex(cmd), cmd))


class EvaluateCommonPolicyCliHelpAuditTests(unittest.TestCase):
    """`evaluate_common_policy`'s cli_help audit leaves audit_detail on the ALLOW path."""

    def _make(self, command: str) -> HookInput:
        return HookInput(
            event_name=HookEventName.PRE_COMMAND_EXECUTE,
            backend="claude",
            payload={"repo_root": "."},
            tool_name="Bash",
            command=command,
        )

    def test_cli_help_invocation_allows_with_audit_detail_in_workflow_mode(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}):
            decision = evaluate_common_policy(
                self._make("python3 tools/orchestration_runtime.py record-launch --help")
            )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)
        self.assertIsNotNone(decision.audit_detail)
        assert decision.audit_detail is not None
        self.assertEqual(decision.audit_detail["policy"], "cli_help_invocation_observed")
        self.assertEqual(decision.audit_detail["subcommand"], "record-launch")

    def test_non_help_invocation_allows_without_audit_detail(self) -> None:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}):
            decision = evaluate_common_policy(
                self._make("python3 tools/orchestration_runtime.py record-launch --repo-root .")
            )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)
        self.assertIsNone(decision.audit_detail)

    def test_implementation_read_still_blocked_in_workflow_mode(self) -> None:
        """Even while permitting the `--help` path, a direct implementation read such as `cat tools/X.py` remains blocked."""
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}):
            decision = evaluate_common_policy(
                self._make("cat tools/orchestration_runtime.py")
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        assert decision.audit_detail is not None
        self.assertEqual(decision.audit_detail["policy"], "forbid_tools_direct_read")

    def test_cli_help_audit_skipped_outside_workflow_mode(self) -> None:
        """When workflow mode is disabled, the hook does not attach audit_detail."""
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "0"}):
            decision = evaluate_common_policy(
                self._make("python3 tools/orchestration_runtime.py record-launch --help")
            )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)
        self.assertIsNone(decision.audit_detail)


class ValidateWriteAccessDirectoryAllowlistTests(unittest.TestCase):
    """validate_write_access: extension policy must be enforced for directory allowlist entries."""

    def _write_manifest(
        self,
        repo_root: Path,
        *,
        orchestration_id: str,
        agent_run_id: str,
        allowed_output_paths: list[str],
    ) -> None:
        from pathlib import Path
        manifest_dir = (
            repo_root
            / "workspace"
            / "orchestrations"
            / orchestration_id
            / "output_manifests"
        )
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / f"{agent_run_id}.json").write_text(
            json.dumps({
                "allowed_output_paths": allowed_output_paths,
                "allowed_file_tool_paths": [],
            }),
            encoding="utf-8",
        )

    def _call(
        self,
        repo_root: "Path",
        orchestration_id: str,
        agent_run_id: str,
        file_path: str,
    ) -> "HookDecision":
        from tools.hooks.common import validate_write_access
        from pathlib import Path
        return validate_write_access(repo_root, orchestration_id, agent_run_id, file_path)

    def test_allows_known_extension_under_directory_entry(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch1", agent_run_id="run1",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch1", "run1",
                "workspace/pipelines/a/generate/g1/src/flux.f90",
            )
            self.assertEqual(decision.action, HookDecisionAction.ALLOW)

    def test_blocks_makefile_under_directory_entry(self) -> None:
        """Makefile is a build-control file — requires explicit file pin."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch2", agent_run_id="run2",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch2", "run2",
                "workspace/pipelines/a/generate/g1/src/Makefile",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_script_under_directory_entry(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch3", agent_run_id="run3",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch3", "run3",
                "workspace/pipelines/a/generate/g1/src/exploit.sh",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_unknown_extensionless_under_directory_entry(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch4", agent_run_id="run4",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch4", "run4",
                "workspace/pipelines/a/generate/g1/src/myexe",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_shared_lib_under_directory_entry(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch5", agent_run_id="run5",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch5", "run5",
                "workspace/pipelines/a/generate/g1/src/lib.so",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_cmake_under_directory_entry(self) -> None:
        """Build control file (.cmake) requires explicit file pin — can inject arbitrary commands."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch_cmake", agent_run_id="run_cmake",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch_cmake", "run_cmake",
                "workspace/pipelines/a/generate/g1/src/CMakeLists.txt",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_mk_under_directory_entry(self) -> None:
        """Build control file (.mk) requires explicit file pin."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch_mk", agent_run_id="run_mk",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch_mk", "run_mk",
                "workspace/pipelines/a/generate/g1/src/rules.mk",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_toml_under_directory_entry(self) -> None:
        """Build control file (.toml) requires explicit file pin."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch_toml", agent_run_id="run_toml",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch_toml", "run_toml",
                "workspace/pipelines/a/generate/g1/src/build.toml",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_nml_under_directory_entry(self) -> None:
        """Namelist file (.nml) requires explicit file pin — data injection risk."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch_nml", agent_run_id="run_nml",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch_nml", "run_nml",
                "workspace/pipelines/a/generate/g1/src/params.nml",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_json_under_directory_entry(self) -> None:
        """Structured data (.json) requires explicit file pin, not directory allowlist."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch6", agent_run_id="run6",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch6", "run6",
                "workspace/pipelines/a/generate/g1/src/results.json",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_yaml_under_directory_entry(self) -> None:
        """Structured data (.yaml) requires explicit file pin, not directory allowlist."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch7", agent_run_id="run7",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch7", "run7",
                "workspace/pipelines/a/generate/g1/src/config.yaml",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_object_file_under_directory_entry(self) -> None:
        """Compiler byproducts (.o) are created by subprocess, never via Edit/Write — must be blocked."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch8", agent_run_id="run8",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch8", "run8",
                "workspace/pipelines/a/generate/g1/src/flux.o",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_module_file_under_directory_entry(self) -> None:
        """Compiler byproducts (.mod) are created by subprocess, never via Edit/Write — must be blocked."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch9", agent_run_id="run9",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch9", "run9",
                "workspace/pipelines/a/generate/g1/src/flux.mod",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)

    def test_blocks_archive_file_under_directory_entry(self) -> None:
        """Compiler byproducts (.a) are created by subprocess, never via Edit/Write — must be blocked."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root, orchestration_id="orch10", agent_run_id="run10",
                allowed_output_paths=["workspace/pipelines/a/generate/g1/src/"],
            )
            decision = self._call(
                repo_root, "orch10", "run10",
                "workspace/pipelines/a/generate/g1/src/libflux.a",
            )
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)


class ForbidPythonInlineWriteNewPatternsTests(unittest.TestCase):
    """B-1: heredoc / write_text / shutil detection added in forbid_python_inline_write."""

    def _call(self, command: str) -> HookDecision:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            return evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": command},
                    command=command,
                )
            )

    def test_blocks_python_heredoc_inline_write(self) -> None:
        decision = self._call("python3 - <<'EOF'\nopen('out.txt','w').write('x')\nEOF")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_heredoc_dash_variant_with_write(self) -> None:
        decision = self._call(
            "python3 - <<-EOF\n"
            "from pathlib import Path\n"
            "Path('x').write_text('y')\n"
            "EOF"
        )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_read_only_python_heredoc_under_fail_closed(self) -> None:
        """Workflow-mode policy is now fail-closed for ALL python heredocs,
        including read-only diagnostics. Regex-based read-vs-write detection
        proved unreliable; agents should use tools/audit_orchestration.py
        or a real script file for log inspection."""
        decision = self._call(
            "python3 - <<'EOF'\n"
            "import json, pathlib\n"
            "for line in pathlib.Path('x.jsonl').read_text().splitlines():\n"
            "    obj = json.loads(line)\n"
            "    print(obj.get('action'))\n"
            "EOF"
        )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"),
            "forbid_python_inline_write",
        )

    def test_blocks_python_heredoc_print_only_under_fail_closed(self) -> None:
        """Even a `print('x')` heredoc is blocked under fail-closed."""
        decision = self._call("python3 - <<-EOF\nprint('x')\nEOF")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"),
            "forbid_python_inline_write",
        )

    def test_blocks_python_c_with_path_write_text(self) -> None:
        decision = self._call("python3 -c 'from pathlib import Path; Path(\"x.txt\").write_text(\"hi\")'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_path_write_bytes(self) -> None:
        decision = self._call("python3 -c 'from pathlib import Path; Path(\"x.bin\").write_bytes(b\"\")'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_shutil_copy(self) -> None:
        decision = self._call("python3 -c 'import shutil; shutil.copy(\"a\", \"b\")'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_shutil_move(self) -> None:
        decision = self._call("python3 -c 'import shutil; shutil.move(\"a\", \"b\")'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_path_touch(self) -> None:
        """Regression: Path('x').touch() creates a file — must block."""
        decision = self._call("python3 -c 'from pathlib import Path; Path(\"x\").touch()'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_path_mkdir(self) -> None:
        """Regression: Path('d').mkdir() creates a directory — must block."""
        decision = self._call("python3 -c 'from pathlib import Path; Path(\"d\").mkdir()'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_os_rename(self) -> None:
        """Regression: os.rename(a, b) is a filesystem mutation — must block."""
        decision = self._call("python3 -c 'import os; os.rename(\"a\",\"b\")'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_os_system(self) -> None:
        """Regression: os.system shells out to anything — must block."""
        decision = self._call("python3 -c 'import os; os.system(\"whoami\")'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_subprocess_run(self) -> None:
        """Regression: subprocess.run can invoke any command — must block."""
        decision = self._call("python3 -c 'import subprocess; subprocess.run([\"ls\"])'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_shutil_rmtree(self) -> None:
        """Regression: shutil.rmtree deletes filesystem trees — must block."""
        decision = self._call("python3 -c 'import shutil; shutil.rmtree(\"x\")'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_c_with_tempfile_mkstemp(self) -> None:
        """Regression: tempfile.mkstemp creates temporary files — must block."""
        decision = self._call("python3 -c 'import tempfile; tempfile.mkstemp()'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_all_python_dash_c_inline_execution(self) -> None:
        """Workflow-mode policy is fail-closed for ALL `python -c` execution.

        Regex-based filtering cannot reliably catch alias bypasses like
        `from pathlib import Path as P; P('x').write_text(...)` or string
        literals embedded in inline source. Even a `print(1)` snippet is
        blocked — agents must use a real script file or
        tools/audit_orchestration.py for log inspection.
        """
        decision = self._call('python3 -c "print(1)"')
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_dash_c_with_alias_bypass(self) -> None:
        """Regression: alias `Path as P; P('x').write_text(...)` is no longer
        regex-matchable but still blocked under fail-closed policy."""
        decision = self._call(
            "python3 -c \"from pathlib import Path as P; P('x').write_text('y')\""
        )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_dash_c_with_open_then_write(self) -> None:
        """Regression: `Path('x').open('w').write(...)` is not in the old
        regex list but is still blocked under fail-closed."""
        decision = self._call(
            "python3 -c \"Path('x').open('w').write('y')\""
        )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_python_inline_write")

    def test_blocks_python_dash_c_with_dev_shm_string_literal(self) -> None:
        """Regression: `python3 -c \"open('/dev/shm/x').read()\"` previously
        bypassed both the /dev/shm guard and inline-write detection."""
        decision = self._call("python3 -c \"open('/dev/shm/x').read()\"")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        # The python inline policy fires before the /dev/shm check, so the
        # policy code is forbid_python_inline_write rather than
        # output_manifest_write_guard. Either is acceptable enforcement.
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertIn(policy, ("forbid_python_inline_write", "output_manifest_write_guard"))

    def test_allows_python_script_file_invocation(self) -> None:
        """Real script files (`python3 script.py`) must NOT be blocked —
        they go through normal write/read manifest validation."""
        decision = self._call("python3 script.py")
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "forbid_python_inline_write")

    def test_allows_python_dash_m_module_invocation(self) -> None:
        """`python3 -m json.tool x.json` is module invocation, not inline -c."""
        decision = self._call("python3 -m json.tool x.json")
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "forbid_python_inline_write")

    def test_allows_normal_python_script(self) -> None:
        decision = self._call("python3 tools/run_workflow.py spec generate --llm claude")
        # Should NOT block on forbid_python_inline_write (may still block on tools-direct-read
        # if workflow mode active; we only verify not blocked by inline-write policy)
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "forbid_python_inline_write")

    def test_uuid_intent_emits_new_agent_run_id_hint(self) -> None:
        decision = self._call("python3 -c 'import uuid; print(uuid.uuid4())'")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        detail = decision.audit_detail or {}
        self.assertEqual(detail.get("policy"), "forbid_python_inline_write")
        self.assertEqual(detail.get("intent_detected"), "uuid")
        self.assertEqual(
            (detail.get("fix_hint") or {}).get("next_command"),
            "python3 tools/new_agent_run_id.py",
        )

    def test_uuid1_and_uuid5_also_classified_as_uuid_intent(self) -> None:
        """Pin coverage of uuid.uuid1/uuid3/uuid5 — agents that reach for
        non-uuid4 variants must get the same new_agent_run_id.py hint, not the
        default write hint."""
        for fn in ("uuid1", "uuid3", "uuid5"):
            decision = self._call(f"python3 -c 'import uuid; print(uuid.{fn}())'")
            detail = decision.audit_detail or {}
            self.assertEqual(decision.action, HookDecisionAction.BLOCK)
            self.assertEqual(
                detail.get("intent_detected"), "uuid",
                msg=f"uuid.{fn} should classify as uuid intent",
            )
            self.assertEqual(
                (detail.get("fix_hint") or {}).get("next_command"),
                "python3 tools/new_agent_run_id.py",
            )

    def test_json_read_intent_emits_read_tool_hint(self) -> None:
        decision = self._call(
            "python3 -c \"import json; print(json.load(open('x.json')))\""
        )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        detail = decision.audit_detail or {}
        self.assertEqual(detail.get("policy"), "forbid_python_inline_write")
        self.assertEqual(detail.get("intent_detected"), "json_read")
        self.assertIn(
            "Read tool",
            (detail.get("fix_hint") or {}).get("next_command", ""),
        )

    def test_default_write_intent_emits_edit_write_hint(self) -> None:
        decision = self._call("python3 -c \"open('x.json','w').write('{}')\"")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        detail = decision.audit_detail or {}
        self.assertEqual(detail.get("policy"), "forbid_python_inline_write")
        self.assertEqual(detail.get("intent_detected"), "write")
        # Phase-2: the write-intent recovery is the Edit/Write tool, not the
        # deprecated guarded-apply-patch.
        next_command = (detail.get("fix_hint") or {}).get("next_command", "")
        self.assertIn("Edit/Write tool", next_command)
        self.assertNotIn("guarded-apply-patch", next_command)

    def test_heredoc_uuid_intent_emits_proc_random_hint(self) -> None:
        """Boundary: intent classification must work for the heredoc form, not
        only `python -c`. The block path differs (heredoc detected by regex,
        not by `-c` token) but the intent-detection scan over `command`
        applies uniformly."""
        decision = self._call(
            "python3 - <<'EOF'\nimport uuid; print(uuid.uuid4())\nEOF"
        )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        detail = decision.audit_detail or {}
        self.assertEqual(detail.get("policy"), "forbid_python_inline_write")
        self.assertEqual(detail.get("intent_detected"), "uuid")
        self.assertEqual(
            (detail.get("fix_hint") or {}).get("next_command"),
            "python3 tools/new_agent_run_id.py",
        )


class AutoReadToleratedTests(unittest.TestCase):
    """B-2: orchestration agent auto-read of MEMORY.md/README.md/etc. returns allow."""

    def _make_hook_input_read(self, file_path: str, role: str | None = None) -> HookInput:
        payload: dict = {
            "file_path": file_path,
            "orchestration_id": "orch_test",
            "agent_run_id": "run_orch",
        }
        if role:
            payload["agent_role"] = role
        return HookInput(
            event_name=HookEventName.PRE_TOOL_USE,
            backend="claude",
            payload=payload,
            tool_name="Read",
        )

    def _call_validate_read(self, file_path: str, agent_role: str) -> HookDecision:
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_test"
            orch_root.mkdir(parents=True)
            # Within the startup window — the auto-read tolerance check is
            # fail-closed without a verifiable started_at.
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({
                    "started_at": recent_ts,
                    "orchestration_agent_run_id": "run_orch",
                    "orchestration_id": "orch_test",
                }),
                encoding="utf-8",
            )
            manifest_dir = orch_root / "read_manifests"
            manifest_dir.mkdir()
            (manifest_dir / "run_orch.json").write_text(json.dumps({
                "allowed_read_roots": ["workspace/orchestrations/orch_test/"],
                "denied_read_roots": [],
            }), encoding="utf-8")
            return validate_read_access(
                repo_root,
                "orch_test",
                "run_orch",
                file_path,
                agent_role=agent_role,
            )

    def test_orchestration_reads_memory_md_blocked_as_expected(self) -> None:
        # Auto-read paths must BLOCK to preserve the read trust boundary,
        # but be tagged with the distinct `auto_read_expected_block` policy
        # so audit can categorize them as benign platform noise.
        decision = self._call_validate_read("MEMORY.md", "orchestration")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "auto_read_expected_block")

    def test_auto_read_expected_block_includes_agent_run_id(self) -> None:
        """Regression: audit_detail must include agent_run_id so the audit
        helper's per-agent benign-volume thresholding can attribute counts
        instead of aggregating under <unknown>."""
        decision = self._call_validate_read("MEMORY.md", "orchestration")
        self.assertEqual((decision.audit_detail or {}).get("policy"), "auto_read_expected_block")
        self.assertEqual((decision.audit_detail or {}).get("agent_run_id"), "run_orch")
        self.assertEqual(
            (decision.audit_detail or {}).get("orchestration_id"),
            "orch_test",
        )

    def test_second_read_of_same_path_is_substantive(self) -> None:
        """Regression: the FIRST read of an allowlisted path is benign
        (Claude Code one-time startup auto-read), but a SECOND read of the
        same path by the same agent is a prompt-induced access and must
        fall through to the substantive read_manifest_read_guard policy."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_test"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": recent_ts}), encoding="utf-8"
            )
            manifest_dir = orch_root / "read_manifests"
            manifest_dir.mkdir()
            (manifest_dir / "run_orch.json").write_text(json.dumps({
                "allowed_read_roots": ["workspace/orchestrations/orch_test/"],
                "denied_read_roots": [],
            }), encoding="utf-8")
            # First read → benign
            d1 = validate_read_access(
                repo_root, "orch_test", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertEqual(
                (d1.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )
            # Second read → substantive
            d2 = validate_read_access(
                repo_root, "orch_test", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertEqual(d2.action, HookDecisionAction.BLOCK)
            self.assertNotEqual(
                (d2.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_late_first_read_outside_startup_window_is_substantive(self) -> None:
        """Regression: a 'first read' of an allowlisted path that arrives long
        after orchestration started_at is more likely a prompt-induced access
        than a delayed startup probe — must NOT be classified benign."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone, timedelta
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_late"
            orch_root.mkdir(parents=True)
            # started_at one hour ago — well outside the 120s startup window
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace(
                "+00:00", "Z"
            )
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({
                    "started_at": old_ts,
                    "orchestration_agent_run_id": "run_orch",
                }),
                encoding="utf-8",
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_late/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            decision = validate_read_access(
                repo_root, "orch_late", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_fail_closed_when_orchestration_meta_missing(self) -> None:
        """Regression: if orchestration_meta.json is missing, the startup-window
        check has no anchor and we cannot prove the read is benign platform
        noise. Fail-closed: classify as substantive read_manifest_read_guard."""
        from tools.hooks.common import validate_read_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_no_meta"
            orch_root.mkdir(parents=True)
            # No orchestration_meta.json on purpose
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_no_meta/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            decision = validate_read_access(
                repo_root, "orch_no_meta", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_fail_closed_when_started_at_missing(self) -> None:
        """orchestration_meta.json exists but has no started_at field."""
        from tools.hooks.common import validate_read_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_no_ts"
            orch_root.mkdir(parents=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_no_ts"}),
                encoding="utf-8",
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_no_ts/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            decision = validate_read_access(
                repo_root, "orch_no_ts", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_fail_closed_when_started_at_malformed(self) -> None:
        """Malformed started_at must trigger fail-closed substantive behavior."""
        from tools.hooks.common import validate_read_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_bad_ts"
            orch_root.mkdir(parents=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": "not-a-valid-iso-timestamp"}),
                encoding="utf-8",
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_bad_ts/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            decision = validate_read_access(
                repo_root, "orch_bad_ts", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_fail_closed_under_persistent_lock_contention(self) -> None:
        """Regression: a stuck holder of the seen-set lock must NOT cause
        every subsequent Read hook to hang indefinitely. The bounded
        retry-then-fail-closed path returns within ~5*backoff seconds."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import fcntl
        import os
        import tempfile
        import time
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_locked"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": recent_ts}), encoding="utf-8"
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_locked/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            audit_dir = orch_root / "audit"
            audit_dir.mkdir()
            seen_path = audit_dir / "run_orch.auto_reads_seen.json"
            seen_path.write_text("[]", encoding="utf-8")
            # Hold an exclusive lock from this test process.
            holder = os.open(str(seen_path), os.O_RDWR)
            fcntl.flock(holder, fcntl.LOCK_EX)
            try:
                t0 = time.monotonic()
                decision = validate_read_access(
                    repo_root, "orch_locked", "run_orch", "MEMORY.md",
                    agent_role="orchestration",
                )
                elapsed = time.monotonic() - t0
            finally:
                fcntl.flock(holder, fcntl.LOCK_UN)
                os.close(holder)
            # Must NOT hang — bounded by retry-count × backoff (≈ 0.5s).
            # Use a tighter cap (2.0s) so a regression that increased the
            # retry count or backoff would be caught.
            self.assertLess(elapsed, 2.0)
            # Must fail-closed → substantive policy hit, not benign
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_fail_closed_on_non_posix_no_fcntl(self) -> None:
        """Regression: on Windows / non-POSIX, `_fcntl` is None at module
        scope. Auto-read tolerance must fail-closed (no portable file lock)
        rather than crashing or returning benign by default."""
        from unittest.mock import patch
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_winlike"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": recent_ts}), encoding="utf-8"
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_winlike/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            with patch("tools.hooks.common._fcntl", None):
                decision = validate_read_access(
                    repo_root, "orch_winlike", "run_orch", "MEMORY.md",
                    agent_role="orchestration",
                )
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_fail_closed_when_seen_set_oversized(self) -> None:
        """Regression: a seen-set file larger than 64KiB indicates corruption
        or attack. Previous code silently truncated to 1MB and reset the set
        on JSON-parse failure, discarding all prior entries. Now it
        fail-closes and PRESERVES the file (does not overwrite it)."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_big"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": recent_ts}), encoding="utf-8"
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_big/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            audit_dir = orch_root / "audit"
            audit_dir.mkdir()
            seen_path = audit_dir / "run_orch.auto_reads_seen.json"
            # Write 2 MiB seen-set — well above the 64 KiB cap
            big_list = ["/path_" + ("x" * 1000) + str(i) for i in range(2000)]
            seen_path.write_text(json.dumps(big_list), encoding="utf-8")
            original_size = seen_path.stat().st_size
            decision = validate_read_access(
                repo_root, "orch_big", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )
            # File preserved — must NOT be silently overwritten
            self.assertEqual(seen_path.stat().st_size, original_size)

    def test_first_read_recovers_when_seen_set_corrupted_json(self) -> None:
        """A non-list JSON value (e.g. {"corrupted": true}) in the seen-set
        file must not cause the function to crash. It should treat the
        seen-set as empty for this call (recovering gracefully)."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_corrupt"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": recent_ts}), encoding="utf-8"
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_corrupt/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            (orch_root / "audit").mkdir()
            (orch_root / "audit" / "run_orch.auto_reads_seen.json").write_text(
                json.dumps({"corrupted": True}), encoding="utf-8"
            )
            decision = validate_read_access(
                repo_root, "orch_corrupt", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            # Treats seen-set as empty → first read is benign
            self.assertEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_fail_closed_when_audit_dir_read_only(self) -> None:
        """Regression: when the audit dir is read-only, persistence of the
        seen-set fails. The previous fallback returned True (benign) which
        let an attacker who can chmod audit/ keep MEMORY.md in benign
        classification permanently. Fail-closed: refuse benign classification."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_ro"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": recent_ts}), encoding="utf-8"
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_ro/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            # Replace audit/ with a regular file. The persistence path tries
            # `state_path.parent.mkdir(..., exist_ok=True)` first, which fails
            # with FileExistsError/NotADirectoryError when the path exists as a
            # file rather than a directory. This simulation reliably exercises
            # the fail-closed branch regardless of the test runner's uid (root
            # can bypass chmod-based read-only directories on POSIX).
            audit_dir = orch_root / "audit"
            audit_dir.write_text("placeholder file blocking mkdir\n", encoding="utf-8")
            try:
                decision = validate_read_access(
                    repo_root, "orch_ro", "run_orch", "MEMORY.md",
                    agent_role="orchestration",
                )
            finally:
                audit_dir.unlink(missing_ok=True)
            self.assertNotEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_first_read_within_startup_window_is_benign(self) -> None:
        """Positive: a first read that arrives within the startup window
        IS classified as benign auto-read."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_fresh"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({
                    "started_at": recent_ts,
                    "orchestration_agent_run_id": "run_orch",
                }),
                encoding="utf-8",
            )
            (orch_root / "read_manifests").mkdir()
            (orch_root / "read_manifests" / "run_orch.json").write_text(
                json.dumps({"allowed_read_roots": ["workspace/orchestrations/orch_fresh/"], "denied_read_roots": []}),
                encoding="utf-8",
            )
            decision = validate_read_access(
                repo_root, "orch_fresh", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertEqual(
                (decision.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_repeated_read_with_different_path_spellings_collapses(self) -> None:
        """Regression: re-spelling the same protected file (./MEMORY.md vs
        absolute path vs MEMORY.md) MUST NOT reset the seen-set. Otherwise
        a second read can stay in the benign bucket by changing the
        spelling — defeating the first-read invariant."""
        from tools.hooks.common import validate_read_access
        from datetime import datetime, timezone
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_test"
            orch_root.mkdir(parents=True)
            recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"started_at": recent_ts}), encoding="utf-8"
            )
            manifest_dir = orch_root / "read_manifests"
            manifest_dir.mkdir()
            (manifest_dir / "run_orch.json").write_text(json.dumps({
                "allowed_read_roots": ["workspace/orchestrations/orch_test/"],
                "denied_read_roots": [],
            }), encoding="utf-8")
            # First read with bare relative path → benign
            d1 = validate_read_access(
                repo_root, "orch_test", "run_orch", "MEMORY.md",
                agent_role="orchestration",
            )
            self.assertEqual(
                (d1.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )
            # Second read with `./` prefix — must collapse to same key
            d2 = validate_read_access(
                repo_root, "orch_test", "run_orch", "./MEMORY.md",
                agent_role="orchestration",
            )
            self.assertNotEqual(
                (d2.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )
            # Third read with absolute path — must also collapse
            d3 = validate_read_access(
                repo_root, "orch_test", "run_orch", str(repo_root / "MEMORY.md"),
                agent_role="orchestration",
            )
            self.assertNotEqual(
                (d3.audit_detail or {}).get("policy"),
                "auto_read_expected_block",
            )

    def test_orchestration_reads_readme_blocked_as_expected(self) -> None:
        decision = self._call_validate_read("README.md", "orchestration")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "auto_read_expected_block")

    def test_orchestration_reads_claude_settings_blocked_as_expected(self) -> None:
        decision = self._call_validate_read(".claude/settings.json", "orchestration")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "auto_read_expected_block")

    def test_substep_reads_memory_md_not_tolerated(self) -> None:
        # substep should still be blocked by the substantive read_manifest_read_guard,
        # not auto-read tolerance (which is orchestration-only for MEMORY.md).
        decision = self._call_validate_read("MEMORY.md", "substep")
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    # ---- Harness-mandatory auto-read (all agent roles) -----------------

    def test_substep_reads_claude_settings_is_harness_tolerated(self) -> None:
        # .claude/settings.json is Claude Code harness auto-discovery; the
        # substep harness reads it at startup regardless of agent role and
        # the agent cannot suppress it. Must be classified benign.
        decision = self._call_validate_read(".claude/settings.json", "substep")
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"), "auto_read_expected_block",
        )

    def test_substep_reads_mcp_servers_readme_is_harness_tolerated(self) -> None:
        decision = self._call_validate_read("mcp_servers/README.md", "substep")
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"), "auto_read_expected_block",
        )


    def test_substep_reads_mcp_servers_tools_run_linter_is_harness_tolerated(self) -> None:
        decision = self._call_validate_read(
            "mcp_servers/tools/run_linter.json", "substep",
        )
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"), "auto_read_expected_block",
        )

    def test_substep_reads_mcp_servers_example_json_is_harness_tolerated(self) -> None:
        # mcp_servers.example.json is in _HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS
        # — verify substep harness Read is benign.
        decision = self._call_validate_read(
            "mcp_servers/mcp_servers.example.json", "substep",
        )
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"), "auto_read_expected_block",
        )

    def test_orchestration_reads_mcp_servers_tools_prefix_is_harness_tolerated(self) -> None:
        # Regression-lock: harness category 1 is branchless on agent_role.
        # If a future refactor mistakenly gates the prefix loop on
        # `agent_role == "orchestration"`, this positive test for the
        # orchestration role on a prefix path catches the regression.
        decision = self._call_validate_read(
            "mcp_servers/tools/run_linter.json", "orchestration",
        )
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"), "auto_read_expected_block",
        )

    def test_substep_reads_mcp_servers_tools_arbitrary_json_is_harness_tolerated(self) -> None:
        # Prefix match: any file under mcp_servers/tools/ is harness-discovered.
        decision = self._call_validate_read(
            "mcp_servers/tools/other.json", "substep",
        )
        self.assertEqual(
            (decision.audit_detail or {}).get("policy"), "auto_read_expected_block",
        )

    def test_substep_reads_mcp_servers_runtime_py_not_tolerated(self) -> None:
        # The Python runtime file is NOT in harness set/prefix — it must
        # remain blocked by the substantive read_manifest_read_guard so an
        # agent cannot use the tolerance path to dig into implementation.
        decision = self._call_validate_read(
            "mcp_servers/build_runtime_server.py", "substep",
        )
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    def test_substep_reads_mcp_servers_other_dir_json_not_tolerated(self) -> None:
        # Regression: prefix is "mcp_servers/tools/" only. A nested dir
        # under mcp_servers/ that is not /tools/ must not be tolerated.
        decision = self._call_validate_read(
            "mcp_servers/other_dir/file.json", "substep",
        )
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    def test_substep_cannot_bypass_via_traversal_in_mcp_tools(self) -> None:
        # Regression: ../mcp_servers/tools/x.json normalised lexically must
        # not lead outside the repo prefix.
        decision = self._call_validate_read(
            "../mcp_servers/tools/x.json", "substep",
        )
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    def test_orchestration_rejects_symlinked_mcp_servers_readme(self) -> None:
        """Symlink attack: a symlink at mcp_servers/README.md pointing at an
        arbitrary host file must NOT be tolerated even for orchestration."""
        from tools.hooks.common import _is_auto_read_tolerated
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_root = tmp_path / "repo"
            (repo_root / "mcp_servers").mkdir(parents=True)
            target = tmp_path / "secret.txt"
            target.write_text("secret", encoding="utf-8")
            os.symlink(target, repo_root / "mcp_servers" / "README.md")
            self.assertFalse(
                _is_auto_read_tolerated(
                    repo_root, "orchestration", "mcp_servers/README.md",
                )
            )

    def test_substep_rejects_symlinked_mcp_tools_glob(self) -> None:
        """Symlink attack on a prefix-matched file: a symlink inside
        mcp_servers/tools/ pointing outside must NOT be tolerated."""
        from tools.hooks.common import _is_auto_read_tolerated
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_root = tmp_path / "repo"
            (repo_root / "mcp_servers" / "tools").mkdir(parents=True)
            target = tmp_path / "secret.txt"
            target.write_text("secret", encoding="utf-8")
            os.symlink(target, repo_root / "mcp_servers" / "tools" / "x.json")
            self.assertFalse(
                _is_auto_read_tolerated(
                    repo_root, "substep", "mcp_servers/tools/x.json",
                )
            )

    def test_substep_rejects_when_mcp_tools_directory_is_symlink(self) -> None:
        """Symlink attack on the prefix directory itself: if `mcp_servers/tools`
        is a symlink to e.g. /etc, the per-component lstat walk must reject the
        read regardless of the leaf path being within the prefix set."""
        from tools.hooks.common import _is_auto_read_tolerated
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_root = tmp_path / "repo"
            (repo_root / "mcp_servers").mkdir(parents=True)
            attacker_dir = tmp_path / "attacker_payload"
            attacker_dir.mkdir()
            (attacker_dir / "passwd").write_text("secret", encoding="utf-8")
            os.symlink(attacker_dir, repo_root / "mcp_servers" / "tools")
            self.assertFalse(
                _is_auto_read_tolerated(
                    repo_root, "substep", "mcp_servers/tools/passwd",
                )
            )

    def test_orchestration_cannot_bypass_via_absolute_etc_readme(self) -> None:
        # Regression: /etc/README.md must NOT be tolerated even though it ends with /README.md
        decision = self._call_validate_read("/etc/README.md", "orchestration")
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    def test_orchestration_cannot_bypass_via_traversal_readme(self) -> None:
        # Regression: ../etc/README.md must NOT be tolerated
        decision = self._call_validate_read("../README.md", "orchestration")
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    def test_orchestration_cannot_bypass_via_subdir_readme(self) -> None:
        # Regression: workspace/foo/README.md is NOT one of the auto-read paths
        decision = self._call_validate_read("workspace/foo/README.md", "orchestration")
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    def test_orchestration_cannot_bypass_via_settings_in_other_dir(self) -> None:
        # Regression: foo/.claude/settings.json must NOT be tolerated
        decision = self._call_validate_read("subdir/.claude/settings.json", "orchestration")
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "auto_read_expected_block")

    def test_orchestration_cannot_read_other_project_memory(self) -> None:
        """Regression: ~/.claude/projects/<other-slug>/memory/MEMORY.md must NOT be tolerated.

        Tolerance is bound to the current repo's slug only — cross-project memory
        access is forbidden.
        """
        from tools.hooks.common import _is_auto_read_tolerated
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "my-repo"
            repo_root.mkdir()
            other_project_path = (
                Path.home() / ".claude" / "projects"
                / "-some-other-project" / "memory" / "MEMORY.md"
            )
            self.assertFalse(
                _is_auto_read_tolerated(repo_root, "orchestration", str(other_project_path))
            )

    def test_orchestration_can_read_own_project_memory(self) -> None:
        """Positive case: own project's ~/.claude/projects/<own-slug>/memory/MEMORY.md is tolerated."""
        from tools.hooks.common import _is_auto_read_tolerated, _claude_project_slug
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = (Path(tmp) / "my-repo").resolve()
            repo_root.mkdir()
            own_slug = _claude_project_slug(repo_root)
            own_memory = (
                Path.home() / ".claude" / "projects" / own_slug / "memory" / "MEMORY.md"
            )
            self.assertTrue(
                _is_auto_read_tolerated(repo_root, "orchestration", str(own_memory))
            )

    def test_orchestration_rejects_symlinked_memory_md(self) -> None:
        """Regression: if the tolerated path is a symlink, refuse tolerance.

        An attacker who can place a symlink at ~/.claude/projects/<slug>/memory/MEMORY.md
        could otherwise redirect reads to arbitrary host files.
        """
        from tools.hooks.common import _is_auto_read_tolerated
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Construct a fake "MEMORY.md" symlink inside repo
            repo_root = tmp_path / "repo"
            repo_root.mkdir()
            target = tmp_path / "secret.txt"
            target.write_text("secret", encoding="utf-8")
            symlinked_memory = repo_root / "MEMORY.md"
            os.symlink(target, symlinked_memory)
            self.assertFalse(
                _is_auto_read_tolerated(repo_root, "orchestration", "MEMORY.md")
            )

    def test_orchestration_rejects_when_intermediate_dir_is_symlink(self) -> None:
        """Regression: if an intermediate directory in the tolerated path is a
        symlink, refuse tolerance."""
        from tools.hooks.common import _is_auto_read_tolerated
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_root = tmp_path / "repo"
            repo_root.mkdir()
            real_settings_dir = tmp_path / "real_claude"
            real_settings_dir.mkdir()
            (real_settings_dir / "settings.json").write_text("{}", encoding="utf-8")
            os.symlink(real_settings_dir, repo_root / ".claude")
            self.assertFalse(
                _is_auto_read_tolerated(repo_root, "orchestration", ".claude/settings.json")
            )


class FixHintInAuditDetailTests(unittest.TestCase):
    """B-3: audit_detail.fix_hint is populated on output_manifest_write_guard blocks."""

    def _write_manifest(
        self,
        repo_root,
        *,
        orchestration_id: str,
        agent_run_id: str,
        allowed_output_paths: list,
        allowed_tmp_root: str = "workspace/tmp/run_x",
    ) -> None:
        from pathlib import Path
        mdir = Path(repo_root) / "workspace" / "orchestrations" / orchestration_id / "output_manifests"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / f"{agent_run_id}.json").write_text(json.dumps({
            "agent_run_id": agent_run_id,
            "allowed_output_paths": allowed_output_paths,
            "allowed_file_tool_paths": [],
            "allowed_tmp_root": allowed_tmp_root,
        }), encoding="utf-8")

    def test_fix_hint_present_on_unauthorized_write(self) -> None:
        from tools.hooks.common import validate_write_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root,
                orchestration_id="orchFH",
                agent_run_id="runFH",
                allowed_output_paths=["workspace/outputs/"],
            )
            decision = validate_write_access(
                repo_root,
                "orchFH",
                "runFH",
                "workspace/bad/out.json",
                tool_name="Write",
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        audit = decision.audit_detail or {}
        self.assertIn("fix_hint", audit)
        fix_hint = audit["fix_hint"]
        # Literal-path write hint replaces the legacy `next_command: export TMPDIR=...` form
        # so the recovery instruction never recommends a Bash command that would itself
        # trigger Claude Code session sandbox approval.
        self.assertIn("write_under", fix_hint)
        self.assertIn("workspace/tmp/", fix_hint["write_under"])
        self.assertIn("docs_ref", fix_hint)
        self.assertIn("AGENT_CONTRACT.md", fix_hint["docs_ref"])

    def test_fix_hint_flags_tmpdir_fallback_or_hardcode_in_bash(self) -> None:
        """Regression: when the offending Bash command contains `${TMPDIR:-fallback}`
        or hardcodes /tmp/, the fix_hint should mark tmpdir_fallback_or_hardcode=True
        and surface the canonical_doc anchor for the AGENT_CONTRACT.md tmp-area rules.
        The recovery hint must instruct the agent to use the literal allowed_tmp_root
        path, never `export TMPDIR=...` (which would itself need session approval)."""
        from tools.hooks.common import validate_write_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root,
                orchestration_id="orchSTP",
                agent_run_id="runSTP",
                allowed_output_paths=["workspace/outputs/"],
            )
            decision = validate_write_access(
                repo_root,
                "orchSTP",
                "runSTP",
                '"',  # actual file_path the parser would extract from a stripped redirect
                tool_name="Bash",
                bash_command='cat > "${TMPDIR:-workspace/tmp/runSTP}/x.py" <<EOF\npass\nEOF',
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        audit = decision.audit_detail or {}
        self.assertEqual(audit.get("policy"), "output_manifest_write_guard")
        fix_hint = audit.get("fix_hint") or {}
        self.assertTrue(fix_hint.get("tmpdir_fallback_or_hardcode"))
        self.assertIn("AGENT_CONTRACT.md", fix_hint.get("canonical_doc", ""))
        self.assertIn("workspace/tmp/", fix_hint.get("write_under", ""))
        # Recovery hint must not recommend an approval-blocking Bash form.
        note = fix_hint.get("note", "")
        self.assertNotIn("export TMPDIR=", note.replace("`export TMPDIR=...`", ""))
        self.assertNotIn("jq -er", note.replace("`jq -er ...`", ""))

    def test_fix_hint_absent_when_no_tmpdir_fallback_or_hardcode(self) -> None:
        """Negative regression: when no TMPDIR-fallback / hardcoded /tmp/ pattern
        is present, the fix_hint must NOT mark tmpdir_fallback_or_hardcode (otherwise
        the hint would mis-attribute writes that fail for unrelated reasons)."""
        from tools.hooks.common import validate_write_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root,
                orchestration_id="orchSTP2",
                agent_run_id="runSTP2",
                allowed_output_paths=["workspace/outputs/"],
            )
            decision = validate_write_access(
                repo_root,
                "orchSTP2",
                "runSTP2",
                "workspace/bad/out.json",
                tool_name="Bash",
                bash_command='cat > "${TMPDIR}/x.py" <<EOF\npass\nEOF',
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        audit = decision.audit_detail or {}
        fix_hint = audit.get("fix_hint") or {}
        self.assertFalse(fix_hint.get("tmpdir_fallback_or_hardcode"))
        self.assertNotIn("canonical_doc", fix_hint)

    def test_bash_redirect_to_exact_pinned_path_requires_gate_provenance(self) -> None:
        """Fix 2: Bash heredoc/redirect to an exact-pinned allowed_output_paths
        target must be blocked unless the path is in allowed_file_tool_paths.
        Matches the post-hoc check in record-agent-run that requires gate
        provenance for paths absent from manifest_file_tool_paths."""
        from tools.hooks.common import validate_write_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root,
                orchestration_id="orchB",
                agent_run_id="runB",
                allowed_output_paths=["workspace/pipelines/x/lineage.json"],
            )
            decision = validate_write_access(
                repo_root,
                "orchB",
                "runB",
                "workspace/pipelines/x/lineage.json",
                tool_name="Bash",
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        audit = decision.audit_detail or {}
        self.assertEqual(audit.get("policy"), "forbid_unauthorized_file_write")
        self.assertEqual(audit.get("tool_name"), "Bash")

    def test_bash_redirect_to_allowed_file_tool_path_is_blocked(self) -> None:
        """Phase-2: a Bash redirect / tee / sed -i is NEVER an authorized
        artifact-write path, even when the target is in allowed_file_tool_paths.
        Managed artifacts are written with the structured Edit/Write (or codex
        apply_patch) tools; Bash may only write scratch under allowed_tmp_root.
        Allowing a Bash redirect to a listed canonical path would let a managed
        output that became Edit/Write-eligible also silently authorize shell
        writes (incl. command-substitution exfil) to it."""
        from tools.hooks.common import validate_write_access
        import tempfile
        import json as _json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            mdir = repo_root / "workspace" / "orchestrations" / "orchBA" / "output_manifests"
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / "runBA.json").write_text(_json.dumps({
                "agent_run_id": "runBA",
                "allowed_output_paths": ["workspace/pipelines/x/src/foo.f90"],
                "allowed_file_tool_paths": ["workspace/pipelines/x/src/foo.f90"],
                "allowed_tmp_root": "workspace/tmp/runBA",
            }), encoding="utf-8")
            decision = validate_write_access(
                repo_root,
                "orchBA",
                "runBA",
                "workspace/pipelines/x/src/foo.f90",
                tool_name="Bash",
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "forbid_unauthorized_file_write")
        self.assertEqual((decision.audit_detail or {}).get("tool_name"), "Bash")

    def test_output_manifest_write_guard_fix_hint_is_literal_path(self) -> None:
        """The recovery hint surfaced for an unauthorized write must be a literal
        allowed_tmp_root path, not a shell command. Step 0 was eliminated precisely
        because `export TMPDIR=$(jq -er ...)` triggers Claude Code session sandbox
        approval prompts that stall the workflow indefinitely. The hook only checks
        whether the write target sits under allowed_tmp_root and ignores $TMPDIR env,
        so a literal path works without any shell variable setup.
        """
        from tools.hooks.common import validate_write_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root,
                orchestration_id="orchHS",
                agent_run_id="runHS",
                allowed_output_paths=["workspace/outputs/"],
                allowed_tmp_root="workspace/tmp/runHS",
            )
            decision = validate_write_access(
                repo_root,
                "orchHS",
                "runHS",
                "workspace/bad/out.json",
                tool_name="Write",
            )
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        fix_hint = (decision.audit_detail or {}).get("fix_hint") or {}
        write_under = fix_hint.get("write_under", "")
        self.assertTrue(write_under, "fix_hint.write_under must be present")
        self.assertIn("workspace/tmp/runHS", write_under)
        # The recovery hint must not be a runnable shell command — it must be a path.
        self.assertNotIn("export TMPDIR=", write_under)
        self.assertNotIn("jq -er", write_under)
        self.assertNotIn("python3 -", write_under)
        # next_command is intentionally absent: any Bash form recommended here would
        # itself need to clear session-sandbox approval, defeating the purpose.
        self.assertNotIn("next_command", fix_hint)

    def test_bash_redirect_to_tmpdir_is_allowed(self) -> None:
        """Bash redirect into allowed_tmp_root remains permitted (TMPDIR is the
        sanctioned scratch area for heredocs and patch staging)."""
        from tools.hooks.common import validate_write_access
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_manifest(
                repo_root,
                orchestration_id="orchT",
                agent_run_id="runT",
                allowed_output_paths=["workspace/pipelines/x/lineage.json"],
                allowed_tmp_root="workspace/tmp/runT",
            )
            decision = validate_write_access(
                repo_root,
                "orchT",
                "runT",
                "workspace/tmp/runT/scratch.patch",
                tool_name="Bash",
            )
        self.assertEqual(decision.action, HookDecisionAction.ALLOW)


class DevShmWriteBlockTests(unittest.TestCase):
    """C-4: cp/mv/rsync/install to /dev/shm is blocked in workflow mode."""

    def _call(self, command: str) -> HookDecision:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            return evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": command},
                    command=command,
                )
            )

    def test_blocks_cp_to_dev_shm(self) -> None:
        decision = self._call("cp workspace/outputs/result.json /dev/shm/result.json")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_dev_shm_block_fix_hint_uses_literal_path(self) -> None:
        """Regression: the /dev/shm fix_hint must recommend a literal allowed_tmp_root
        path (write_under) and NOT a shell command. Step 0 was eliminated precisely
        because `export TMPDIR=...` triggers Claude Code session sandbox approval.
        The /dev/shm branch in evaluate_common_policy does not know the actual
        agent_run_id, so the placeholder string is acceptable here.
        """
        decision = self._call("cp workspace/outputs/result.json /dev/shm/result.json")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        fix_hint = (decision.audit_detail or {}).get("fix_hint") or {}
        write_under = fix_hint.get("write_under", "")
        self.assertTrue(write_under, "fix_hint.write_under must be present")
        self.assertIn("workspace/tmp/", write_under)
        self.assertNotIn("export TMPDIR=", write_under)
        self.assertNotIn("jq -er", write_under)
        # Recovery hint must not be a runnable shell command.
        self.assertNotIn("next_command", fix_hint)
        docs_ref = fix_hint.get("docs_ref", "")
        self.assertIn("AGENT_CONTRACT.md", docs_ref)

    def test_blocks_mv_to_dev_shm(self) -> None:
        decision = self._call("mv /tmp/result.json /dev/shm/result.json")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_rsync_to_dev_shm(self) -> None:
        decision = self._call("rsync -av workspace/outputs/ /dev/shm/outputs/")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_install_to_dev_shm(self) -> None:
        decision = self._call("install -m 644 result.json /dev/shm/result.json")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_allows_cp_to_workspace(self) -> None:
        decision = self._call("cp workspace/outputs/a.json workspace/outputs/b.json")
        # cp to workspace should not be blocked by shm guard
        policy = (decision.audit_detail or {}).get("policy", "")
        self.assertNotEqual(policy, "output_manifest_write_guard")

    def test_blocks_install_t_dev_shm(self) -> None:
        # Regression: install -t /dev/shm src must be blocked (option-arg destination)
        decision = self._call("install -t /dev/shm src.bin")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_cp_target_directory_long_form(self) -> None:
        # Regression: cp --target-directory=/dev/shm must be blocked
        decision = self._call("cp --target-directory=/dev/shm src.json")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_cp_t_short_form(self) -> None:
        # Regression: cp -t /dev/shm src must be blocked
        decision = self._call("cp -t /dev/shm src1 src2")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_rsync_with_dev_shm_anywhere(self) -> None:
        # Regression: rsync with /dev/shm in any position must be blocked
        decision = self._call("rsync -av /dev/shm/data/ workspace/outputs/")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_after_shell_chain_and(self) -> None:
        # Regression: cd . && cp ... /dev/shm/x must NOT bypass guard
        decision = self._call("cd . && cp a /dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_after_shell_chain_semicolon(self) -> None:
        decision = self._call("true ; cp a /dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_with_env_wrapper(self) -> None:
        # Regression: env cp ... /dev/shm/x must NOT bypass guard
        decision = self._call("env cp a /dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_inside_bash_dash_c(self) -> None:
        # Regression: bash -c "cp a /dev/shm/x" must NOT bypass guard
        decision = self._call('bash -c "cp a /dev/shm/x"')
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_via_tee_redirect(self) -> None:
        decision = self._call("echo hi | tee /dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_allows_grep_for_dev_shm_string_in_log(self) -> None:
        """Regression: `grep '/dev/shm' file.log` is a legitimate diagnostic
        that does not access /dev/shm. The previous substring fallback
        over-blocked these, removing observability during fail_closed
        investigation."""
        decision = self._call(
            "grep '/dev/shm' workspace/orchestrations/o/hooks/native_hook_events.jsonl"
        )
        self.assertNotEqual(
            (decision.audit_detail or {}).get("policy"),
            "output_manifest_write_guard",
        )

    def test_allows_echo_dev_shm_literal(self) -> None:
        """Regression: `echo /dev/shm` does not access /dev/shm."""
        decision = self._call("echo /dev/shm")
        self.assertNotEqual(
            (decision.audit_detail or {}).get("policy"),
            "output_manifest_write_guard",
        )

    def test_allows_rg_for_dev_shm_pattern(self) -> None:
        """Regression: `rg '/dev/shm' docs/` is a diagnostic search."""
        decision = self._call("rg '/dev/shm' docs/RUNBOOK.md")
        self.assertNotEqual(
            (decision.audit_detail or {}).get("policy"),
            "output_manifest_write_guard",
        )

    def test_blocks_dev_shm_via_redirect_no_space(self) -> None:
        """Regression: shlex glues `>/path` together, so `echo hi >/dev/shm/x`
        produces the token `>/dev/shm/x`. The previous suffix check missed
        this form."""
        decision = self._call("echo hi >/dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_via_input_redirect(self) -> None:
        """Regression: `cat </dev/shm/x` → token `</dev/shm/x` reads /dev/shm."""
        decision = self._call("cat </dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_via_stderr_redirect(self) -> None:
        """Regression: `echo hi 2>/dev/shm/x` writes stderr to /dev/shm."""
        decision = self._call("echo hi 2>/dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_via_combined_redirect(self) -> None:
        """Regression: `echo hi &>/dev/shm/x` writes both stdout and stderr."""
        decision = self._call("echo hi &>/dev/shm/x")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_via_redirect_inside_bash_dash_c(self) -> None:
        """Regression: nested redirect inside bash -c "..."."""
        decision = self._call('bash -c "echo hi >/dev/shm/x"')
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_via_tar_chdir(self) -> None:
        """Regression: `tar -C /dev/shm -cf out.tar .` previously bypassed
        because tar wasn't in the path-access command list."""
        decision = self._call("tar -C /dev/shm -cf out.tar .")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")

    def test_blocks_dev_shm_via_find_traversal(self) -> None:
        """Regression: `find /dev/shm -type f` previously bypassed."""
        decision = self._call("find /dev/shm -type f")
        self.assertEqual(decision.action, HookDecisionAction.BLOCK)
        self.assertEqual((decision.audit_detail or {}).get("policy"), "output_manifest_write_guard")


class PipeTailInlinePythonAstTests(unittest.TestCase):
    """P0: pipe-tail `... | python3 -c '...'` exception is AST-allowlisted.

    The legitimate read-only stdin-parsing case is allowed; arbitrary code
    execution / file-write / sandbox-escape bodies are blocked (the prior
    substring blocklist was trivially defeated)."""

    def _call(self, command: str) -> HookDecision:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            return evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": command},
                    command=command,
                )
            )

    def _is_blocked(self, command: str) -> bool:
        return self._call(command).action == HookDecisionAction.BLOCK

    def test_allows_legitimate_stdin_json_parse(self) -> None:
        for cmd in (
            "cat x | python3 -c 'import sys,json; print(json.loads(sys.stdin.read()))'",
            "cat x | python3 -c 'import sys,re; print(re.findall(r\"x\", sys.stdin.read()))'",
            "cat x | python3 -c 'import sys; print(sys.stdin.read().strip())'",
            "echo {} | python3 -c 'import sys,json; print(json.load(sys.stdin).get(\"k\"))'",
            "cat x | /usr/bin/python3 -c 'import sys,json; json.loads(sys.stdin.read())'",
        ):
            self.assertFalse(self._is_blocked(cmd), msg=f"should ALLOW: {cmd}")

    def test_blocks_rce_via_import_system(self) -> None:
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c '__import__(\"os\").system(\"id\")'"))

    def test_blocks_exec_input(self) -> None:
        self.assertTrue(self._is_blocked("cat x | python3 -c 'exec(input())'"))

    def test_blocks_builtins_dict_open(self) -> None:
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c 'f=__builtins__.__dict__[\"open\"]; f(\"/tmp/p\",\"w\")'"))

    def test_blocks_sys_modules_escape(self) -> None:
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c 'import sys; sys.modules[\"os\"].system(\"id\")'"))

    def test_blocks_subclasses_traversal(self) -> None:
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c '().__class__.__bases__[0].__subclasses__()'"))

    def test_blocks_os_and_socket_imports(self) -> None:
        self.assertTrue(self._is_blocked("cat x | python3 -c 'import os; os.write(1,b\"x\")'"))
        self.assertTrue(self._is_blocked("cat x | python3 -c 'import socket'"))
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c 'import subprocess; subprocess.run([\"id\"])'"))

    def test_blocks_open_for_write(self) -> None:
        self.assertTrue(self._is_blocked("cat x | python3 -c 'open(\"/tmp/p\",\"w\")'"))

    def test_blocks_logical_or_not_pipe_tail(self) -> None:
        """`||` is NOT a pipe-tail; the body must not be granted the exception."""
        self.assertTrue(self._is_blocked(
            "cat x || python3 -c 'import sys,json; json.loads(sys.stdin.read())'"))

    def test_blocks_unparseable_body_fail_closed(self) -> None:
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c 'import sys; print(sys.stdin.read()"))  # unmatched quote

    def test_standalone_c_still_blocked(self) -> None:
        self.assertTrue(self._is_blocked(
            "python3 -c 'import sys,json; json.loads(sys.stdin.read())'"))

    def test_blocks_coexisting_python2_with_benign_pipe_tail(self) -> None:
        """A benign `python3 -c` pipe-tail must NOT whitelist a coexisting
        `python2 -c` (different interpreter version) running unguarded."""
        self.assertTrue(self._is_blocked(
            'cat x | python3 -c "import sys; print(sys.stdin.read())"'
            ' ; python2 -c "import os; os.system(0)"'))

    def test_standalone_python2_c_blocked(self) -> None:
        self.assertTrue(self._is_blocked("python2 -c 'import os; os.system(1)'"))

    def test_blocks_string_formatter_get_field_rce(self) -> None:
        """RCE: string.Formatter().get_field resolves a string-literal attribute
        path to a LIVE object — the AST walker never sees the dunder chain."""
        self.assertTrue(self._is_blocked(
            "cat /dev/null | python3 -c 'import string; "
            "f=string.Formatter(); o,_=f.get_field(\"0.__class__.__bases__\",[\"\"],{}); print(o)'"))

    def test_blocks_dunder_in_string_literal(self) -> None:
        """Attribute paths smuggled inside string literals are rejected."""
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c 'import sys; x=\"0.__class__\"; print(x, sys.stdin.read())'"))

    def test_blocks_operator_attrgetter(self) -> None:
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c 'from operator import attrgetter; attrgetter(\"__globals__\")(print)'"))

    def test_blocks_bare_string_module_import(self) -> None:
        """`string` is no longer an allowed import (Formatter is an RCE sink)."""
        self.assertTrue(self._is_blocked(
            "cat x | python3 -c 'import string; print(string.ascii_letters)'"))

    def test_blocks_module_reexport_attribute_chains(self) -> None:
        """RCE: allowed modules re-export builtins/os/operator as plain
        attributes (json.codecs.builtins.open, statistics.random._os.environ,
        re.enum.bltns) — deny-by-default attribute allowlist must block these."""
        for body in (
            'import json; json.codecs.builtins.open("/tmp/p","w").write("x")',
            "import statistics; print(statistics.random._os.environ)",
            "import re; print(re.enum.bltns.eval(\"6*7\"))",
            "import fractions; print(fractions.operator.add)",
            "import json; print(json.decoder.re.enum)",
        ):
            self.assertTrue(
                self._is_blocked(f"cat x | python3 -c '{body}'"),
                msg=f"re-export chain should block: {body}")

    def test_allows_richer_legit_parsers_under_allowlist(self) -> None:
        """The attribute allowlist must not break common stdin parsing."""
        for body in (
            "import sys; print(sys.stdin.read().strip().split())",
            'import sys,json; print(json.load(sys.stdin).get("k"))',
            "import sys,csv; [print(r) for r in csv.reader(sys.stdin)]",
            "import sys,base64; print(base64.b64decode(sys.stdin.read()))",
            'import sys; d={}; d.setdefault("a",[]).append(sys.stdin.read())',
        ):
            self.assertFalse(
                self._is_blocked(f"cat x | python3 -c '{body}'"),
                msg=f"legit parser should allow: {body}")

    def test_blocks_builtin_aliasing(self) -> None:
        """RCE: dangerous builtins aliased through a local Name (not called
        directly) must be rejected — `e=eval; e(stdin)`, `w=open`, `g=getattr`."""
        for body in (
            "import sys; e=eval; e(sys.stdin.read())",
            'w=open; w("/tmp/p","w")',
            "g=getattr; print(g(object, \"x\"))",
            'import functools; functools.reduce(eval, ["1"])',
            "m=map; list(m(eval, [\"1\"]))",
        ):
            self.assertTrue(
                self._is_blocked(f"cat x | python3 -c '{body}'"),
                msg=f"alias body should block: {body}")


class ForbidOperatorSecretReadTests(unittest.TestCase):
    """P1: ~/.met-dsl/ reads are blocked regardless of the read command."""

    def _call(self, command: str) -> HookDecision:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            return evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": command, "repo_root": os.getcwd()},
                    command=command,
                )
            )

    def _policy(self, command: str) -> str:
        return (self._call(command).audit_detail or {}).get("policy", "")

    def test_blocks_cat_head_tail(self) -> None:
        for c in ("cat", "head", "tail", "less"):
            self.assertEqual(
                self._policy(f"{c} ~/.met-dsl/operator_tokens/x.txt"),
                "forbid_operator_secret_direct_read", msg=c)

    def test_blocks_non_read_commands(self) -> None:
        """od/xxd/cut/read are not in the read-command set but must still block."""
        for c in (
            "od -c ~/.met-dsl/operator_tokens/x.txt",
            "xxd ~/.met-dsl/operator_tokens/x.txt",
            "cut -c1- ~/.met-dsl/operator_tokens/x.txt",
            "read X < ~/.met-dsl/operator_tokens/x.txt",
        ):
            self.assertEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)

    def test_blocks_command_substitution(self) -> None:
        self.assertEqual(
            self._policy("x=$(cat ~/.met-dsl/operator_tokens/x.txt)"),
            "forbid_operator_secret_direct_read")

    def test_blocks_glob_metacharacters(self) -> None:
        """Shell globs expand at runtime; the guard must fail-closed on them."""
        from pathlib import Path
        home = str(Path.home())
        for c in (
            "cat ~/.met-d*/operator_tokens/x.txt",
            "cat $HOME/.met-d*/operator_tokens/x.txt",
            f"cat {home}/.met-d*/operator_tokens/x.txt",
            "od ~/.m?t-dsl/operator_tokens/x.txt",
            "cat ~/.[m]et-dsl/operator_tokens/x.txt",
        ):
            self.assertEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)

    def test_blocks_brace_expansion(self) -> None:
        """Shell brace expansion `{a,b}` in the .met-dsl segment must fail-closed."""
        for c in (
            "cat ~/.met-{dsl,x}/operator_tokens/x.txt",
            "cat ~/.met-ds{l}/operator_tokens/x.txt",
            "cat ~/.{met-dsl,foo}/operator_tokens/x.txt",
            "cat $HOME/.met-{dsl,x}/operator_tokens/x.txt",
        ):
            self.assertEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)

    def test_blocks_brace_sequence_and_nested(self) -> None:
        """`{k..m}` sequence and nested braces both expand to .met-dsl in bash."""
        for c in (
            "cat ~/.met-ds{k..m}/operator_tokens/x.txt",
            "cat ~/.{met-{dsl,x},y}/operator_tokens/x.txt",
            "od ~/.met-ds{a..z}/operator_tokens/x.txt",
        ):
            self.assertEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)

    def test_blocks_brace_step_sequence(self) -> None:
        """bash 3-part step sequence `{lo..hi..incr}` also expands to .met-dsl."""
        for c in (
            "cat ~/.met-ds{k..m..1}/operator_tokens/x.txt",
            "od -c ~/.met-ds{j..p..2}/x",
            "cat ~/.met-ds{a..z..1}/x",
        ):
            self.assertEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)

    def test_multi_wildcard_glob_no_dos(self) -> None:
        """`~/*/*/*` patterns must not trigger an unbounded glob.glob walk of
        $HOME in this synchronous hook — the cheap lexical check fires first."""
        import time
        t0 = time.time()
        # `*` at the .met-dsl depth lexically targets the secret root → blocks,
        # but crucially must do so WITHOUT a multi-second filesystem walk.
        self.assertEqual(
            self._policy("echo ~/*/*/*/x"),
            "forbid_operator_secret_direct_read")
        self._policy("cat " + " ".join(["~/*/*/*/q"] * 40))
        self.assertLess(time.time() - t0, 2.0)

    def test_single_wildcard_glob_allowed_fast(self) -> None:
        """A single-wildcard glob not targeting the secret root is allowed and fast."""
        import time
        t0 = time.time()
        self.assertNotEqual(
            self._policy("ls ~/.config/*"),
            "forbid_operator_secret_direct_read")
        self.assertLess(time.time() - t0, 2.0)

    def test_giant_brace_sequence_no_dos(self) -> None:
        """A huge single `{0..N}` sequence must not allocate/hang the hook,
        and a met-dsl-targeting one must still block."""
        import time
        t0 = time.time()
        self.assertEqual(
            self._policy("cat ~/.met-ds{0..999999999}/operator_tokens/x.txt"),
            "forbid_operator_secret_direct_read")
        self._policy("cat ~/x{0..999999999}/y")  # non-secret, must also be fast
        self.assertLess(time.time() - t0, 2.0)

    def test_blocks_embedded_quote_backslash_fallback(self) -> None:
        """When shlex parse fails and evaluate_common_policy falls back to
        command.split(), embedded quote/backslash forms (`~/.met-d''sl`,
        `~/\\.met-dsl`) must still be caught by the collapse pass."""
        from pathlib import Path
        from tools.hooks.common import _command_reads_operator_secret
        repo = Path.cwd()
        root = (Path.home() / ".met-dsl").resolve()
        for cmd in (
            r"cat ~/\.met-dsl/operator_tokens/x.txt 'unbalanced",
            "cat ~/.met-d''sl/operator_tokens/x.txt 'unbalanced",
        ):
            self.assertTrue(
                _command_reads_operator_secret(cmd, cmd.split(), repo, root),
                msg=cmd)

    def test_brace_expansion_is_bounded_no_dos(self) -> None:
        """A crafted many-group brace token must not hang the hook."""
        import time
        c = "cat " + "{a,b}" * 25 + "x"
        t0 = time.time()
        self._policy(c)  # must return quickly
        self.assertLess(time.time() - t0, 2.0)

    def test_blocks_home_var_and_absolute_and_traversal(self) -> None:
        from pathlib import Path
        home = str(Path.home())
        for c in (
            "cat $HOME/.met-dsl/operator_tokens/x.txt",
            "cat ${HOME}/.met-dsl/operator_tokens/x.txt",
            f"cat {home}/.met-dsl/operator_tokens/x.txt",
            f"cat {home}/foo/../.met-dsl/operator_tokens/x.txt",
        ):
            self.assertEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)

    def test_allows_normal_reads(self) -> None:
        for c in (
            "cat docs/RUNBOOK.md",
            "cat workspace/orchestrations/o/meta.json",
            "echo met-dsl is fine in text",
        ):
            self.assertNotEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)

    def test_legit_dotfile_braces_not_overblocked(self) -> None:
        """Precise brace expansion must not over-block unrelated `~/.{a,b}` reads."""
        for c in (
            "cat ~/.{bashrc,profile}",
            "tar czf x ~/.{config,local}",
            "ls ~/.{cache,config}/app",
        ):
            self.assertNotEqual(
                self._policy(c), "forbid_operator_secret_direct_read", msg=c)


class ForbidDismissViolationTokenizationTests(unittest.TestCase):
    """P1: dismiss-violation block resists quote/backslash/var reassembly."""

    def _policy(self, command: str) -> str:
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
            d = evaluate_common_policy(
                HookInput(
                    event_name=HookEventName.PRE_COMMAND_EXECUTE,
                    backend="claude",
                    payload={"command": command, "repo_root": os.getcwd()},
                    command=command,
                )
            )
        return (d.audit_detail or {}).get("policy", "")

    def test_blocks_literal(self) -> None:
        self.assertEqual(
            self._policy("python3 tools/orchestration_runtime.py dismiss-violation --operator-token X"),
            "forbid_dismiss_violation_in_workflow")

    def test_blocks_quote_split(self) -> None:
        self.assertEqual(
            self._policy('python3 tools/orchestration_runtime.py dismiss-vio""lation --operator-token X'),
            "forbid_dismiss_violation_in_workflow")

    def test_blocks_backslash_split(self) -> None:
        self.assertEqual(
            self._policy(r"python3 tools/orchestration_runtime.py dismiss-vi\olation --operator-token X"),
            "forbid_dismiss_violation_in_workflow")

    def test_blocks_variable_indirection(self) -> None:
        self.assertEqual(
            self._policy("V=violation; python3 tools/orchestration_runtime.py dismiss-${V} --operator-token X"),
            "forbid_dismiss_violation_in_workflow")

    def test_blocks_pattern_substitution(self) -> None:
        """bash `${V//from/to}` replacement reaches argparse as dismiss-violation."""
        self.assertEqual(
            self._policy(
                "V=dismiss_violation; python3 tools/orchestration_runtime.py "
                "${V//_/-} --operator-token X"),
            "forbid_dismiss_violation_in_workflow")

    def test_blocks_command_substitution_literal(self) -> None:
        self.assertEqual(
            self._policy(
                "python3 tools/orchestration_runtime.py "
                "$(echo dismiss-violation) --operator-token X"),
            "forbid_dismiss_violation_in_workflow")

    def test_blocks_case_modification(self) -> None:
        """bash `${V,,}` / `${V^^}` case modification reassembly."""
        for c in (
            "V=DISMISS-VIOLATION; python3 tools/orchestration_runtime.py ${V,,} --operator-token X",
            "V=dismiss-violation; python3 tools/orchestration_runtime.py ${V^^} --operator-token X",
        ):
            self.assertEqual(
                self._policy(c), "forbid_dismiss_violation_in_workflow", msg=c)

    def test_allows_unrelated_command(self) -> None:
        self.assertNotEqual(
            self._policy("python3 tools/orchestration_runtime.py record-agent-run --foo bar"),
            "forbid_dismiss_violation_in_workflow")


if __name__ == "__main__":
    unittest.main()
