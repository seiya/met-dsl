#!/usr/bin/env python3
"""Tests for tools/audit_orchestration.py."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.audit_orchestration import (
    audit,
    collect_allow_auto_approve_stats,
    collect_fix_hint_stats,
    collect_policy_block_counts,
    collect_fail_closed_timeline,
    collect_agent_run_summary,
    detect_suspicious_benign_volume,
    split_substantive_and_benign,
    _render_markdown,
)


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _make_block(policy: str, command: str = "cmd", fix_hint: dict | None = None) -> dict:
    audit_detail: dict = {"policy": policy}
    if fix_hint is not None:
        audit_detail["fix_hint"] = fix_hint
    return {
        "action": "block",
        "tool_name": "Bash",
        "payload_summary": {"command": command},
        "audit_detail": audit_detail,
        "ts": "2026-05-09T00:00:00Z",
    }


class CollectPolicyBlockCountsTests(unittest.TestCase):
    def test_counts_by_policy(self) -> None:
        blocks = [
            _make_block("read_manifest_read_guard"),
            _make_block("read_manifest_read_guard"),
            _make_block("output_manifest_write_guard"),
        ]
        result = collect_policy_block_counts(blocks)
        self.assertEqual(result["read_manifest_read_guard"], 2)
        self.assertEqual(result["output_manifest_write_guard"], 1)

    def test_empty_blocks(self) -> None:
        self.assertEqual(collect_policy_block_counts([]), {})

    def test_unknown_policy_when_no_audit_detail(self) -> None:
        blocks = [{"action": "block", "ts": "2026-05-09T00:00:00Z"}]
        result = collect_policy_block_counts(blocks)
        self.assertIn("unknown", result)


class SplitSubstantiveAndBenignTests(unittest.TestCase):
    def test_auto_read_expected_block_is_benign(self) -> None:
        blocks = [
            _make_block("auto_read_expected_block"),
            _make_block("auto_read_expected_block"),
            _make_block("read_manifest_read_guard"),
        ]
        substantive, benign = split_substantive_and_benign(blocks)
        self.assertEqual(len(benign), 2)
        self.assertEqual(len(substantive), 1)
        self.assertEqual(
            (substantive[0].get("audit_detail") or {}).get("policy"),
            "read_manifest_read_guard",
        )

    def test_audit_separates_benign_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_separation"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            _write_jsonl(
                orch_root / "hooks" / "native_hook_events.jsonl",
                [
                    _make_block("auto_read_expected_block"),
                    _make_block("auto_read_expected_block"),
                    _make_block("read_manifest_read_guard"),
                    _make_block("output_manifest_write_guard"),
                ],
            )
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["benign_block_count"], 2)
        self.assertEqual(result["substantive_block_count"], 2)
        # Substantive policies appear in main counts
        self.assertIn("read_manifest_read_guard", result["policy_block_counts"])
        # Benign policies do NOT appear in main counts
        self.assertNotIn("auto_read_expected_block", result["policy_block_counts"])
        # They appear in the dedicated benign bucket
        self.assertEqual(
            result["benign_policy_block_counts"]["auto_read_expected_block"], 2
        )


class DetectSuspiciousBenignVolumeTests(unittest.TestCase):
    """Regression: explicit (post-startup) reads of allowlisted paths must NOT
    be silently aggregated into the benign bucket — operators need visibility."""

    def _make_benign(self, agent_id: str) -> dict:
        return {
            "action": "block",
            "agent_run_id": agent_id,
            "audit_detail": {"policy": "auto_read_expected_block"},
        }

    def test_below_budget_not_flagged(self) -> None:
        # Expected platform startup: at most ~6 reads
        blocks = [self._make_benign("agent_a") for _ in range(6)]
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(flagged, [])

    def test_above_budget_flagged(self) -> None:
        # 50 reads of MEMORY.md from one orchestration agent → suspicious
        blocks = [self._make_benign("agent_a") for _ in range(50)]
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["agent_run_id"], "agent_a")
        self.assertEqual(flagged[0]["policy"], "auto_read_expected_block")
        self.assertEqual(flagged[0]["count"], 50)

    def test_per_agent_threshold(self) -> None:
        # Two agents, only one over budget
        blocks = [self._make_benign("agent_a") for _ in range(50)]
        blocks.extend(self._make_benign("agent_b") for _ in range(3))
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["agent_run_id"], "agent_a")

    def test_reads_agent_run_id_from_audit_detail(self) -> None:
        """Regression: hook's `auto_read_expected_block` puts agent_run_id in
        `audit_detail`, not top-level. The detector must look there so blocks
        are not aggregated under <unknown>."""
        blocks = [
            {
                "action": "block",
                # No top-level agent_run_id — must come from audit_detail
                "audit_detail": {
                    "policy": "auto_read_expected_block",
                    "agent_run_id": "agent_x",
                },
            }
            for _ in range(50)
        ]
        flagged = detect_suspicious_benign_volume(blocks)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["agent_run_id"], "agent_x")

    def test_audit_surfaces_suspicious_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_susp"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            blocks = [
                {
                    "action": "block",
                    "agent_run_id": "run_orch",
                    "audit_detail": {"policy": "auto_read_expected_block"},
                }
                for _ in range(50)
            ]
            _write_jsonl(orch_root / "hooks" / "native_hook_events.jsonl", blocks)
            result = audit(Path(tmp), orch_id)
        self.assertEqual(len(result["suspicious_benign_volume"]), 1)
        self.assertEqual(result["suspicious_benign_volume"][0]["agent_run_id"], "run_orch")
        # Markdown rendering surfaces the warning
        md = _render_markdown(result)
        self.assertIn("Suspicious benign-block volume", md)


class CollectFixHintStatsTests(unittest.TestCase):
    def test_hint_present_counted(self) -> None:
        blocks = [
            _make_block("output_manifest_write_guard", fix_hint={"next_command": "do this"}),
        ]
        stats = collect_fix_hint_stats(blocks)
        self.assertEqual(stats["hint_present"].get("output_manifest_write_guard"), 1)
        self.assertNotIn("output_manifest_write_guard", stats["hint_absent"])

    def test_hint_absent_counted(self) -> None:
        blocks = [_make_block("forbid_python_inline_write")]
        stats = collect_fix_hint_stats(blocks)
        self.assertEqual(stats["hint_absent"].get("forbid_python_inline_write"), 1)

    def test_repeated_command_detected(self) -> None:
        blocks = [
            _make_block("forbid_tools_direct_read", command="cat tools/foo.py"),
            _make_block("forbid_tools_direct_read", command="cat tools/foo.py"),
        ]
        stats = collect_fix_hint_stats(blocks)
        self.assertIn("forbid_tools_direct_read", stats["repeated"])
        self.assertEqual(len(stats["repeated"]["forbid_tools_direct_read"]), 1)


class CollectFailClosedTimelineTests(unittest.TestCase):
    def test_no_fail_closed(self) -> None:
        result = collect_fail_closed_timeline([], [])
        self.assertIsNone(result["fail_closed_at"])
        self.assertEqual(result["last_events"], [])

    def test_returns_last_5_events(self) -> None:
        phase_log = [{"event": "set_status", "to": "fail_closed", "ts": "2026-05-09T01:00:00Z"}]
        hook_events = [
            {"action": "block", "ts": f"2026-05-09T00:0{i}:00Z", "audit_detail": {"policy": f"p{i}"}}
            for i in range(8)
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(result["fail_closed_at"], "2026-05-09T01:00:00Z")
        self.assertEqual(len(result["last_events"]), 5)

    def test_events_are_ordered_before_fail_ts(self) -> None:
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"}]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:03:00Z", "audit_detail": {}},
            {"action": "allow", "ts": "2026-05-09T00:06:00Z", "audit_detail": {}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        # Only event before or at fail_ts
        self.assertEqual(len(result["last_events"]), 1)

    def test_orders_by_parsed_timestamp_not_file_order(self) -> None:
        """Regression: hook events appended out-of-order (multiple hook
        processes) must still be sliced by parsed timestamp before the
        fail_closed cutoff. The previous file-order logic could surface the
        wrong commands as the events leading up to fail_closed."""
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"}]
        # File order is (late, early1, early2, after) but chronological order
        # before fail_closed is early1 < early2 < late.
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "late"}},
            {"action": "block", "ts": "2026-05-09T00:00:30Z", "audit_detail": {"policy": "early1"}},
            {"action": "block", "ts": "2026-05-09T00:01:00Z", "audit_detail": {"policy": "early2"}},
            {"action": "allow", "ts": "2026-05-09T00:06:00Z", "audit_detail": {"policy": "after"}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=2)
        # Last 2 by time, not by file position
        policies = [e["policy"] for e in result["last_events"]]
        self.assertEqual(policies, ["early2", "late"])

    def test_unparseable_timestamps_surfaced_not_dropped(self) -> None:
        """Regression: events with malformed timestamps must NOT be silently
        dropped from `last_events`. They should appear in the timeline and
        be counted via `unparseable_timestamp_count`."""
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"}]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "p1"}},
            {"action": "block", "ts": "BAD-TIMESTAMP", "audit_detail": {"policy": "malformed"}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(result["unparseable_timestamp_count"], 1)
        policies = [e["policy"] for e in result["last_events"]]
        # Both events appear (parseable first, unparseable appended at end)
        self.assertIn("p1", policies)
        self.assertIn("malformed", policies)

    def test_unparseable_timestamps_trigger_data_integrity_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_bad_ts"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            _write_jsonl(orch_root / "phase_state_log.jsonl", [
                {"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"},
            ])
            _write_jsonl(orch_root / "hooks" / "native_hook_events.jsonl", [
                {"action": "block", "ts": "BAD-TS", "audit_detail": {"policy": "x"}},
            ])
            result = audit(Path(tmp), orch_id)
        self.assertTrue(result["data_integrity_warning"])
        self.assertEqual(result["unparseable_timestamp_count"], 1)

    def test_handles_z_and_offset_timestamps(self) -> None:
        """Both `Z` (UTC) and explicit offset timestamps must parse correctly."""
        phase_log = [{"to": "fail_closed", "ts": "2026-05-09T00:05:00+00:00"}]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "p1"}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(len(result["last_events"]), 1)
        self.assertEqual(result["last_events"][0]["policy"], "p1")

    def test_picks_latest_fail_closed_when_multiple(self) -> None:
        # Regression: multiple fail_closed transitions (reopen + re-fail) should
        # use the LATEST timestamp, not the first.
        phase_log = [
            {"to": "fail_closed", "ts": "2026-05-09T00:01:00Z"},
            {"to": "running", "ts": "2026-05-09T00:02:00Z"},
            {"to": "fail_closed", "ts": "2026-05-09T00:05:00Z"},
        ]
        hook_events = [
            {"action": "block", "ts": "2026-05-09T00:00:30Z", "audit_detail": {"policy": "early"}},
            {"action": "block", "ts": "2026-05-09T00:04:30Z", "audit_detail": {"policy": "late"}},
            {"action": "allow", "ts": "2026-05-09T00:06:00Z", "audit_detail": {}},
        ]
        result = collect_fail_closed_timeline(hook_events, phase_log, n=5)
        self.assertEqual(result["fail_closed_at"], "2026-05-09T00:05:00Z")
        # Both pre-fail blocks should be included (under the latest fail_ts cutoff)
        policies = [e.get("policy") for e in result["last_events"]]
        self.assertIn("early", policies)
        self.assertIn("late", policies)


class CollectAgentRunSummaryTests(unittest.TestCase):
    def test_status_counts(self) -> None:
        runs = [
            {"agent_run_id": "r1", "status": "pass", "finished_at": "2026-05-09T00:00:00Z"},
            {"agent_run_id": "r2", "status": "fail", "finished_at": "2026-05-09T00:01:00Z"},
            {"agent_run_id": "r3", "status": "pass", "finished_at": "2026-05-09T00:02:00Z"},
        ]
        result = collect_agent_run_summary(runs)
        self.assertEqual(result["status_counts"]["pass"], 2)
        self.assertEqual(result["status_counts"]["fail"], 1)
        self.assertEqual(result["missing_finished_at"], [])

    def test_invalid_runs_appear_in_status_counts(self) -> None:
        """Regression: agent_runs_invalid.jsonl entries (terminal-validation
        fallback fail records) must appear in status_counts so operators see
        them in the per-status breakdown — not just in the separate
        invalid_run_count field."""
        from tools.audit_orchestration import collect_agent_run_summary
        result = collect_agent_run_summary(
            [{"agent_run_id": "r1", "status": "pass", "finished_at": "x"}],
            [
                {"agent_run_id": "r2", "status": "fail",
                 "fail_reason": "terminal_payload_validation_error"},
                {"agent_run_id": "r3", "status": "fail"},
            ],
        )
        self.assertEqual(result["status_counts"]["pass"], 1)
        self.assertEqual(result["status_counts"]["fail"], 2)

    def test_missing_finished_at(self) -> None:
        runs = [{"agent_run_id": "r1", "status": "pass"}]
        result = collect_agent_run_summary(runs)
        self.assertIn("r1", result["missing_finished_at"])


class CollectAllowAutoApproveStatsTests(unittest.TestCase):
    """Aggregation for visualizing `action=allow_auto_approve` events."""

    def _make_allow_auto(self, tool_name: str) -> dict:
        return {
            "action": "allow_auto_approve",
            "tool_name": tool_name,
            "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": tool_name},
            "ts": "2026-05-09T00:00:00Z",
        }

    def test_empty_events_returns_zero_total(self) -> None:
        result = collect_allow_auto_approve_stats([])
        self.assertEqual(result, {"total": 0, "by_tool": {}})

    def test_ignores_non_allow_auto_approve_actions(self) -> None:
        events = [
            {"action": "allow", "tool_name": "Read"},
            {"action": "block", "tool_name": "Write"},
            self._make_allow_auto("Write"),
        ]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["by_tool"], {"Write": 1})

    def test_aggregates_by_tool_name_and_sorts_by_count(self) -> None:
        events = [
            self._make_allow_auto("Write"),
            self._make_allow_auto("Write"),
            self._make_allow_auto("Write"),
            self._make_allow_auto("Edit"),
        ]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["total"], 4)
        self.assertEqual(list(result["by_tool"].keys()), ["Write", "Edit"])
        self.assertEqual(result["by_tool"]["Write"], 3)
        self.assertEqual(result["by_tool"]["Edit"], 1)

    def test_falls_back_to_audit_detail_tool_name_when_top_level_missing(self) -> None:
        events = [
            {
                "action": "allow_auto_approve",
                "audit_detail": {"tool_name": "Write"},
            }
        ]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["by_tool"], {"Write": 1})

    def test_unknown_tool_when_no_tool_name_anywhere(self) -> None:
        events = [{"action": "allow_auto_approve"}]
        result = collect_allow_auto_approve_stats(events)
        self.assertEqual(result["by_tool"], {"unknown": 1})


class AuditIntegrationTests(unittest.TestCase):
    """audit() end-to-end with a small fixture workspace."""

    def _build_fixture(self, tmp: str, orch_id: str) -> None:
        root = Path(tmp)
        orch_root = root / "workspace" / "orchestrations" / orch_id
        hooks_dir = orch_root / "hooks"
        hooks_dir.mkdir(parents=True)

        hook_events = [
            _make_block("read_manifest_read_guard", "cat tools/x.py"),
            _make_block("read_manifest_read_guard", "cat tools/y.py"),
            _make_block("read_manifest_read_guard", "cat tools/z.py"),
            _make_block("read_manifest_read_guard", "cat tools/z.py"),
            _make_block("read_manifest_read_guard", "cat tools/z.py"),
            _make_block("output_manifest_write_guard", fix_hint={"next_command": "guarded-apply-patch ..."}),
            {"action": "allow", "tool_name": "Read", "ts": "2026-05-09T00:10:00Z"},
            {
                "action": "allow_auto_approve",
                "tool_name": "Write",
                "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": "Write"},
                "ts": "2026-05-09T00:06:00Z",
            },
            {
                "action": "allow_auto_approve",
                "tool_name": "Write",
                "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": "Write"},
                "ts": "2026-05-09T00:07:00Z",
            },
            {
                "action": "allow_auto_approve",
                "tool_name": "Edit",
                "audit_detail": {"policy": "output_manifest_write_allow", "tool_name": "Edit"},
                "ts": "2026-05-09T00:08:00Z",
            },
        ]
        _write_jsonl(hooks_dir / "native_hook_events.jsonl", hook_events)

        phase_log = [
            {"event": "set_status", "to": "fail_closed", "ts": "2026-05-09T00:10:00Z"},
        ]
        _write_jsonl(orch_root / "phase_state_log.jsonl", phase_log)

        agent_runs = [
            {"agent_run_id": "run1", "status": "pass", "finished_at": "2026-05-09T00:05:00Z"},
            {"agent_run_id": "run2", "status": "fail", "finished_at": "2026-05-09T00:09:00Z"},
        ]
        _write_jsonl(orch_root / "agent_runs.jsonl", agent_runs)

    def test_audit_returns_expected_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_test_20260509T000000Z_aabbccdd"
            self._build_fixture(tmp, orch_id)
            result = audit(Path(tmp), orch_id)

        self.assertEqual(result["orchestration_id"], orch_id)
        self.assertEqual(result["total_blocks"], 6)
        self.assertEqual(result["policy_block_counts"]["read_manifest_read_guard"], 5)
        self.assertEqual(result["policy_block_counts"]["output_manifest_write_guard"], 1)
        self.assertEqual(result["fix_hint_stats"]["hint_present"]["output_manifest_write_guard"], 1)
        self.assertEqual(result["fix_hint_stats"]["hint_absent"]["read_manifest_read_guard"], 5)
        self.assertIsNotNone(result["fail_closed_timeline"]["fail_closed_at"])
        self.assertEqual(result["agent_run_summary"]["status_counts"]["pass"], 1)
        self.assertEqual(result["agent_run_summary"]["status_counts"]["fail"], 1)
        aa = result["allow_auto_approve_stats"]
        self.assertEqual(aa["total"], 3)
        self.assertEqual(aa["by_tool"], {"Write": 2, "Edit": 1})

    def test_audit_renders_markdown_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_test_md"
            self._build_fixture(tmp, orch_id)
            result = audit(Path(tmp), orch_id)
        md = _render_markdown(result)
        self.assertIn("REPEATED ERROR PATTERN", md)
        self.assertIn("read_manifest_read_guard", md)
        self.assertIn("fail_closed", md)
        self.assertIn("Auto-approved Write/Edit", md)
        self.assertIn("Total: 3", md)

    def test_audit_markdown_omits_auto_approve_section_when_zero(self) -> None:
        """Section is suppressed when no allow_auto_approve events fired."""
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_no_auto_approve"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            _write_jsonl(
                orch_root / "hooks" / "native_hook_events.jsonl",
                [_make_block("read_manifest_read_guard")],
            )
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["allow_auto_approve_stats"]["total"], 0)
        md = _render_markdown(result)
        self.assertNotIn("Auto-approved Write/Edit", md)

    def test_audit_handles_missing_log_files_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_empty"
            (Path(tmp) / "workspace" / "orchestrations" / orch_id).mkdir(parents=True)
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["total_blocks"], 0)
        self.assertIsNone(result["fail_closed_timeline"]["fail_closed_at"])
        self.assertEqual(result["invalid_run_count"], 0)

    def test_audit_flags_corrupted_jsonl(self) -> None:
        """Regression: malformed JSON lines must be surfaced, not silently dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_corrupt"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            (orch_root / "hooks" / "native_hook_events.jsonl").write_text(
                '{"action":"block"}\n'
                '{this is not valid json\n'
                '{"action":"allow"}\n',
                encoding="utf-8",
            )
            result = audit(Path(tmp), orch_id)
        self.assertTrue(result["data_integrity_warning"])
        self.assertEqual(result["parse_error_count"], 1)
        self.assertEqual(result["parse_errors"][0]["line_number"], 2)
        # Valid lines still parsed
        self.assertEqual(result["total_hook_events"], 2)
        self.assertEqual(result["total_blocks"], 1)

    def test_audit_clean_logs_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_clean"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            (orch_root / "hooks").mkdir(parents=True)
            (orch_root / "hooks" / "native_hook_events.jsonl").write_text(
                '{"action":"block"}\n', encoding="utf-8",
            )
            result = audit(Path(tmp), orch_id)
        self.assertFalse(result["data_integrity_warning"])
        self.assertEqual(result["parse_error_count"], 0)

    def test_audit_picks_up_invalid_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_inv"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            orch_root.mkdir(parents=True)
            _write_jsonl(orch_root / "agent_runs_invalid.jsonl", [
                {"agent_run_id": "run_bad", "status": "fail",
                 "fail_reason": "terminal_payload_validation_error"},
            ])
            result = audit(Path(tmp), orch_id)
        self.assertEqual(result["invalid_run_count"], 1)
        self.assertIn("run_bad", result["invalid_run_ids"])


