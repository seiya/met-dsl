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

    def test_claude_adapter_encode_decision_block_omits_continue_processing(self) -> None:
        adapter = ClaudeHookAdapter()
        code, stdout_text = adapter.encode_decision(
            HookDecision(action=HookDecisionAction.BLOCK, reason="denied")
        )
        self.assertEqual(code, 2)
        body = json.loads(stdout_text)
        self.assertEqual(body.get("decision"), "block")
        self.assertNotIn("continue_processing", body)


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


if __name__ == "__main__":
    unittest.main()
