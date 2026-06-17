#!/usr/bin/env python3
"""Tests for tools/orchestration_diagnostics.py (dangling-launch post-mortem)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import orchestration_diagnostics as diag

ORCH_ID = "orch_test"
CHILD_ARID = "f00d83b5-bfbf-4c0b-8e78-95da6bf6ba5e"


def _orch_root(repo_root: Path) -> Path:
    root = repo_root / "workspace" / "orchestrations" / ORCH_ID
    root.mkdir(parents=True, exist_ok=True)
    return root


def _open_dangling_window(root: Path, *, with_marker: bool = True) -> None:
    """Lay out an open active_child window for CHILD_ARID."""
    (root / "active_child_agent_run_id.txt").write_text(CHILD_ARID, encoding="utf-8")
    if with_marker:
        (root / "active_children").mkdir(exist_ok=True)
        (root / "active_children" / f"{CHILD_ARID}.txt").write_text(CHILD_ARID, encoding="utf-8")
    (root / "phase_state_log.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-16T12:36:58.834343Z",
                "event": "record_launch",
                "node_key_safe": "component__demo__0.1.0",
                "step": "compile",
                "agent_run_id": CHILD_ARID,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "launches").mkdir(exist_ok=True)
    (root / "launches" / f"{CHILD_ARID}.request.json").write_text(
        json.dumps({"substep": "verify"}), encoding="utf-8"
    )
    (root / "orchestration_meta.json").write_text(
        json.dumps({"orchestration_id": ORCH_ID, "status": "running"}), encoding="utf-8"
    )


class DetectDanglingTests(unittest.TestCase):
    def test_open_window_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _open_dangling_window(_orch_root(repo))
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["agent_run_id"], CHILD_ARID)
            self.assertEqual(result["step"], "compile")
            self.assertEqual(result["substep"], "verify")
            self.assertEqual(result["launch_recorded_at"], "2026-06-16T12:36:58.834343Z")
            self.assertIsInstance(result["elapsed_seconds"], float)

    def test_empty_active_child_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            (root / "active_child_agent_run_id.txt").write_text("", encoding="utf-8")
            self.assertIsNone(diag.detect_dangling_active_child(repo, ORCH_ID))

    def test_child_return_ack_closes_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            _open_dangling_window(root)
            (root / "child_returns").mkdir(exist_ok=True)
            (root / "child_returns" / f"{CHILD_ARID}.txt").write_text("ack", encoding="utf-8")
            self.assertIsNone(diag.detect_dangling_active_child(repo, ORCH_ID))

    def test_terminal_agent_run_closes_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            _open_dangling_window(root)
            (root / "agent_runs.jsonl").write_text(
                json.dumps(
                    {"agent_run_id": CHILD_ARID, "status": "pass", "finished_at": "2026-06-16T12:48:00Z"}
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertIsNone(diag.detect_dangling_active_child(repo, ORCH_ID))

    def test_claude_pointer_only_window_is_detected(self) -> None:
        # record_launch writes active_child_agent_run_id.txt BEFORE active_children/
        # <arid>.txt, so a crash between the two leaves a pointer-only open window
        # that still blocks the next record-launch. It must be detected.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _open_dangling_window(_orch_root(repo), with_marker=False)
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["agent_run_id"], CHILD_ARID)

    def test_codex_backend_dangling_detected_without_claude_pointer(self) -> None:
        # codex/cursor: record_launch writes active_children/<arid>.txt for ALL
        # backends but NOT active_child_agent_run_id.txt (Claude-only). Detection
        # must key off the backend-neutral marker, else these dangling launches are
        # missed and the orchestration stays `running`.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            # No active_child_agent_run_id.txt (codex/cursor have no pointer).
            markers = root / "active_children"
            markers.mkdir(exist_ok=True)
            (markers / f"{CHILD_ARID}.txt").write_text(CHILD_ARID, encoding="utf-8")
            (root / "phase_state_log.jsonl").write_text(
                json.dumps(
                    {
                        "ts": "2026-06-16T12:36:58Z",
                        "event": "record_launch",
                        "node_key_safe": "component__demo__0.1.0",
                        "step": "compile",
                        "agent_run_id": CHILD_ARID,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["agent_run_id"], CHILD_ARID)
            self.assertEqual(result["dangling_child_arids"], [CHILD_ARID])
            self.assertFalse((root / "active_child_agent_run_id.txt").exists())

    def test_invalid_run_attempt_is_not_dangling(self) -> None:
        # A child diverted to agent_runs_invalid.jsonl reached record-agent-run (an
        # invalid terminal attempt). Even with its marker still present, it must NOT
        # be classified as a dangling (launch_incomplete) launch.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            markers = root / "active_children"
            markers.mkdir(exist_ok=True)
            (markers / f"{CHILD_ARID}.txt").write_text(CHILD_ARID, encoding="utf-8")
            (root / "agent_runs_invalid.jsonl").write_text(
                json.dumps({"agent_run_id": CHILD_ARID, "status": "fail",
                            "fail_reason": "sandbox_enforcement_violation"})
                + "\n",
                encoding="utf-8",
            )
            self.assertIsNone(diag.detect_dangling_active_child(repo, ORCH_ID))

    def test_multiple_parallel_dangling_children_all_listed(self) -> None:
        # Parallel (codex/cursor) backends can leave several dangling markers.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            markers = root / "active_children"
            markers.mkdir(exist_ok=True)
            for arid in ("child-a", "child-b"):
                (markers / f"{arid}.txt").write_text(arid, encoding="utf-8")
            # child-a completed (terminal run) → only child-b is dangling.
            (root / "agent_runs.jsonl").write_text(
                json.dumps({"agent_run_id": "child-a", "status": "pass", "finished_at": "x"})
                + "\n",
                encoding="utf-8",
            )
            result = diag.detect_dangling_active_child(repo, ORCH_ID)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["dangling_child_arids"], ["child-b"])
            self.assertEqual(result["agent_run_id"], "child-b")


class TranscriptSummaryTests(unittest.TestCase):
    def test_dead_air_and_interrupt_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "child.jsonl"
            records = [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-16T12:38:47.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "python3 x.py"}}
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-16T12:38:47.421Z",
                    "message": {"role": "user", "content": [{"type": "tool_result", "content": "PASS"}]},
                },
                {
                    "type": "user",
                    "timestamp": "2026-06-16T12:48:47.526Z",
                    "toolUseResult": "Error: [Request interrupted by user]",
                    "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            summary = diag.summarize_transcript_tail(path)
            self.assertTrue(summary["found"])
            self.assertEqual(summary["last_activity_ts"], "2026-06-16T12:38:47.421Z")
            self.assertTrue(summary["interrupted"])
            self.assertEqual(summary["interrupt_ts"], "2026-06-16T12:48:47.526Z")
            self.assertAlmostEqual(summary["dead_air_seconds"], 600.105, places=2)
            self.assertEqual(summary["last_tool_use"]["name"], "Bash")

    def test_missing_file_is_not_found(self) -> None:
        summary = diag.summarize_transcript_tail(Path("/nonexistent/x.jsonl"))
        self.assertFalse(summary["found"])

    def test_transient_api_error_529_surfaced(self) -> None:
        """A synthetic 529 assistant record is extracted as a structured, retryable
        api_error so the operator can tell the dangling launch was a transport blip."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "child.jsonl"
            records = [
                {
                    "type": "user",
                    "timestamp": "2026-06-17T01:14:13.121Z",
                    "message": {"role": "user", "content": "You are a substep agent."},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-06-17T01:17:30.724Z",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 529,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "API Error: 529 Overloaded. Temporary."}
                        ],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            summary = diag.summarize_transcript_tail(path)
            self.assertTrue(summary["found"])
            self.assertFalse(summary["interrupted"])
            self.assertIsNotNone(summary["api_error"])
            self.assertEqual(summary["api_error"]["status"], 529)
            self.assertTrue(summary["api_error"]["retryable"])
            self.assertIn("Overloaded", summary["api_error"]["message"])

    def test_recovered_api_error_cleared_by_later_activity(self) -> None:
        """A 529 that is FOLLOWED by normal activity was recovered — it must not be
        reported, else a later unrelated hang would be mislabeled safe-to-resume."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "child.jsonl"
            records = [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-17T01:17:30.724Z",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 529,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "API Error: 529 Overloaded."}],
                    },
                },
                # Normal activity after the error → the error was recovered.
                {
                    "type": "assistant",
                    "timestamp": "2026-06-17T01:18:05.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                        ],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            summary = diag.summarize_transcript_tail(path)
            self.assertIsNone(summary["api_error"])
            self.assertEqual(summary["last_tool_use"]["name"], "Bash")

    def test_non_retryable_api_error_marked(self) -> None:
        """A 400-class API error is surfaced but flagged non-retryable."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "child.jsonl"
            records = [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-17T01:17:30.724Z",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 400,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "API Error: 400 Bad Request."}],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            summary = diag.summarize_transcript_tail(path)
            self.assertEqual(summary["api_error"]["status"], 400)
            self.assertFalse(summary["api_error"]["retryable"])

    def test_no_api_error_when_clean_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "child.jsonl"
            records = [
                {
                    "type": "assistant",
                    "timestamp": "2026-06-17T01:17:30.724Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            summary = diag.summarize_transcript_tail(path)
            self.assertIsNone(summary["api_error"])


class BuildLaunchIncidentTests(unittest.TestCase):
    def _fake_home_with_transcripts(self, repo: Path, home: Path) -> None:
        slug = str(repo.resolve()).replace("/", "-")
        host_session = "hostsess"
        (repo / "workspace" / "orchestrations" / ORCH_ID / "orchestration_meta.json").write_text(
            json.dumps({"orchestration_id": ORCH_ID, "status": "running", "host_session_id": host_session}),
            encoding="utf-8",
        )
        projects = home / ".claude" / "projects" / slug
        subagents = projects / host_session / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        # Host transcript with an Agent tool_use whose id ties to the subagent meta.
        (projects / f"{host_session}.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-06-16T12:37:46Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "Agent", "id": "toolu_X", "input": {"prompt": "go"}}
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (subagents / "agent-abc.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "toolUseId": "toolu_X"}), encoding="utf-8"
        )
        (subagents / "agent-abc.jsonl").write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {
                        "type": "user",
                        "timestamp": "2026-06-16T12:38:47.421Z",
                        "message": {"role": "user", "content": [{"type": "tool_result", "content": "PASS"}]},
                    },
                    {
                        "type": "user",
                        "timestamp": "2026-06-16T12:48:47.526Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def test_incident_with_transcript_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            _open_dangling_window(_orch_root(repo))
            self._fake_home_with_transcripts(repo, home)
            with mock.patch.object(diag.Path, "home", return_value=home):
                incident = diag.build_launch_incident(repo, ORCH_ID)
            self.assertIsNotNone(incident)
            assert incident is not None
            self.assertEqual(incident["dangling_child"]["agent_run_id"], CHILD_ARID)
            child = incident["transcripts"]["child_transcript"]
            self.assertTrue(child["found"])
            self.assertEqual(child["match_method"], "tool_use_id")
            self.assertEqual(incident["abort_marker"]["interrupt_text"], "[Request interrupted by user]")
            self.assertAlmostEqual(incident["abort_marker"]["dead_air_seconds"], 600.105, places=2)

    def test_incident_degrades_when_transcript_ephemeral(self) -> None:
        # ~/.claude cleaned: dangling still detected from in-repo artifacts, child
        # transcript reported as not-found rather than raising.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            empty_home = Path(tmp) / "home"
            empty_home.mkdir()
            root = _orch_root(repo)
            _open_dangling_window(root)
            (root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": ORCH_ID, "status": "running", "host_session_id": "gone"}),
                encoding="utf-8",
            )
            with mock.patch.object(diag.Path, "home", return_value=empty_home):
                incident = diag.build_launch_incident(repo, ORCH_ID)
            self.assertIsNotNone(incident)
            assert incident is not None
            self.assertEqual(incident["dangling_child"]["agent_run_id"], CHILD_ARID)
            self.assertFalse(incident["transcripts"]["child_transcript"]["found"])
            self.assertIsNone(incident["abort_marker"])

    def test_no_incident_when_window_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _orch_root(repo)  # empty orchestration, no active_child file
            self.assertIsNone(diag.build_launch_incident(repo, ORCH_ID))


if __name__ == "__main__":
    unittest.main()