class LaunchIncidentSnapshotTests(unittest.TestCase):
    def test_audit_surfaces_persisted_snapshot_after_window_cleared(self) -> None:
        # P2: after --resume clears the active-child markers, live detection returns
        # None, but a persisted launch_incident.runtime.*.json must still be surfaced
        # so the documented later-diagnosis path works.
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_snap"
            orch_root = Path(tmp) / "workspace" / "orchestrations" / orch_id
            orch_root.mkdir(parents=True)
            # No active_child markers (window cleared) → live build returns None.
            (orch_root / "launch_incident.runtime.0123456789ab.json").write_text(
                json.dumps(
                    {
                        "schema": "launch_incident/v1",
                        "orchestration_id": orch_id,
                        "dangling_child": {
                            "agent_run_id": "f00d83b5",
                            "node_key_safe": "component__x__0.1.0",
                            "step": "compile",
                            "substep": "verify",
                            "launch_recorded_at": "2026-06-16T12:36:58Z",
                            "elapsed_seconds": 700.0,
                        },
                        "host_session_id": "b60f2e51",
                        "transcripts": {"child_transcript": {"found": False, "reason": "cleaned"}},
                        "abort_marker": {
                            "interrupted": True,
                            "interrupt_ts": "2026-06-16T12:48:47Z",
                            "interrupt_text": "[Request interrupted by user]",
                            "last_activity_ts": "2026-06-16T12:38:47Z",
                            "dead_air_seconds": 600.0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = audit(Path(tmp), orch_id)
            self.assertIsNone(result["launch_incident"])
            self.assertEqual(len(result["launch_incident_snapshots"]), 1)
            md = _render_markdown(result)
        self.assertIn("Captured incident snapshots", md)
        self.assertIn("launch_incident.runtime.0123456789ab.json", md)
        # Decisive evidence from the snapshot's abort_marker is rendered even though
        # the live transcript is gone.
        self.assertIn("[Request interrupted by user]", md)
        self.assertIn("600s", md)

    def test_audit_reports_nothing_when_no_window_and_no_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orch_id = "orch_clean"
            (Path(tmp) / "workspace" / "orchestrations" / orch_id).mkdir(parents=True)
            result = audit(Path(tmp), orch_id)
            self.assertIsNone(result["launch_incident"])
            self.assertEqual(result["launch_incident_snapshots"], [])
            md = _render_markdown(result)
        self.assertIn("no captured incident snapshots", md)


if __name__ == "__main__":
    unittest.main()
