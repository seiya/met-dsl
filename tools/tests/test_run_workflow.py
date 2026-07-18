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
        # Load-bearing resume markers parsed by _extract_prompt_params.
        self.assertIn("target_spec_ref: `spec/problem/sample.md`", text)
        self.assertIn("end phase: `Validate`", text)
        self.assertIn("workflow_mode: `dev`", text)
        self.assertIn("dependency_ref: `spec/problem/deps.yaml`", text)
        self.assertNotIn("(not specified)", text)
        # Conductor-only: the record is no longer an LLM prompt.
        self.assertIn("driver: conductor", text)

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
        status: str = "fail",
        invocation: dict | None = None,
        record_executor: str | None = "pure",
    ) -> None:
        """Create the on-disk artifacts a resume recovers params from.

        Since M-F every real orchestration records `invocation.generate_executor = "pure"` (the
        resume fail-close gate rejects anything else), so this helper injects `pure` by default —
        `setdefault`, so a caller that passes its own `generate_executor` (e.g. a legacy/garbage
        record under test) wins. Pass `record_executor=None` to seed a pre-field orchestration
        (no executor key at all) for the fail-close path."""
        orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
        (orch_root / "launches").mkdir(parents=True, exist_ok=True)
        dep_ref = source_dependency_ref
        meta = {
            "orchestration_id": orchestration_id,
            "status": status,
            "started_at": started_at,
            "spec_ref": spec_ref,
            "source_dependency_ref": dep_ref,
            "orchestration_agent_run_id": "orch_agent_prev",
        }
        if record_executor is not None:
            invocation = dict(invocation or {})
            invocation.setdefault("generate_executor", record_executor)
        if invocation is not None:
            meta["invocation"] = invocation
        (orch_root / "orchestration_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False),
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
        # Force JSONL stdout so the harness can parse the final summary line
        # regardless of the main() default (which is human-readable).
        argv_with_jsonl = list(argv)
        if "--stdout-format" not in argv_with_jsonl:
            argv_with_jsonl += ["--stdout-format", "jsonl"]
        try:
            run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
            with redirect_stdout(buf):
                code = run_workflow.main(argv_with_jsonl)
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
                            "--stdout-format",
                            "jsonl",
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

    def test_conductor_dev_failure_writes_failure_analysis(self) -> None:
        # In dev mode, a non-pass conductor run must persist failure_analysis.json
        # (the documented dev-failure artifact that init --resume-from-checkpoint
        # reads to build the cross-phase reopen resume_directive).
        import tools.workflow_conductor as wc
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            def fake_runtime_command(root, env, args):  # type: ignore[no-untyped-def]
                if args[0] == "init":
                    return run_workflow.RuntimeResult(
                        payload={"status": "ok", "orchestration_agent_run_id": "oar"},
                        raw_stdout="{}",
                    )
                if args[0] == "preflight":
                    return run_workflow.RuntimeResult(
                        payload={"status": "pass", "can_launch_step_agents": True,
                                 "can_launch_substep_agents": True},
                        raw_stdout="{}",
                    )
                return run_workflow.RuntimeResult(payload={"status": "ok"}, raw_stdout="{}")

            orig_rt = run_workflow._runtime_command
            orig_rc = wc.run_conductor
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = fake_runtime_command  # type: ignore[assignment]
                wc.run_conductor = lambda **kw: "fail"  # type: ignore[assignment]
                with redirect_stdout(buf):
                    code = run_workflow.main([
                        "spec/problem/test.md", "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_devfail",
                        "--llm", "claude", "--mode", "dev",
                        "--stdout-format", "jsonl",
                    ])
            finally:
                run_workflow._runtime_command = orig_rt  # type: ignore[assignment]
                wc.run_conductor = orig_rc  # type: ignore[assignment]

            out = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["status"], "fail")
            self.assertIn("analysis_ref", out)
            fa = repo_root / "workspace" / "orchestrations" / "orch_devfail" / "failure_analysis.json"
            self.assertTrue(fa.exists(), "conductor dev failure must write failure_analysis.json")

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
        orchestration agent_runs row records the model (P2). The default is the
        operator's UNPINNED alias (e.g. 'opus'), not a pinned version."""
        from tools.orchestration_runtime import resolve_claude_model_alias
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
            recorded = init_calls[0][idx + 1]
            self.assertEqual(recorded, resolve_claude_model_alias())
            # never a pinned version id
            self.assertNotRegex(recorded, r"-\d+-\d+$")

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
                until_phase="Build", mode="dev", backend="codex",
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

    # ------------------------------------------------------------------
    # invocation record + closure-aware resume
    # ------------------------------------------------------------------
    def test_build_invocation_record_single_node_has_no_closure(self) -> None:
        rec = run_workflow._build_invocation_record(
            argv=["spec/problem/a", "validate"],
            spec_ref="spec/problem/a",
            until_phase="Validate",
            llm="claude",
            llm_command="claude",
            workflow_mode="dev",
            agent_model="opus",
            with_deps=False,
        )
        self.assertEqual(rec["argv"], ["spec/problem/a", "validate"])
        self.assertEqual(rec["generate_executor"], "pure")  # M-F: always the hardcoded provenance
        self.assertIn("python3 tools/run_workflow.py", rec["command"])
        self.assertEqual(rec["spec_ref"], "spec/problem/a")
        self.assertEqual(rec["until_phase"], "Validate")
        self.assertEqual(rec["agent_model"], "opus")
        self.assertFalse(rec["with_deps"])
        self.assertNotIn("closure_id", rec)

    def test_build_invocation_record_closure_fields_present(self) -> None:
        rec = run_workflow._build_invocation_record(
            argv=["spec/problem/a", "validate", "--with-deps"],
            spec_ref="spec/component/c",
            until_phase="Validate",
            llm="claude",
            llm_command="claude",
            workflow_mode="dev",
            agent_model=None,
            with_deps=True,
            closure_id="orch_target",
            closure_target_spec_ref="spec/problem/a",
            closure_until_phase="Validate",
        )
        self.assertTrue(rec["with_deps"])
        self.assertEqual(rec["closure_id"], "orch_target")
        self.assertEqual(rec["closure_target_spec_ref"], "spec/problem/a")
        self.assertEqual(rec["closure_until_phase"], "Validate")
        # agent_model omitted when falsy
        self.assertNotIn("agent_model", rec)

    def test_load_resume_params_recovers_closure_from_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_dep00000"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                invocation={
                    "closure_id": "orch_target",
                    "closure_target_spec_ref": "spec/problem/a",
                    "closure_until_phase": "Validate",
                },
            )
            params = run_workflow._load_resume_params(repo_root, oid)
            self.assertEqual(params["closure_id"], "orch_target")
            self.assertEqual(params["closure_target_spec_ref"], "spec/problem/a")
            self.assertEqual(params["closure_until_phase"], "Validate")

    def test_load_resume_params_closure_none_when_no_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_legacy00"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
            )
            params = run_workflow._load_resume_params(repo_root, oid)
            self.assertIsNone(params["closure_id"])
            self.assertIsNone(params["closure_target_spec_ref"])
            self.assertIsNone(params["closure_until_phase"])
            # non-closure params still recovered
            self.assertEqual(params["spec_ref"], "spec/problem/test.md")

    # --- Z2 executor provenance + M-F legacy-removal fail-close ----------------
    def test_build_invocation_record_persists_generate_executor(self) -> None:
        # M-F: the executor is no longer a per-run choice; the record always stamps "pure" as a
        # provenance value (the `generate_executor` kwarg was removed from the builder).
        rec = run_workflow._build_invocation_record(
            argv=["spec/problem/a", "generate"], spec_ref="spec/problem/a",
            until_phase="Generate", llm="claude", llm_command="claude",
            workflow_mode="dev", agent_model=None, with_deps=False)
        self.assertEqual(rec["generate_executor"], "pure")

    def test_load_resume_params_recovers_generate_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_pure0000"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md",
                until_phase="Generate", mode="dev", backend="claude",
                invocation={"generate_executor": "pure"})
            params = run_workflow._load_resume_params(repo_root, oid)
            self.assertEqual(params["generate_executor"], "pure")
            # An orchestration predating the field recovers None — the M-F resume gate rejects it
            # (see test_resume_prefield_orchestration_fails_closed).
            oid2 = "orch_20260101T000000Z_nofield0"
            self._seed_resumable_orchestration(
                repo_root, oid2, spec_ref="spec/problem/test.md",
                until_phase="Generate", mode="dev", backend="claude",
                invocation={}, record_executor=None)
            self.assertIsNone(run_workflow._load_resume_params(repo_root, oid2)["generate_executor"])

    def _resume_capture(self, repo_root: Path, oid: str,
                        extra_argv: list[str]) -> tuple[int, dict]:
        """Resume with the fake runtime and return (exit_code, final_json)."""
        code, out, _ = self._run_main_with_fake_runtime(
            ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm", *extra_argv])
        return code, out

    def test_resume_pure_recorded_orchestration_succeeds(self) -> None:
        # M-F: a pure-recorded run resumes normally, and the executor env is NOT touched (the env
        # var was removed — the executor is no longer threaded through the environment).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_pure0001"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "pure"})
            prev = os.environ.pop("METDSL_GENERATE_EXECUTOR", None)
            try:
                code, out = self._resume_capture(repo_root, oid, [])
                self.assertEqual(code, 0, out)
                self.assertNotIn("METDSL_GENERATE_EXECUTOR", os.environ)
            finally:
                if prev is not None:
                    os.environ["METDSL_GENERATE_EXECUTOR"] = prev

    def test_resume_legacy_recorded_orchestration_fails_closed(self) -> None:
        # M-F: a legacy-recorded run cannot be resumed — legacy execution was removed. Resume must
        # fail-closed with generate_executor_legacy_removed, NOT silently switch to pure.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_legacy01"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "legacy"})
            code, out = self._resume_capture(repo_root, oid, [])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "generate_executor_legacy_removed")

    def test_resume_prefield_orchestration_fails_closed(self) -> None:
        # An orchestration predating the field recovers None -> a pre-adoption legacy run -> the
        # same fail-close (inversion of the old "stays legacy" behavior).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_nofield1"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={}, record_executor=None)
            code, out = self._resume_capture(repo_root, oid, [])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "generate_executor_legacy_removed")

    def test_resume_garbage_recorded_executor_fails_closed(self) -> None:
        # A garbage recorded value ("pur") must NEVER be read as pure — fail-closed with the same
        # reason (pins that the gate does not do a fuzzy pure match).
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_garbage1"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "pur"})
            code, out = self._resume_capture(repo_root, oid, [])
            self.assertEqual(code, 2, out)
            self.assertEqual(out["reason"], "generate_executor_legacy_removed")

    def test_ambient_env_executor_is_inert(self) -> None:
        # M-F: METDSL_GENERATE_EXECUTOR was removed and is fully inert. A stale ambient value (even
        # an old "legacy" or a typo) neither blocks a pure resume nor changes its outcome.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            oid = "orch_20260101T000000Z_pure0004"
            self._seed_resumable_orchestration(
                repo_root, oid, spec_ref="spec/problem/test.md", until_phase="Generate",
                mode="dev", backend="claude", invocation={"generate_executor": "pure"})
            prev = os.environ.get("METDSL_GENERATE_EXECUTOR")
            os.environ["METDSL_GENERATE_EXECUTOR"] = "legacy"  # stale ambient value: must be inert
            try:
                code, out = self._resume_capture(repo_root, oid, [])
                self.assertEqual(code, 0, out)
            finally:
                if prev is not None:
                    os.environ["METDSL_GENERATE_EXECUTOR"] = prev
                else:
                    os.environ.pop("METDSL_GENERATE_EXECUTOR", None)

    def test_generate_executor_flag_removed(self) -> None:
        # M-F: the --generate-executor flag was deleted. A cold run that still passes it (legacy OR
        # pure) is rejected at argparse — SystemExit(2), not a JSON envelope.
        import contextlib
        for value in ("legacy", "pure"):
            with self.assertRaises(SystemExit) as ctx, \
                    contextlib.redirect_stderr(io.StringIO()):
                run_workflow.main(
                    ["spec/problem/test.md", "generate", "--generate-executor", value])
            self.assertEqual(ctx.exception.code, 2)

    def test_index_closure_orchestrations_latest_wins_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)

            def seed(oid, spec, closure, started):
                self._seed_resumable_orchestration(
                    repo_root, oid, spec_ref=spec, until_phase="Validate",
                    mode="dev", backend="claude", started_at=started,
                    invocation={"closure_id": closure},
                )

            # two orchs for the same spec under one closure — latest started_at wins
            seed("orch_c_old", "spec/component/c", "orch_target",
                 "2026-01-01T00:00:00.000000Z")
            seed("orch_c_new", "spec/component/c", "orch_target",
                 "2026-02-01T00:00:00.000000Z")
            seed("orch_b", "spec/component/b", "orch_target",
                 "2026-01-15T00:00:00.000000Z")
            # a foreign closure — must be excluded
            seed("orch_foreign", "spec/component/c", "orch_other",
                 "2026-03-01T00:00:00.000000Z")

            index = run_workflow._index_closure_orchestrations(repo_root, "orch_target")
            self.assertEqual(index, {
                "spec/component/c": "orch_c_new",
                "spec/component/b": "orch_b",
            })

    def _run_main_with_closure_spy(self, argv):
        """Run main() with _run_with_dependency_closure and _run_node replaced by
        spies. Returns (code, closure_kwargs_or_None, run_node_kwargs_or_None)."""
        closure_kwargs: dict = {}
        run_node_kwargs: dict = {}

        def spy_closure(**kw):
            closure_kwargs.update(kw)
            return 0

        def spy_run_node(**kw):
            run_node_kwargs.update(kw)
            return 0

        orig_closure = run_workflow._run_with_dependency_closure
        orig_run_node = run_workflow._run_node
        buf = io.StringIO()
        argv2 = list(argv)
        if "--stdout-format" not in argv2:
            argv2 += ["--stdout-format", "jsonl"]
        try:
            run_workflow._run_with_dependency_closure = spy_closure  # type: ignore[assignment]
            run_workflow._run_node = spy_run_node  # type: ignore[assignment]
            with redirect_stdout(buf):
                code = run_workflow.main(argv2)
        finally:
            run_workflow._run_with_dependency_closure = orig_closure  # type: ignore[assignment]
            run_workflow._run_node = orig_run_node  # type: ignore[assignment]
        return (
            code,
            closure_kwargs or None,
            run_node_kwargs or None,
        )

    def _seed_closure_target_specs(self, repo_root: Path) -> None:
        """Minimal on-disk target spec + deps.yaml so main()'s startup validation
        (canonicalize + discover dep ref) succeeds for the closure target."""
        _write_deps(repo_root, "spec/problem/a", "problem", "a",
                    components=[("c", ">=0.1.0 <1.0.0")])
        _write_deps(repo_root, "spec/component/c", "component", "c")

    def test_resume_enters_closure_driver_when_closure_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            # entry orch is a dependency node carrying the closure back-link
            self._seed_resumable_orchestration(
                repo_root, "orch_target", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={
                    "closure_id": "orch_target",
                    "closure_target_spec_ref": "spec/problem/a",
                    "closure_until_phase": "Validate",
                },
            )
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0)
            self.assertIsNotNone(closure_kwargs, "should enter closure driver")
            self.assertIsNone(run_node_kwargs, "must not fall through to single _run_node")
            self.assertTrue(closure_kwargs["resume"])
            self.assertEqual(closure_kwargs["target_orchestration_id"], "orch_target")
            self.assertEqual(closure_kwargs["target_spec_ref"], "spec/problem/a")
            self.assertEqual(closure_kwargs["until_phase"], "Validate")
            self.assertEqual(
                closure_kwargs["prior_orch_by_spec"],
                {"spec/component/c": "orch_target"},
            )

    def test_resume_without_closure_uses_single_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_legacy", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
            )
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0)
            self.assertIsNone(closure_kwargs, "legacy resume must not enter closure driver")
            self.assertIsNotNone(run_node_kwargs)
            self.assertTrue(run_node_kwargs["resume_mode"])

    def _find_init_invocation(self, observed_calls) -> dict | None:
        for args in observed_calls:
            if args and args[0] == "init" and "--invocation-json" in args:
                return json.loads(args[args.index("--invocation-json") + 1])
        return None

    def test_cold_single_node_init_carries_invocation_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/test.md", "validate", "--repo-root", str(repo_root),
                 "--no-invoke-llm"]
            )
            self.assertEqual(code, 0)
            inv = self._find_init_invocation(calls)
            self.assertIsNotNone(inv, "cold init must carry --invocation-json")
            self.assertFalse(inv["with_deps"])
            self.assertNotIn("closure_id", inv)
            self.assertEqual(inv["spec_ref"], "spec/problem/test.md")

    def test_cold_with_deps_init_carries_closure_invocation(self) -> None:
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            (repo_root / "tools").mkdir(parents=True, exist_ok=True)
            (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
            # Full diamond (catalog + deps) so the real closure resolver runs.
            DependencyClosureTests._seed_diamond(self, repo_root)
            _load_spec_catalog.cache_clear()
            code, out, calls = self._run_main_with_fake_runtime(
                ["spec/problem/a", "validate", "--with-deps",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            # closure stops at the first dep (not ready after a no-op run), but its
            # cold init must already carry the closure back-link.
            inv = self._find_init_invocation(calls)
            self.assertIsNotNone(inv)
            self.assertTrue(inv["with_deps"])
            self.assertEqual(inv["closure_target_spec_ref"], "spec/problem/a")
            self.assertTrue(inv["closure_id"])  # = the target orchestration id

    def test_resume_closure_until_recovered_from_target_prompt(self) -> None:
        # After a phase-override resume, the target's own prompt end-phase is the
        # authoritative closure end-phase; a later plain resume entering via a dep
        # (whose copied closure_until_phase is stale "Compile") must recover the
        # refreshed "Validate" from the target, not revert to Compile.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_dep_c", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={"spec_ref": "spec/component/c", "closure_id": "ORCHT",
                            "closure_target_spec_ref": "spec/problem/a",
                            "closure_until_phase": "Compile"})
            # target ORCHT belongs to this closure; its prompt end-phase is Validate
            self._seed_resumable_orchestration(
                repo_root, "ORCHT", spec_ref="spec/problem/a",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/a/deps.yaml",
                invocation={"spec_ref": "spec/problem/a", "closure_id": "ORCHT",
                            "closure_target_spec_ref": "spec/problem/a",
                            "closure_until_phase": "Compile"})
            code, closure_kwargs, _ = self._run_main_with_closure_spy(
                ["--resume", "--orchestration-id", "orch_dep_c",
                 "--repo-root", str(repo_root), "--no-invoke-llm"])
            self.assertEqual(code, 0)
            self.assertIsNotNone(closure_kwargs)
            self.assertEqual(closure_kwargs["until_phase"], "Validate")

    def test_resume_closure_until_ignores_unrelated_target(self) -> None:
        # If the reserved target id names an UNRELATED orchestration (its own
        # invocation.closure_id differs), its phase must NOT be trusted; keep the
        # entry node's recorded closure_until_phase.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_dep_c", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={"spec_ref": "spec/component/c", "closure_id": "ORCHT",
                            "closure_target_spec_ref": "spec/problem/a",
                            "closure_until_phase": "Compile"})
            # ORCHT prompt says Validate, but it belongs to a DIFFERENT closure
            self._seed_resumable_orchestration(
                repo_root, "ORCHT", spec_ref="spec/problem/a",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/problem/a/deps.yaml",
                invocation={"spec_ref": "spec/problem/a", "closure_id": "OTHER"})
            code, closure_kwargs, _ = self._run_main_with_closure_spy(
                ["--resume", "--orchestration-id", "orch_dep_c",
                 "--repo-root", str(repo_root), "--no-invoke-llm"])
            self.assertEqual(code, 0)
            self.assertIsNotNone(closure_kwargs)
            self.assertEqual(closure_kwargs["until_phase"], "Compile")

    def test_resume_partial_closure_block_falls_back_to_single_node(self) -> None:
        # A corrupt/partial invocation block (missing closure_until_phase) must NOT
        # drive the closure with a wrong until_phase; fall back to single-node resume.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_partial", spec_ref="spec/problem/test.md",
                until_phase="Validate", mode="dev", backend="claude",
                invocation={
                    "closure_id": "orch_partial",
                    "closure_target_spec_ref": "spec/problem/a",
                    # closure_until_phase intentionally omitted
                },
            )
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["--resume", "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0)
            self.assertIsNone(closure_kwargs, "partial closure block must not drive closure")
            self.assertIsNotNone(run_node_kwargs)

    def test_resume_explicit_spec_override_forces_single_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed_spec_tree(repo_root)
            self._seed_closure_target_specs(repo_root)
            self._seed_resumable_orchestration(
                repo_root, "orch_target", spec_ref="spec/component/c",
                until_phase="Validate", mode="dev", backend="claude",
                source_dependency_ref="spec/component/c/deps.yaml",
                invocation={
                    "closure_id": "orch_target",
                    "closure_target_spec_ref": "spec/problem/a",
                    "closure_until_phase": "Validate",
                },
            )
            # explicit spec positional (non-phase) → single-node escape hatch
            code, closure_kwargs, run_node_kwargs = self._run_main_with_closure_spy(
                ["spec/component/c", "--resume", "--orchestration-id", "orch_target",
                 "--repo-root", str(repo_root), "--no-invoke-llm"]
            )
            self.assertEqual(code, 0)
            self.assertIsNone(closure_kwargs)
            self.assertIsNotNone(run_node_kwargs)

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
                        "--stdout-format", "jsonl",
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
                        "--stdout-format", "jsonl",
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
                            "--stdout-format",
                            "jsonl",
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
                            "--stdout-format",
                            "jsonl",
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
                            "--stdout-format",
                            "jsonl",
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
                    "--stdout-format",
                    "jsonl",
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
                    "--stdout-format",
                    "jsonl",
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

    def _seed_prior_member(self, repo_root: Path, orch_id: str, spec_ref: str,
                           *, executor: str | None = "pure") -> None:
        """Seed a minimal member orchestration_meta.json for a warm-resumed closure node.

        Production always has this on disk (the id came from `_index_closure_orchestrations`
        scanning existing metas). `executor` defaults to `pure` (post-M-F reality); pass a
        non-pure value / None to exercise the per-member fail-close gate."""
        meta_path = (repo_root / "workspace" / "orchestrations" / orch_id
                     / "orchestration_meta.json")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        invocation: dict = {"closure_id": "orch_target"}
        if executor is not None:
            invocation["generate_executor"] = executor
        meta_path.write_text(
            json.dumps({"spec_ref": spec_ref, "invocation": invocation}), encoding="utf-8")

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

    def test_overlong_spec_id_dependency_fails_closed(self) -> None:
        # M3d closure-build gate: an over-length dependency spec_id is rejected at closure
        # resolution — before any node runs and before an already-ready dep is skipped, so
        # it cannot slip the per-node resolve_node gate. Mirrors runner_renderer.MAX_SPEC_ID_LEN.
        from tools.runner_renderer import MAX_SPEC_ID_LEN
        long_id = "d" * (MAX_SPEC_ID_LEN + 6)
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_catalog(repo_root, [
                {"spec_kind": "problem", "spec_id": "a", "spec_version": "0.3.0",
                 "deps_path": "spec/problem/a/deps.yaml"},
                {"spec_kind": "component", "spec_id": long_id, "spec_version": "0.1.0",
                 "deps_path": f"spec/component/{long_id}/deps.yaml"},
            ])
            _write_deps(repo_root, "spec/problem/a", "problem", "a",
                        components=[(long_id, ">=0.1.0 <1.0.0")])
            _write_deps(repo_root, f"spec/component/{long_id}", "component", long_id)
            ordered, err = run_workflow._resolve_dependency_closure(
                repo_root, "spec/problem/a")
            self.assertEqual(ordered, [])
            self.assertEqual(err["reason"], "spec_id_too_long")
            self.assertIn(str(MAX_SPEC_ID_LEN), err["detail"])

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

    def _drive_closure_raw(self, repo_root, *, resume, prior_orch_by_spec):
        """Run the closure driver over the seeded diamond with _run_node captured.
        Nodes become ready once run. Returns (rc, captured kwargs list, stdout text)."""
        from tools.orchestration_runtime import _load_spec_catalog
        _load_spec_catalog.cache_clear()
        captured: list[dict] = []
        ran: set[str] = set()

        def fake_run_node(**kw):
            captured.append(kw)
            ran.add(kw["spec_ref"])
            return 0

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
                    resume=resume,
                    prior_orch_by_spec=prior_orch_by_spec,
                    raw_argv=["spec/problem/a", "validate", "--with-deps"],
                )
        finally:
            run_workflow._run_node = orig  # type: ignore[assignment]
            run_workflow._dependency_node_ready = orig_ready  # type: ignore[assignment]
        return rc, captured, buf.getvalue()

    def _drive_closure_capture(self, repo_root, *, resume, prior_orch_by_spec):
        """Like `_drive_closure_raw` but asserts a clean (rc 0) run and returns just the
        captured _run_node kwargs dicts."""
        rc, captured, _ = self._drive_closure_raw(
            repo_root, resume=resume, prior_orch_by_spec=prior_orch_by_spec)
        self.assertEqual(rc, 0)
        return captured

    def test_fresh_closure_records_closure_id_on_every_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            captured = self._drive_closure_capture(
                repo_root, resume=False, prior_orch_by_spec=None)
            # c, b, then target a
            self.assertEqual([c["spec_ref"] for c in captured],
                             ["spec/component/c", "spec/component/b", "spec/problem/a"])
            for kw in captured:
                self.assertFalse(kw["resume_mode"])
                self.assertIsNotNone(kw["invocation"])
                self.assertEqual(kw["invocation"]["closure_id"], "orch_target")
                self.assertEqual(kw["invocation"]["closure_target_spec_ref"],
                                 "spec/problem/a")
                self.assertTrue(kw["invocation"]["with_deps"])

    def test_closure_resume_reuses_prior_orch_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(repo_root, "orch_c_prev", "spec/component/c")
            captured = self._drive_closure_capture(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            by_spec = {c["spec_ref"]: c for c in captured}
            # c has a prior orch → resumed warm, no fresh invocation
            self.assertEqual(by_spec["spec/component/c"]["orchestration_id"], "orch_c_prev")
            self.assertTrue(by_spec["spec/component/c"]["resume_mode"])
            self.assertIsNone(by_spec["spec/component/c"]["invocation"])
            # b has no prior orch → fresh, records the closure invocation
            self.assertFalse(by_spec["spec/component/b"]["resume_mode"])
            self.assertIsNotNone(by_spec["spec/component/b"]["invocation"])
            self.assertEqual(
                by_spec["spec/component/b"]["invocation"]["closure_id"], "orch_target")

    def test_closure_resume_refreshes_closure_until_on_resumed_deps(self) -> None:
        # A resumed dependency gets the effective closure until_phase forwarded so its
        # persisted copy stays current (durable phase override); a freshly cold-inited
        # node relies on its written invocation, not this arg.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(repo_root, "orch_c_prev", "spec/component/c")
            captured = self._drive_closure_capture(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            by_spec = {c["spec_ref"]: c for c in captured}
            # c is resumed → closure_until_phase forwarded (= the closure until)
            self.assertEqual(by_spec["spec/component/c"]["closure_until_phase"], "Validate")
            # b is fresh → not forwarded (cold init writes it via invocation)
            self.assertIsNone(by_spec["spec/component/b"]["closure_until_phase"])

    def test_closure_resume_target_resume_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            # target orchestration already exists AND is this closure's prior target
            # run (matching spec_ref AND invocation.closure_id) → target is warm-resumed.
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            target_meta.write_text(json.dumps(
                {"spec_ref": "spec/problem/a",
                 "invocation": {"closure_id": "orch_target", "generate_executor": "pure"}}),
                encoding="utf-8")
            captured = self._drive_closure_capture(
                repo_root, resume=True, prior_orch_by_spec={})
            target = [c for c in captured if c["spec_ref"] == "spec/problem/a"][0]
            self.assertEqual(target["orchestration_id"], "orch_target")
            self.assertTrue(target["resume_mode"])
            self.assertIsNone(target["invocation"])

    def test_closure_resume_legacy_recorded_dependency_fails_closed(self) -> None:
        # M-F: a closure resume must validate EVERY warm-resumed member, not just the entry. A
        # dependency orchestration recorded `legacy` (a mixed closure) must fail-close here rather
        # than silently resume under the pure-only dispatch.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            self._seed_prior_member(repo_root, "orch_c_prev", "spec/component/c",
                                    executor="legacy")
            rc, captured, out = self._drive_closure_raw(
                repo_root, resume=True,
                prior_orch_by_spec={"spec/component/c": "orch_c_prev"})
            self.assertEqual(rc, 2, out)
            self.assertIn("generate_executor_legacy_removed", out)
            # the legacy dependency was NOT resumed (fail-closed before _run_node)
            self.assertNotIn("spec/component/c",
                             [c["spec_ref"] for c in captured])

    def test_closure_resume_prefield_recorded_target_fails_closed(self) -> None:
        # M-F: a warm-resumed TARGET whose recorded executor is absent (a pre-adoption run) must
        # also fail-close, mirroring the per-dependency gate.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            # spec_ref + closure_id match → target_resume True, but no generate_executor recorded.
            target_meta.write_text(json.dumps(
                {"spec_ref": "spec/problem/a",
                 "invocation": {"closure_id": "orch_target"}}),
                encoding="utf-8")
            rc, captured, out = self._drive_closure_raw(
                repo_root, resume=True, prior_orch_by_spec={})
            self.assertEqual(rc, 2, out)
            self.assertIn("generate_executor_legacy_removed", out)
            self.assertNotIn("spec/problem/a",
                             [c["spec_ref"] for c in captured])

    def test_closure_resume_target_unrelated_meta_cold_inits(self) -> None:
        # The reserved target id already holds an UNRELATED pre-existing
        # orchestration (different spec) that the failed closure never reached.
        # It must be cold-initialized as the intended target, NOT warm-resumed off
        # the unrelated run's checkpoint.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            target_meta.write_text(
                json.dumps({"spec_ref": "spec/problem/UNRELATED", "status": "pass"}),
                encoding="utf-8")
            captured = self._drive_closure_capture(
                repo_root, resume=True, prior_orch_by_spec={})
            target = [c for c in captured if c["spec_ref"] == "spec/problem/a"][0]
            self.assertEqual(target["orchestration_id"], "orch_target")
            self.assertFalse(target["resume_mode"], "unrelated meta must not warm-resume")
            # cold init → a fresh closure invocation is written for the real target
            self.assertIsNotNone(target["invocation"])
            self.assertEqual(target["invocation"]["closure_target_spec_ref"],
                             "spec/problem/a")

    def test_closure_resume_target_same_spec_unlinked_cold_inits(self) -> None:
        # The reserved target id holds an orchestration for the SAME spec but not
        # created as THIS closure's target (e.g. a standalone run under a reused id):
        # no invocation.closure_id link. Must cold-init, not warm-resume its stale
        # checkpoint. Guards spec-match-only false positive.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            target_meta = (repo_root / "workspace" / "orchestrations"
                           / "orch_target" / "orchestration_meta.json")
            target_meta.parent.mkdir(parents=True, exist_ok=True)
            # same spec as the target, but NO closure link (standalone prior run)
            target_meta.write_text(
                json.dumps({"spec_ref": "spec/problem/a", "status": "pass"}),
                encoding="utf-8")
            captured = self._drive_closure_capture(
                repo_root, resume=True, prior_orch_by_spec={})
            target = [c for c in captured if c["spec_ref"] == "spec/problem/a"][0]
            self.assertFalse(target["resume_mode"],
                             "same-spec but unlinked orch must not warm-resume")
            self.assertIsNotNone(target["invocation"])

    def test_driver_renders_closure_events_in_human_mode(self) -> None:
        # In human mode the closure-level events (dependency_node_begin and the
        # final failure summary) must NOT leak raw JSON: they go through the same
        # _format_event_human renderer the per-node tee uses.
        from tools.orchestration_runtime import _load_spec_catalog
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _seed_shape_expr_schema_into(repo_root)
            self._seed_diamond(repo_root)
            _load_spec_catalog.cache_clear()

            def fake_run_node(**kw):
                return 0  # success, but no artifacts → not_ready_after_run

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
                        stdout_format="human",
                    )
            finally:
                run_workflow._run_node = orig  # type: ignore[assignment]

            self.assertEqual(rc, 2)
            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            # No raw JSON braces leak onto the terminal in human mode.
            self.assertFalse(
                any(ln.lstrip().startswith("{") for ln in lines), lines)
            # dependency_node_begin renders with the [dep ] prefix.
            self.assertTrue(
                any(ln.startswith("[dep ]") and "component/c" in ln
                    for ln in lines),
                lines,
            )
            # The closure failure summary renders with the [FAIL] prefix.
            self.assertTrue(
                any(ln.startswith("[FAIL]")
                    and "dependency_not_ready_after_run" in ln
                    for ln in lines),
                lines,
            )

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


