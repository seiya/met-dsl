#!/usr/bin/env python3
"""Tests for workflow startup bootstrap script."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import run_workflow
from tools.validate_pipeline_semantics import _BUNDLED_SHAPE_EXPR_SCHEMA_PATH


def _seed_shape_expr_schema_into(repo_root: Path) -> None:
    """Copy the validator-bundled shape_expr.schema.json into a tmp repo so
    `run_workflow.main()`'s startup assertion (canonical schema must exist
    at <repo_root>/spec/schema/ir/shape_expr.schema.json) passes for tests
    that exercise normal main() flows. Tests that intentionally exercise the
    missing-schema path must NOT call this helper."""
    target = repo_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_BUNDLED_SHAPE_EXPR_SCHEMA_PATH.read_bytes())


class RunWorkflowTests(unittest.TestCase):
    def test_collect_failure_analysis_includes_unauthorized_write_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_vio"
            violations = orch_root / "violations"
            violations.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_vio", "status": "fail"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (violations / "run_001.unauthorized_write_violation.json").write_text(
                json.dumps(
                    {
                        "agent_run_id": "run_001",
                        "unauthorized_paths": ["workspace/pipelines/x/test3.tmp"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_vio")
            self.assertEqual(len(analysis.get("unauthorized_write_violations", [])), 1)
            decisions = analysis.get("recommended_retry_decisions", [])
            self.assertTrue(isinstance(decisions, list) and decisions)
            self.assertEqual(decisions[0].get("repair_strategy"), "restart")
            self.assertIn("unauthorized_write_violation", str(decisions[0].get("repair_reason")))

    def test_collect_failure_analysis_excludes_superseded_nonpass_runs(self) -> None:
        """A terminal-nonpass agent_run that a *later* same-(node,step,substep) run
        resolved to pass must not be reported as the workflow failure (audit:
        orch_20260615T095217Z_74450292 — a judge timeout superseded by a passing
        re-run produced a false workflow_failed). A genuinely unresolved failure
        (no later pass for its key) is still selected."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_sup"
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_sup", "status": "pass"}, ensure_ascii=False),
                encoding="utf-8",
            )
            node = "component/x@0.1.0"
            rows = [
                # judge timeout, then a later passing judge re-run of the same key
                {"agent_run_id": "judge_to", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "timeout"},
                {"agent_run_id": "judge_ok", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "pass"},
                # genuinely unresolved failure: no later pass for its key
                {"agent_run_id": "build_fail", "node_key": node, "step": "build",
                 "substep": "", "status": "fail"},
            ]
            (orch_root / "agent_runs.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_sup")
            failed = analysis.get("failed_agent_run")
            self.assertIsNotNone(failed)
            # The superseded judge timeout must NOT be the reported failure.
            self.assertEqual(failed.get("agent_run_id"), "build_fail")

    def test_collect_failure_analysis_none_when_all_nonpass_superseded(self) -> None:
        """When every terminal-nonpass run was resolved by a later passing re-run of
        the same key, failed_agent_run is None (the run materially passed)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_allok"
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_allok", "status": "pass"}, ensure_ascii=False),
                encoding="utf-8",
            )
            node = "component/x@0.1.0"
            rows = [
                {"agent_run_id": "verify_blocked", "node_key": node, "step": "generate",
                 "substep": "verify", "status": "blocked"},
                {"agent_run_id": "verify_ok", "node_key": node, "step": "generate",
                 "substep": "verify", "status": "pass"},
                {"agent_run_id": "judge_to", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "timeout"},
                {"agent_run_id": "judge_ok", "node_key": node, "step": "validate",
                 "substep": "judge", "status": "pass"},
            ]
            (orch_root / "agent_runs.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_allok")
            self.assertIsNone(analysis.get("failed_agent_run"))

    def test_is_valid_failure_analysis_accepts_launch_incident_refs_only(self) -> None:
        """In the degraded dangling-launch path the incident ref is the sole evidence
        (no reason_code/detail, no failed_agent_run). It must count as evidence so the
        canonical failure_analysis.json is not misclassified as stale (Codex P3)."""
        obj = {
            "orchestration_id": "orch_x",
            "status": "fail",
            "orchestration_agent_run_id": "orch_arid_1",
            "reason_code": None,
            "reason_detail": None,
            "failed_agent_run": None,
            "failed_step_results": [],
            "recommended_retry_decisions": [],
            "launch_reply_tail": "",
            "agent_summary_tail": "",
            "launch_incident_refs": [
                "workspace/orchestrations/orch_x/launch_incident.runtime.0123456789ab.json"
            ],
        }
        self.assertTrue(
            run_workflow._is_valid_failure_analysis(
                obj, "orch_x", orchestration_agent_run_id="orch_arid_1"
            )
        )
        # With no evidence at all (empty incident refs too), it is invalid.
        obj_no_evidence = {**obj, "launch_incident_refs": []}
        self.assertFalse(
            run_workflow._is_valid_failure_analysis(
                obj_no_evidence, "orch_x", orchestration_agent_run_id="orch_arid_1"
            )
        )

    def test_collect_failure_analysis_includes_launch_incident_refs(self) -> None:
        """A `launch_incident.runtime.*.json` snapshot is linked from failure_analysis."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            orch_root = repo_root / "workspace" / "orchestrations" / "orch_inc"
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps({"orchestration_id": "orch_inc", "status": "fail"}, ensure_ascii=False),
                encoding="utf-8",
            )
            snap = orch_root / "launch_incident.runtime.0123456789ab.json"
            snap.write_text(json.dumps({"schema": "launch_incident/v1"}), encoding="utf-8")
            analysis = run_workflow._collect_failure_analysis(repo_root, "orch_inc")
            self.assertEqual(
                analysis.get("launch_incident_refs"),
                ["workspace/orchestrations/orch_inc/launch_incident.runtime.0123456789ab.json"],
            )

    def test_discover_source_dependency_ref_from_file_spec_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec" / "problem"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "test.md").write_text("spec\n", encoding="utf-8")
            (spec_dir / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")

            dep_ref = run_workflow._discover_source_dependency_ref(repo_root, "spec/problem/test.md")
            self.assertEqual(dep_ref, "spec/problem/deps.yaml")

    def test_discover_source_dependency_ref_from_directory_spec_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec" / "problem"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")

            dep_ref = run_workflow._discover_source_dependency_ref(repo_root, "spec/problem")
            self.assertEqual(dep_ref, "spec/problem/deps.yaml")

    def test_discover_source_dependency_ref_from_spec_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "task.md").write_text("spec\n", encoding="utf-8")
            (spec_dir / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")

            dep_ref = run_workflow._discover_source_dependency_ref(repo_root, "spec/task.md")
            self.assertEqual(dep_ref, "spec/deps.yaml")

    def test_discover_source_dependency_ref_rejects_missing_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            spec_dir = repo_root / "spec" / "problem"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "test.md").write_text("spec\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                run_workflow._discover_source_dependency_ref(repo_root, "spec/problem/test.md")

    def test_validate_source_dependency_ref_rejects_non_spec_deps_path(self) -> None:
        with self.assertRaises(ValueError):
            run_workflow._validate_source_dependency_ref("workspace/ir/x/spec.ir.yaml")

    def test_normalize_phase_accepts_known_values(self) -> None:
        self.assertEqual(run_workflow._normalize_phase("compile"), "Compile")
        self.assertEqual(run_workflow._normalize_phase("VALIDATE"), "Validate")

    def test_normalize_phase_rejects_unknown_value(self) -> None:
        with self.assertRaises(ValueError):
            run_workflow._normalize_phase("spec")

    def test_new_orchestration_id_prefix(self) -> None:
        value = run_workflow._new_orchestration_id()
        self.assertTrue(value.startswith("orch_"))

    def test_preflight_pass_conditions(self) -> None:
        ok, detail = run_workflow._ensure_preflight_pass(
            {
                "status": "pass",
                "can_launch_step_agents": True,
                "can_launch_substep_agents": True,
            }
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "pass")

    def test_preflight_fail_conditions(self) -> None:
        ok, detail = run_workflow._ensure_preflight_pass(
            {
                "status": "fail",
                "can_launch_step_agents": False,
                "can_launch_substep_agents": True,
            }
        )
        self.assertFalse(ok)
        self.assertIn("status='fail'", detail)
        self.assertIn("can_launch_step_agents=False", detail)

    def test_prompt_contains_required_inputs(self) -> None:
        text = run_workflow._build_orchestration_prompt(
            orchestration_id="orch_test",
            orchestration_agent_run_id="run_orch_001",
            spec_ref="spec/problem/sample.md",
            source_dependency_ref="spec/problem/deps.yaml",
            until_phase="Validate",
            workflow_mode="dev",
        )
        self.assertIn("orch_test", text)
        self.assertIn("run_orch_001", text)
        self.assertIn("spec/problem/sample.md", text)
        self.assertIn("Validate", text)
        self.assertIn("workflow_mode: `dev`", text)
        self.assertIn("dependency_ref: `spec/problem/deps.yaml`", text)
        self.assertNotIn("(not specified)", text)
        self.assertIn("METDSL_WORKFLOW_MODE=1", text)
        self.assertIn("If the information needed to start is insufficient, stop immediately", text)
        self.assertIn("issue_severity", text)

    def test_parse_args_defaults(self) -> None:
        ns = run_workflow._parse_args(["spec/problem.md", "generate"])
        # --mode / --llm default to None so main() can tell "omitted" from
        # "explicitly passed"; the historical codex/dev defaults are applied in main().
        self.assertIsNone(ns.mode)
        self.assertIsNone(ns.llm)
        self.assertFalse(ns.resume)
        self.assertTrue(ns.invoke_llm)

    def test_parse_args_allows_omitted_positionals_for_resume(self) -> None:
        ns = run_workflow._parse_args(["--resume", "--no-invoke-llm"])
        self.assertTrue(ns.resume)
        self.assertIsNone(ns.spec_ref)
        self.assertIsNone(ns.until_phase)

    def test_parse_args_supports_no_invoke_flag(self) -> None:
        ns = run_workflow._parse_args(
            [
                "spec/problem.md",
                "generate",
                "--no-invoke-llm",
            ]
        )
        self.assertFalse(ns.invoke_llm)

    def test_launch_command_for_codex_uses_exec_subcommand(self) -> None:
        command, stdin_text = run_workflow._launch_command_and_input(
            llm="codex",
            llm_command="codex",
            prompt_text="run workflow",
        )
        self.assertEqual(command, ["codex", "exec", "run workflow"])
        self.assertIsNone(stdin_text)

    def test_launch_command_for_claude_uses_print_flag(self) -> None:
        command, stdin_text = run_workflow._launch_command_and_input(
            llm="claude",
            llm_command="claude",
            prompt_text="run workflow",
        )
        self.assertEqual(command, ["claude", "-p", "run workflow"])
        self.assertIsNone(stdin_text)

    def test_prompt_params_roundtrip(self) -> None:
        # The resume extractor must recover until_phase/mode/spec_ref from the
        # exact text emitted by _build_orchestration_prompt(). This pins the two
        # functions together so a prompt wording change that breaks resume fails here.
        for until_phase, mode in (("Build", "dev"), ("Validate", "prod"), ("Compile", "dev")):
            prompt = run_workflow._build_orchestration_prompt(
                orchestration_id="orch_x",
                orchestration_agent_run_id="arid_x",
                spec_ref="spec/problem/test.md",
                source_dependency_ref="spec/problem/deps.yaml",
                until_phase=until_phase,
                workflow_mode=mode,
            )
            extracted = run_workflow._extract_prompt_params(prompt)
            self.assertEqual(extracted.get("until_phase"), until_phase)
            self.assertEqual(extracted.get("mode"), mode)
            self.assertEqual(extracted.get("spec_ref"), "spec/problem/test.md")

    def test_prompt_params_recovers_legacy_japanese_start_prompt(self) -> None:
        # Backward compatibility: an orchestration.start.prompt.txt written before
        # the English translation used the Japanese "終了 phase:" label. Resume must
        # still recover until_phase from such persisted prompts.
        legacy_prompt = (
            "target_phases: `compile, generate`（終了 phase: `generate`）\n"
            "workflow_mode: `dev`\n"
            "target_spec_ref: `spec/problem/test.md`\n"
        )
        extracted = run_workflow._extract_prompt_params(legacy_prompt)
        self.assertEqual(extracted.get("until_phase"), "generate")
        self.assertEqual(extracted.get("mode"), "dev")
        self.assertEqual(extracted.get("spec_ref"), "spec/problem/test.md")

    def _seed_resumable_orchestration(
        self,
        repo_root: Path,
        orchestration_id: str,
        *,
        spec_ref: str,
        until_phase: str,
        mode: str,
        backend: str,
        started_at: str = "2026-01-01T00:00:00.000000Z",
        source_dependency_ref: str = "spec/problem/deps.yaml",
        probe_command: str | None = None,
    ) -> None:
        """Create the on-disk artifacts a resume recovers params from."""
        orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
        (orch_root / "launches").mkdir(parents=True, exist_ok=True)
        dep_ref = source_dependency_ref
        (orch_root / "orchestration_meta.json").write_text(
            json.dumps(
                {
                    "orchestration_id": orchestration_id,
                    "status": "fail",
                    "started_at": started_at,
                    "spec_ref": spec_ref,
                    "source_dependency_ref": dep_ref,
                    "orchestration_agent_run_id": "orch_agent_prev",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (orch_root / "preflight.json").write_text(
            json.dumps(
                {
                    "status": "pass",
                    "backend": backend,
                    "probe_command": probe_command if probe_command is not None else backend,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        prompt = run_workflow._build_orchestration_prompt(
            orchestration_id=orchestration_id,
            orchestration_agent_run_id="orch_agent_prev",
            spec_ref=spec_ref,
            source_dependency_ref=dep_ref,
            until_phase=until_phase,
            workflow_mode=mode,
        )
        (orch_root / "launches" / "orchestration.start.prompt.txt").write_text(
            prompt, encoding="utf-8"
        )

    def _seed_spec_tree(self, repo_root: Path) -> None:
        _seed_shape_expr_schema_into(repo_root)
        (repo_root / "tools").mkdir(parents=True, exist_ok=True)
        (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
        (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")

    def _run_main_with_fake_runtime(
        self, argv: list[str]
    ) -> tuple[int, dict, list[list[str]]]:
        observed_calls: list[list[str]] = []

        def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(args)
            if args[0] == "init":
                return run_workflow.RuntimeResult(
                    payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_002"},
                    raw_stdout="{}",
                )
            if args[0] == "preflight":
                return run_workflow.RuntimeResult(
                    payload={
                        "status": "pass",
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                    },
                    raw_stdout="{}",
                )
            return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

        original = run_workflow._runtime_command
        buf = io.StringIO()
        try:
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            with redirect_stdout(buf):
                code = run_workflow.main(argv)
        finally:
            run_workflow._runtime_command = original  # type: ignore[assignment]
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        return code, out, observed_calls

    def test_node_start_event_emitted_once_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_002"},
                        raw_stdout="{}",
                    )
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original = run_workflow._runtime_command
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                with redirect_stdout(buf):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_node_start",
                            "--no-invoke-llm",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original  # type: ignore[assignment]

            self.assertEqual(code, 0)
            events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            node_starts = [e for e in events if e.get("event") == "node_start"]
            self.assertEqual(len(node_starts), 1)
            self.assertEqual(node_starts[0]["spec_ref"], "spec/problem/test.md")
            self.assertEqual(node_starts[0]["until_phase"], "Build")
            self.assertEqual(node_starts[0]["orchestration_id"], "orch_node_start")
            self.assertFalse(node_starts[0]["resume"])
            # node_start carries no `ts` (consistent with sibling info events)
            self.assertNotIn("ts", node_starts[0])

    def test_resume_recovers_params_and_uses_checkpoint_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root,
                "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md",
                until_phase="Build",
                mode="dev",
                backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["status"], "ok")
            self.assertTrue(out["resumed"])
            # Latest (only) orchestration reused, params recovered from artifacts.
            self.assertEqual(out["orchestration_id"], "orch_20260101T000000Z_aaaaaaaa")
            self.assertEqual(out["until_phase"], "Build")
            self.assertEqual(out["llm"], "claude")
            self.assertEqual(out["workflow_mode"], "dev")
            # init must use --resume-from-checkpoint (not a fresh init), and pass the
            # resolved spec/dep refs so meta stays in sync with the resumed run.
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertEqual(len(init_calls), 1)
            self.assertIn("--resume-from-checkpoint", init_calls[0])
            idx = init_calls[0].index("--spec-ref")
            self.assertEqual(init_calls[0][idx + 1], "spec/problem/test.md")

    def test_resume_forwards_explicit_agent_model(self) -> None:
        """An explicit --agent-model on --resume reaches the resume init (and thus
        repair-agent-runs), so an operator can fix a needs_manual row on resume."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm",
                 "--agent-model", "claude-opus-4-8"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertIn("--resume-from-checkpoint", init_calls[0])
            idx = init_calls[0].index("--agent-model")
            self.assertEqual(init_calls[0][idx + 1], "claude-opus-4-8")

    def test_resume_without_agent_model_omits_default(self) -> None:
        """No override on --resume: --agent-model is NOT injected, so repair uses the
        more-accurate sibling_uniform derivation rather than a possibly-wrong default."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Build",
                mode="dev", backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertNotIn("--agent-model", init_calls[0])

    def test_fresh_claude_run_records_orchestration_agent_model(self) -> None:
        """A fresh (non-resume) claude run threads --agent-model into init so the
        orchestration agent_runs row records the model (P2)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm", "claude",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertEqual(len(init_calls), 1)
            self.assertNotIn("--resume-from-checkpoint", init_calls[0])
            idx = init_calls[0].index("--agent-model")
            self.assertEqual(init_calls[0][idx + 1], "claude-opus-4-8")

    def test_fresh_run_explicit_agent_model_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm", "claude",
                 "--agent-model", "claude-sonnet-4-6",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            idx = init_calls[0].index("--agent-model")
            self.assertEqual(init_calls[0][idx + 1], "claude-sonnet-4-6")

    def test_fresh_codex_run_omits_agent_model_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm", "codex",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertNotIn("--agent-model", init_calls[0])

    def test_overridden_claude_command_omits_opus_default(self) -> None:
        """A custom --llm-command may launch a non-Opus model, so the Opus default
        must NOT be asserted; without --agent-model, agent_model is left to sibling
        backfill rather than wrongly recording Opus."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm", "claude",
                 "--llm-command", "claude --model claude-sonnet-4-6",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            self.assertNotIn("--agent-model", init_calls[0])

    def test_overridden_claude_command_with_explicit_agent_model(self) -> None:
        """An explicit --agent-model is still honored even with a custom --llm-command."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "compile", "--llm", "claude",
                 "--llm-command", "claude --model claude-sonnet-4-6",
                 "--agent-model", "claude-sonnet-4-6",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            init_calls = [c for c in calls if c and c[0] == "init"]
            idx = init_calls[0].index("--agent-model")
            self.assertEqual(init_calls[0][idx + 1], "claude-sonnet-4-6")

    def test_resume_picks_latest_by_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            for oid, phase, started in (
                ("orch_20260101T000000Z_aaaaaaaa", "Compile", "2026-01-01T00:00:00.000000Z"),
                ("orch_20260301T000000Z_bbbbbbbb", "Validate", "2026-03-01T00:00:00.000000Z"),
            ):
                self._seed_resumable_orchestration(
                    repo_root, oid, spec_ref="spec/problem/test.md",
                    until_phase=phase, mode="dev", backend="codex", started_at=started,
                )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["orchestration_id"], "orch_20260301T000000Z_bbbbbbbb")
            self.assertEqual(out["until_phase"], "Validate")

    def test_resume_latest_uses_started_at_not_id_text(self) -> None:
        # Regression for the lexical-max bug: the newest started_at must win even
        # when its id sorts BEFORE another candidate, and even when a custom
        # (non-timestamp) id that sorts lexically last is present.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            # newest start, but lexically-smallest id
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
                started_at="2026-05-01T00:00:00.000000Z",
            )
            # older start, lexically-larger timestamp id
            self._seed_resumable_orchestration(
                repo_root, "orch_20260301T000000Z_bbbbbbbb", spec_ref="spec/problem/test.md",
                until_phase="Compile", mode="dev", backend="codex",
                started_at="2026-02-01T00:00:00.000000Z",
            )
            # custom id that sorts lexically last ('u' > '2') but is oldest
            self._seed_resumable_orchestration(
                repo_root, "orch_unit_run", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="cursor",
                started_at="2026-01-01T00:00:00.000000Z",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            # The 2026-05-01 start wins despite its lexically-smaller id.
            self.assertEqual(out["orchestration_id"], "orch_20260101T000000Z_aaaaaaaa")
            self.assertEqual(out["until_phase"], "Validate")
            self.assertEqual(out["llm"], "claude")

    def test_resume_includes_custom_orchestration_ids(self) -> None:
        # A run launched with a custom --orchestration-id (no `orch_` prefix) must
        # still be resumable as "the latest" when it is the newest started.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Compile", mode="dev", backend="codex",
                started_at="2026-01-01T00:00:00.000000Z",
            )
            self._seed_resumable_orchestration(
                repo_root, "customrun", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
                started_at="2026-05-01T00:00:00.000000Z",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["orchestration_id"], "customrun")
            self.assertEqual(out["until_phase"], "Validate")

    def test_resume_reuses_recovered_dependency_ref(self) -> None:
        # The dependency ref recorded at init must be reused on resume rather than
        # rediscovered from the spec path, so resume stays stable even when the
        # default deps.yaml next to the spec is absent/moved.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            # Intentionally NO spec/problem/deps.yaml: _discover_source_dependency_ref
            # would raise here, so success proves the recovered ref is used instead.
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/sub/deps.yaml",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            prompt = (
                repo_root / "workspace" / "orchestrations"
                / "orch_20260101T000000Z_aaaaaaaa" / "launches"
                / "orchestration.start.prompt.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("spec/problem/sub/deps.yaml", prompt)

    def test_resume_preserves_custom_llm_command(self) -> None:
        # A custom --llm-command from the original run (recorded as preflight
        # probe_command) must be reused on resume, not replaced by the default binary.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                probe_command="/opt/wrappers/claude-wrapper",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["llm_command"], "/opt/wrappers/claude-wrapper")
            preflight_calls = [c for c in calls if c and c[0] == "preflight"]
            self.assertEqual(len(preflight_calls), 1)
            idx = preflight_calls[0].index("--agent-command")
            self.assertEqual(preflight_calls[0][idx + 1], "/opt/wrappers/claude-wrapper")

    def test_resume_same_spec_explicit_keeps_recovered_dependency(self) -> None:
        # Restating the SAME spec_ref explicitly is not a change: the recovered
        # (possibly non-default) dependency must still be reused, not rediscovered.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            # No spec/problem/deps.yaml: rediscovery would fail, proving reuse.
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/sub/deps.yaml",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "spec/problem/test.md",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            prompt = (
                repo_root / "workspace" / "orchestrations"
                / "orch_20260101T000000Z_aaaaaaaa" / "launches"
                / "orchestration.start.prompt.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("spec/problem/sub/deps.yaml", prompt)

    def test_resume_same_backend_explicit_keeps_custom_llm_command(self) -> None:
        # Restating the SAME --llm is not a change: the recovered custom command
        # must still be reused, not replaced by the default backend binary.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                probe_command="/opt/wrappers/claude-wrapper",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--llm", "claude",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["llm"], "claude")
            self.assertEqual(out["llm_command"], "/opt/wrappers/claude-wrapper")

    def test_resume_cli_llm_command_overrides_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                probe_command="/opt/wrappers/old",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--llm-command", "/opt/wrappers/new",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["llm_command"], "/opt/wrappers/new")

    def test_resume_backend_override_uses_new_backend_default_command(self) -> None:
        # Switching backend on resume must not reuse the old backend's recovered
        # command; it falls back to the new backend's default.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
                probe_command="/opt/wrappers/claude-wrapper",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "--llm", "codex", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["llm"], "codex")
            self.assertEqual(out["llm_command"], run_workflow.DEFAULT_LLM_COMMANDS["codex"])

    def test_resume_cli_overrides_recovered_until_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa",
                spec_ref="spec/problem/test.md", until_phase="Compile",
                mode="dev", backend="claude",
            )
            code, out, _ = self._run_main_with_fake_runtime(
                ["--resume", "build", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["until_phase"], "Build")

    def test_resume_refuses_running_latest_without_explicit_id(self) -> None:
        # Implicit `--resume` must not auto-attach to a non-terminal (running) latest.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
            )
            meta_path = (
                repo_root / "workspace" / "orchestrations" / oid / "orchestration_meta.json"
            )
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["status"] = "running"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 2)
            self.assertEqual(out["reason"], "latest_orchestration_not_resumable")
            self.assertEqual(calls, [])

            # An explicit --orchestration-id bypasses the guard (deliberate choice).
            code2, out2, _ = self._run_main_with_fake_runtime(
                ["--resume", "--orchestration-id", oid,
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code2, 0, out2)
            self.assertEqual(out2["orchestration_id"], oid)

    def test_resume_passes_overridden_spec_ref_to_init(self) -> None:
        # An explicit spec_ref override on resume must be forwarded to
        # init --resume-from-checkpoint so meta is updated (not left stale).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            (repo_root / "spec" / "other").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "other" / "alt.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "other" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            self._seed_resumable_orchestration(
                repo_root, "orch_20260101T000000Z_aaaaaaaa", spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
            )
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "spec/other/alt.md",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["target_spec_ref"], "spec/other/alt.md")
            init_calls = [c for c in calls if c and c[0] == "init"]
            idx = init_calls[0].index("--spec-ref")
            self.assertEqual(init_calls[0][idx + 1], "spec/other/alt.md")
            # Overridden spec rediscovers its own deps, not the recovered one.
            didx = init_calls[0].index("--source-dependency-ref")
            self.assertEqual(init_calls[0][didx + 1], "spec/other/deps.yaml")

    def test_resume_fails_when_no_orchestration_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 2)
            self.assertEqual(out["reason"], "no_resumable_orchestration")
            self.assertEqual(calls, [])

    def test_resume_fails_when_until_phase_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_aaaaaaaa"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Build", mode="dev", backend="claude",
            )
            # Corrupt the prompt so until_phase/mode cannot be extracted.
            (
                repo_root / "workspace" / "orchestrations" / oid
                / "launches" / "orchestration.start.prompt.txt"
            ).write_text("no parseable params here\n", encoding="utf-8")
            code, out, calls = self._run_main_with_fake_runtime(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 2)
            self.assertEqual(out["reason"], "resume_params_unrecoverable")
            self.assertIn("until_phase", out["detail"])
            self.assertEqual(calls, [])

    def test_main_writes_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_001"},
                        raw_stdout="{}",
                    )
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_unit",
                        "--no-invoke-llm",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 0)
            self.assertTrue(any(call[0] == "init" for call in observed_calls))
            self.assertTrue(any(call[0] == "preflight" for call in observed_calls))
            prompt_path = (
                repo_root
                / "workspace"
                / "orchestrations"
                / "orch_unit"
                / "launches"
                / "orchestration.start.prompt.txt"
            )
            self.assertTrue(prompt_path.exists())
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("orchestration_agent_run_id: `orch_agent_run_001`", prompt_text)

    def _run_main_with_failing_launch(
        self,
        repo_root: Path,
        *,
        meta_status_after_launch: str | None,
        orchestration_id: str = "orch_interrupt",
    ) -> tuple[int, dict, list[list[str]]]:
        """Drive main() through the invoke_llm branch with a non-zero subprocess
        exit (simulating a token/session-limit kill mid-run).

        meta_status_after_launch seeds orchestration_meta.json so the post-launch
        status read reflects whether the orchestration agent terminalized
        (e.g. "fail") or was killed non-terminal (None → file absent → "running").
        """
        self._seed_spec_tree(repo_root)
        orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
        if meta_status_after_launch is not None:
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps(
                    {
                        "orchestration_id": orchestration_id,
                        "status": meta_status_after_launch,
                        "orchestration_agent_run_id": "orch_agent_run_002",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        observed_calls: list[list[str]] = []

        def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(args)
            if args[0] == "init":
                return run_workflow.RuntimeResult(
                    payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_002"},
                    raw_stdout="{}",
                )
            if args[0] == "preflight":
                return run_workflow.RuntimeResult(
                    payload={
                        "status": "pass",
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                    },
                    raw_stdout="{}",
                )
            return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

        def fake_subprocess_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(args=cmd, returncode=1)

        original_runtime = run_workflow._runtime_command
        original_run = run_workflow.subprocess.run
        buf = io.StringIO()
        try:
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            run_workflow.subprocess.run = fake_subprocess_run  # type: ignore[assignment]
            with redirect_stdout(buf):
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        orchestration_id,
                    ]
                )
        finally:
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
            run_workflow.subprocess.run = original_run  # type: ignore[assignment]
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        return code, out, observed_calls

    def test_failing_launch_terminalizes_nonterminal_orchestration(self) -> None:
        # A token-limit kill leaves the orchestration meta non-terminal ("running").
        # run_workflow must terminalize it (set-status fail) so an implicit --resume
        # is not refused by the non-terminal-latest guard.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            code, out, calls = self._run_main_with_failing_launch(
                repo_root, meta_status_after_launch=None
            )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "workflow_failed")
            self.assertEqual(out["workflow_status"], "fail")
            status_calls = [c for c in calls if c and c[0] == "set-status"]
            self.assertEqual(len(status_calls), 1, calls)
            call = status_calls[0]
            self.assertEqual(call[call.index("--status") + 1], "fail")
            self.assertEqual(
                call[call.index("--reason-code") + 1], "llm_launch_interrupted"
            )

    def test_failing_launch_does_not_double_terminalize(self) -> None:
        # When the orchestration agent already recorded a terminal status before
        # the non-zero exit, run_workflow must NOT issue a redundant set-status.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            code, out, calls = self._run_main_with_failing_launch(
                repo_root, meta_status_after_launch="fail"
            )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "workflow_failed")
            self.assertEqual(out["workflow_status"], "fail")
            interrupt_calls = [
                c
                for c in calls
                if c
                and c[0] == "set-status"
                and "llm_launch_interrupted" in c
            ]
            self.assertEqual(interrupt_calls, [], calls)

    def _run_main_with_dangling_launch(
        self,
        repo_root: Path,
        *,
        set_status_raises: bool,
        orchestration_id: str = "orch_dangling",
    ) -> tuple[int, dict, list[list[str]]]:
        """Drive main() through invoke_llm with a CLEAN (returncode 0) subprocess
        exit but an open active_child window left behind (dangling launch).

        set_status_raises simulates the terminalize set-status failing, to prove the
        run still reports failure (not ok) via the launch_incident_detected flag.
        """
        self._seed_spec_tree(repo_root)
        orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
        orch_root.mkdir(parents=True, exist_ok=True)
        (orch_root / "orchestration_meta.json").write_text(
            json.dumps(
                {
                    "orchestration_id": orchestration_id,
                    "status": "running",
                    "orchestration_agent_run_id": "orch_agent_run_003",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        child = "dangling-child-arid"
        (orch_root / "active_child_agent_run_id.txt").write_text(child, encoding="utf-8")
        (orch_root / "active_children").mkdir(exist_ok=True)
        (orch_root / "active_children" / f"{child}.txt").write_text(child, encoding="utf-8")

        observed_calls: list[list[str]] = []

        def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(args)
            if args[0] == "init":
                return run_workflow.RuntimeResult(
                    payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_003"},
                    raw_stdout="{}",
                )
            if args[0] == "preflight":
                return run_workflow.RuntimeResult(
                    payload={
                        "status": "pass",
                        "can_launch_step_agents": True,
                        "can_launch_substep_agents": True,
                    },
                    raw_stdout="{}",
                )
            if args[0] == "set-status" and set_status_raises:
                raise RuntimeError("simulated set-status failure")
            return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

        def fake_subprocess_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        original_runtime = run_workflow._runtime_command
        original_run = run_workflow.subprocess.run
        buf = io.StringIO()
        try:
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            run_workflow.subprocess.run = fake_subprocess_run  # type: ignore[assignment]
            with redirect_stdout(buf):
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        orchestration_id,
                    ]
                )
        finally:
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
            run_workflow.subprocess.run = original_run  # type: ignore[assignment]
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        return code, out, observed_calls

    def test_dangling_launch_clean_exit_terminalizes(self) -> None:
        # Clean (returncode 0) exit but the child launch never returned: run_workflow
        # must terminalize with reason_code launch_incomplete_active_child and report
        # failure (NOT ok), and snapshot the incident.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            code, out, calls = self._run_main_with_dangling_launch(
                repo_root, set_status_raises=False
            )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "workflow_failed")
            self.assertEqual(out["workflow_status"], "fail")
            incident_calls = [
                c for c in calls
                if c and c[0] == "set-status" and "launch_incomplete_active_child" in c
            ]
            self.assertEqual(len(incident_calls), 1, calls)
            snapshots = list(
                (repo_root / "workspace" / "orchestrations" / "orch_dangling").glob(
                    "launch_incident.runtime.*.json"
                )
            )
            self.assertEqual(len(snapshots), 1, "incident snapshot must be persisted")

    def test_dangling_launch_clean_exit_still_fails_when_set_status_raises(self) -> None:
        # P2 regression: even if BOTH terminalize set-status calls fail, a detected
        # dangling launch must not be reported as ok. launch_incident_detected forces
        # the failure path despite returncode 0 + non-terminal workflow_status.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            code, out, calls = self._run_main_with_dangling_launch(
                repo_root, set_status_raises=True
            )
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "workflow_failed")
            self.assertNotEqual(out.get("status"), "ok")
            # The degraded fallback must preserve the dangling-launch reason, not
            # downgrade to the generic returncode reason (P3).
            status_calls = [c for c in calls if c and c[0] == "set-status"]
            self.assertTrue(status_calls)
            for c in status_calls:
                self.assertEqual(c[c.index("--reason-code") + 1], "launch_incomplete_active_child")
            self.assertFalse(
                any("llm_launch_interrupted" in c for c in status_calls), calls
            )

    def test_recorded_pass_meta_overrides_nonzero_cli_returncode(self) -> None:
        # The orchestration agent recorded a terminal pass, but the launched CLI
        # exited nonzero (e.g. a transport hiccup it already recovered from — the
        # audit case orch_20260615T095217Z_74450292). The recorded pass is
        # authoritative: report ok with a cli_returncode_warning, do NOT emit
        # workflow_failed, and do NOT terminalize to fail.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            code, out, calls = self._run_main_with_failing_launch(
                repo_root, meta_status_after_launch="pass"
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(out["status"], "ok")
            self.assertEqual(out["workflow_status"], "pass")
            self.assertEqual(out.get("cli_returncode_warning"), 1)
            # The subprocess WAS invoked (it just returned nonzero), so llm_invoked
            # must be true — distinct from a --no-invoke-llm (prompt-only) run.
            self.assertIs(out["llm_invoked"], True)
            interrupt_calls = [
                c
                for c in calls
                if c and c[0] == "set-status" and "llm_launch_interrupted" in c
            ]
            self.assertEqual(interrupt_calls, [], calls)

    def test_no_invoke_llm_claude_does_not_record_host_session_id(self) -> None:
        # With --no-invoke-llm no `claude --session-id` process starts, so NO runtime
        # call (init or preflight) may carry --host-session-id (recording it would point
        # meta at a transcript that never exists).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                [
                    "spec/problem/test.md",
                    "build",
                    "--repo-root",
                    str(repo_root),
                    "--llm",
                    "claude",
                    "--no-invoke-llm",
                ]
            )
            self.assertEqual(code, 0, out)
            self.assertTrue([c for c in calls if c and c[0] == "preflight"], calls)
            for call in calls:
                self.assertNotIn("--host-session-id", call)

    def test_invoke_llm_claude_records_host_session_id_via_preflight(self) -> None:
        # A real claude launch pins the host session UUID and threads it into PREFLIGHT
        # (not init) so it is recorded only when preflight is launchable. The same id is
        # pinned on the launch via --session-id, and init must NOT carry it.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            captured: dict[str, list[str]] = {}

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if args and args[0] == "init":
                    captured["init"] = args
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "oar"},
                        raw_stdout="{}",
                    )
                if args and args[0] == "preflight":
                    captured["preflight"] = args
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            captured_cmd: dict[str, list[str]] = {}

            def fake_subprocess_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
                captured_cmd["cmd"] = cmd
                return subprocess.CompletedProcess(args=cmd, returncode=0)

            original_runtime = run_workflow._runtime_command
            original_run = run_workflow.subprocess.run
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                run_workflow.subprocess.run = fake_subprocess_run  # type: ignore[assignment]
                with redirect_stdout(buf):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--llm",
                            "claude",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
                run_workflow.subprocess.run = original_run  # type: ignore[assignment]
            self.assertEqual(code, 0, buf.getvalue())
            self.assertNotIn("--host-session-id", captured["init"])
            self.assertIn("--host-session-id", captured["preflight"])
            hsid = captured["preflight"][
                captured["preflight"].index("--host-session-id") + 1
            ]
            # The same id is pinned on the claude launch via --session-id.
            self.assertIn("--session-id", captured_cmd["cmd"])
            self.assertEqual(
                captured_cmd["cmd"][captured_cmd["cmd"].index("--session-id") + 1], hsid
            )

    def _write_verify_step_result(
        self,
        repo_root: Path,
        orchestration_id: str,
        *,
        step: str,
        status: str,
        severity: str,
        node_safe: str = "component__x__0.1.0",
        agent_run_id: str = "writer_arid",
    ) -> None:
        d = (
            repo_root
            / "workspace" / "orchestrations" / orchestration_id
            / "steps" / node_safe / step / agent_run_id
        )
        d.mkdir(parents=True, exist_ok=True)
        (d / "step_result.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "retry_decisions": [
                        {"issue_severity": severity, "repair_reason": "x"}
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_detect_verify_issue_flags_major_even_in_passed_step(self) -> None:
        # dev-mode contract (WORKFLOW_CORE.md/startup_contract.md/SKILL.md): a
        # major/critical verify severity must fail closed, with no "resolved" exception.
        # So even a step_result whose final status is `pass` but whose retry_decisions
        # contain a major issue must be flagged.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_verify_step_result(
                repo_root, "orch_v", step="generate", status="pass", severity="major"
            )
            issue = run_workflow._detect_non_minor_verify_issue(repo_root, "orch_v")
            self.assertIsNotNone(issue)
            self.assertEqual(issue["issue_severity"], "major")

    def test_detect_verify_issue_ignores_minor(self) -> None:
        # A minor severity is acceptable and must not be flagged.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._write_verify_step_result(
                repo_root, "orch_v", step="validate", status="pass", severity="minor"
            )
            self.assertIsNone(
                run_workflow._detect_non_minor_verify_issue(repo_root, "orch_v")
            )

    def test_unresolved_severe_verify_fails_closed_even_when_meta_pass(self) -> None:
        # Codex P1: even with meta.status=pass and a zero CLI returncode, an UNRESOLVED
        # severe verify issue (non-pass step_result) must fail closed in dev mode.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_verify_failclosed"
            orch_root = repo_root / "workspace" / "orchestrations" / oid
            orch_root.mkdir(parents=True, exist_ok=True)
            (orch_root / "orchestration_meta.json").write_text(
                json.dumps(
                    {"orchestration_id": oid, "status": "pass",
                     "orchestration_agent_run_id": "oar"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            # A verify step that did NOT pass but carries a major issue.
            self._write_verify_step_result(
                repo_root, oid, step="validate", status="fail", severity="major"
            )

            observed: list[list[str]] = []

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                observed.append(args)
                if args and args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "oar"},
                        raw_stdout="{}",
                    )
                if args and args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            def fake_subprocess_run(cmd, *a, **kw):  # type: ignore[no-untyped-def]
                return subprocess.CompletedProcess(args=cmd, returncode=0)

            original_runtime = run_workflow._runtime_command
            original_run = run_workflow.subprocess.run
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                run_workflow.subprocess.run = fake_subprocess_run  # type: ignore[assignment]
                with redirect_stdout(buf):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            oid,
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
                run_workflow.subprocess.run = original_run  # type: ignore[assignment]
            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "workflow_failed")
            # The dev-mode verify backstop must have fired despite meta=pass.
            violation_calls = [
                c
                for c in observed
                if c and c[0] == "set-status" and "verify_issue_severity_violation" in c
            ]
            self.assertTrue(violation_calls, observed)

    def test_direct_script_invocation_does_not_crash_on_module_import(self) -> None:
        """Regression: `python3 tools/run_workflow.py ...` is the canonical
        entrypoint per CLAUDE.md. Under direct-script invocation `sys.path[0]`
        is `tools/`, NOT the repo root, so `from tools.X import Y` raises
        `ModuleNotFoundError` unless the script bootstraps `sys.path` first.
        Previously the new schema-load guard imported `tools.validate_pipeline_semantics`
        without that bootstrap, crashing direct-CLI invocation with a raw
        traceback instead of the intended structured failure. This test
        spawns the actual subprocess to exercise the real direct-script
        codepath that `run_workflow.main()` from in-process import would
        otherwise mask."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Build a minimal valid spec so we reach the schema guard.
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            # Deliberately omit the canonical schema so the guard fires.
            run_workflow_path = (
                Path(__file__).resolve().parent.parent / "run_workflow.py"
            )
            # Strip PYTHONPATH so the subprocess only has its own bootstrap.
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            proc = subprocess.run(
                [
                    sys.executable,
                    str(run_workflow_path),
                    "spec/problem/test.md",
                    "build",
                    "--repo-root", str(repo_root),
                    "--orchestration-id", "orch_direct_cli",
                    "--no-invoke-llm",
                ],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        # Must NOT crash with a Python traceback (ModuleNotFoundError or otherwise).
        self.assertNotIn(
            "Traceback",
            proc.stderr,
            f"direct-CLI invocation must not produce a traceback; stderr:\n{proc.stderr}",
        )
        self.assertEqual(
            proc.returncode, 2,
            f"expected exit 2 (structured fail); stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        # Must emit structured JSON identifying the schema gap.
        last_line = proc.stdout.strip().splitlines()[-1]
        payload = json.loads(last_line)
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["reason"], "missing_canonical_schema")
        self.assertIn("shape_expr.schema.json", payload["missing_path"])

    def test_main_fails_fast_when_canonical_schema_missing(self) -> None:
        """Regression: tools/run_workflow.py must abort BEFORE init/preflight
        if `<repo_root>/spec/schema/ir/shape_expr.schema.json` is missing,
        because validate_pipeline_semantics is now fail-closed under repo
        scope and would otherwise collapse every downstream phase gate with
        `schema_load_failed` after orchestration state has already been
        mutated. Emits structured `missing_canonical_schema` JSON on stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Deliberately do NOT seed the schema (this test exercises absence).
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_no_schema",
                        "--no-invoke-llm",
                    ]
                )
            output = buf.getvalue()
        self.assertEqual(code, 2)
        # Verify structured JSON output with the right reason code.
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["reason"], "missing_canonical_schema")
        self.assertIn("spec/schema/ir/shape_expr.schema.json", payload["missing_path"])
        # Critical: orchestration state must NOT have been created — the
        # check must run before init().
        self.assertFalse(
            (repo_root / "workspace" / "orchestrations").exists(),
            "init/preflight must not run before the schema-existence check",
        )

    def test_main_fails_fast_when_canonical_schema_is_malformed(self) -> None:
        """Regression: the startup guard must surface NOT only missing-file
        but also malformed JSON, invalid regex, and structural-classifier
        failures BEFORE any orchestration state mutation. Previously the
        guard only did `is_file()`, so a corrupted schema slipped through and
        crashed mid-phase after `workspace/tmp/<arid>/` was already created."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            schema_dir = repo_root / "spec" / "schema" / "ir"
            schema_dir.mkdir(parents=True)
            # Schema EXISTS as a file but has malformed JSON.
            (schema_dir / "shape_expr.schema.json").write_text(
                "{ this is not json", encoding="utf-8"
            )
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            (repo_root / "spec" / "problem" / "deps.yaml").write_text("nodes: []\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_corrupt_schema",
                        "--no-invoke-llm",
                    ]
                )
            output = buf.getvalue()
        self.assertEqual(code, 2)
        payload = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["reason"], "missing_canonical_schema")
        # Detail must surface the underlying parse error so operators can fix
        # the schema rather than just learning "something is wrong".
        self.assertIn("malformed JSON", payload["detail"])
        # Critical: NO orchestration state was touched.
        self.assertFalse(
            (repo_root / "workspace" / "orchestrations").exists(),
            "init/preflight must not run before the schema-load check",
        )
        self.assertFalse(
            (repo_root / "workspace" / "tmp").exists(),
            "workspace/tmp must not be created before the schema-load check",
        )

    def test_main_fails_when_spec_ref_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            code = run_workflow.main(
                [
                    "spec/problem/missing.md",
                    "build",
                    "--repo-root",
                    str(repo_root),
                    "--orchestration-id",
                    "orch_missing",
                    "--no-invoke-llm",
                ]
            )
            self.assertEqual(code, 2)

    def test_main_returns_structured_error_when_init_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "init":
                    raise RuntimeError("runtime command failed (init): boom")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_init_fail",
                            "--no-invoke-llm",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_init_fail")
            self.assertIn("init", payload["detail"])

    def test_main_returns_structured_error_when_preflight_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "preflight":
                    raise RuntimeError("runtime command failed (preflight): boom")
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_preflight_fail"},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_preflight_fail",
                            "--no-invoke-llm",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_preflight_fail")
            self.assertIn("preflight", payload["detail"])

    def test_main_returns_structured_error_when_init_result_lacks_orchestration_agent_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "init":
                    return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_init_missing_run_id",
                            "--no-invoke-llm",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "runtime_command_failed")
            self.assertEqual(payload["orchestration_id"], "orch_init_missing_run_id")
            self.assertIn("missing orchestration_agent_run_id", payload["detail"])

    def test_main_dev_mode_writes_failure_analysis_on_llm_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_dev_fail"},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            class DummyCompletedProcess:
                def __init__(self, returncode: int) -> None:
                    self.returncode = returncode

            original_runtime = run_workflow._runtime_command
            original_subprocess_run = run_workflow.subprocess.run
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                run_workflow.subprocess.run = lambda *args, **kwargs: DummyCompletedProcess(1)  # type: ignore[assignment]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = run_workflow.main(
                        [
                            "spec/problem/test.md",
                            "build",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            "orch_dev_fail",
                        ]
                    )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]
                run_workflow.subprocess.run = original_subprocess_run  # type: ignore[assignment]

            self.assertEqual(code, 2)
            payload = json.loads(stdout.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(payload["reason"], "workflow_failed")
            self.assertEqual(payload["workflow_mode"], "dev")
            analysis_ref = payload.get("analysis_ref")
            self.assertIsInstance(analysis_ref, str)
            self.assertTrue((repo_root / str(analysis_ref)).exists())

    def test_main_resolves_dependency_ref_from_spec_deps_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")
            dep_ref = "spec/problem/deps.yaml"
            (repo_root / dep_ref).write_text("nodes: []\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_auto_dep"},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_auto_dep",
                        "--no-invoke-llm",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 0)
            init_call = next(call for call in observed_calls if call[0] == "init")
            self.assertIn("--source-dependency-ref", init_call)
            dep_idx = init_call.index("--source-dependency-ref") + 1
            self.assertEqual(init_call[dep_idx], dep_ref)

    def test_main_fails_when_dependency_ref_cannot_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
            (repo_root / "spec" / "problem" / "test.md").write_text("spec\n", encoding="utf-8")

            observed_calls: list[list[str]] = []

            def fake_runtime_command(root: Path, env: dict[str, str], args: list[str]) -> run_workflow.RuntimeResult:
                observed_calls.append(args)
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={
                            "status": "pass",
                            "can_launch_step_agents": True,
                            "can_launch_substep_agents": True,
                        },
                        raw_stdout="{}",
                    )
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "orch_agent_run_no_dep"},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            original_runtime = run_workflow._runtime_command
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                code = run_workflow.main(
                    [
                        "spec/problem/test.md",
                        "build",
                        "--repo-root",
                        str(repo_root),
                        "--orchestration-id",
                        "orch_no_dep",
                        "--no-invoke-llm",
                    ]
                )
            finally:
                run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

            self.assertEqual(code, 2)
            self.assertFalse(observed_calls)

    def test_main_fails_fast_when_required_cli_tool_missing(self) -> None:
        """If jq (or any REQUIRED_CLI_TOOLS entry) is not on PATH, main() must
        return 2 with status=fail/reason=missing_required_cli_tools BEFORE
        running any orchestration_runtime command. This protects against
        partial-failure states where downstream procedures (e.g. TMPDIR
        extraction via jq) would otherwise be prescribed despite the tool
        being absent."""
        original_which = run_workflow.shutil.which

        def fake_which(name: str) -> str | None:
            if name == "jq":
                return None
            return original_which(name)

        observed_calls: list[list[str]] = []

        def fake_runtime(repo_root, env, args):  # type: ignore[no-untyped-def]
            observed_calls.append(list(args))
            raise AssertionError("orchestration_runtime must not be invoked")

        original_runtime = run_workflow._runtime_command
        run_workflow.shutil.which = fake_which  # type: ignore[assignment]
        run_workflow._runtime_command = fake_runtime  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md",
                    "Compile",
                    "--llm",
                    "claude",
                ])
        finally:
            run_workflow.shutil.which = original_which  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertFalse(observed_calls)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("status"), "fail")
        self.assertEqual(payload.get("reason"), "missing_required_cli_tools")
        self.assertEqual(payload.get("missing"), ["jq"])
        self.assertIn("python3", payload.get("required", []))
        self.assertEqual(payload.get("detail"), "missing tools: jq")

    def test_check_required_cli_tools_returns_empty_when_all_present(self) -> None:
        """Sanity check: in the test environment all required tools are
        present, so the helper returns []. If this fails, the test environment
        is missing a tool needed for workflow runs."""
        self.assertEqual(run_workflow._check_required_cli_tools(), [])

    def test_main_reports_multiple_missing_tools_in_detail(self) -> None:
        """When multiple required tools are missing, `detail` must enumerate
        all of them as a comma-separated list (no spaces). This pins the
        format so future separator changes don't silently drift away from the
        documented shape in docs/RUNBOOK.md#0-1."""
        original_which = run_workflow.shutil.which

        def fake_which(name: str) -> str | None:
            if name in {"jq", "git"}:
                return None
            return original_which(name)

        original_runtime = run_workflow._runtime_command

        def fake_runtime(repo_root, env, args):  # type: ignore[no-untyped-def]
            raise AssertionError("orchestration_runtime must not be invoked")

        run_workflow.shutil.which = fake_which  # type: ignore[assignment]
        run_workflow._runtime_command = fake_runtime  # type: ignore[assignment]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_workflow.main([
                    "spec/problem/dummy.md",
                    "Compile",
                    "--llm",
                    "claude",
                ])
        finally:
            run_workflow.shutil.which = original_which  # type: ignore[assignment]
            run_workflow._runtime_command = original_runtime  # type: ignore[assignment]

        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue().strip())
        self.assertEqual(payload.get("missing"), ["jq", "git"])
        self.assertEqual(payload.get("detail"), "missing tools: jq,git")


