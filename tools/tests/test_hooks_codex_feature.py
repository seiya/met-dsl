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
            "multi_agent stable true\ncodex_hooks under-development false\n"
        )
        self.assertEqual(parsed.get("multi_agent"), True)
        self.assertEqual(parsed.get("codex_hooks"), False)

    def test_codex_hooks_feature_enabled_true(self) -> None:
        def runner(args, **kwargs):  # type: ignore[no-untyped-def]
            if args[1:] == ["features", "list"]:
                return _FakeCompletedProcess(
                    0,
                    stdout="multi_agent stable true\ncodex_hooks under-development true\n",
                )
            raise AssertionError(args)

        enabled, detail = codex_hooks_feature_enabled(runner=runner)
        self.assertTrue(enabled)
        self.assertEqual(detail, "codex_hooks=true")

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
        self.assertIn("codex_hooks=None", detail)

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


if __name__ == "__main__":
    unittest.main()