class StdoutTeeTests(unittest.TestCase):
    """Cover the host-side run-log tee added to run_workflow: stdout mirroring,
    best-effort IO suppression, attribute fall-through, and the open helper's
    success / failure (None) contract plus filename collision-safety."""

    def test_tee_mirrors_to_both_stream_and_log(self) -> None:
        terminal = io.StringIO()
        logf = io.StringIO()
        tee = run_workflow._StdoutTee(terminal, logf)
        n = tee.write("hello\n")
        self.assertEqual(n, len("hello\n"))
        self.assertEqual(terminal.getvalue(), "hello\n")
        self.assertEqual(logf.getvalue(), "hello\n")

    def test_tee_swallows_log_write_errors_without_losing_terminal(self) -> None:
        terminal = io.StringIO()

        class _BrokenLog:
            def write(self, data: str) -> int:
                raise OSError("disk full")

            def flush(self) -> None:
                raise OSError("disk full")

        tee = run_workflow._StdoutTee(terminal, _BrokenLog())
        # Must not raise, and the terminal must still receive the data.
        tee.write("payload\n")
        tee.flush()
        self.assertEqual(terminal.getvalue(), "payload\n")

    def test_tee_attribute_fall_through(self) -> None:
        # fileno() is load-bearing: subprocesses derive stdout from the parent fd.
        tee = run_workflow._StdoutTee(sys.__stdout__, io.StringIO())
        self.assertEqual(tee.fileno(), sys.__stdout__.fileno())

    def test_open_run_log_writes_unique_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            oid = "orch_log_001"
            f1 = run_workflow._open_run_log(repo_root, oid)
            f2 = run_workflow._open_run_log(repo_root, oid)
            self.assertIsNotNone(f1)
            self.assertIsNotNone(f2)
            try:
                run_logs = repo_root / "workspace" / "orchestrations" / oid / "run_logs"
                files = sorted(run_logs.glob("run_*.jsonl"))
                # Two opens against the SAME orchestration_id (the --resume case)
                # must not collide.
                self.assertEqual(len(files), 2)
                for p in files:
                    self.assertTrue(p.name.startswith("run_"))
                    self.assertTrue(p.name.endswith(".jsonl"))
            finally:
                for f in (f1, f2):
                    if f is not None:
                        f.close()

    def test_open_run_log_returns_none_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            # Make `workspace` a regular file so mkdir of the run_logs dir fails;
            # the helper must degrade to None rather than raise.
            (repo_root / "workspace").write_text("not a dir", encoding="utf-8")
            self.assertIsNone(run_workflow._open_run_log(repo_root, "orch_x"))

    def test_run_node_closes_log_and_restores_stdout_when_node_start_print_raises(
        self,
    ) -> None:
        """Regression: the tee swap + node_start print must be INSIDE the try so a
        raising print (e.g. a broken terminal pipe, which the tee does not swallow
        for the real stream) still triggers the finally — closing the log file and
        restoring stdout — instead of leaking the handle and leaving stdout
        wrapped."""

        class _TrackedLog:
            def __init__(self) -> None:
                self.closed = False

            def write(self, data: str) -> int:
                return len(data)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class _BrokenStdout:
            def write(self, data: str) -> int:
                raise BrokenPipeError("closed pipe")

            def flush(self) -> None:
                pass

        tracked = _TrackedLog()
        orig_open = run_workflow._open_run_log
        run_workflow._open_run_log = lambda *a, **k: tracked  # type: ignore[assignment]
        saved_stdout = sys.stdout
        broken = _BrokenStdout()
        with tempfile.TemporaryDirectory() as tmp:
            sys.stdout = broken  # type: ignore[assignment]
            try:
                with self.assertRaises(BrokenPipeError):
                    run_workflow._run_node(
                        repo_root=Path(tmp),
                        base_env={},
                        orchestration_id="orch_leak_001",
                        spec_ref="spec/x",
                        source_dependency_ref="spec/x/deps.yaml",
                        until_phase="compile",
                        llm="claude",
                        llm_command="claude",
                        workflow_mode="dev",
                        agent_model=None,
                        status="running",
                        invoke_llm=False,
                        resume_mode=False,
                    )
                # stdout restored to the original stream (not left wrapped), and the
                # log file handle closed — no leak.
                self.assertIs(sys.stdout, broken)
                self.assertNotIsInstance(sys.stdout, run_workflow._StdoutTee)
                self.assertTrue(tracked.closed)
            finally:
                sys.stdout = saved_stdout
                run_workflow._open_run_log = orig_open  # type: ignore[assignment]


