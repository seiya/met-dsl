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
    def setUp(self) -> None:
        self._saved_hook_repo_root = os.environ.pop("METDSL_HOOK_REPO_ROOT", None)

    def tearDown(self) -> None:
        if self._saved_hook_repo_root is not None:
            os.environ["METDSL_HOOK_REPO_ROOT"] = self._saved_hook_repo_root

    @staticmethod
    def _assert_allow_output(raw_stdout: str) -> None:
        # Login shells may print noise (e.g. nvm) before the CLI output; only
        # consider lines that look like JSON objects.
        json_lines = [ln.strip() for ln in raw_stdout.splitlines() if ln.strip().startswith("{")]
        if not json_lines:
            return  # empty / non-JSON stdout → allow (CLI returns exit 0 with no output)
        body = json.loads(json_lines[-1])
        assert isinstance(body, dict)
        assert body.get("decision") == "allow"

    def test_subprocess_command_works_with_module_entrypoint(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_subprocess_001",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            env["METDSL_HOOK_REPO_ROOT"] = tmp
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
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_subprocess_002",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
            cmd = (
                "ROOT=$(git rev-parse --show-toplevel); "
                "PYTHONPATH=\"$ROOT${PYTHONPATH:+:$PYTHONPATH}\" "
                f"METDSL_HOOK_REPO_ROOT=\"{tmp}\" "
                f"python3 -m tools.hooks.cli --backend codex --event PreToolUse --repo-root \"{tmp}\""
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
        with tempfile.TemporaryDirectory() as tmp:
            command = (
                hooks_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                .replace(
                    'METDSL_HOOK_REPO_ROOT="$ROOT"',
                    f'METDSL_HOOK_REPO_ROOT="{tmp}"',
                )
                .replace('--repo-root "$ROOT"', f'--repo-root "{tmp}"')
            )
            payload = {
                "orchestration_id": "orch_subprocess_003",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
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
            env["METDSL_REQUIRE_CODEX_HOOKS_FEATURE"] = "0"
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
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                payload = {
                    "orchestration_id": "orch_block_001",
                    "repo_root": str(repo_root),
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
                self.assertEqual(out.getvalue().strip(), "")

    def test_dev_mode_blocks_verify_bypass_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                payload = {
                    "orchestration_id": "orch_dev_policy_001",
                    "repo_root": str(repo_root),
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
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                payload = {
                    "orchestration_id": "orch_default_dev_policy_001",
                    "repo_root": str(repo_root),
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
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            with patch("tools.hooks.cli.codex_hooks_feature_enabled") as feature_mock:
                feature_mock.return_value = (True, "codex_hooks=true")
                payload = {
                    "orchestration_id": "orch_prod_policy_001",
                    "repo_root": str(repo_root),
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
                self.assertEqual(out.getvalue().strip(), "")

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
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
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
                self.assertFalse(log_path.exists())

    def test_missing_orchestration_id_falls_back_to_global_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
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
                self.assertFalse(log_path.exists())

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
            with patch.dict(os.environ, {"METDSL_HOOK_FEATURE_RETRY_TTL_SECONDS": "0"}):
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
            with patch.dict(os.environ, {"METDSL_HOOK_FEATURE_RETRY_TTL_SECONDS": "abc"}):
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
    def setUp(self) -> None:
        self._saved_hook_repo_root = os.environ.pop("METDSL_HOOK_REPO_ROOT", None)

    def tearDown(self) -> None:
        if self._saved_hook_repo_root is not None:
            os.environ["METDSL_HOOK_REPO_ROOT"] = self._saved_hook_repo_root

    def test_claude_backend_allows_safe_command(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_claude_allow_001",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hello"},
            }
            env = os.environ.copy()
            env["METDSL_HOOK_REPO_ROOT"] = tmp
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
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_claude_backend_blocks_git_reset_hard(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "orchestration_id": "orch_claude_block_001",
                "repo_root": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard HEAD~1"},
            }
            env = os.environ.copy()
            env["METDSL_HOOK_REPO_ROOT"] = tmp
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
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            body = json.loads(proc.stdout.strip())
            self.assertEqual(body.get("decision"), "block")

    def test_detect_bash_write_targets_collects_all_tee_output_paths(self) -> None:
        targets = cli._detect_bash_write_targets("echo test | tee file1.txt file2.txt")
        self.assertIn("file1.txt", targets)
        self.assertIn("file2.txt", targets)

    def test_detect_bash_write_targets_detects_sed_inplace_without_space_after_i(self) -> None:
        targets = cli._detect_bash_write_targets("sed -i's/a/b/' file.txt")
        self.assertIn("file.txt", targets)

    def test_detect_bash_write_targets_detects_sed_inplace_when_i_comes_after_script(self) -> None:
        targets = cli._detect_bash_write_targets("sed -e 's/a/b/' -i file.txt")
        self.assertIn("file.txt", targets)

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

    def test_claude_backend_falls_back_to_global_without_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
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
            self.assertFalse(log_path.exists())

    def test_claude_global_audit_uses_metdsl_hook_repo_root_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch.dict(os.environ, {"METDSL_HOOK_REPO_ROOT": str(repo_root_path)}):
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
            self.assertFalse(log_path.exists())

    def test_claude_backend_settings_json_command_works(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        settings_doc = json.loads(
            (repo_root / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            command = (
                settings_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                .replace(
                    'METDSL_HOOK_REPO_ROOT="$ROOT"',
                    f'METDSL_HOOK_REPO_ROOT="{tmp}"',
                )
                .replace('--repo-root "$ROOT"', f'--repo-root "{tmp}"')
            )
            payload = {
                "orchestration_id": "orch_claude_settings_001",
                "repo_root": tmp,
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
            # Strip shell-profile noise (e.g. nvm banner from `sh -lc`) before asserting.
            hook_lines = [l for l in proc.stdout.splitlines() if l.strip() not in {"nvm", ""}]
            self.assertEqual(hook_lines, [])

    def test_resolve_repo_root_uses_metdsl_hook_repo_root_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"METDSL_HOOK_REPO_ROOT": tmp}):
                for backend in ("claude", "codex"):
                    result = cli._resolve_repo_root({}, backend=backend)
                    self.assertEqual(result, Path(tmp).resolve())

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
            self.assertFalse(log_path.exists())

    def test_strict_policy_allows_missing_orchestration_id_as_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root_path = Path(tmp)
            payload = {
                "repo_root": str(repo_root_path),
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
            with patch.dict(
                os.environ,
                {"METDSL_MISSING_ORCHESTRATION_ID_POLICY": "strict"},
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
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), "")

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
                    "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0",
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
                    "command": "python3 tools/orchestration_runtime.py run-gate --gate orchestration_read"
                },
            }
            with patch.dict(
                os.environ,
                {
                    "METDSL_WORKFLOW_MODE": "0",
                    "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0",
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
            self.assertFalse(log_path.exists())

    def test_claude_file_tool_blocks_write_outside_manifest_when_active_child_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_001"
            run_id = "step_run_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps({
                    "allowed_output_paths": ["workspace/pipelines/safe/out.txt"],
                    "allowed_file_tool_paths": ["workspace/pipelines/safe/out.txt"],
                }),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "tool_input": {"file_path": "workspace/forbidden.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
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
            self.assertIn("unauthorized write", body.get("reason", ""))
            self.assertIn("guarded-apply-patch", body.get("reason", ""))

    def test_codex_file_tool_resolves_session_to_agent_run_and_allows_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_002"
            run_id = "step_run_build_001"
            session_id = "sess_step_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                json.dumps({
                    "allowed_output_paths": ["workspace/pipelines/safe/out.txt"],
                    "allowed_file_tool_paths": ["workspace/pipelines/safe/out.txt"],
                }),
                encoding="utf-8",
            )
            (orch_root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {
                        "agent_run_id": run_id,
                        "agent_backend": "codex",
                        "agent_session_id": session_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "session_id": session_id,
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
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

    def test_codex_file_tool_blocks_when_session_mapping_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_003"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Read",
                "session_id": "sess_unknown_001",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
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
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))
            self.assertIn("orchestration_read", body.get("reason", ""))

    def test_codex_write_tool_blocks_with_write_hint_when_session_mapping_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_004"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "session_id": "sess_unknown_002",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
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
            self.assertIn("session-to-run mapping not found", body.get("reason", ""))
            self.assertIn("guarded-apply-patch", body.get("reason", ""))
            self.assertNotIn("orchestration_read", body.get("reason", ""))

    def test_codex_raw_apply_patch_blocks_in_workflow_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_apply_patch_guard_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "apply_patch",
                "tool_input": {"patch": "*** Begin Patch\n*** End Patch\n"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
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
            self.assertIn("raw apply_patch tool is forbidden", body.get("reason", ""))
            self.assertIn("guarded-apply-patch", body.get("reason", ""))

    def test_claude_file_tool_blocks_when_active_agent_run_id_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_005"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_agent_run_id": "orch_agent_001"}, ensure_ascii=False),
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
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
            self.assertIn("orchestration agent must not use Write/Edit tools directly", body.get("reason", ""))
            self.assertIn("guarded-apply-patch", body.get("reason", ""))

    def test_claude_file_tool_blocks_when_active_agent_run_id_file_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_006"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text("   \n", encoding="utf-8")
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Read",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
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
            self.assertIn("active child agent_run_id is empty", body.get("reason", ""))
            self.assertIn("orchestration_read", body.get("reason", ""))

    def test_claude_write_blocks_with_manifest_hint_when_output_manifest_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_007"
            run_id = "step_run_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
            (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
            (orch_root / "output_manifests" / f"{run_id}.json").write_text(
                "{invalid-json",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Write",
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
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
            self.assertIn("invalid JSON", body.get("reason", ""))
            self.assertIn("Ensure record-launch generated the manifest", body.get("reason", ""))

    def test_codex_read_blocks_with_manifest_hint_when_read_manifest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_file_guard_008"
            run_id = "step_run_build_001"
            session_id = "sess_step_build_001"
            orch_root = repo_root / "workspace" / "orchestrations" / orch
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {
                        "agent_run_id": run_id,
                        "agent_backend": "codex",
                        "agent_session_id": session_id,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "orchestration_id": orch,
                "repo_root": str(repo_root),
                "tool_name": "Read",
                "session_id": session_id,
                "tool_input": {"file_path": "workspace/pipelines/safe/out.txt"},
            }
            out = io.StringIO()
            with patch.dict(
                os.environ,
                {"METDSL_WORKFLOW_MODE": "1", "METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"},
                clear=False,
            ):
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
            self.assertIn("read manifest not found", body.get("reason", ""))
            self.assertIn("Ensure record-launch generated the manifest", body.get("reason", ""))


class WriteToolExtensionPolicyTests(unittest.TestCase):
    """`Edit` / `Write` 直接書き込みの extension 別 policy 検証。"""

    def _setup_orchestration_for_write(
        self,
        repo_root: Path,
        *,
        orch: str,
        run_id: str,
        allowed_output_paths: list[str],
        allowed_file_tool_paths: list[str],
    ) -> None:
        orch_root = repo_root / "workspace" / "orchestrations" / orch
        (orch_root / "output_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "read_manifests").mkdir(parents=True, exist_ok=True)
        (orch_root / "active_child_agent_run_id.txt").write_text(run_id, encoding="utf-8")
        (orch_root / "output_manifests" / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "orchestration_id": orch,
                    "agent_run_id": run_id,
                    "allowed_output_paths": allowed_output_paths,
                    "allowed_file_tool_paths": allowed_file_tool_paths,
                    "write_roots": ["workspace/plans"],
                }
            ),
            encoding="utf-8",
        )

    def _invoke_write_hook(self, *, orch: str, repo_root: Path, file_path: str) -> tuple[int, dict]:
        payload = {
            "orchestration_id": orch,
            "repo_root": str(repo_root),
            "tool_name": "Write",
            "tool_input": {"file_path": file_path},
        }
        out = io.StringIO()
        with patch.dict(os.environ, {"METDSL_WORKFLOW_MODE": "1"}, clear=False):
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
        body_text = out.getvalue().strip()
        body: dict = json.loads(body_text) if body_text else {}
        return code, body

    def test_write_tool_blocks_json_path_even_when_listed_in_allowed_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_001"
            run_id = "step_run_ext_hooks_001"
            json_path = "workspace/plans/p/derived_contract.json"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[json_path],
                allowed_file_tool_paths=[],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=json_path
            )
            self.assertEqual(code, 2)
            self.assertEqual(body.get("decision"), "block")
            self.assertIn("guarded-apply-patch", body.get("reason", ""))

    def test_write_tool_allows_yaml_when_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_002"
            run_id = "step_run_ext_hooks_002"
            yaml_path = "workspace/plans/p/case.resolved.yaml"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[yaml_path],
                allowed_file_tool_paths=[yaml_path],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=yaml_path
            )
            self.assertEqual(code, 0)
            if body:
                self.assertEqual(body.get("decision"), "allow")

    def test_write_tool_allows_markdown_when_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_003"
            run_id = "step_run_ext_hooks_003"
            md_path = "workspace/plans/p/algorithm.summary.md"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[md_path],
                allowed_file_tool_paths=[md_path],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=md_path
            )
            self.assertEqual(code, 0)
            if body:
                self.assertEqual(body.get("decision"), "allow")

    def test_write_tool_allows_source_code_when_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_004"
            run_id = "step_run_ext_hooks_004"
            src_path = "workspace/plans/p/src/main.f90"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[src_path],
                allowed_file_tool_paths=[src_path],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=src_path
            )
            self.assertEqual(code, 0)
            if body:
                self.assertEqual(body.get("decision"), "allow")

    def test_write_tool_blocks_yaml_when_not_listed_in_allowed_file_tool_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_005"
            run_id = "step_run_ext_hooks_005"
            yaml_path = "workspace/plans/p/case.resolved.yaml"
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[yaml_path],
                allowed_file_tool_paths=[],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=yaml_path
            )
            self.assertEqual(code, 2)
            self.assertEqual(body.get("decision"), "block")

    def test_write_tool_blocks_cli_managed_internal_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch = "orch_ext_hooks_006"
            run_id = "step_run_ext_hooks_006"
            cli_managed_path = (
                f"workspace/orchestrations/{orch}/launches/{run_id}.reply.txt"
            )
            self._setup_orchestration_for_write(
                repo_root,
                orch=orch,
                run_id=run_id,
                allowed_output_paths=[cli_managed_path],
                allowed_file_tool_paths=[cli_managed_path],
            )
            code, body = self._invoke_write_hook(
                orch=orch, repo_root=repo_root, file_path=cli_managed_path
            )
            self.assertEqual(code, 2)
            self.assertEqual(body.get("decision"), "block")


if __name__ == "__main__":
    unittest.main()
