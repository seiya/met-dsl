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
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION

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
        # codex: record_launch writes active_children/<arid>.txt for ALL
        # backends but NOT active_child_agent_run_id.txt (Claude-only). Detection
        # must key off the backend-neutral marker, else these dangling launches are
        # missed and the orchestration stays `running`.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = _orch_root(repo)
            # No active_child_agent_run_id.txt (codex has no pointer).
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
        # Parallel backends can leave several dangling markers.
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


class ApiErrorFromRecordsTests(unittest.TestCase):
    def test_transient_api_error_529_surfaced(self) -> None:
        """A synthetic 529 assistant record is extracted as a structured, retryable
        api_error so the operator can tell the dangling launch was a transport blip."""
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
                    "content": [{"type": "text", "text": "API Error: 529 Overloaded. Temporary."}],
                },
            },
        ]
        api_error = diag.api_error_from_records(records)
        self.assertIsNotNone(api_error)
        assert api_error is not None
        self.assertEqual(api_error["status"], 529)
        self.assertTrue(api_error["retryable"])
        self.assertIn("Overloaded", api_error["message"])

    def test_recovered_api_error_cleared_by_later_activity(self) -> None:
        """A 529 that is FOLLOWED by normal activity was recovered — it must not be
        reported, else a later unrelated hang would be mislabeled safe-to-resume."""
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
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
                },
            },
        ]
        self.assertIsNone(diag.api_error_from_records(records))

    def test_non_retryable_api_error_marked(self) -> None:
        """A 400-class API error is surfaced but flagged non-retryable."""
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
        api_error = diag.api_error_from_records(records)
        self.assertIsNotNone(api_error)
        assert api_error is not None
        self.assertEqual(api_error["status"], 400)
        self.assertFalse(api_error["retryable"])

    def test_no_api_error_when_clean_tail(self) -> None:
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-06-17T01:17:30.724Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        ]
        self.assertIsNone(diag.api_error_from_records(records))


class TranscriptTailTests(unittest.TestCase):
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


class BuildLaunchIncidentTests(unittest.TestCase):
    def test_incident_correlates_leaf_transcript_by_arid(self) -> None:
        # The conductor pins each leaf's Claude session id to its agent_run_id, so
        # the child transcript is addressable as ~/.claude/projects/<slug>/<arid>.jsonl
        # — no host session needed. build_launch_incident recovers last-activity /
        # dead-air / abort-marker from it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            home = Path(tmp) / "home"
            _open_dangling_window(_orch_root(repo))
            proj = home / ".claude" / "projects" / "some-slug"
            proj.mkdir(parents=True)
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
                    "timestamp": "2026-06-16T12:48:47.000Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]},
                },
            ]
            (proj / f"{CHILD_ARID}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
            )
            with mock.patch.object(diag.Path, "home", return_value=home):
                incident = diag.build_launch_incident(repo, ORCH_ID)
            self.assertIsNotNone(incident)
            assert incident is not None
            self.assertEqual(incident["dangling_child"]["agent_run_id"], CHILD_ARID)
            child = incident["transcripts"]["child_transcript"]
            self.assertTrue(child["found"])
            self.assertEqual(child["last_tool_use"]["name"], "Bash")
            self.assertTrue(incident["abort_marker"]["interrupted"])
            self.assertEqual(
                incident["abort_marker"]["interrupt_text"], "[Request interrupted by user]"
            )
            self.assertAlmostEqual(incident["abort_marker"]["dead_air_seconds"], 600.0, places=1)

    def test_incident_degrades_when_transcript_ephemeral(self) -> None:
        # ~/.claude cleaned/absent: dangling still detected from in-repo artifacts;
        # child transcript reported not-found and abort_marker is None (never raises).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            empty_home = Path(tmp) / "home"
            empty_home.mkdir()
            _open_dangling_window(_orch_root(repo))
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


def _usage_record(inp: int, out: int, cr: int, cc: int) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
        },
    }


class SummarizeUsageTests(unittest.TestCase):
    def test_sums_and_peak_context(self) -> None:
        records = [
            _usage_record(10, 5, 100, 20),   # ctx = 130
            _usage_record(8, 4, 300, 0),     # ctx = 308 (peak)
            {"type": "user", "message": {"role": "user", "content": "no usage"}},
        ]
        u = diag.summarize_jsonl_usage(records)
        self.assertEqual(u["input_tokens"], 18)
        self.assertEqual(u["output_tokens"], 9)
        self.assertEqual(u["cache_read_input_tokens"], 400)
        self.assertEqual(u["cache_creation_input_tokens"], 20)
        self.assertEqual(u["total_tokens"], 18 + 9 + 400 + 20)
        self.assertEqual(u["assistant_turns"], 2)
        self.assertEqual(u["peak_context_tokens"], 308)

    def test_empty_is_all_zero(self) -> None:
        u = diag.summarize_jsonl_usage([])
        self.assertEqual(u["total_tokens"], 0)
        self.assertEqual(u["assistant_turns"], 0)
        self.assertEqual(u["peak_context_tokens"], 0)