class StdoutFormatTests(unittest.TestCase):
    """Cover the new --stdout-format flag, the human formatter, and the
    run_logs always-full-jsonl contract."""

    def _seed(self, repo_root: Path) -> None:
        _seed_shape_expr_schema_into(repo_root)
        (repo_root / "tools").mkdir(parents=True, exist_ok=True)
        (repo_root / "workspace").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem").mkdir(parents=True, exist_ok=True)
        (repo_root / "spec" / "problem" / "test.md").write_text(
            "spec\n", encoding="utf-8"
        )
        (repo_root / "spec" / "problem" / "deps.yaml").write_text(
            "nodes: []\n", encoding="utf-8"
        )

    def _fake_runtime(self, args, *, oar: str = "orch_agent_run_fmt"):
        # Minimal fake init/preflight so main() can reach the final summary.
        if args[0] == "init":
            return run_workflow.RuntimeResult(
                payload={"status": "ok", "orchestration_agent_run_id": oar},
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

    def test_human_format_renders_node_start_and_final_summary(self) -> None:
        """In human mode the operator sees compact lines, not raw JSON, for the
        node-start announcement and the final ok summary."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            orig = run_workflow._runtime_command
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = (  # type: ignore[assignment]
                    lambda root, env, args: self._fake_runtime(args))
                with redirect_stdout(buf):
                    code = run_workflow.main([
                        "spec/problem/test.md", "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_human_fmt",
                        "--no-invoke-llm",
                        "--stdout-format", "human",
                    ])
            finally:
                run_workflow._runtime_command = orig  # type: ignore[assignment]
            self.assertEqual(code, 0)
            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            # No JSON braces leaking onto the terminal in human mode.
            self.assertFalse(any(ln.lstrip().startswith("{") for ln in lines), lines)
            # node_start renders with the [node] prefix and the spec/until fields.
            self.assertTrue(
                any(ln.startswith("[node]") and "spec=spec/problem/test.md" in ln
                    and "until=Build" in ln for ln in lines),
                lines,
            )
            # The final ok summary renders with the [ok  ] prefix.
            self.assertTrue(any(ln.startswith("[ok") for ln in lines), lines)

    def test_jsonl_format_keeps_raw_json_on_stdout(self) -> None:
        """--stdout-format jsonl emits the raw structured payload so existing
        parsers see the same JSONL contract they always have."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._seed(repo_root)
            orig = run_workflow._runtime_command
            buf = io.StringIO()
            try:
                run_workflow._runtime_command = (  # type: ignore[assignment]
                    lambda root, env, args: self._fake_runtime(args))
                with redirect_stdout(buf):
                    code = run_workflow.main([
                        "spec/problem/test.md", "build",
                        "--repo-root", str(repo_root),
                        "--orchestration-id", "orch_jsonl_fmt",
                        "--no-invoke-llm",
                        "--stdout-format", "jsonl",
                    ])
            finally:
                run_workflow._runtime_command = orig  # type: ignore[assignment]
            self.assertEqual(code, 0)
            # Every non-empty line must parse as JSON in jsonl mode.
            events = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.strip()]
            self.assertTrue(any(e.get("event") == "node_start" for e in events))
            self.assertEqual(events[-1].get("status"), "ok")

    def test_run_logs_always_contain_full_jsonl_regardless_of_mode(self) -> None:
        """Whichever stdout format the operator picked, the per-run jsonl file
        under workspace/orchestrations/<oid>/run_logs/ must hold the raw JSON
        payloads of every event — it is the workspace-side full-fidelity
        record."""
        for mode, oid in (("human", "orch_log_human"), ("jsonl", "orch_log_jsonl")):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    repo_root = Path(tmp)
                    self._seed(repo_root)
                    orig = run_workflow._runtime_command
                    try:
                        run_workflow._runtime_command = (  # type: ignore[assignment]
                            lambda root, env, args: self._fake_runtime(args))
                        code = run_workflow.main([
                            "spec/problem/test.md", "build",
                            "--repo-root", str(repo_root),
                            "--orchestration-id", oid,
                            "--no-invoke-llm",
                            "--stdout-format", mode,
                        ])
                    finally:
                        run_workflow._runtime_command = orig  # type: ignore[assignment]
                    self.assertEqual(code, 0)
                    run_logs = (
                        repo_root / "workspace" / "orchestrations" / oid
                        / "run_logs"
                    )
                    files = sorted(run_logs.glob("run_*.jsonl"))
                    self.assertEqual(len(files), 1, mode)
                    contents = files[0].read_text(encoding="utf-8")
                    events = [
                        json.loads(ln) for ln in contents.splitlines() if ln.strip()
                    ]
                    self.assertTrue(
                        any(e.get("event") == "node_start" for e in events), mode)
                    self.assertEqual(events[-1].get("status"), "ok", mode)

    def test_format_event_human_known_events(self) -> None:
        """Spot-check the human formatter for each shape the conductor and the
        run_workflow driver actually emit, so a wording change is a deliberate
        edit rather than a silent drift."""
        f = run_workflow._format_event_human
        self.assertEqual(
            f({"status": "info", "event": "node_start",
               "spec_ref": "spec/x", "until_phase": "Build",
               "orchestration_id": "orch_1", "resume": False}),
            "[node] spec=spec/x until=Build orch=orch_1",
        )
        self.assertIn(
            "[resume]",
            f({"status": "info", "event": "node_start",
               "spec_ref": "spec/x", "until_phase": "Build",
               "orchestration_id": "orch_1", "resume": True}) or "",
        )
        self.assertEqual(
            f({"status": "info", "event": "phase_start",
               "node_key": "n", "phase": "compile", "attempt": 2,
               "orchestration_id": "o"}),
            "  [phase   ] compile (attempt 2)",
        )
        self.assertEqual(
            f({"status": "info", "event": "phase_complete",
               "node_key": "n", "phase": "generate", "result": "pass",
               "elapsed_seconds": 12.34, "orchestration_id": "o"}),
            "  [phase   ] generate ok (12.34s)",
        )
        self.assertIn(
            "skipped (resumed)",
            f({"status": "info", "event": "phase_complete",
               "node_key": "n", "phase": "compile", "result": "skipped",
               "orchestration_id": "o"}) or "",
        )
        self.assertEqual(
            f({"status": "info", "event": "substep_start",
               "node_key": "n", "phase": "validate", "substep": "execute",
               "attempt": 1, "orchestration_id": "o"}),
            "    [substep] validate.execute ...",
        )
        self.assertEqual(
            f({"status": "info", "event": "substep_complete",
               "node_key": "n", "phase": "validate", "substep": "judge",
               "result": "pass", "elapsed_seconds": 4.5,
               "agent_run_id": "ar_judge", "orchestration_id": "o"}),
            "    [substep] validate.judge ok (4.5s)",
        )
        # Non-pass substep tags arid so the operator can jump to its dir.
        self.assertIn(
            "FAIL".lower() if False else "",  # placeholder to keep test stable
            f({"status": "info", "event": "substep_complete",
               "node_key": "n", "phase": "build", "substep": "step",
               "result": "fail", "elapsed_seconds": 2.0,
               "agent_run_id": "ar_x", "orchestration_id": "o"}) or "",
        )
        fail_line = f({"status": "info", "event": "substep_complete",
                       "node_key": "n", "phase": "build", "substep": "step",
                       "result": "fail", "elapsed_seconds": 2.0,
                       "agent_run_id": "ar_x", "orchestration_id": "o"})
        self.assertIn("fail", fail_line or "")
        self.assertIn("arid=ar_x", fail_line or "")
        # A transient-transport retry: the run is NOT stuck and the operator should not kill it,
        # so the wait is announced rather than left as a silent gap in the stream.
        retry_line = f({"status": "info", "event": "leaf_transient_retry",
                        "node_key": "n", "step": "compile", "substep": "verify",
                        "tag": "llm_transport_flake", "attempt": 1, "max_attempts": 3,
                        "backoff_seconds": 2.0, "dead_agent_run_id": "ar_dead",
                        "orchestration_id": "o"})
        self.assertEqual(
            retry_line,
            "    [warn   ] transient leaf failure (llm_transport_flake) in compile.verify "
            "[attempt 1/3]: retrying in 2.0s",
        )
        # Final ok / fail summaries.
        self.assertTrue(
            (f({"status": "ok", "orchestration_id": "orch_1",
                "workflow_status": "pass", "llm_invoked": True}) or "")
            .startswith("[ok"),
        )
        self.assertTrue(
            (f({"status": "fail", "orchestration_id": "orch_1",
                "reason": "preflight_failed", "detail": "x"}) or "")
            .startswith("[FAIL]"),
        )
        # Unknown event shapes return None so the caller falls back to JSON.
        self.assertIsNone(f({"status": "info", "event": "unknown_marker"}))
        self.assertIsNone(f({"hello": "world"}))


