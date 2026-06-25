#!/usr/bin/env python3
"""Tests for codex feature probes used by hook entrypoint."""

from __future__ import annotations

import subprocess
import unittest

from tools.hooks.codex_feature import codex_hooks_feature_enabled, parse_feature_list


class _FakeCompletedProcess:
    def __init__(self, returncode: int, *, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class HookCodexFeatureTests(unittest.TestCase):
    def test_parse_feature_list_extracts_flags(self) -> None:
        parsed = parse_feature_list(
            "multi_agent stable true\nhooks under-development false\n"
        )
        self.assertEqual(parsed.get("multi_agent"), True)
        self.assertEqual(parsed.get("hooks"), False)

    def test_codex_hooks_feature_enabled_true(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent stable true\nhooks under-development true\n",
                )
            raise AssertionError(args)

        enabled, detail = codex_hooks_feature_enabled(runner=runner)
        self.assertTrue(enabled)
        self.assertEqual(detail, "hooks=true")

    def test_codex_hooks_feature_enabled_false_when_missing(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent stable true\n",
                )
            raise AssertionError(args)

        enabled, detail = codex_hooks_feature_enabled(runner=runner)
        self.assertFalse(enabled)
        self.assertIn("hooks=None", detail)

    def test_codex_hooks_feature_enabled_false_on_command_failure(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(1, stderr="unknown command")
            raise AssertionError(args)

        enabled, detail = codex_hooks_feature_enabled(runner=runner)
        self.assertFalse(enabled)
        self.assertIn("codex features list failed", detail)

    def test_codex_hooks_feature_enabled_false_on_timeout(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

        enabled, detail = codex_hooks_feature_enabled(runner=runner, timeout_seconds=0.2)
        self.assertFalse(enabled)
        self.assertIn("timed out", detail)

    def test_exec_error_serialized_as_probe_failure(self) -> None:
        # A command that cannot be executed (missing binary / bad wrapper) must serialize
        # as a probe failure (enabled=False, retryable detail), not raise OSError — so the
        # conductor writes a disabled cache and fail-closes instead of crashing.
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("no such file or directory: codexwrap")

        enabled, detail = codex_hooks_feature_enabled(command=["codexwrap"], runner=runner)
        self.assertFalse(enabled)
        self.assertTrue(detail.startswith("codex features list failed:"))

    def test_command_prefix_list_is_invoked_verbatim(self) -> None:
        # A custom --llm-command wrapper (list prefix) is run verbatim before
        # `features list`, so the probe hits the same executable the leaf will run.
        seen: dict[str, list[str]] = {}

        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            seen["argv"] = list(args)
            return _FakeCompletedProcess(0, stdout="hooks stable true\n")

        enabled, _ = codex_hooks_feature_enabled(
            command=["codexwrap", "--profile", "x"], runner=runner)
        self.assertTrue(enabled)
        self.assertEqual(seen["argv"], ["codexwrap", "--profile", "x", "features", "list"])


class CodexFeatureCachePathTests(unittest.TestCase):
    def test_clean_orchestration_id_builds_orch_root_path(self) -> None:
        from pathlib import Path
        from tools.hooks.codex_feature import codex_feature_cache_path
        p = codex_feature_cache_path(repo_root=Path("/repo"), orchestration_id="orch_abc")
        self.assertEqual(
            p, Path("/repo/workspace/orchestrations/orch_abc/codex_feature_check.json"))
        # the cache must NOT sit under the leaf-writable hooks/ dir
        self.assertNotIn("/hooks/", str(p))

    def test_unsafe_orchestration_id_rejected(self) -> None:
        from pathlib import Path
        from tools.hooks.codex_feature import codex_feature_cache_path
        # traversal / separators that could redirect the RO read into a writable dir
        for bad in ("orch/hooks", "../orch", "..", ".", "", "a\\b", "x\x00y"):
            with self.assertRaises(ValueError):
                codex_feature_cache_path(repo_root=Path("/repo"), orchestration_id=bad)


if __name__ == "__main__":
    unittest.main()