class OwnAridDisambiguationTests(unittest.TestCase):
    def test_capability_path_wins_over_parent_mention(self) -> None:
        own = "11111111-1111-4111-8111-111111111111"
        parent = "22222222-2222-4222-8222-222222222222"
        # parent arid appears (as parent_agent_run_id) but own arid owns the
        # capability path — the own arid must win.
        text = (
            f'{{"parent_agent_run_id": "{parent}"}}\n'
            f'capabilities/{own}.json output_manifests/{own}.json'
        )
        self.assertEqual(
            diag._own_arid_of_transcript(text, {own, parent}), own
        )

    def test_frequency_fallback_when_no_manifest_path(self) -> None:
        own = "33333333-3333-4333-8333-333333333333"
        parent = "44444444-4444-4444-8444-444444444444"
        text = f"{own} {own} {own} {parent}"
        self.assertEqual(diag._own_arid_of_transcript(text, {own, parent}), own)

    def test_none_when_no_target_present(self) -> None:
        self.assertIsNone(diag._own_arid_of_transcript("nothing here", {"x"}))


class AggregateChildUsageTests(unittest.TestCase):
    def _write_child(self, subagents: Path, fname: str, arid: str, parent: str, records: list) -> None:
        body_head = (
            f'{{"type":"user","message":{{"role":"user","content":'
            f'"capabilities/{arid}.json output_manifests/{arid}.json '
            f'parent_agent_run_id {parent}"}}}}'
        )
        lines = [body_head] + [json.dumps(r) for r in records]
        (subagents / fname).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_attributes_per_child_and_avoids_parent_misattribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            subagents = home / ".claude" / "projects" / slug / "hostsess" / "subagents"
            subagents.mkdir(parents=True, exist_ok=True)
            parent = "0e750000-0000-4000-8000-000000000000"
            child_a = "aaaa1111-1111-4111-8111-111111111111"
            child_b = "bbbb2222-2222-4222-8222-222222222222"
            self._write_child(subagents, "agent-a.jsonl", child_a, parent, [_usage_record(10, 10, 1000, 0)])
            self._write_child(subagents, "agent-b.jsonl", child_b, parent, [_usage_record(5, 5, 500, 0)])
            with mock.patch.object(diag.Path, "home", return_value=home):
                # parent arid is in the target set but must NOT capture a child file.
                agg = diag.aggregate_child_usage(repo, [child_a, child_b, parent])
            self.assertTrue(agg["available"])
            self.assertEqual(agg["matched_count"], 2)
            self.assertIn(child_a, agg["per_child"])
            self.assertIn(child_b, agg["per_child"])
            self.assertNotIn(parent, agg["per_child"])
            self.assertEqual(agg["per_child"][child_a]["total_tokens"], 1020)
            self.assertEqual(agg["children_total"]["total_tokens"], 1020 + 510)
            self.assertEqual(agg["unmatched_arids"], [parent])

    def test_unavailable_when_projects_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            empty_home = Path(tmp) / "home"
            empty_home.mkdir()
            with mock.patch.object(diag.Path, "home", return_value=empty_home):
                agg = diag.aggregate_child_usage(repo, ["aaaa1111-1111-4111-8111-111111111111"])
            self.assertFalse(agg["available"])
            self.assertIn("reason", agg)

    def test_no_targets_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agg = diag.aggregate_child_usage(Path(tmp), [])
            self.assertFalse(agg["available"])

    def test_scans_all_host_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            base = home / ".claude" / "projects" / slug
            child = "aaaa1111-1111-4111-8111-111111111111"
            parent = "0e750000-0000-4000-8000-000000000000"
            for sess in ("sessA", "sessB"):
                d = base / sess / "subagents"
                d.mkdir(parents=True, exist_ok=True)
                self._write_child(d, "agent-x.jsonl", child, parent, [_usage_record(1, 1, 100, 0)])
            with mock.patch.object(diag.Path, "home", return_value=home):
                # Every host session's subagents dir is scanned; the child is found
                # regardless of which session ran it.
                agg_all = diag.aggregate_child_usage(repo, [child])
            self.assertTrue(agg_all["available"])
            self.assertIn(child, agg_all["per_child"])


