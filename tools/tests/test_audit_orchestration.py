#!/usr/bin/env python3
"""Tests for tools/audit_orchestration.py."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import orchestration_diagnostics as diag
from tools.audit_orchestration import (
    audit,
    collect_allow_auto_approve_stats,
    collect_fix_hint_stats,
    collect_policy_block_counts,
    collect_fail_closed_timeline,
    collect_agent_run_summary,
    collect_token_cost_summary,
    collect_pure_leaf_ab_summary,
    detect_suspicious_benign_volume,
    split_substantive_and_benign,
    _render_markdown,
    _render_pure_leaf_ab,
    _render_pure_leaf_row,
    _render_incident_body,
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

    def test_legacy_policy_id_aggregates_under_current_id(self) -> None:
        # Audit-log continuity: a historical record carrying the pre-rename id
        # (enforce_guarded_apply_patch) must count under the current id so the two
        # do not split into separate buckets in retrospective aggregation.
        blocks = [
            _make_block("enforce_guarded_apply_patch"),
            _make_block("forbid_unauthorized_file_write"),
        ]
        result = collect_policy_block_counts(blocks)
        self.assertEqual(result["forbid_unauthorized_file_write"], 2)
        self.assertNotIn("enforce_guarded_apply_patch", result)


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


def _usage_rec(inp: int, out: int, cr: int, cc: int) -> dict:
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


class TokenCostSummaryTests(unittest.TestCase):
    """The parent-vs-children token breakdown — surfaces the child subagent cost
    that agent_runs.jsonl and the host transcript otherwise hide."""

    def _setup(self, tmp: str) -> tuple[Path, str]:
        repo = Path(tmp) / "repo"
        orch_id = "orch_tokens"
        root = repo / "workspace" / "orchestrations" / orch_id
        root.mkdir(parents=True)
        parent_arid = "0e750000-0000-4000-8000-000000000000"
        child_a = "aaaa1111-1111-4111-8111-111111111111"
        child_b = "bbbb2222-2222-4222-8222-222222222222"
        host_session = "hostsess"
        (root / "orchestration_meta.json").write_text(
            json.dumps(
                {
                    "orchestration_id": orch_id,
                    "orchestration_agent_run_id": parent_arid,
                }
            ),
            encoding="utf-8",
        )
        _write_jsonl(
            root / "agent_runs.jsonl",
            [
                {"agent_run_id": parent_arid, "agent_role": "orchestration", "status": "pass"},
                {"agent_run_id": child_a, "agent_role": "substep", "status": "pass"},
                {"agent_run_id": child_b, "agent_role": "substep", "status": "pass"},
            ],
        )
        home = Path(tmp) / "home"
        slug = str(repo.resolve()).replace("/", "-")
        projects = home / ".claude" / "projects" / slug
        subagents = projects / host_session / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        # Parent host transcript: 1 turn. Located by aggregate_parent_usage via the
        # `workspace/tmp/<parent_arid>` marker in its first user (launch) message.
        (projects / f"{host_session}.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": f"Start the workflow workspace/tmp/{parent_arid}",
                            },
                        }
                    ),
                    json.dumps(_usage_rec(100, 50, 2000, 0)),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        def _child(fname: str, arid: str, rec: dict) -> None:
            head = json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": (
                            f"capabilities/{arid}.json output_manifests/{arid}.json "
                            f"parent_agent_run_id {parent_arid}"
                        ),
                    },
                }
            )
            (subagents / fname).write_text(head + "\n" + json.dumps(rec) + "\n", encoding="utf-8")

        _child("agent-a.jsonl", child_a, _usage_rec(10, 10, 1000, 0))
        _child("agent-b.jsonl", child_b, _usage_rec(5, 5, 500, 0))
        return repo, orch_id

    def test_collect_token_cost_summary_attributes_parent_and_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, orch_id = self._setup(tmp)
            home = Path(tmp) / "home"
            with mock.patch.object(diag.Path, "home", return_value=home):
                result = audit(repo, orch_id)
            tcs = result["token_cost_summary"]
            self.assertTrue(tcs["available"])
            self.assertEqual(tcs["parent_total_tokens"], 100 + 50 + 2000)
            self.assertEqual(tcs["children_total_tokens"], 1020 + 510)
            self.assertEqual(tcs["node_total_tokens"], 2150 + 1530)
            # Parent arid is excluded from the child set (not an unlocatable child).
            self.assertEqual(tcs["children"]["unmatched_arids"], [])
            self.assertEqual(tcs["children"]["matched_count"], 2)
            md = _render_markdown(result)
            self.assertIn("Token cost breakdown", md)
            self.assertIn("child subagents", md)

    def test_available_and_renders_when_only_parent_locatable(self) -> None:
        # Post-cleanup audit: parent session survives, child transcripts are gone.
        # The surviving parent total must still be reported, not discarded.
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            base = home / ".claude" / "projects" / slug
            base.mkdir(parents=True, exist_ok=True)
            parent_arid = "88c4f71a-efb3-4c89-a706-9d41969cc12e"
            marker = f"workspace/tmp/{parent_arid}"
            (base / "orig.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "message": {"role": "user", "content": f"Start the workflow {marker}"}}),
                        json.dumps(_usage_rec(100, 50, 2000, 0)),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            meta = {"orchestration_agent_run_id": parent_arid}
            # No child agent_runs (only the parent): child attribution is
            # unavailable, but the parent total must still be reported.
            runs = [{"agent_run_id": parent_arid, "agent_role": "orchestration", "status": "pass"}]
            with mock.patch.object(diag.Path, "home", return_value=home):
                tcs = collect_token_cost_summary(repo, meta, runs)
            self.assertEqual(tcs["children"]["matched_count"], 0)  # no children measured
            self.assertTrue(tcs["available"])  # parent rescues availability
            self.assertEqual(tcs["parent_total_tokens"], 2150)
            self.assertEqual(tcs["children_total_tokens"], 0)
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            self.assertIn("2,150", joined)
            self.assertIn("partial", joined)
            self.assertIn("child subagents**: unavailable", joined)

    def test_prefers_persisted_usage_over_missing_transcript(self) -> None:
        # finalize_child persists each child's usage into agent_runs.jsonl; a later
        # audit must use it even when the ephemeral transcript is gone.
        from tools.audit_orchestration import collect_token_cost_summary

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            (home / ".claude" / "projects" / slug).mkdir(parents=True)  # dir exists, no transcripts
            child = "aaaa1111-1111-4111-8111-111111111111"
            runs = [
                {
                    "agent_run_id": child,
                    "agent_role": "substep",
                    "status": "pass",
                    "usage": {
                        "input_tokens": 10, "output_tokens": 10,
                        "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0,
                        "total_tokens": 1020, "assistant_turns": 5, "peak_context_tokens": 1010,
                    },
                }
            ]
            with mock.patch.object(diag.Path, "home", return_value=home):
                tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertEqual(tcs["children_total_tokens"], 1020)
            self.assertEqual(tcs["children"]["per_child"][child]["source"], "agent_runs.jsonl")
            # The {"status":"unavailable"} marker must NOT count as usage.
            runs2 = [{"agent_run_id": child, "agent_role": "substep", "status": "pass",
                      "usage": {"status": "unavailable", "reason": "x"}}]
            with mock.patch.object(diag.Path, "home", return_value=home):
                tcs2 = collect_token_cost_summary(repo, {}, runs2)
            self.assertEqual(tcs2["children"]["matched_count"], 0)

    def test_unavailable_when_nothing_matched(self) -> None:
        # ~/.claude dir present but holds no transcripts for this orchestration, and
        # no persisted usage / parent: report unavailable, not a 0-token breakdown.
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            (home / ".claude" / "projects" / slug).mkdir(parents=True)
            runs = [{"agent_run_id": "bbbb2222-2222-4222-8222-222222222222", "agent_role": "substep", "status": "pass"}]
            with mock.patch.object(diag.Path, "home", return_value=home):
                tcs = collect_token_cost_summary(repo, {}, runs)
            self.assertFalse(tcs["available"])
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            self.assertIn("unavailable", "\n".join(lines))
            self.assertNotIn("0 tokens", "\n".join(lines))

    def test_render_partial_when_only_children_locatable(self) -> None:
        # Children present, parent session not locatable (no orchestration_agent_run_id
        # in meta): the child total must still render, with the parent
        # side marked unavailable and the node total flagged partial.
        from tools.audit_orchestration import collect_token_cost_summary, _render_token_cost

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            slug = str(repo.resolve()).replace("/", "-")
            subagents = home / ".claude" / "projects" / slug / "hostsess" / "subagents"
            subagents.mkdir(parents=True, exist_ok=True)
            child = "aaaa1111-1111-4111-8111-111111111111"
            head = json.dumps(
                {"type": "user", "message": {"role": "user", "content": f"capabilities/{child}.json"}}
            )
            (subagents / "agent-a.jsonl").write_text(
                head + "\n" + json.dumps(_usage_rec(10, 10, 1000, 0)) + "\n", encoding="utf-8"
            )
            meta: dict = {}  # no parent identity → parent unavailable
            runs = [{"agent_run_id": child, "agent_role": "substep", "status": "pass"}]
            with mock.patch.object(diag.Path, "home", return_value=home):
                tcs = collect_token_cost_summary(repo, meta, runs)
            self.assertTrue(tcs["available"])
            self.assertFalse(tcs["parent"].get("found"))
            self.assertEqual(tcs["children_total_tokens"], 1020)
            lines: list[str] = []
            _render_token_cost(tcs, lines)
            joined = "\n".join(lines)
            self.assertIn("parent orchestration: unavailable", joined)
            self.assertIn("partial — parent transcript(s) unavailable", joined)
            self.assertIn("1,020", joined)

    def test_render_handles_unavailable(self) -> None:
        summary = {"available": False, "reason": "claude projects dir missing"}
        from tools.audit_orchestration import _render_token_cost

        lines: list[str] = []
        _render_token_cost(summary, lines)
        joined = "\n".join(lines)
        self.assertIn("unavailable", joined)
        self.assertIn("claude projects dir missing", joined)


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


class LegacyIncidentApiErrorRenderTests(unittest.TestCase):
    def test_renders_api_error_from_raw_tail_when_structured_field_missing(self) -> None:
        """A legacy snapshot predating the structured api_error field still carries the
        529 marker in raw_tail; audit must surface it from there."""
        incident = {
            "dangling_child": {"agent_run_id": "child-1", "step": "compile",
                               "substep": "generate"},
            "host_session_id": "host-1",
            "transcripts": {
                "child_transcript": {
                    "found": True,
                    "path": "/x.jsonl",
                    "match_method": "tool_use_id",
                    "last_activity_ts": "2026-06-17T01:17:30.724Z",
                    "last_event_type": "assistant",
                    # No structured "api_error" field (legacy snapshot) ...
                    "raw_tail": [
                        {
                            "type": "assistant",
                            "isApiErrorMessage": True,
                            "apiErrorStatus": 529,
                            "message": {"role": "assistant", "content": [
                                {"type": "text", "text": "API Error: 529 Overloaded."}]},
                        }
                    ],
                }
            },
        }
        lines: list[str] = []
        _render_incident_body(incident, lines)
        md = "\n".join(lines)
        self.assertIn("transient API error", md)
        self.assertIn("529", md)
        self.assertIn("safe to", md)


class PureLeafABSummaryTest(unittest.TestCase):
    """collect_pure_leaf_ab_summary + _render_pure_leaf_ab (Z2 M-E)."""

    ORCH = "orch_pure_ab"
    SAFE = "comp__demo__0.1.0"
    PIPELINE_ID = "demo_20260716_001"
    PIPE = f"workspace/pipelines/{SAFE}/{PIPELINE_ID}"
    SRC = f"workspace/pipelines/{SAFE}/{PIPELINE_ID}/source/src_20260716_001"

    def _reserve(self, repo: Path, *, pipeline_id: str | None = None) -> None:
        """Write the pipeline reservation `prepare_node` writes before Compile runs.

        This — NOT `orchestration_checkpoint.json` — is what discovery reads. The
        checkpoint only ever carries a non-empty `pipeline_ref` for the `validate`
        step (verified against every real orchestration in-repo), so a fixture that
        hand-builds a compile/generate entry WITH a `pipeline_ref` encodes a shape
        the runtime never produces, and would hide a generate-only run finding nothing.
        """
        res = repo / "workspace" / "orchestrations" / self.ORCH / "reservations" / self.SAFE
        res.mkdir(parents=True, exist_ok=True)
        (res / "generate.json").write_text(
            json.dumps(
                {
                    "node_key": "comp/demo@0.1.0",
                    "step": "generate",
                    # `is not None`, not `or`: an empty-string id is a case under test
                    # and `or` would silently swallow it into the default.
                    "reserved_ir_id": (
                        pipeline_id if pipeline_id is not None else self.PIPELINE_ID
                    ),
                }
            ),
            encoding="utf-8",
        )

    def _lay_out(self, repo: Path, *, executor="pure", with_metas=True) -> None:
        root = repo / "workspace" / "orchestrations" / self.ORCH
        root.mkdir(parents=True, exist_ok=True)
        (root / "orchestration_meta.json").write_text(
            json.dumps({"invocation": {"generate_executor": executor}}), encoding="utf-8"
        )
        (root / "preflight.json").write_text(
            json.dumps({"backend": "claude", "agent_version": "1.2.3 (Claude Code)"}),
            encoding="utf-8",
        )
        self._reserve(repo)
        if with_metas:
            src = repo / self.SRC
            src.mkdir(parents=True, exist_ok=True)
            (src / "bundle_meta.json").write_text(
                json.dumps(
                    {
                        "result": "pass",
                        "failure_category": None,
                        "attempts": 1,
                        "prompt_contract_version": "pure-1",
                        "per_attempt": [
                            {"agent_run_id": "g1", "model": "claude-opus-4-8", "usage": {"input_tokens": 400, "output_tokens": 900}}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (src / "verdict_meta.json").write_text(
                json.dumps(
                    {
                        "result": "pass",
                        "failure_category": None,
                        "attempts": 1,
                        "prompt_contract_version": "pure-1",
                        "per_attempt": [
                            {"agent_run_id": "v1", "model": "claude-opus-4-8", "usage": {"input_tokens": 500, "output_tokens": 30}}
                        ],
                    }
                ),
                encoding="utf-8",
            )

    def test_collect_surfaces_executor_version_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)
            meta = json.loads(
                (repo / "workspace" / "orchestrations" / self.ORCH / "orchestration_meta.json").read_text()
            )
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertTrue(out["available"])
        self.assertEqual(out["generate_executor"], "pure")
        self.assertEqual(out["agent_cli_version"], "1.2.3 (Claude Code)")
        self.assertEqual(len(out["pure_nodes"]), 1)
        node = out["pure_nodes"][0]
        self.assertEqual(node["source_dir"], self.SRC)  # repo-relative
        self.assertEqual(node["generate"]["usage_total"]["total_tokens"], 1300)
        self.assertEqual(node["verify"]["result"], "pass")

    def test_legacy_run_reports_unavailable_but_keeps_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, executor="legacy", with_metas=False)
            meta = {"invocation": {"generate_executor": "legacy"}}
            out = collect_pure_leaf_ab_summary(repo, self.ORCH, meta)
        self.assertFalse(out["available"])
        self.assertEqual(out["generate_executor"], "legacy")
        self.assertEqual(out["pure_nodes"], [])

    def test_audit_includes_pure_leaf_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)
            result = audit(repo, self.ORCH)
            self.assertIn("pure_leaf_ab_summary", result)
            self.assertTrue(result["pure_leaf_ab_summary"]["available"])
            md = _render_markdown(result)
        self.assertIn("Pure-leaf A/B metrics (Z2)", md)
        self.assertIn("generate-executor: `pure`", md)
        self.assertIn("claude --version", md)
        self.assertIn(self.SRC, md)

    def test_discovers_failed_and_rotated_source_dirs_with_no_checkpoint_at_all(self) -> None:
        # A terminally-failed generate is never checkpointed, and a cold restart
        # rotates to a fresh source dir. Discovery must find BOTH from the pipeline
        # reservation alone — this fixture writes NO orchestration_checkpoint.json,
        # which is also the real shape of a generate-only run.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = repo / "workspace" / "orchestrations" / self.ORCH
            root.mkdir(parents=True, exist_ok=True)
            (root / "preflight.json").write_text(
                json.dumps({"backend": "claude", "agent_version": "9.9"}), encoding="utf-8"
            )
            self._reserve(repo)
            # Two source dirs under the pipeline: a rotated failed attempt + the retry.
            for sid, result, cat in (("src_001", "fail", "bundle_schema_violation"), ("src_002", "pass", None)):
                sdir = repo / self.PIPE / "source" / sid
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / "bundle_meta.json").write_text(
                    json.dumps(
                        {
                            "result": result,
                            "failure_category": cat,
                            "attempts": 2,
                            "prompt_contract_version": "pure-1",
                            "per_attempt": [
                                {"agent_run_id": f"{sid}-a", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 1}},
                                {"agent_run_id": f"{sid}-b", "model": "m", "usage": {"input_tokens": 20, "output_tokens": 2}},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertTrue(out["available"])
        self.assertEqual(len(out["pure_nodes"]), 2)  # failed + retry both measured
        results = {n["source_dir"].split("/")[-1]: n["generate"]["result"] for n in out["pure_nodes"]}
        self.assertEqual(results, {"src_001": "fail", "src_002": "pass"})

    def test_provenance_strings_are_stripped_not_just_validated(self) -> None:
        # _clean_str must clean, not merely validate: these render inline into
        # markdown, so surrounding whitespace would break the line.
        from tools.audit_orchestration import _clean_str

        self.assertEqual(_clean_str("  pure  "), "pure")
        self.assertEqual(_clean_str("1.2.3 (Claude Code)\n"), "1.2.3 (Claude Code)")
        self.assertIsNone(_clean_str("   "))  # whitespace-only is absent
        self.assertIsNone(_clean_str(""))
        self.assertIsNone(_clean_str(None))
        self.assertIsNone(_clean_str(["pure"]))

    def test_wrong_typed_provenance_reported_absent(self) -> None:
        out = collect_pure_leaf_ab_summary(
            Path("/nonexistent"),
            "orch_x",
            {"invocation": {"generate_executor": ["not", "a", "string"]}},
        )
        self.assertIsNone(out["generate_executor"])
        self.assertIsNone(out["agent_cli_version"])

    def test_traversal_reserved_pipeline_id_is_skipped(self) -> None:
        # `reserved_ir_id` is JSON-sourced: a non-segment value would escape the
        # pipeline root (`repo_root / "workspace/pipelines/<safe>" / "../.."`).
        from tools.audit_orchestration import _pure_source_dirs_of

        for bad in ("..", ".", "/abs", "a/b", "../evil", ""):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                self._reserve(repo, pipeline_id=bad)
                dirs, refs = _pure_source_dirs_of(repo, self.ORCH)
            self.assertEqual(dirs, [], f"{bad!r} must not be globbed")
            # A rejected id must not count as "accepted" either, or the caller would
            # misreport it as a benign not-yet-generated run.
            self.assertEqual(refs, [], f"{bad!r} must not be an accepted ref")

    def test_compile_only_run_is_not_reported_as_a_discovery_failure(self) -> None:
        # A reserved pipeline whose source/ does not exist yet is the NORMAL state of a
        # run stopped at Compile (and of a --with-deps dependency node when the TARGET
        # stops at Compile — `dep_until_phase` follows the target). It must not be
        # reported as a discovery failure.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / self.ORCH).mkdir(parents=True)
            self._reserve(repo)
            (repo / self.PIPE).mkdir(parents=True)  # pipeline exists, no source/ yet
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertFalse(out["available"])
        self.assertIn("Generate has not produced one", out["reason"])
        self.assertNotIn("discovery found no node", out["reason"])

    def test_generate_only_run_is_measured_without_any_checkpoint(self) -> None:
        # REGRESSION: discovery previously read completed_steps[].pipeline_ref, which
        # update_checkpoint only populates for the `validate` step — so the natural A/B
        # command (`run_workflow.py <spec> generate --generate-executor pure`) measured
        # NOTHING. This fixture writes no checkpoint at all, which is that run's shape.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo)  # reservation + metas, no orchestration_checkpoint.json
            ck = repo / "workspace" / "orchestrations" / self.ORCH / "orchestration_checkpoint.json"
            self.assertFalse(ck.exists(), "fixture must have no checkpoint")
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertTrue(out["available"], "a generate-only pure run must still be measured")
        self.assertEqual(len(out["pure_nodes"]), 1)
        self.assertEqual(out["pure_nodes"][0]["generate"]["result"], "pass")

    def test_codex_backend_version_is_not_labelled_claude(self) -> None:
        # REGRESSION: `preflight.json#agent_version` holds whatever backend was probed
        # (`_probe_codex_backend` runs `codex --version`). Labelling it "claude
        # --version" reported false provenance on every codex orchestration — which
        # this section still renders, since a codex node stays legacy even under
        # --generate-executor pure.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            root = repo / "workspace" / "orchestrations" / self.ORCH
            root.mkdir(parents=True)
            (root / "preflight.json").write_text(
                json.dumps({"backend": "codex", "agent_version": "codex-cli 0.9.1"}),
                encoding="utf-8",
            )
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "legacy"}}
            )
        self.assertEqual(out["backend"], "codex")
        self.assertEqual(out["agent_cli_version"], "codex-cli 0.9.1")
        lines: list[str] = []
        _render_pure_leaf_ab(out, lines)
        md = "\n".join(lines)
        self.assertIn("codex --version: `codex-cli 0.9.1`", md)
        self.assertNotIn("claude --version", md)  # the false-provenance label

    def test_unrecorded_backend_does_not_claim_a_cli_name(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": "pure", "backend": None,
             "agent_cli_version": None, "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertNotIn("claude --version", md)
        self.assertNotIn("codex --version", md)
        self.assertIn("unrecorded", md)

    def test_legacy_source_dir_on_disk_is_filtered_out(self) -> None:
        # The `found` filter must exclude a source dir that EXISTS but carries no
        # pure metas (a legacy node under the same pipeline). The sibling
        # legacy test can't pin this: it creates no source dir at all, so discovery
        # returns nothing regardless of the filter — it would pass even with the
        # filter deleted.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._lay_out(repo, executor="legacy", with_metas=False)
            legacy_src = repo / self.PIPE / "source" / "src_legacy_001"
            legacy_src.mkdir(parents=True)
            (legacy_src / "src").mkdir()  # a real legacy source dir, no bundle/verdict meta
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "legacy"}}
            )
        self.assertFalse(out["available"], "a legacy source dir must not become a pure node")
        self.assertEqual(out["pure_nodes"], [])
        self.assertIn("no pure-leaf meta located", out["reason"])

    def test_render_keeps_distinct_models_in_attempt_order(self) -> None:
        # Dedup must be distinct-in-order (dict.fromkeys), not sorted(set(...)):
        # a repair loop that switched models must render in the order it used them.
        lines: list[str] = []
        _render_pure_leaf_row(
            "generate",
            {
                "found": True, "result": "pass", "attempts": 3, "repair_turns": 2,
                "failure_category": None, "prompt_contract_version": "pure-1",
                "usage_total": {"total_tokens": 1},
                "models": ["zeta", "alpha", "zeta"],
            },
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("model(s): zeta, alpha", md)  # first-seen order, not alphabetical
        self.assertNotIn("alpha, zeta", md)

    def test_no_reservation_reports_discovery_reason(self) -> None:
        # No pipeline reservation at all (prepare_node never ran): the one case where
        # discovery genuinely could not proceed. Must be named, not rendered as an
        # indistinguishable legacy-looking zero.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / self.ORCH).mkdir(parents=True)
            out = collect_pure_leaf_ab_summary(
                repo, self.ORCH, {"invocation": {"generate_executor": "pure"}}
            )
        self.assertFalse(out["available"])
        self.assertIn("no pipeline reservation", out["reason"])
        lines: list[str] = []
        _render_pure_leaf_ab(out, lines)
        md = "\n".join(lines)
        # Must not claim "legacy/agentic run" while the executor line says pure.
        self.assertNotIn("legacy/agentic run", md)
        self.assertIn("no pipeline reservation", md)

    def test_render_collapses_repeated_model_and_shows_contract(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {
                "available": True,
                "generate_executor": "pure",
                "backend": "claude", "agent_cli_version": "1.0",
                "pure_nodes": [
                    {
                        "source_dir": "p/source/s1",
                        "generate": {
                            "found": True, "result": "pass", "attempts": 3, "repair_turns": 2,
                            "failure_category": None, "prompt_contract_version": "pure-1",
                            "usage_total": {"input_tokens": 1, "output_tokens": 2,
                                            "cache_read_input_tokens": 3,
                                            "cache_creation_input_tokens": 4, "total_tokens": 10},
                            "models": ["m-a", "m-a", "m-a"],
                        },
                        "verify": {"found": False},
                    }
                ],
            },
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("model(s): m-a", md)
        self.assertNotIn("m-a, m-a", md)  # repeated alias collapsed
        self.assertIn("contract=`pure-1`", md)
        self.assertIn("cache_creation 4", md)  # reconciles with total 10
        self.assertIn("no verify meta recorded", md)  # not "not a pure leaf"

    def test_unrecognized_executor_is_flagged_not_read_as_legacy(self) -> None:
        # A corrupt/typo'd executor must not be silently classified as legacy: the
        # hint branches on the exact value, so an unknown one gets its own wording
        # plus a warning. The recorded value is still shown verbatim.
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": "purre",
             "backend": "claude", "agent_cli_version": "1.0", "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("generate-executor: `purre`", md)  # verbatim, not corrected
        self.assertIn("unrecognized executor value", md)
        self.assertNotIn("legacy/agentic run", md)

    def test_unrecorded_executor_does_not_claim_legacy(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": None,
             "backend": "claude", "agent_cli_version": None, "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("generate-executor: `unknown`", md)
        self.assertNotIn("legacy/agentic run", md)
        self.assertNotIn("unrecognized executor value", md)  # absent != invalid

    def test_render_legacy_notes_no_pure_node(self) -> None:
        lines: list[str] = []
        _render_pure_leaf_ab(
            {"available": False, "generate_executor": "legacy", "backend": "claude", "agent_cli_version": None, "pure_nodes": []},
            lines,
        )
        md = "\n".join(lines)
        self.assertIn("no pure-leaf node located", md)
        self.assertIn("unrecorded", md)


if __name__ == "__main__":
    unittest.main()