class SubstepEventTests(unittest.TestCase):
    """The conductor must surface per-substep activity (start/complete) so the
    host event stream is informative even during long substep loops."""

    def test_run_phase_emits_substep_start_and_complete(self) -> None:
        import tools.workflow_conductor as wc

        # Drive run_phase via a minimal stub conductor. We only need to verify
        # that the substep_start/substep_complete pair fire for every substep
        # of a phase, in order, with the phase + substep labels and a result.
        captured: list[dict[str, object]] = []

        class _Stub(wc.Conductor):
            def __init__(self):
                pass

            orchestration_id = "orch_sub"
            orchestration_agent_run_id = "orch_agent_run"
            workflow_mode = "dev"
            backend = "claude"

            def emit(self, event, **fields):
                captured.append({"event": event, **fields})

            def check_step_completed(self, *_a, **_k):
                return None

            def workflow_launch_check(self, *_a, **_k):
                return None

            def _ensure_fresh_producer_id(self, *_a, **_k):
                return None

            def _write_lineage(self, *_a, **_k):
                return ()

            def _conductor_authors_makefile(self, *_a, **_k):
                return False

            def _judge_pre_spawn_dag_block(self, *_a, **_k):
                return None

            def run_substep(self, refs, phase, substep, repair=None,
                            resolved_dependencies=()):
                return wc.SubstepOutcome(
                    agent_run_id=f"ar_{phase}_{substep or 'step'}",
                    status="pass", output_refs=[], leaf_returncode=0,
                )

            def write_step_result(self, *_a, **_k):
                return None

            def _resolve_exe_name(self, *_a, **_k):
                return None

        stub = _Stub()
        # Validate uses four substeps (pre_judge, execute, judge, post_judge).
        refs = wc.NodeRefs(
            node_key="component/x@0.1.0", spec_path="spec/x",
            ir_id="ir1", pipeline_id="pl1",
            source_id="src", binary_id="bin", run_id="r1", source_binary_id="bin",
        )
        outcome = stub.run_phase(refs, "validate")
        self.assertEqual(outcome.status, "pass")
        starts = [e for e in captured if e["event"] == "substep_start"]
        completes = [e for e in captured if e["event"] == "substep_complete"]
        self.assertEqual([(e["phase"], e["substep"]) for e in starts],
                         [("validate", "pre_judge"), ("validate", "execute"),
                          ("validate", "judge"), ("validate", "post_judge")])
        self.assertEqual([(e["phase"], e["substep"], e["result"]) for e in completes],
                         [("validate", "pre_judge", "pass"),
                          ("validate", "execute", "pass"),
                          ("validate", "judge", "pass"),
                          ("validate", "post_judge", "pass")])
        # Every complete carries a numeric elapsed_seconds and the substep's arid.
        for e in completes:
            self.assertIsInstance(e["elapsed_seconds"], (int, float))
            self.assertTrue(str(e["agent_run_id"]).startswith("ar_validate_"))

    def test_run_phase_build_emits_step_label_for_none_substep(self) -> None:
        """Build's SUBSTEPS == (None,) — the host event stream must still label
        the substep field so the operator gets a readable line. We render
        ``None`` as ``"step"`` (the agent_role of the single child)."""
        import tools.workflow_conductor as wc

        captured: list[dict[str, object]] = []

        class _Stub(wc.Conductor):
            def __init__(self):
                pass

            orchestration_id = "orch_sub_build"
            orchestration_agent_run_id = "orch_agent_run"
            workflow_mode = "dev"
            backend = "claude"

            def emit(self, event, **fields):
                captured.append({"event": event, **fields})

            def check_step_completed(self, *_a, **_k):
                return None

            def workflow_launch_check(self, *_a, **_k):
                return None

            def _ensure_fresh_producer_id(self, *_a, **_k):
                return None

            def _write_lineage(self, *_a, **_k):
                return ()

            def _conductor_authors_makefile(self, *_a, **_k):
                return False

            def run_substep(self, refs, phase, substep, repair=None,
                            resolved_dependencies=()):
                return wc.SubstepOutcome(
                    agent_run_id="ar_build", status="pass",
                    output_refs=[], leaf_returncode=0,
                )

            def write_step_result(self, *_a, **_k):
                return None

            def _resolve_exe_name(self, *_a, **_k):
                return None

        stub = _Stub()
        refs = wc.NodeRefs(
            node_key="component/x@0.1.0", spec_path="spec/x",
            ir_id="ir1", pipeline_id="pl1",
            source_id="src", binary_id="bin", run_id="r1", source_binary_id="bin",
        )
        outcome = stub.run_phase(refs, "build")
        self.assertEqual(outcome.status, "pass")
        starts = [e for e in captured if e["event"] == "substep_start"]
        self.assertEqual(starts[0]["substep"], "step")


if __name__ == "__main__":
    unittest.main()