class AggregateParentUsageTests(unittest.TestCase):
    def _parent_session(self, base: Path, name: str, first_user: str, recs: list) -> None:
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": first_user}})
        ] + [json.dumps(r) for r in recs]
        (base / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_sums_across_resume_sessions_and_excludes_discussion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            base = home / ".claude" / "projects" / slug
            base.mkdir(parents=True, exist_ok=True)
            arid = "88c4f71a-efb3-4c89-a706-9d41969cc12e"
            marker = f"workspace/tmp/{arid}"
            # Two genuine parent sessions (launch prompt carries the marker).
            self._parent_session(
                base, "orig", f"Start the workflow. allowed_tmp_root: {marker}",
                [_usage_record(10, 10, 1000, 0)],
            )
            self._parent_session(
                base, "resume", f"Start the workflow. allowed_tmp_root: {marker}",
                [_usage_record(5, 5, 500, 0)],
            )
            # A diagnostic session that merely DISCUSSES the orchestration: the
            # marker appears later in the body but NOT in the first user message.
            (base / "investigate.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "message": {"role": "user", "content": "review the workflow result"}}),
                        json.dumps({"type": "user", "message": {"role": "user", "content": f"look at {marker}/foo"}}),
                        json.dumps(_usage_record(9999, 9999, 999999, 0)),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(diag.Path, "home", return_value=home):
                agg = diag.aggregate_parent_usage(repo, arid)
            self.assertTrue(agg["available"])
            self.assertEqual(agg["session_count"], 2)
            self.assertEqual(agg["total_tokens"], 1020 + 510)  # discussion excluded

    def test_unavailable_when_no_parent_located(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            (home / ".claude" / "projects").mkdir(parents=True)
            with mock.patch.object(diag.Path, "home", return_value=home):
                agg = diag.aggregate_parent_usage(repo, "nope-arid")
            self.assertFalse(agg["available"])

    def test_empty_arid_unavailable(self) -> None:
        self.assertFalse(diag.aggregate_parent_usage(Path("/x"), "")["available"])


class SummarizePureLeafMetasTest(unittest.TestCase):
    """tools/orchestration_diagnostics.summarize_pure_leaf_metas (Z2 M-E)."""

    def _bundle_meta(self, **overrides) -> dict:
        meta = {
            "result": "pass",
            "failure_category": None,
            "attempts": 2,
            "prompt_contract_version": "pure-1",
            "per_attempt": [
                {
                    "agent_run_id": "arid-1",
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                    },
                },
                {
                    "agent_run_id": "arid-2",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 200, "output_tokens": 80},
                },
            ],
        }
        meta.update(overrides)
        return meta

    def test_reads_both_metas_and_sums_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "source" / "src_x"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text(
                json.dumps(self._bundle_meta()), encoding="utf-8"
            )
            (src / "verdict_meta.json").write_text(
                json.dumps(
                    {
                        "result": "fail",
                        "failure_category": "verdict_schema_violation",
                        "attempts": 1,
                        "prompt_contract_version": "pure-1",
                        "per_attempt": [
                            {"agent_run_id": "v-1", "model": "claude-opus-4-8", "usage": {"input_tokens": 300, "output_tokens": 20}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out = diag.summarize_pure_leaf_metas(src)
        self.assertTrue(out["found"])
        gen = out["generate"]
        self.assertTrue(gen["found"])
        self.assertEqual(gen["result"], "pass")
        self.assertEqual(gen["attempts"], 2)
        self.assertEqual(gen["repair_turns"], 1)
        self.assertEqual(gen["usage_total"]["input_tokens"], 300)
        self.assertEqual(gen["usage_total"]["output_tokens"], 130)
        self.assertEqual(gen["usage_total"]["cache_read_input_tokens"], 10)
        self.assertEqual(gen["usage_total"]["total_tokens"], 300 + 130 + 10 + 5)
        self.assertEqual(gen["models"], ["claude-opus-4-8", "claude-opus-4-8"])
        ver = out["verify"]
        self.assertEqual(ver["result"], "fail")
        self.assertEqual(ver["failure_category"], "verdict_schema_violation")
        self.assertEqual(ver["repair_turns"], 0)

    def test_legacy_node_has_no_metas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_legacy"
            src.mkdir(parents=True)
            out = diag.summarize_pure_leaf_metas(src)
        self.assertFalse(out["found"])
        self.assertFalse(out["generate"]["found"])
        self.assertFalse(out["verify"]["found"])

    def test_malformed_usage_and_missing_attempts_do_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_bad"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text(
                json.dumps(
                    {
                        "result": "pass",
                        "per_attempt": [
                            {"agent_run_id": "a", "model": None, "usage": None},
                            {"agent_run_id": "b", "usage": {"input_tokens": "oops"}},
                            "not-a-dict",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out = diag.summarize_pure_leaf_metas(src)
        gen = out["generate"]
        self.assertTrue(gen["found"])
        # attempts falls back to the count of structurally-valid (dict) attempts
        # when absent — the "not-a-dict" entry is not counted.
        self.assertEqual(gen["attempts"], 2)
        # non-int usage values ("oops", None) coerce to 0, never raise
        self.assertEqual(gen["usage_total"]["total_tokens"], 0)
        self.assertEqual(gen["models"], [])

    def test_non_pure_dict_and_infinity_usage_do_not_mislead_or_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_edge"
            src.mkdir(parents=True)
            # An empty / unrelated JSON object is not a pure-leaf meta envelope.
            (src / "bundle_meta.json").write_text(json.dumps({}), encoding="utf-8")
            # Infinity is a valid JSON value to Python's decoder; int(inf) would
            # raise OverflowError — the coercion must swallow it as 0.
            (src / "verdict_meta.json").write_text(
                '{"result": "pass", "per_attempt": [{"usage": {"input_tokens": Infinity, "output_tokens": -5}}]}',
                encoding="utf-8",
            )
            out = diag.summarize_pure_leaf_metas(src)
        self.assertFalse(out["generate"]["found"])  # empty dict → not a pure meta
        ver = out["verify"]
        self.assertTrue(ver["found"])
        self.assertEqual(ver["usage_total"]["total_tokens"], 0)  # inf→0, -5→0

    def test_unparseable_meta_degrades_to_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_broken"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text("{not json", encoding="utf-8")
            out = diag.summarize_pure_leaf_metas(src)
        self.assertFalse(out["generate"]["found"])

    def test_non_utf8_meta_degrades_and_does_not_raise(self) -> None:
        # `read_text(encoding="utf-8")` raises UnicodeDecodeError — a ValueError, NOT
        # an OSError — so a narrow `(OSError, JSONDecodeError)` catch would let a
        # corrupt-byte file escape the "degrade, never raise" contract.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_badbytes"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_bytes(
                b'{"per_attempt": [], "result": "\xff\xfe pass"}'
            )
            out = diag.summarize_pure_leaf_metas(src)  # must not raise
        self.assertFalse(out["generate"]["found"])
        self.assertFalse(out["found"])

    def test_bools_are_not_counted_as_ints(self) -> None:
        # bool IS an int subclass, so a missing isinstance(value, bool) guard would
        # let attempts=True render verbatim and sum booleans as tokens.
        row = diag._summarize_one_pure_meta(
            {
                "result": "pass",
                "attempts": True,
                "per_attempt": [{"usage": {"input_tokens": True, "output_tokens": 5}}],
            }
        )
        self.assertEqual(row["attempts"], 1)  # True rejected → falls back to 1 valid attempt
        self.assertIsNot(row["attempts"], True)
        self.assertEqual(row["usage_total"]["input_tokens"], 0)  # True is not 1 token
        self.assertEqual(row["usage_total"]["output_tokens"], 5)

    def test_non_list_per_attempt_does_not_raise(self) -> None:
        # A corrupt scalar per_attempt must degrade, not TypeError out of the
        # "never raises" contract.
        for bad in (5, "x", {"a": 1}, None):
            row = diag._summarize_one_pure_meta({"result": "pass", "per_attempt": bad})
            self.assertTrue(row["found"])
            self.assertEqual(row["attempts"], 0)
            self.assertEqual(row["usage_total"]["total_tokens"], 0)
            self.assertEqual(row["models"], [])

    def test_models_preserve_attempt_order_and_skip_empty(self) -> None:
        # `models` is distinct-IN-ORDER: a set-based dedup would sort and lose the
        # per-attempt sequence. An empty-string model is not a model.
        _, models = diag._sum_pure_attempt_usage(
            [
                {"model": "zeta"}, {"model": "alpha"}, {"model": "zeta"},
                {"model": ""}, {"model": None},
            ]
        )
        self.assertEqual(models, ["zeta", "alpha", "zeta"])  # order kept, empties dropped

    def test_zero_attempts_does_not_produce_negative_repair_turns(self) -> None:
        # repair_turns is clamped: a corrupt attempts=0 must not render as -1.
        row = diag._summarize_one_pure_meta({"result": "fail", "attempts": 0, "per_attempt": []})
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["repair_turns"], 0)

    def test_foreign_json_without_payload_key_is_not_a_pure_meta(self) -> None:
        # A stale/hand-edited document carrying only common keys must NOT be
        # reported as a pure-leaf row of all-zero metrics: `per_attempt` (the
        # measurement payload) is the discriminator, not `result`/`attempts`.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_foreign"
            src.mkdir(parents=True)
            (src / "bundle_meta.json").write_text(
                json.dumps({"result": "ok", "attempts": 7}), encoding="utf-8"
            )
            out = diag.summarize_pure_leaf_metas(src)
        self.assertFalse(out["generate"]["found"])
        self.assertFalse(out["found"])

    def test_usage_key_tuples_stay_consistent(self) -> None:
        # Both sum-key tuples derive from the CLI token classes. Pin the relation AND
        # the derived contents: the obvious re-derivation
        # `(*_CLI_TOKEN_USAGE_KEYS, "assistant_turns")` silently drops "total_tokens",
        # which the transcript aggregators sum.
        self.assertEqual(diag._PURE_ATTEMPT_USAGE_KEYS, diag._CLI_TOKEN_USAGE_KEYS)
        self.assertTrue(set(diag._PURE_ATTEMPT_USAGE_KEYS) < set(diag._USAGE_SUM_KEYS))
        self.assertIn("total_tokens", diag._USAGE_SUM_KEYS)
        self.assertIn("assistant_turns", diag._USAGE_SUM_KEYS)
        # total_tokens/assistant_turns are derived, not per-attempt CLI fields.
        self.assertNotIn("total_tokens", diag._PURE_ATTEMPT_USAGE_KEYS)
        self.assertNotIn("assistant_turns", diag._PURE_ATTEMPT_USAGE_KEYS)
        self.assertEqual(
            diag._USAGE_SUM_KEYS,
            ("input_tokens", "output_tokens", "cache_read_input_tokens",
             "cache_creation_input_tokens", "total_tokens", "assistant_turns"),
        )

    def test_row_carries_no_source_dir_key(self) -> None:
        # The caller owns the label (the audit rollup attaches a repo-relative
        # path); the callee must not publish a second, absolute notion of it.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_x"
            src.mkdir(parents=True)
            out = diag.summarize_pure_leaf_metas(src)
        self.assertNotIn("source_dir", out)


class PureLeafMetaWriterReaderContractTest(unittest.TestCase):
    """The conductor writes bundle_meta/verdict_meta; this module reads them. The
    two live in different modules with no shared schema constant, so drive the REAL
    writer into the reader — a renamed/dropped field breaks here rather than
    silently reporting zero tokens (both modules' own suites would stay green)."""

    def test_conductor_written_metas_round_trip_through_reader(self) -> None:
        from tools.tests.test_pure_leaf_producer import _conductor, _write_node

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = _conductor(repo)
            src_dir = repo / refs.source_dir()
            src_dir.mkdir(parents=True, exist_ok=True)
            per_attempt = [
                {"agent_run_id": "a1", "model": "claude-opus-4-8",
                 "usage": {"input_tokens": 11, "output_tokens": 22,
                           "cache_read_input_tokens": 33, "cache_creation_input_tokens": 44}},
            ]
            c._write_bundle_meta(
                refs, result="fail", failure_category="bundle_schema_violation",
                failure_excerpt="boom", attempts=2, per_attempt=per_attempt,
            )
            c._write_verdict_meta(
                refs, result="pass", failure_category=None, failure_excerpt=None,
                attempts=1, per_attempt=per_attempt,
            )
            out = diag.summarize_pure_leaf_metas(src_dir)

        self.assertTrue(out["found"])
        gen = out["generate"]
        self.assertTrue(gen["found"], "reader must recognize the conductor's bundle_meta envelope")
        self.assertEqual(gen["result"], "fail")
        self.assertEqual(gen["attempts"], 2)
        self.assertEqual(gen["failure_category"], "bundle_schema_violation")
        # The writer stamps the contract version; the reader must surface it.
        self.assertEqual(gen["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
        # Every usage class the writer persists must be summed by the reader.
        self.assertEqual(gen["usage_total"]["total_tokens"], 11 + 22 + 33 + 44)
        self.assertEqual(gen["models"], ["claude-opus-4-8"])
        self.assertTrue(out["verify"]["found"])
        self.assertEqual(out["verify"]["result"], "pass")


if __name__ == "__main__":
    unittest.main()
