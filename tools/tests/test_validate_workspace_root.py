#!/usr/bin/env python3
"""Regression tests for workspace root validation."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.validate_workspace_root import validate, validate_with_scope


class ValidateWorkspaceRootTests(unittest.TestCase):
    def _init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)

    def test_detects_forbidden_python_script_under_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            forbidden = (
                repo_root
                / "workspace"
                / "pipelines"
                / "node"
                / "pipe"
                / "execute"
                / "exec_001"
                / "problem"
                / "shallow_water2d@0.3.0"
                / "manual_writer.py"
            )
            forbidden.parent.mkdir(parents=True, exist_ok=True)
            forbidden.write_text("print('forbidden script')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("python script under workspace/ is forbidden" in v for v in violations)
            )

    def _seed_orchestration(
        self,
        repo_root: Path,
        *,
        orchestration_id: str,
        launched_arids: list[str],
        terminated_arid_status: dict[str, str] | None = None,
        orchestration_status: str | None = None,
        skip_runs_jsonl: bool = False,
    ) -> None:
        """Create launches/<arid>.request.json + agent_runs.jsonl + (optional)
        terminal entries so _live_agent_tmp_run_ids() reports the expected
        live set. orchestration_status sets orchestration_meta.json#status.

        agent_runs.jsonl is always created (empty if no terminal entries) to
        match init_orchestration's behavior. skip_runs_jsonl=True opts out for
        Adv-12 regression coverage of the missing-file failure mode.
        """
        orch_dir = repo_root / "workspace" / "orchestrations" / orchestration_id
        (orch_dir / "launches").mkdir(parents=True, exist_ok=True)
        for arid in launched_arids:
            (orch_dir / "launches" / f"{arid}.request.json").write_text(
                json.dumps({"agent_run_id": arid}), encoding="utf-8"
            )
        runs = orch_dir / "agent_runs.jsonl"
        if not skip_runs_jsonl:
            if not runs.exists():
                runs.write_text("", encoding="utf-8")
            if terminated_arid_status:
                with runs.open("a", encoding="utf-8") as fh:
                    for arid, status in terminated_arid_status.items():
                        fh.write(json.dumps({"agent_run_id": arid, "status": status}) + "\n")
        if orchestration_status is not None:
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({"status": orchestration_status}), encoding="utf-8"
            )

    def test_allows_python_script_under_workspace_tmp_for_LIVE_agent_run(self) -> None:
        """Fix 4 (post Adv-2 + Adv-6): workspace/tmp/<arid>/*.py is sanctioned
        ONLY if a launch record exists, no terminal entry has been appended,
        AND the orchestration_meta.json#status is explicitly active ("running").
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-agent-run-id-001"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_live", launched_arids=[arid],
                orchestration_status="running",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "run_record_launch.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('helper')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"Script under live workspace/tmp/<arid>/ must not be flagged: {violations}",
            )

    def test_flags_python_script_under_workspace_tmp_for_TERMINATED_agent(self) -> None:
        """Adv-2: cleanup leakage scenario — terminal entry exists in
        agent_runs.jsonl but tmp dir survives. Must be flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "terminated-agent-run-id-002"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_dead",
                launched_arids=[arid],
                terminated_arid_status={arid: "pass"},
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leftover.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v and "python script under workspace/ is forbidden" in v for v in violations),
                f"Script under terminated agent's tmp dir must be flagged: {violations}",
            )

    def test_flags_python_script_under_workspace_tmp_for_TIMEOUT_agent(self) -> None:
        """Adv-2: timeout is also a terminal status; cleanup must have run."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "timeout-agent-run-id-003"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_to",
                launched_arids=[arid],
                terminated_arid_status={arid: "timeout"},
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leftover.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Script under timeout agent's tmp dir must be flagged: {violations}",
            )

    def test_flags_python_script_when_orchestration_terminated_but_child_not_recorded(self) -> None:
        """Adv-4: orchestration crashed (or user aborted) before reaching
        record-timeout. orchestration_meta.json shows fail_closed/fail/timeout
        but the launched child has no terminal entry. The exemption must NOT
        apply — leaked scratch must be surfaced."""
        for orch_terminal in ("fail", "fail_closed", "timeout", "cancel", "blocked", "pass"):
            with self.subTest(orchestration_status=orch_terminal):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    arid = f"crashed-child-{orch_terminal}"
                    self._seed_orchestration(
                        repo_root,
                        orchestration_id=f"orch_crashed_{orch_terminal}",
                        launched_arids=[arid],
                        terminated_arid_status=None,  # <-- crash: no terminal entry for child
                        orchestration_status=orch_terminal,
                    )
                    tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
                    tmp_script.parent.mkdir(parents=True, exist_ok=True)
                    tmp_script.write_text("print('leak from crashed orch')\n", encoding="utf-8")

                    violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
                    self.assertTrue(
                        any(str(tmp_script) in v for v in violations),
                        f"orchestration_status={orch_terminal!r}: leaked tmp script must be flagged: {violations}",
                    )

    def test_flags_python_script_when_orchestration_meta_json_missing(self) -> None:
        """Adv-6: missing orchestration_meta.json must NOT keep tmp scripts
        exempt. Failing closed surfaces leakage when state is corrupt."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "no-meta-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_no_meta",
                launched_arids=[arid],
                orchestration_status=None,  # don't write orchestration_meta.json
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak with no meta')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Missing orchestration_meta.json must fail closed: {violations}",
            )

    def test_flags_python_script_when_orchestration_meta_json_corrupt(self) -> None:
        """Adv-6: malformed JSON in orchestration_meta.json must NOT keep tmp
        scripts exempt."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "corrupt-meta-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_corrupt",
                launched_arids=[arid],
                orchestration_status=None,
            )
            (repo_root / "workspace" / "orchestrations" / "orch_corrupt"
             / "orchestration_meta.json").write_text("{ this is not json", encoding="utf-8")
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak with corrupt meta')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Corrupt orchestration_meta.json must fail closed: {violations}",
            )

    def test_flags_python_script_when_orchestration_meta_json_lacks_status(self) -> None:
        """Adv-6: orchestration_meta.json present but with no `status` field
        must NOT keep tmp scripts exempt."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "no-status-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_no_status",
                launched_arids=[arid],
                orchestration_status=None,
            )
            (repo_root / "workspace" / "orchestrations" / "orch_no_status"
             / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_no_status"}), encoding="utf-8"
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('no status field')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Missing status field must fail closed: {violations}",
            )

    def test_flags_python_script_when_agent_runs_jsonl_missing(self) -> None:
        """Adv-12: a missing agent_runs.jsonl is also unhealthy.
        init_orchestration always creates this file, so an absent file
        indicates deletion or broken init. Without the ledger we cannot
        confirm 'no terminal entry', so exemption must be refused."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-with-no-runs-jsonl"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_no_runs",
                launched_arids=[arid],
                orchestration_status="running",
                skip_runs_jsonl=True,  # explicitly omit agent_runs.jsonl
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak with no runs ledger')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Missing agent_runs.jsonl must fail closed: {violations}",
            )

    def test_flags_python_script_when_agent_runs_jsonl_is_malformed(self) -> None:
        """Adv-8: a malformed NON-TRAILING line in agent_runs.jsonl indicates
        durable corruption (a complete entry was written after the broken one,
        ruling out an in-flight append). Validator must fail closed.

        Adv-15 amendment: a malformed TRAILING line is now tolerated as an
        in-flight append; that case is exercised by
        test_tolerates_truncated_last_line_in_agent_runs_jsonl below.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-but-corrupt-runs-jsonl"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_corrupt_runs",
                launched_arids=[arid],
                orchestration_status="running",
            )
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_corrupt_runs"
                / "agent_runs.jsonl"
            )
            # Sandwich a malformed line BETWEEN two complete entries so it
            # cannot be the trailing in-flight write — durable corruption.
            runs_path.write_text(
                json.dumps({"agent_run_id": arid, "status": "running"}) + "\n"
                + '{"agent_run_id": "x", "status": "pa' + "\n"  # corrupt middle line
                + json.dumps({"agent_run_id": "later", "status": "pass"}) + "\n",
                encoding="utf-8",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak from corrupt runs')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Durable corruption (non-trailing malformed line) must fail closed: {violations}",
            )

    def test_tolerates_truncated_last_line_in_agent_runs_jsonl(self) -> None:
        """Adv-15: a truncated TRAILING line is most likely a concurrent
        append in flight. Treating it as durable corruption would spuriously
        fail healthy live runs whenever the validator races a writer."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-during-append"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_concurrent",
                launched_arids=[arid],
                orchestration_status="running",
            )
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_concurrent"
                / "agent_runs.jsonl"
            )
            # Complete first line + truncated trailing line (mid-append snapshot).
            runs_path.write_text(
                json.dumps({"agent_run_id": arid, "status": "running"}) + "\n"
                + '{"agent_run_id": "next", "status": "passing-th',
                encoding="utf-8",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "scratch.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('legitimate scratch')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"Trailing truncated line must be tolerated as in-flight write: {violations}",
            )

    def test_flags_python_script_when_agent_runs_jsonl_has_non_object_line(self) -> None:
        """Adv-8: a line that parses but is not a JSON object (e.g. a stray
        array or string from a partial concatenation) is treated as corrupt."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-with-non-object"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_non_obj",
                launched_arids=[arid],
                orchestration_status="running",
            )
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_non_obj"
                / "agent_runs.jsonl"
            )
            runs_path.write_text(
                json.dumps({"agent_run_id": arid, "status": "running"}) + "\n"
                + json.dumps(["not", "an", "object"]) + "\n",
                encoding="utf-8",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Non-object line must fail closed: {violations}",
            )

    def test_flags_python_script_when_agent_runs_jsonl_unreadable(self) -> None:
        """Adv-8: agent_runs.jsonl present but unreadable (e.g. permission
        error) must fail closed. Simulated by patching read_text."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-unreadable-runs"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_unread",
                launched_arids=[arid],
                orchestration_status="running",
            )
            # Create the file so is_file() returns True.
            runs_path = (
                repo_root / "workspace" / "orchestrations" / "orch_unread"
                / "agent_runs.jsonl"
            )
            runs_path.write_text("", encoding="utf-8")
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('leak')\n", encoding="utf-8")

            from unittest.mock import patch as _patch
            real_read_text = Path.read_text

            def _selective_read_text(self, *a, **kw):
                if str(self).endswith("agent_runs.jsonl") and "orch_unread" in str(self):
                    raise OSError("simulated permission denied")
                return real_read_text(self, *a, **kw)

            with _patch.object(Path, "read_text", _selective_read_text):
                violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Unreadable agent_runs.jsonl must fail closed: {violations}",
            )

    def test_orchestration_agent_tmp_root_is_exempt_when_orchestration_running(self) -> None:
        """Adv-9: orchestration agents are not 'launched' via record-launch
        and have no launches/<arid>.request.json. Their identity lives in
        orchestration_meta.json#orchestration_agent_run_id and they own
        workspace/tmp/<orch_arid>/. While the orchestration is running, its
        own scratch must be exempt from the *.py forbidden-script scan.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_arid = "orch-agent-arid-001"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_self"
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_self",
                    "orchestration_agent_run_id": orch_arid,
                    "status": "running",
                }),
                encoding="utf-8",
            )
            # init_orchestration always creates agent_runs.jsonl; mimic that.
            (orch_dir / "agent_runs.jsonl").write_text("", encoding="utf-8")
            tmp_script = repo_root / "workspace" / "tmp" / orch_arid / "run_record_launch.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('orchestration scratch')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"Orchestration agent's own tmp script must be exempt: {violations}",
            )

    def test_orchestration_agent_tmp_root_is_flagged_when_terminated_and_cleanup_committed(self) -> None:
        """Corollary to Adv-9 + Adv-35: once the orchestration is terminated
        AND the cleanup_committed marker has been written, leaked scratch
        must be flagged. (Without the committed marker, exemption is kept
        as cleanup-pending — see test below for that case.)"""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_arid = "orch-agent-arid-002"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_done"
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_done",
                    "orchestration_agent_run_id": orch_arid,
                    "status": "fail_closed",
                }),
                encoding="utf-8",
            )
            (orch_dir / "agent_runs.jsonl").write_text("", encoding="utf-8")
            # Adv-35: write the committed marker too, simulating a normal
            # finalization where cleanup ran but the validator catches a
            # subsequent leak (e.g., a sibling tool re-creates a script
            # under workspace/tmp/<orch_arid>/ after cleanup).
            (orch_dir / "cleanup_committed").mkdir(parents=True, exist_ok=True)
            (orch_dir / "cleanup_committed" / f"{orch_arid}.json").write_text(
                json.dumps({"agent_run_id": orch_arid, "committed_at": "2026-05-09T10:00:00Z"}),
                encoding="utf-8",
            )
            tmp_script = repo_root / "workspace" / "tmp" / orch_arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('orch leak')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Terminated + committed orchestration's leaked tmp script must be flagged: {violations}",
            )

    def test_child_cleanup_pending_keeps_exemption_after_orch_finalized(self) -> None:
        """Adv-38: a child run with terminal entry but missing its own
        cleanup_committed marker must KEEP its tmp exemption even after the
        parent orchestration is fully terminated. Otherwise per-child
        recovery state is silently lost as soon as the orch finalizes."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            child_arid = "child-cleanup-pending"
            orch_arid = "orch-finalized-arid"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_finalized"
            orch_dir.mkdir(parents=True, exist_ok=True)
            # Orch is fully terminated AND committed.
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_finalized",
                    "orchestration_agent_run_id": orch_arid,
                    "status": "fail",
                }),
                encoding="utf-8",
            )
            (orch_dir / "cleanup_committed").mkdir(parents=True, exist_ok=True)
            (orch_dir / "cleanup_committed" / f"{orch_arid}.json").write_text(
                json.dumps({"agent_run_id": orch_arid, "committed_at": "2026-05-09T10:00:00Z"}),
                encoding="utf-8",
            )
            # Child has launch record + terminal entry but NO cleanup_committed.
            (orch_dir / "launches").mkdir(parents=True, exist_ok=True)
            (orch_dir / "launches" / f"{child_arid}.request.json").write_text(
                json.dumps({"agent_run_id": child_arid}), encoding="utf-8",
            )
            # Adv-39: child terminal entry must include a recent finished_at,
            # otherwise the cleanup-pending TTL ages it out immediately.
            from datetime import datetime, timezone, timedelta
            recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            (orch_dir / "agent_runs.jsonl").write_text(
                json.dumps({
                    "agent_run_id": child_arid,
                    "status": "fail",
                    "finished_at": recent,
                }) + "\n",
                encoding="utf-8",
            )
            child_tmp = repo_root / "workspace" / "tmp" / child_arid / "diagnostic.py"
            child_tmp.parent.mkdir(parents=True, exist_ok=True)
            child_tmp.write_text("print('preserve diagnostic')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(child_tmp) in v for v in violations),
                f"child cleanup-pending tmp must remain exempt after orch finalize: {violations}",
            )

    def test_cleanup_pending_arid_loses_exemption_after_ttl(self) -> None:
        """Adv-39: a cleanup-pending arid (terminal entry but no
        cleanup_committed marker) must lose exemption after the bounded
        recovery TTL — otherwise a transient cleanup refusal would hide
        leaked executable scratch indefinitely."""
        from datetime import datetime, timezone, timedelta
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "stale-cleanup-pending-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_pending_stale",
                launched_arids=[arid],
                orchestration_status="running",
            )
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_pending_stale"
            old_finished = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            (orch_dir / "agent_runs.jsonl").write_text(
                json.dumps({
                    "agent_run_id": arid,
                    "status": "fail",
                    "finished_at": old_finished,
                }) + "\n",
                encoding="utf-8",
            )
            # No cleanup_committed marker → cleanup pending, but old.
            tmp_script = repo_root / "workspace" / "tmp" / arid / "old_leak.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('aged out')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"cleanup-pending arid past TTL must be flagged: {violations}",
            )

    def test_cleanup_pending_without_finished_at_uses_runs_mtime_for_recovery_window(self) -> None:
        """H1: a terminal entry that lacks finished_at falls back to using
        agent_runs.jsonl mtime as the recovery-window start. A FRESH file
        keeps the exemption alive (recovery in progress); an OLD file
        flags it (recovery window exhausted)."""
        import os as _os
        # Fresh runs.jsonl mtime → exempt.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "no-finished-at-fresh"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_no_finish_fresh",
                launched_arids=[arid],
                orchestration_status="running",
            )
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_no_finish_fresh"
            (orch_dir / "agent_runs.jsonl").write_text(
                json.dumps({"agent_run_id": arid, "status": "fail"}) + "\n",
                encoding="utf-8",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "no_finished.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('x')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"missing finished_at + fresh runs.jsonl → recovery window active: {violations}",
            )
        # Stale runs.jsonl mtime → flagged.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "no-finished-at-stale"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_no_finish_stale",
                launched_arids=[arid],
                orchestration_status="running",
            )
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_no_finish_stale"
            (orch_dir / "agent_runs.jsonl").write_text(
                json.dumps({"agent_run_id": arid, "status": "fail"}) + "\n",
                encoding="utf-8",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "no_finished.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('x')\n", encoding="utf-8")
            past = time.time() - 7 * 86400
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{arid}.request.json",
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"missing finished_at + stale runs.jsonl → flagged: {violations}",
            )

    def test_child_truly_terminated_loses_exemption_independently_of_orch(self) -> None:
        """Adv-38 corollary: a child with BOTH terminal entry AND
        cleanup_committed loses exemption regardless of orch state."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            child_arid = "child-fully-terminated"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_active"
            orch_dir.mkdir(parents=True, exist_ok=True)
            # Orch is still active.
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_active",
                    "orchestration_agent_run_id": "orch-arid",
                    "status": "running",
                }),
                encoding="utf-8",
            )
            (orch_dir / "launches").mkdir(parents=True, exist_ok=True)
            (orch_dir / "launches" / f"{child_arid}.request.json").write_text(
                json.dumps({"agent_run_id": child_arid}), encoding="utf-8",
            )
            (orch_dir / "agent_runs.jsonl").write_text(
                json.dumps({"agent_run_id": child_arid, "status": "pass"}) + "\n",
                encoding="utf-8",
            )
            (orch_dir / "cleanup_committed").mkdir(parents=True, exist_ok=True)
            (orch_dir / "cleanup_committed" / f"{child_arid}.json").write_text(
                json.dumps({"agent_run_id": child_arid, "committed_at": "2026-05-09T10:00:00Z"}),
                encoding="utf-8",
            )
            child_tmp = repo_root / "workspace" / "tmp" / child_arid / "leaked.py"
            child_tmp.parent.mkdir(parents=True, exist_ok=True)
            child_tmp.write_text("print('post-cleanup leak')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(child_tmp) in v for v in violations),
                f"truly-terminated child must lose exemption regardless of orch: {violations}",
            )

    def test_orchestration_terminated_without_committed_marker_keeps_exemption(self) -> None:
        """Adv-35: if status is terminal but cleanup_committed marker is
        missing (cleanup pending or partial failure), exemption stays alive
        so the validator does not orphan scratch needed for diagnostics."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_arid = "orch-arid-cleanup-pending"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_pending"
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_pending",
                    "orchestration_agent_run_id": orch_arid,
                    "status": "fail",  # terminal
                }),
                encoding="utf-8",
            )
            (orch_dir / "agent_runs.jsonl").write_text("", encoding="utf-8")
            # No cleanup_committed marker → cleanup considered pending.
            tmp_script = repo_root / "workspace" / "tmp" / orch_arid / "scratch_during_cleanup.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('mid cleanup')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"Terminated-but-not-committed orch must keep exemption: {violations}",
            )

    def test_arid_collision_between_two_orchestrations_disables_exemption(self) -> None:
        """Adv-10: if two distinct orchestrations have ever launched the same
        arid, the flat workspace/tmp/<arid>/ namespace cannot disambiguate
        which orch's content lives there. Even if one orchestration is active
        and otherwise eligible, the collision disables exemption (conservative
        — surfaces leaks that an inactive sibling may have left)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            shared_arid = "collided-arid-across-orchs"
            # Orch A: running, claims arid as live.
            self._seed_orchestration(
                repo_root, orchestration_id="orch_A",
                launched_arids=[shared_arid],
                orchestration_status="running",
            )
            # Orch B: terminated, has ALSO launched the same arid in its past.
            self._seed_orchestration(
                repo_root, orchestration_id="orch_B",
                launched_arids=[shared_arid],
                terminated_arid_status={shared_arid: "fail"},
                orchestration_status="fail_closed",
            )
            tmp_script = repo_root / "workspace" / "tmp" / shared_arid / "ambiguous.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('whose data is this?')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Cross-orch arid collision must disable exemption: {violations}",
            )

    def test_arid_collision_between_two_running_orchestrations_disables_exemption(self) -> None:
        """Adv-10 stronger case: two simultaneously running orchestrations
        with the same arid still cannot share workspace/tmp/<arid>/ safely."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            shared_arid = "double-live-collision"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_X",
                launched_arids=[shared_arid],
                orchestration_status="running",
            )
            self._seed_orchestration(
                repo_root, orchestration_id="orch_Y",
                launched_arids=[shared_arid],
                orchestration_status="running",
            )
            tmp_script = repo_root / "workspace" / "tmp" / shared_arid / "race.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('which orch?')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Two-running-orch collision must disable exemption: {violations}",
            )

    def test_orchestration_agent_arid_collides_with_substep_arid_disables_exemption(self) -> None:
        """Adv-10 + Adv-9: if orch A's orchestration_agent_run_id collides with
        orch B's substep arid (both lay claim to workspace/tmp/<arid>/), the
        exemption is disabled even if both orchestrations are running."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            shared_arid = "orch-vs-substep-arid"
            # Orch A claims it as its orchestration_agent_run_id.
            orch_a = repo_root / "workspace" / "orchestrations" / "orch_A_self"
            orch_a.mkdir(parents=True, exist_ok=True)
            (orch_a / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_A_self",
                    "orchestration_agent_run_id": shared_arid,
                    "status": "running",
                }),
                encoding="utf-8",
            )
            # Orch B has launched it as a substep arid.
            self._seed_orchestration(
                repo_root, orchestration_id="orch_B_child",
                launched_arids=[shared_arid],
                orchestration_status="running",
            )
            tmp_script = repo_root / "workspace" / "tmp" / shared_arid / "shared.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('shared')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"orchestration_agent_run_id collision with substep arid must disable exemption: {violations}",
            )

    def test_flags_python_script_when_orchestration_is_stale_beyond_ttl(self) -> None:
        """Adv-17: an orchestration whose status is still 'running' but whose
        artifacts have not been updated within the TTL is presumed
        crashed/abandoned. Leaked tmp scripts must be flagged rather than
        permanently hidden behind the running status.
        """
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "stale-running-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_stale",
                launched_arids=[arid],
                orchestration_status="running",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('stale')\n", encoding="utf-8")
            # Age every artifact past the TTL.
            past = time.time() - 7 * 86400  # 7 days ago
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_stale"
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{arid}.request.json",
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Stale running orchestration past TTL must lose exemption: {violations}",
            )

    def test_active_marker_overrides_ttl_for_long_running_child(self) -> None:
        """Adv-19: a long-running child legitimately may not touch control
        artifacts for many hours. As long as active_children/<arid>.txt is
        present (proof that the child is still in-flight from the
        orchestration's perspective), the TTL must NOT revoke its tmp dir's
        exemption — otherwise validate_workspace_root spuriously fails the
        run after 24h."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "long-running-child-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_long_running",
                launched_arids=[arid],
                orchestration_status="running",
            )
            # Seed an active_children marker (record-launch would write this
            # for any backend; the seed helper only creates launches/, so we
            # add the marker manually here).
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_long_running"
            (orch_dir / "active_children").mkdir(parents=True, exist_ok=True)
            marker = orch_dir / "active_children" / f"{arid}.txt"
            marker.write_text(arid, encoding="utf-8")
            tmp_script = repo_root / "workspace" / "tmp" / arid / "still_running_helper.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('still working')\n", encoding="utf-8")
            # Age every artifact (including the marker) past the TTL.
            past = time.time() - 7 * 86400
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{arid}.request.json",
                marker,
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"Active marker presence must override TTL for in-flight child: {violations}",
            )

    def test_orchestration_agent_tmp_remains_exempt_after_ttl_with_active_child(self) -> None:
        """Adv-23: the orchestration agent never gets its own active_children
        marker (only child agents do). A long-running orchestration that
        crosses the TTL boundary must NOT lose the exemption for
        workspace/tmp/<orchestration_agent_run_id>/ — otherwise validate
        starts flagging the orch agent's own helper scripts after 24h."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_arid = "orch-agent-arid-long-running"
            child_arid = "live-child-arid"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_long_run"
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_long_run",
                    "orchestration_agent_run_id": orch_arid,
                    "status": "running",
                }),
                encoding="utf-8",
            )
            (orch_dir / "agent_runs.jsonl").write_text("", encoding="utf-8")
            (orch_dir / "launches").mkdir(parents=True, exist_ok=True)
            (orch_dir / "launches" / f"{child_arid}.request.json").write_text(
                json.dumps({"agent_run_id": child_arid}), encoding="utf-8",
            )
            (orch_dir / "active_children").mkdir(parents=True, exist_ok=True)
            child_marker = orch_dir / "active_children" / f"{child_arid}.txt"
            child_marker.write_text(child_arid, encoding="utf-8")
            # Long-running child: marker file is old (created at launch),
            # but its tmp dir is being actively written. The Adv-29 freshness
            # check accepts this via the tmp-dir mtime path.
            child_tmp_scratch = repo_root / "workspace" / "tmp" / child_arid / "current_work.log"
            child_tmp_scratch.parent.mkdir(parents=True, exist_ok=True)
            child_tmp_scratch.write_text("still working\n", encoding="utf-8")
            # Orchestration agent's own helper script under its tmp dir.
            orch_tmp = repo_root / "workspace" / "tmp" / orch_arid / "run_record_launch.py"
            orch_tmp.parent.mkdir(parents=True, exist_ok=True)
            orch_tmp.write_text("print('orch helper')\n", encoding="utf-8")
            # Age the orchestration's CONTROL artifacts past TTL but leave
            # the child's tmp scratch fresh (proof-of-life for the long run).
            past = time.time() - 7 * 86400
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{child_arid}.request.json",
                child_marker,
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(orch_tmp) in v for v in violations),
                f"orchestration agent's own tmp script must remain exempt past TTL when a "
                f"child marker still exists with fresh tmp activity: {violations}",
            )

    def test_ttl_bypass_is_per_arid_not_orchestration_wide(self) -> None:
        """Adv-22: when TTL is exceeded, a SINGLE leaked active_children
        marker must NOT whitelist unrelated arids in the same orchestration.
        Only arids whose own marker still exists may remain exempt."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid_alive = "still-running-arid"
            arid_leaked = "leaked-no-marker-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_partial",
                launched_arids=[arid_alive, arid_leaked],
                orchestration_status="running",
            )
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_partial"
            (orch_dir / "active_children").mkdir(parents=True, exist_ok=True)
            # Only arid_alive has its marker present; arid_leaked's was
            # cleared (e.g., by deactivate-child) but record-agent-run /
            # record-timeout never ran to clean its tmp dir.
            (orch_dir / "active_children" / f"{arid_alive}.txt").write_text(
                arid_alive, encoding="utf-8",
            )
            tmp_alive = repo_root / "workspace" / "tmp" / arid_alive / "scratch_alive.py"
            tmp_alive.parent.mkdir(parents=True, exist_ok=True)
            tmp_alive.write_text("print('alive')\n", encoding="utf-8")
            tmp_leaked = repo_root / "workspace" / "tmp" / arid_leaked / "leaked.py"
            tmp_leaked.parent.mkdir(parents=True, exist_ok=True)
            tmp_leaked.write_text("print('leaked')\n", encoding="utf-8")
            # Age all artifacts past the TTL.
            past = time.time() - 7 * 86400
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{arid_alive}.request.json",
                orch_dir / "launches" / f"{arid_leaked}.request.json",
                orch_dir / "active_children" / f"{arid_alive}.txt",
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            # arid_alive: marker present → still exempt
            self.assertFalse(
                any(str(tmp_alive) in v for v in violations),
                f"alive arid with marker must remain exempt: {violations}",
            )
            # arid_leaked: no marker, TTL exceeded → flagged
            self.assertTrue(
                any(str(tmp_leaked) in v for v in violations),
                f"leaked arid without marker must be flagged: {violations}",
            )

    def test_symlink_outside_tmp_pointing_into_tmp_does_not_get_exempted(self) -> None:
        """Adv-32: a symlink at workspace/plans/.../helper.py whose target
        resolves into workspace/tmp/<live-arid>/ must NOT be exempted. The
        validation must judge by where the file appears in the workspace
        tree (lexical path), not where it dereferences."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-arid-target"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_symlink_attack",
                launched_arids=[arid],
                orchestration_status="running",
            )
            (repo_root / "workspace" / "orchestrations" / "orch_symlink_attack"
             / "active_children").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace" / "orchestrations" / "orch_symlink_attack"
             / "active_children" / f"{arid}.txt").write_text(arid, encoding="utf-8")
            tmp_target = repo_root / "workspace" / "tmp" / arid / "helper.py"
            tmp_target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target.write_text("print('inside tmp')\n", encoding="utf-8")
            # Symlink at workspace/plans/.../sneaky.py → tmp_target.
            sneaky_dir = repo_root / "workspace" / "plans" / "node" / "src"
            sneaky_dir.mkdir(parents=True, exist_ok=True)
            sneaky = sneaky_dir / "sneaky.py"
            try:
                _os.symlink(tmp_target, sneaky)
            except OSError:
                self.skipTest("symlink not supported on this filesystem")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(sneaky) in v for v in violations),
                f"symlink at non-tmp path must NOT inherit tmp exemption: {violations}",
            )
            # The legitimate tmp_target stays exempt.
            self.assertFalse(
                any(str(tmp_target) in v for v in violations),
                f"legitimate tmp script must remain exempt: {violations}",
            )

    def test_symlinked_descendant_inside_tmp_does_not_get_exempted(self) -> None:
        """Adv-32: even a symlink that itself lives lexically under
        workspace/tmp/<arid>/ but whose target is elsewhere must NOT be
        exempted (defense in depth — symlink chains are forbidden)."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-arid-symlinked-descendant"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_symlink_descendant",
                launched_arids=[arid],
                orchestration_status="running",
            )
            (repo_root / "workspace" / "orchestrations" / "orch_symlink_descendant"
             / "active_children").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace" / "orchestrations" / "orch_symlink_descendant"
             / "active_children" / f"{arid}.txt").write_text(arid, encoding="utf-8")
            external = repo_root / "external_outside_workspace"
            external.mkdir()
            external_target = external / "actual.py"
            external_target.write_text("print('outside')\n", encoding="utf-8")
            tmp_dir = repo_root / "workspace" / "tmp" / arid
            tmp_dir.mkdir(parents=True, exist_ok=True)
            symlinked = tmp_dir / "symlinked.py"
            try:
                _os.symlink(external_target, symlinked)
            except OSError:
                self.skipTest("symlink not supported on this filesystem")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(symlinked) in v for v in violations),
                f"symlinked descendant under tmp/<arid>/ must NOT be exempted: {violations}",
            )

    def test_orch_tmp_freshness_keeps_exemption_after_ttl_without_active_child(self) -> None:
        """Adv-31: between child launches (or during parent-only recovery
        work), active_children/ can legitimately be empty while the orch
        agent itself is still writing scratch. Old TTL-exceeded control
        artifacts must NOT cause the validator to flag the orch tmp scripts."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_arid = "orch-agent-arid-no-active-child"
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_no_active"
            orch_dir.mkdir(parents=True, exist_ok=True)
            (orch_dir / "orchestration_meta.json").write_text(
                json.dumps({
                    "orchestration_id": "orch_no_active",
                    "orchestration_agent_run_id": orch_arid,
                    "status": "running",
                }),
                encoding="utf-8",
            )
            (orch_dir / "agent_runs.jsonl").write_text("", encoding="utf-8")
            (orch_dir / "active_children").mkdir(parents=True, exist_ok=True)
            # Orch agent's own scratch dir with a FRESH script.
            orch_tmp_dir = repo_root / "workspace" / "tmp" / orch_arid
            orch_tmp_dir.mkdir(parents=True, exist_ok=True)
            orch_helper = orch_tmp_dir / "still_recovering.py"
            orch_helper.write_text("print('parent recovery')\n", encoding="utf-8")
            # Age control artifacts past TTL but leave the orch tmp fresh.
            past = time.time() - 7 * 86400
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(orch_helper) in v for v in violations),
                f"orch tmp script must remain exempt while orch tmp is fresh, "
                f"even when active_children/ is empty: {violations}",
            )

    def test_stale_active_marker_with_old_tmp_loses_exemption_after_ttl(self) -> None:
        """Adv-29: a per-arid marker whose mtime AND whose tmp dir are both
        past TTL is treated as stale. Without this, a crashed orchestration's
        leaked marker would whitelist its tmp scripts forever."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "stale-but-marker-still-there"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_stale_marker",
                launched_arids=[arid],
                orchestration_status="running",
            )
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_stale_marker"
            (orch_dir / "active_children").mkdir(parents=True, exist_ok=True)
            marker = orch_dir / "active_children" / f"{arid}.txt"
            marker.write_text(arid, encoding="utf-8")
            tmp_script = repo_root / "workspace" / "tmp" / arid / "leaked.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('crashed orch leak')\n", encoding="utf-8")
            # Age EVERYTHING — the marker AND the tmp scripts. No fresh
            # activity remains anywhere → presumed crashed.
            past = time.time() - 7 * 86400
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{arid}.request.json",
                marker,
                tmp_script,
                tmp_script.parent,
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Stale marker + stale tmp must NOT keep exemption: {violations}",
            )

    def test_active_marker_absent_after_ttl_still_flags(self) -> None:
        """Adv-19 corollary: when no active marker exists AND TTL exceeded,
        the orchestration is presumed truly stale → flagged (Adv-17 base behavior).
        """
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "abandoned-child-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_abandoned",
                launched_arids=[arid],
                orchestration_status="running",
            )
            # Note: no active_children/ marker created.
            tmp_script = repo_root / "workspace" / "tmp" / arid / "abandoned.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('abandoned')\n", encoding="utf-8")
            past = time.time() - 7 * 86400
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_abandoned"
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{arid}.request.json",
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"No active marker + stale → flagged: {violations}",
            )

    def test_freshness_ttl_can_be_overridden_via_env(self) -> None:
        """Adv-17: METDSL_ORCH_LIVENESS_TTL_SECONDS controls the TTL. A very
        large override keeps even very-old orchestrations live (operator
        opt-out for known long-running batch workloads)."""
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "old-but-flagged-as-live"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_long",
                launched_arids=[arid],
                orchestration_status="running",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "long_helper.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('long')\n", encoding="utf-8")
            past = time.time() - 7 * 86400
            orch_dir = repo_root / "workspace" / "orchestrations" / "orch_long"
            for p in [
                orch_dir / "orchestration_meta.json",
                orch_dir / "agent_runs.jsonl",
                orch_dir / "launches" / f"{arid}.request.json",
            ]:
                if p.exists():
                    _os.utime(p, (past, past))

            with patch.dict(_os.environ, {"METDSL_ORCH_LIVENESS_TTL_SECONDS": "31536000"}, clear=False):
                violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"Override must keep old orchestration treated as live: {violations}",
            )

    def test_live_arid_under_running_orchestration_remains_exempt(self) -> None:
        """Sanity for Adv-4: explicit running status keeps the exemption active."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            arid = "live-running-arid"
            self._seed_orchestration(
                repo_root, orchestration_id="orch_running",
                launched_arids=[arid],
                orchestration_status="running",
            )
            tmp_script = repo_root / "workspace" / "tmp" / arid / "scratch.py"
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('live')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any(str(tmp_script) in v for v in violations),
                f"Live tmp script under running orchestration must remain exempt: {violations}",
            )

    def test_flags_python_script_under_workspace_tmp_for_UNTRACKED_agent(self) -> None:
        """Adv-2: tmp dir with a plausible-looking name but no launch record
        (e.g., manually created or from a deleted orchestration) must be flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            tmp_script = (
                repo_root / "workspace" / "tmp" / "untracked-arid-with-no-launch" / "stale.py"
            )
            tmp_script.parent.mkdir(parents=True, exist_ok=True)
            tmp_script.write_text("print('stale')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(tmp_script) in v for v in violations),
                f"Untracked tmp dir's script must be flagged: {violations}",
            )

    def test_still_rejects_python_script_directly_under_workspace_tmp(self) -> None:
        """workspace/tmp/foo.py (not under <agent_run_id>/) is still rejected:
        such a file indicates accidental misplacement, not sanctioned scratch."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            misplaced = repo_root / "workspace" / "tmp" / "stray.py"
            misplaced.parent.mkdir(parents=True, exist_ok=True)
            misplaced.write_text("print('stray')\n", encoding="utf-8")

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(misplaced) in v and "python script under workspace/ is forbidden" in v for v in violations)
            )

    def test_detects_quality_check_script_under_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            gen_qc = (
                repo_root
                / "workspace"
                / "pipelines"
                / "node"
                / "pipe"
                / "generate"
                / "gen_001"
                / "src"
                / "quality_check.py"
            )
            build_qc = (
                repo_root
                / "workspace"
                / "pipelines"
                / "node"
                / "pipe"
                / "build"
                / "build_001"
                / "bin"
                / "quality_check.py"
            )
            gen_qc.parent.mkdir(parents=True, exist_ok=True)
            build_qc.parent.mkdir(parents=True, exist_ok=True)
            gen_qc.write_text("print('ok')\n", encoding="utf-8")
            build_qc.write_text("print('ok')\n", encoding="utf-8")

            violations, created_workspace = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any(str(gen_qc) in v and "python script under workspace/ is forbidden" in v for v in violations)
            )
            self.assertTrue(
                any(str(build_qc) in v and "python script under workspace/ is forbidden" in v for v in violations)
            )
            self.assertFalse(created_workspace)

    def test_write_scope_detects_outside_workspace_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._init_git_repo(repo_root)

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Generate",
                node_key="problem/shallow_water2d@0.3.0",
                pipeline_id="pipe_001",
            )
            self.assertEqual(violations, [])

            outside = repo_root / "tools" / "outside_change.txt"
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text("forbidden\n", encoding="utf-8")

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Generate",
                node_key="problem/shallow_water2d@0.3.0",
                pipeline_id="pipe_001",
            )
            self.assertTrue(
                any("write_scope_violation detected outside workspace" in v for v in violations)
            )

    def test_write_scope_allows_workspace_only_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._init_git_repo(repo_root)

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Execute",
                node_key="component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                pipeline_id="pipe_001",
            )
            self.assertEqual(violations, [])

            inside = repo_root / "workspace" / "pipelines" / "node" / "file.txt"
            inside.parent.mkdir(parents=True, exist_ok=True)
            inside.write_text("allowed\n", encoding="utf-8")

            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Execute",
                node_key="component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0",
                pipeline_id="pipe_001",
            )
            self.assertFalse(
                any("write_scope_violation detected outside workspace" in v for v in violations)
            )

    def test_write_scope_fails_closed_when_git_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            violations, _ = validate_with_scope(
                repo_root=repo_root,
                workspace_root="workspace",
                write_scope_baseline="workspace/write_scope_baseline.json",
                stage="Generate",
                node_key="problem/shallow_water2d@0.3.0",
                pipeline_id="pipe_001",
            )
            self.assertTrue(
                any("write_scope baseline capture failed" in v for v in violations)
            )

    def test_rejects_noncanonical_workspace_root_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace/runs/sample")
            self.assertTrue(
                any("workspace_root must be exactly 'workspace'" in v for v in violations)
            )

    def test_detects_noncanonical_top_level_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            noncanonical_dir = repo_root / "workspace" / "custom_output_root" / "trial_001"
            noncanonical_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("non-canonical workspace directory name" in v for v in violations)
            )

    def test_allows_orchestrations_top_level_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orchestration_dir = repo_root / "workspace" / "orchestrations" / "orch_001"
            orchestration_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(
                any("non-canonical workspace directory name" in v for v in violations)
            )

    def test_allows_plan_dependency_ref_to_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            request = repo_root / "workspace" / "orchestrations" / "orch_001" / "launches" / "run.request.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(
                json.dumps(
                    {
                        "step": "plan",
                        "dependency_ref": "spec/component/example/deps.yaml",
                    }
                ),
                encoding="utf-8",
            )

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("dependency_ref" in v for v in violations))

    def test_rejects_generate_dependency_ref_to_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            request = repo_root / "workspace" / "orchestrations" / "orch_001" / "launches" / "run.request.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(
                json.dumps(
                    {
                        "step": "generate",
                        "dependency_ref": "spec/component/example/deps.yaml",
                    }
                ),
                encoding="utf-8",
            )

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("generate dependency_ref must start with workspace/" in v for v in violations)
            )

    def test_rejects_plan_dependency_ref_outside_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            request = repo_root / "workspace" / "orchestrations" / "orch_001" / "launches" / "run.request.json"
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(
                json.dumps(
                    {
                        "step": "plan",
                        "dependency_ref": "workspace/plans/example/dependency.resolved.yaml",
                    }
                ),
                encoding="utf-8",
            )

            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("Plan dependency_ref must be spec/.../deps.yaml" in v for v in violations)
            )

    def test_detects_invalid_node_key_safe_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            invalid_node = repo_root / "workspace" / "plans" / "shallow_water2d"
            invalid_node.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("invalid node_key_safe directory name" in v for v in violations)
            )

    def test_detects_invalid_plan_or_pipeline_id_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            node_safe = "problem__shallow_water2d__0.3.0"
            invalid_plan = repo_root / "workspace" / "plans" / node_safe / "plan_001"
            invalid_pipeline = repo_root / "workspace" / "pipelines" / node_safe / "pipeline_001"
            invalid_plan.mkdir(parents=True, exist_ok=True)
            invalid_pipeline.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(
                any("invalid plans id directory name" in v or "invalid pipelines id directory name" in v for v in violations)
            )

    def test_allows_valid_uuid_subdirectory_under_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            uuid_dir = repo_root / "workspace" / "tmp" / "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            uuid_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("workspace/tmp" in v for v in violations))

    def test_allows_non_uuid_but_runtime_safe_agent_run_id_under_tmp(self) -> None:
        """IDs like step_run_001 are accepted by runtime (_AGENT_RUN_ID_RE) and must also
        pass workspace validation — both patterns must be consistent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            for safe_id in ["step_run_001", "orch-run-abc", "substep123"]:
                dir_path = repo_root / "workspace" / "tmp" / safe_id
                dir_path.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertFalse(any("invalid workspace/tmp/ subdirectory name" in v for v in violations))

    def test_rejects_dotted_subdirectory_under_tmp(self) -> None:
        """Names starting with '.' or containing '.' are not valid agent_run_ids."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            bad_dir = repo_root / "workspace" / "tmp" / "has.dot"
            bad_dir.mkdir(parents=True, exist_ok=True)
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("invalid workspace/tmp/ subdirectory name" in v for v in violations))

    def test_rejects_file_directly_under_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "workspace" / "tmp").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace" / "tmp" / "stray.txt").write_text("x", encoding="utf-8")
            violations, _ = validate(repo_root=repo_root, workspace_root="workspace")
            self.assertTrue(any("non-directory entry directly under workspace/tmp/" in v for v in violations))


if __name__ == "__main__":
    unittest.main()
