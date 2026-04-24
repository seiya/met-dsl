#!/usr/bin/env python3
"""Tests for unified hook CLI entrypoint."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.hooks import cli


class HookCliTests(unittest.TestCase):
    @staticmethod
    def _assert_allow_output(raw_stdout: str) -> None:
        token = raw_stdout.strip()
        if not token:
            return
        body = json.loads(token)
        assert isinstance(body, dict)
        assert body.get("decision") == "allow"

    def test_subprocess_command_works_with_module_entrypoint(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        payload = {
            "orchestration_id": "orch_subprocess_001",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        }
        env = os.environ.copy()
        env["CODEX_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
        proc = subprocess.run(
            [
                "python3",
                "-m",
                "tools.hooks.cli",
                "--backend",
                "codex",
                "--event",
                "PreToolUse",
                "--input-json",
                json.dumps(payload),
            ],
            cwd=str(repo_root),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self._assert_allow_output(proc.stdout)

    def test_subprocess_command_works_from_subdirectory(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        subdir = repo_root / "tools"
        payload = {
            "orchestration_id": "orch_subprocess_002",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        }
        env = os.environ.copy()
        env["CODEX_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
        cmd = (
            "ROOT=$(git rev-parse --show-toplevel); "
            "PYTHONPATH=\"$ROOT${PYTHONPATH:+:$PYTHONPATH}\" "
            "CODEX_HOOK_REPO_ROOT=\"$ROOT\" "
            "python3 -m tools.hooks.cli --backend codex --event PreToolUse --repo-root \"$ROOT\""
        )
        proc = subprocess.run(
            ["sh", "-lc", cmd],
            cwd=str(subdir),
            env=env,
            text=True,
            capture_output=True,
            input=json.dumps(payload),
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self._assert_allow_output(proc.stdout)

    def test_hooks_json_command_works_from_subdirectory(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        hooks_doc = json.loads((repo_root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        command = hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        payload = {
            "orchestration_id": "orch_subprocess_003",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        }
        env = os.environ.copy()
        env["CODEX_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
        proc = subprocess.run(
            command,
            cwd=str(repo_root / "tools"),
            env=env,
            text=True,
            capture_output=True,
            input=json.dumps(payload),
            shell=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self._assert_allow_output(proc.stdout)

    def test_hooks_json_command_fail_fast_when_not_in_git_repo(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        hooks_doc = json.loads((repo_root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        command = hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_subprocess_004",
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["CODEX_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            proc = subprocess.run(
                command,
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                input=json.dumps(payload),
                shell=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)

    def test_blocks_when_codex_hooks_feature_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.side_effect = lambda: (False, "codex_hooks=false")
                payload = {
                    "orchestration_id": "orch_disabled_002",
                    "repo_root": str(repo_root),
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 2)
                payload = json.loads(out.getvalue().strip())
                self.assertEqual(payload.get("decision"), "block")
                self.assertIn("codex_hooks", payload.get("reason", ""))

    def test_feature_disabled_path_writes_audit_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (False, "codex_hooks=false")
                payload = {
                    "orchestration_id": "orch_disabled_001",
                    "repo_root": str(repo_root),
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 2)
                log_path = (
                    repo_root
                    / "workspace"
                    / "orchestrations"
                    / "orch_disabled_001"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertTrue(log_path.is_file())

    def test_exception_path_writes_audit_log_when_payload_has_orchestration_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            payload = {
                "orchestration_id": "orch_exception_001",
                "repo_root": str(repo_root),
                "event_name": "NotAnEvent",
            }
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 2)
            log_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_exception_001"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertTrue(log_path.is_file())

    def test_blocks_dangerous_command(self) -> None:
        with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
            feature_mock.return_value = (True, "codex_hooks=true")
            payload = {
                "orchestration_id": "orch_block_001",
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard HEAD~1"},
            }
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")

    def test_allows_non_dangerous_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                payload = {
                    "orchestration_id": "orch_allow_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                self._assert_allow_output(out.getvalue())

    def test_dev_mode_blocks_verify_bypass_flags(self) -> None:
        with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
            feature_mock.return_value = (True, "codex_hooks=true")
            payload = {
                "orchestration_id": "orch_dev_policy_001",
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                        "--allow-missing-orchestration"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_EXEC_MODE": "dev"}):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("dev mode forbids verify bypass flags", body.get("reason", ""))

    def test_unset_workflow_mode_defaults_to_dev_and_blocks_verify_bypass_flags(self) -> None:
        with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
            feature_mock.return_value = (True, "codex_hooks=true")
            payload = {
                "orchestration_id": "orch_default_dev_policy_001",
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                        "--allow-missing-orchestration"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(os.environ, {}, clear=True):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 2)
            body = json.loads(out.getvalue().strip())
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("dev mode forbids verify bypass flags", body.get("reason", ""))

    def test_prod_mode_allows_same_command(self) -> None:
        with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
            feature_mock.return_value = (True, "codex_hooks=true")
            payload = {
                "orchestration_id": "orch_prod_policy_001",
                "tool_name": "Bash",
                "tool_input": {
                    "command": (
                        "python3 tools/validate_pipeline_semantics.py --stage pre_judge "
                        "--allow-missing-orchestration"
                    )
                },
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_EXEC_MODE": "prod"}):
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
            self.assertEqual(code, 0)
            self._assert_allow_output(out.getvalue())

    def test_writes_native_hook_audit_log_when_orchestration_id_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                payload = {
                    "orchestration_id": "orch_test_001",
                    "repo_root": str(repo_root),
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo hello"},
                }
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                log_path = (
                    repo_root
                    / "workspace"
                    / "orchestrations"
                    / "orch_test_001"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertTrue(log_path.is_file())
                entry = json.loads(log_path.read_text(encoding="utf-8").strip())
                self.assertEqual(entry.get("backend"), "codex")
                self.assertEqual(entry.get("event"), "pre_command_execute")

    def test_missing_orchestration_id_uses_global_policy_by_default(self) -> None:
        with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
            feature_mock.return_value = (True, "codex_hooks=true")
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            self._assert_allow_output(out.getvalue())

    def test_session_start_without_orchestration_id_uses_global_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                payload = {"repo_root": str(repo_root)}
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "SessionStart",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                log_path = (
                    repo_root
                    / "workspace"
                    / "orchestrations"
                    / "_global"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertTrue(log_path.is_file())

    def test_missing_orchestration_id_can_use_global_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch.dict(os.environ, {"CODEX_HOOK_MISSING_ORCHESTRATION_ID_POLICY": "global"}):
                with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                    feature_mock.return_value = (True, "codex_hooks=true")
                    payload = {
                        "repo_root": str(repo_root),
                        "tool_name": "Bash",
                        "tool_input": {"command": "echo hello"},
                    }
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = cli.main(
                            [
                                "--backend",
                                "codex",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                    self.assertEqual(code, 0)
                    log_path = (
                        repo_root
                        / "workspace"
                        / "orchestrations"
                        / "_global"
                        / "hooks"
                        / "native_hook_events.jsonl"
                    )
                    self.assertTrue(log_path.is_file())

    def test_codex_feature_check_is_cached_after_first_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            payload = {
                "orchestration_id": "orch_cache_001",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                out1 = io.StringIO()
                with redirect_stdout(out1):
                    code1 = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                out2 = io.StringIO()
                with redirect_stdout(out2):
                    code2 = cli.main(
                        [
                            "--backend",
                            "codex",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code1, 0)
                self.assertEqual(code2, 0)
                self.assertEqual(feature_mock.call_count, 1)

    def test_probe_error_cache_is_retried_after_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            payload = {
                "orchestration_id": "orch_retry_001",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            with patch.dict(os.environ, {"CODEX_HOOK_FEATURE_RETRY_TTL_SECONDS": "0"}):
                with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                    feature_mock.side_effect = [
                        (False, "codex features list timed out after 2.0s"),
                        (True, "codex_hooks=true"),
                    ]
                    out1 = io.StringIO()
                    with redirect_stdout(out1):
                        code1 = cli.main(
                            [
                                "--backend",
                                "codex",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                    out2 = io.StringIO()
                    with redirect_stdout(out2):
                        code2 = cli.main(
                            [
                                "--backend",
                                "codex",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                    self.assertEqual(code1, 2)
                    self.assertEqual(code2, 0)
                    self.assertEqual(feature_mock.call_count, 2)

    def test_invalid_retry_ttl_env_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            payload = {
                "orchestration_id": "orch_retry_invalid_ttl_001",
                "repo_root": str(repo_root),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            with patch.dict(os.environ, {"CODEX_HOOK_FEATURE_RETRY_TTL_SECONDS": "abc"}):
                with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                    feature_mock.return_value = (False, "codex features list timed out after 2.0s")
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = cli.main(
                            [
                                "--backend",
                                "codex",
                                "--event",
                                "PreToolUse",
                                "--input-json",
                                json.dumps(payload),
                            ]
                        )
                    self.assertEqual(code, 2)


class ClaudeHookCliTests(unittest.TestCase):
    def test_claude_backend_allows_safe_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        payload = {
            "orchestration_id": "orch_claude_allow_001",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        }
        proc = subprocess.run(
            [
                "python3",
                "-m",
                "tools.hooks.cli",
                "--backend",
                "claude",
                "--event",
                "PreToolUse",
                "--input-json",
                json.dumps(payload),
            ],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_claude_backend_blocks_git_reset_hard(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        payload = {
            "orchestration_id": "orch_claude_block_001",
            "tool_name": "Bash",
            "tool_input": {"command": "git reset --hard HEAD~1"},
        }
        proc = subprocess.run(
            [
                "python3",
                "-m",
                "tools.hooks.cli",
                "--backend",
                "claude",
                "--event",
                "PreToolUse",
                "--input-json",
                json.dumps(payload),
            ],
            cwd=str(repo_root),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        body = json.loads(proc.stdout.strip())
        self.assertEqual(body.get("decision"), "block")

    def test_claude_backend_does_not_require_codex_hooks_feature(self) -> None:
        """Claude backend must not invoke the Codex feature probe at all."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "orchestration_id": "orch_claude_noprobe_001",
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as probe_mock:
                probe_mock.side_effect = AssertionError("codex probe must not be called for claude")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), "")

    def test_claude_backend_uses_global_policy_for_missing_orchestration_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch.dict(os.environ, {"CODEX_HOOK_MISSING_ORCHESTRATION_ID_POLICY": "global"}):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 0)
                log_path = (
                    repo_root_path
                    / "workspace"
                    / "orchestrations"
                    / "_global"
                    / "hooks"
                    / "native_hook_events.jsonl"
                )
                self.assertTrue(log_path.is_file())
                entry = json.loads(log_path.read_text(encoding="utf-8").strip())
                self.assertEqual(entry.get("backend"), "claude")

    def test_claude_backend_settings_json_command_works(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        settings_doc = json.loads(
            (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        command = settings_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        payload = {
            "orchestration_id": "orch_claude_settings_001",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        }
        proc = subprocess.run(
            command,
            cwd=str(repo_root / "tools"),
            text=True,
            capture_output=True,
            input=json.dumps(payload),
            shell=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_resolve_repo_root_uses_claude_env_for_claude_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Include CODEX_HOOK_REPO_ROOT in patch.dict so it is restored on exit,
            # then pop it so only CLAUDE_HOOK_REPO_ROOT is active.
            with patch.dict(os.environ, {"CLAUDE_HOOK_REPO_ROOT": tmp, "CODEX_HOOK_REPO_ROOT": ""}):
                os.environ.pop("CODEX_HOOK_REPO_ROOT", None)
                result = cli._resolve_repo_root({}, backend="claude")
                self.assertEqual(result, Path(tmp).resolve())

    def test_resolve_repo_root_uses_codex_env_for_codex_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Include CLAUDE_HOOK_REPO_ROOT in patch.dict so it is restored on exit,
            # then pop it so only CODEX_HOOK_REPO_ROOT is active.
            with patch.dict(os.environ, {"CODEX_HOOK_REPO_ROOT": tmp, "CLAUDE_HOOK_REPO_ROOT": ""}):
                os.environ.pop("CLAUDE_HOOK_REPO_ROOT", None)
                result = cli._resolve_repo_root({}, backend="codex")
                self.assertEqual(result, Path(tmp).resolve())

    def test_resolve_repo_root_claude_backend_ignores_codex_env(self) -> None:
        """Claude backend must not pick up CODEX_HOOK_REPO_ROOT."""
        with tempfile.TemporaryDirectory() as codex_tmp:
            with tempfile.TemporaryDirectory() as claude_tmp:
                with patch.dict(
                    os.environ,
                    {"CODEX_HOOK_REPO_ROOT": codex_tmp, "CLAUDE_HOOK_REPO_ROOT": claude_tmp},
                ):
                    result = cli._resolve_repo_root({}, backend="claude")
                    self.assertEqual(result, Path(claude_tmp).resolve())
                    self.assertNotEqual(result, Path(codex_tmp).resolve())

    def test_claude_backend_user_prompt_submit_uses_global_without_orchestration_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {"repo_root": str(repo_root_path), "prompt": "do something"}
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(
                    [
                        "--backend",
                        "claude",
                        "--event",
                        "UserPromptSubmit",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "_global"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertTrue(log_path.is_file())
            entry = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(entry.get("event"), "user_prompt_submit")

    def test_claude_backend_blocks_missing_orchestration_id_in_workflow_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch.dict(
                os.environ,
                {
                    "METDSL_WORKFLOW_MODE": "1",
                    "CODEX_HOOK_MISSING_ORCHESTRATION_ID_POLICY": "global",
                },
            ):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(
                        [
                            "--backend",
                            "claude",
                            "--event",
                            "PreToolUse",
                            "--input-json",
                            json.dumps(payload),
                        ]
                    )
                self.assertEqual(code, 2)
                body = json.loads(out.getvalue().strip())
                self.assertEqual(body.get("decision"), "block")
                self.assertIn("orchestration_id is required for workflow hook execution", body.get("reason", ""))

    def test_workflow_mode_accepts_orchestration_id_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch.dict(
                os.environ,
                {
                    "METDSL_WORKFLOW_MODE": "1",
                    "METDSL_ORCHESTRATION_ID": "orch_env_001",
                },
            ):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "orch_env_001"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertTrue(log_path.is_file())

    def test_missing_orchestration_id_allowed_when_workflow_mode_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {
                    "command": "python3 tools/codex_orchestration_runtime.py run-gate --gate orchestration_read"
                },
            }
            with patch.dict(
                os.environ,
                {
                    "METDSL_WORKFLOW_MODE": "0",
                    "CODEX_HOOK_MISSING_ORCHESTRATION_ID_POLICY": "global",
                },
            ):
                code = cli.main(
                    [
                        "--backend",
                        "codex",
                        "--event",
                        "PreToolUse",
                        "--input-json",
                        json.dumps(payload),
                    ]
                )
            self.assertEqual(code, 0)
            log_path = (
                repo_root_path
                / "workspace"
                / "orchestrations"
                / "_global"
                / "hooks"
                / "native_hook_events.jsonl"
            )
            self.assertTrue(log_path.is_file())


if __name__ == "__main__":
    unittest.main()