def _write_catalog(repo_root: Path, entries: list[dict]) -> None:
    """Write a minimal spec_catalog.yaml from a list of {spec_kind, spec_id,
    spec_version, deps_path} dicts."""
    lines = ["catalog_version: 0.2.0", "updated_at: 2026-06-18", "specs:"]
    for e in entries:
        lines.append(f"  - spec_kind: {e['spec_kind']}")
        lines.append(f"    spec_id: {e['spec_id']}")
        lines.append(f"    spec_version: \"{e['spec_version']}\"")
        lines.append(f"    deps_path: {e['deps_path']}")
    (repo_root / "spec" / "registry").mkdir(parents=True, exist_ok=True)
    (repo_root / "spec" / "registry" / "spec_catalog.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _write_deps(repo_root: Path, spec_ref: str, spec_kind: str, spec_id: str,
                components: list[tuple[str, str]] | None = None,
                profiles: list[tuple[str, str]] | None = None) -> None:
    """Write a deps.yaml under <spec_ref>/. components/profiles are
    (id, version_constraint) tuples."""
    d = repo_root / spec_ref
    d.mkdir(parents=True, exist_ok=True)
    lines = [f"spec_id: {spec_id}", f"spec_kind: {spec_kind}", "dependencies:"]
    lines.append("  components:")
    for cid, c in (components or []):
        lines.append(f"    - component_id: {cid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not components:
        lines[-1] = "  components: []"
    lines.append("  profiles:")
    for pid, c in (profiles or []):
        lines.append(f"    - profile_id: {pid}")
        lines.append(f"      version_constraint: \"{c}\"")
    if not profiles:
        lines[-1] = "  profiles: []"
    (d / "deps.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class DependencyClosureTests(unittest.TestCase):
    def _seed_diamond(self, repo_root: Path) -> None:
        # problem A → components B, C ; B → component C ; C leaf.
        _write_catalog(repo_root, [
            {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
             "deps_path": "spec/problem/a/deps.yaml"},
            {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
             "deps_path": "spec/component/b/deps.yaml"},
            {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
             "deps_path": "spec/component/c/deps.yaml"},
        ])
        _write_deps(repo_root, "spec/problem/a", "problem", "a",
                    components=[("b", ">=0.1.0 <1.0.0"), ("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/b", "component", "b",
                    components=[("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/c", "component", "c")

    def test_topological_order_dependencies_before_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_diamond(repo_root)
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertIsNone(err)
            refs = [n["spec_id"] for n in ordered]
            # target 'a' excluded; c precedes b (b depends on c).
            self.assertEqual(refs, ["c", "b"])
            self.assertTrue(all(n["spec_versions"] == ["0.1.0"] for n in ordered))

    def test_cycle_detection_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
                 "deps_path": "spec/component/c/deps.yaml"},
            ])
            # b → c → b
            _write_deps(repo_root, "spec/component/b", "component", "b",
                        components=[("c", ">=0.1.0")])
            _write_deps(repo_root, "spec/component/c", "component", "c",
                        components=[("b", ">=0.1.0")])
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/component/b")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_cycle")

    def test_unresolvable_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                 "deps_path": "spec/problem/a/deps.yaml"},
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
            ])
            # constraint matches no catalog version of b
            _write_deps(repo_root, "spec/problem/a", "problem", "a",
                        components=[("b", ">=2.0.0")])
            _write_deps(repo_root, "spec/component/b", "component", "b")
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_unresolvable")

    def test_version_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                 "deps_path": "spec/problem/a/deps.yaml"},
                {"spec_kind": "component", "spec_id": "b", "spec_version": "1.0.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "b", "spec_version": "2.0.0",
                 "deps_path": "spec/component/b/deps.yaml"},
                {"spec_kind": "component", "spec_id": "c", "spec_version": "0.1.0",
                 "deps_path": "spec/component/c/deps.yaml"},
            ])
            # a → b==1.0.0, c ; c → b==2.0.0  → same spec dir, different version
            _write_deps(repo_root, "spec/problem/a", "problem", "a",
                        components=[("b", "==1.0.0"), ("c", ">=0.1.0")])
            _write_deps(repo_root, "spec/component/b", "component", "b")
            _write_deps(repo_root, "spec/component/c", "component", "c",
                        components=[("b", "==2.0.0")])
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "dependency_version_conflict")

    def test_driver_runs_dependencies_bottom_up_then_target(self) -> None:
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            calls: list[tuple[str, str]] = []
            ran: set[str] = set()

            def fake_run_node(**kw):
                calls.append((kw["spec_ref"], kw["until_phase"]))
                ran.add(kw["spec_ref"])
                return 0

            # A node becomes ready once it has run (simulates artifact production
            # without a real workflow). Exercises both the pre-run skip check and
            # the post-run readiness verification.
            def fake_ready(repo_root, node, required_stages):
                return node["spec_ref"] in ran

            orig = run_workflow._run_node
            orig_ready = run_workflow._dependency_node_ready
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            run_workflow._dependency_node_ready = fake_ready  # type: ignore[assignment]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude",
                        llm_command="claude",
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        invoke_llm=False,
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]
                run_workflow._dependency_node_ready = orig_ready  # type: ignore[assignment]

            self.assertEqual(rc, 0)
            # deps (c, b) run before the target a; target last.
            self.assertEqual([c[0] for c in calls],
                             ["spec/component/c", "spec/component/b", "spec/problem/a"])
            # target until_phase >= generate → deps run to Validate.
            self.assertTrue(all(c[1] == "Validate" for c in calls))

    def test_driver_stops_when_dependency_not_ready_after_run(self) -> None:
        # A dependency that exits 0 without producing readiness evidence
        # (e.g. --no-invoke-llm) must stop the run before the dependent/target.
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            calls: list[str] = []

            def fake_run_node(**kw):
                calls.append(kw["spec_ref"])
                return 0  # success, but no artifacts are produced

            orig = run_workflow._run_node
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude",
                        llm_command="claude",
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        invoke_llm=False,
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]

            self.assertEqual(rc, 2)
            # Stops after the first dependency (c); b and target never run.
            self.assertEqual(calls, ["spec/component/c"])
            last = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(last["reason"], "dependency_not_ready_after_run")
            self.assertEqual(last["failed_dependency_node"], "component/c@0.1.0")

    def test_driver_stops_on_first_dependency_failure(self) -> None:
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            calls: list[str] = []

            def fake_run_node(**kw):
                calls.append(kw["spec_ref"])
                # Fail the first dependency (c).
                return 2 if kw["spec_ref"] == "spec/component/c" else 0

            orig = run_workflow._run_node
            run_workflow._run_node = fake_run_node  # type: ignore[assignment]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_workflow._run_with_dependency_closure(
                        repo_root=repo_root,
                        base_env={"PATH": os.environ.get("PATH", "")},
                        target_orchestration_id="orch_target",
                        target_spec_ref="spec/problem/a",
                        target_source_dependency_ref="spec/problem/a/deps.yaml",
                        until_phase="Validate",
                        llm="claude",
                        llm_command="claude",
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        invoke_llm=False,
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]

            self.assertEqual(rc, 2)
            # Stopped after c failed; b and the target a never ran.
            self.assertEqual(calls, ["spec/component/c"])
            last = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(last["reason"], "dependency_node_failed")
            self.assertEqual(last["failed_dependency_node"], "component/c@0.1.0")

    def test_leaf_target_closure_does_not_require_catalog(self) -> None:
        # A leaf target (empty deps) must resolve to an empty closure without
        # loading the catalog, so a missing/corrupt registry does not break an
        # otherwise-launchable leaf --with-deps run.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_deps(repo_root, "spec/component/leaf", "component", "leaf")
            # Intentionally NO spec/registry/spec_catalog.yaml on disk.
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/component/leaf")
            self.assertIsNone(err)
            self.assertEqual(ordered, [])

    def test_resolve_spec_ref_for_uses_deps_path_dirname(self) -> None:
        from tools.orchestration_runtime import resolve_spec_ref_for, _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "component", "spec_id": "b", "spec_version": "0.1.0",
                 "deps_path": "spec/component/b/deps.yaml"},
            ])
            _load_spec_catalog.cache_clear()
            self.assertEqual(
                resolve_spec_ref_for(repo_root, "component", "b"),
                "spec/component/b",
            )
            self.assertIsNone(resolve_spec_ref_for(repo_root, "component", "missing"))


if __name__ == "__main__":
    unittest.main()
