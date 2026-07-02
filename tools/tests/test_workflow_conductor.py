"""Tests for tools/workflow_conductor.py.

The payload-builder tests validate build_launch_request() field-for-field against
real, working launches/*.request.json artifacts captured from a successful run, so
the deterministic conductor reproduces exactly what the LLM orchestration agent
assembled. The decision-table tests pin the deterministic failure routing.
"""

from __future__ import annotations

import glob
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import tools.workflow_conductor as wc

REPO_ROOT = Path(__file__).resolve().parents[2]
# Tracked, slim copies of real working launch requests (one per step/substep),
# captured from orch_20260619T113225Z_f48fe14b. Committed under test data because
# workspace/ is gitignored — a clean checkout/CI has no live orchestration.
_FIXTURE_DIR = Path(__file__).resolve().parent / "data" / "conductor_launch_requests"
# spec dir for the captured node (derived from the compile request's deps ref).
_SPEC_PATH = (
    "spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2"
)

# Fields record-launch adds/derives; not produced by build_launch_request.
_NON_BUILDER_KEYS = {
    "launch_prompt_full",
    "launch_prompt_ref",
    "child_launch_request_ref",
    "child_launch_prompt_ref",
    "sandbox_profile_ref",
    "_resolved_build_system",
}


def _load_real_requests() -> dict[tuple[str, str | None], dict]:
    """One representative real request per (step, substep), from tracked fixtures."""
    out: dict[tuple[str, str | None], dict] = {}
    for f in sorted(glob.glob(str(_FIXTURE_DIR / "*.request.json"))):
        d = json.load(open(f, encoding="utf-8"))
        out[(d.get("step"), d.get("substep"))] = d
    return out


def _binary_id_from_must_read(must_read: str) -> str | None:
    for ref in must_read.split(","):
        if "/binary/" in ref and ref.endswith("binary_meta.json"):
            return ref.split("/binary/")[1].split("/")[0]
    return None


def _source_id_from_must_read(must_read: str) -> str | None:
    for ref in must_read.split(","):
        if "/source/" in ref and ref.endswith("source_meta.json"):
            return ref.split("/source/")[1].split("/")[0]
    return None


def _case_ids_from_outputs(paths: list[str]) -> tuple[str, ...]:
    cids = []
    for p in paths:
        if "/raw/state_snapshots/" in p and p.endswith(".json"):
            name = p.rsplit("/", 1)[1][:-5]
            if name != "snapshot_schema":
                cids.append(name)
    return tuple(cids)


def _evidence_artifacts_from_outputs(paths: list[str]) -> tuple[str, ...]:
    arts = []
    if any("/raw/state_snapshots/" in p for p in paths):
        arts.append("state_snapshots")
    if any(p.endswith("/raw/execution_trace.json") for p in paths):
        arts.append("execution_trace.json")
    return tuple(arts) or ("state_snapshots",)


def _refs_from_request(req: dict) -> wc.NodeRefs:
    ir_id = req["ir_ref"].rsplit("/", 1)[1]
    pipeline_id = req["pipeline_ref"].rsplit("/", 1)[1]
    must_read = req.get("skill_must_read_refs", "")
    return wc.NodeRefs(
        node_key=req["node_key"],
        spec_path=_SPEC_PATH,
        ir_id=ir_id,
        pipeline_id=pipeline_id,
        source_id=req.get("source_id") or _source_id_from_must_read(must_read),
        binary_id=req.get("binary_id") or _binary_id_from_must_read(must_read),
        run_id=req.get("run_id"),
        source_binary_id=req.get("source_binary_id"),
    )


class BuildLaunchRequestTest(unittest.TestCase):
    """build_launch_request reproduces real request.json payloads exactly."""

    def test_reproduces_every_real_substep_payload(self) -> None:
        real = _load_real_requests()
        self.assertTrue(real, "no captured request.json artifacts found")
        expected_keys = {
            ("compile", "generate"), ("compile", "verify"),
            ("generate", "generate"), ("generate", "verify"),
            ("build", None),
            ("validate", "execute"), ("validate", "judge"),
        }
        self.assertEqual(set(real), expected_keys, "captured fixture set changed")

        for (step, substep), req in real.items():
            with self.subTest(step=step, substep=substep):
                refs = _refs_from_request(req)
                built = wc.build_launch_request(
                    refs,
                    step=step,
                    substep=substep,
                    orchestration_id=req["orchestration_id"],
                    orchestration_agent_run_id=req["parent_agent_run_id"],
                    child_agent_run_id=req["agent_run_id"],
                    agent_model=req["agent_model"],
                    workflow_mode=req["workflow_mode"],
                    case_ids=_case_ids_from_outputs(req.get("allowed_output_paths", [])),
                    evidence_artifacts=_evidence_artifacts_from_outputs(
                        req.get("allowed_output_paths", [])),
                    repair={
                        k: req[k]
                        for k in ("issue_severity", "repair_strategy",
                                  "repair_target_agent_run_id", "repair_reason")
                        if k in req
                    },
                )
                # every field the builder produces must match the real payload
                for key, value in built.items():
                    self.assertIn(key, req, f"{step}/{substep}: builder emitted unknown key {key}")
                    self.assertEqual(
                        value, req[key],
                        f"{step}/{substep}: field {key} mismatch",
                    )
                # the builder must cover every real field except record-launch extras
                real_business_keys = set(req) - _NON_BUILDER_KEYS
                self.assertEqual(
                    real_business_keys - set(built), set(),
                    f"{step}/{substep}: builder missing fields",
                )

    def test_omits_launch_prompt_full(self) -> None:
        # record-launch must render the prompt; the builder must not supply it.
        refs = wc.NodeRefs(node_key="component/x@0.1.0", spec_path="spec/component/x",
                           ir_id="x_20260101_001", pipeline_id="x_20260101_001")
        req = wc.build_launch_request(
            refs, step="compile", substep="generate", orchestration_id="orch_x",
            orchestration_agent_run_id="parent", child_agent_run_id="child",
            agent_model="claude-opus-4-8", workflow_mode="dev",
        )
        self.assertNotIn("launch_prompt_full", req)

    def _generate_refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(node_key="component/x@0.1.0", spec_path="spec/component/x",
                           ir_id="x_20260101_001", pipeline_id="x_20260101_001",
                           source_id="src_20260101_002")

    def _reuse_repair(self) -> dict[str, str]:
        return {
            "issue_severity": "major", "repair_strategy": "reuse",
            "repair_target_agent_run_id": "child-1", "repair_reason": "lint_lint_findings",
            "repair_findings": "x_model.f90:61:17: C061 argument 'u_l' missing 'intent'",
        }

    def test_build_launch_request_sets_warm_resume_findings(self) -> None:
        # warm_resume + reuse repair carrying findings -> slim signal + emptied must-read.
        req = wc.build_launch_request(
            self._generate_refs(), step="generate", substep="generate",
            orchestration_id="orch_x", orchestration_agent_run_id="parent",
            child_agent_run_id="child-2", agent_model="m", workflow_mode="dev",
            repair=self._reuse_repair(), warm_resume=True)
        self.assertTrue(req.get("warm_resume"))
        self.assertEqual(req["skill_must_read_refs"], "")
        self.assertEqual(req["repair_findings"], self._reuse_repair()["repair_findings"])

    def test_build_launch_request_no_warm_resume_keeps_full_must_read(self) -> None:
        # Same reuse repair but warm_resume=False (session not resumable) -> full prompt:
        # no slim signal and the must-read list stays populated.
        req = wc.build_launch_request(
            self._generate_refs(), step="generate", substep="generate",
            orchestration_id="orch_x", orchestration_agent_run_id="parent",
            child_agent_run_id="child-2", agent_model="m", workflow_mode="dev",
            repair=self._reuse_repair(), warm_resume=False)
        self.assertNotIn("warm_resume", req)
        self.assertNotEqual(req["skill_must_read_refs"], "")


class ReuseResumeAndFindingsTest(unittest.TestCase):
    """The warm-resume eligibility resolver and the findings-excerpt reader that feed the
    slim repair turn."""

    def _conductor(self, env: dict) -> "_FakeConductor":
        c = _FakeConductor(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
                           orchestration_agent_run_id="ORCH", backend="claude", env=env)
        c.calls = []
        c.emit = lambda *a, **k: None  # type: ignore[assignment]
        return c

    def test_resolve_reuse_resume_returns_target_when_resumable(self) -> None:
        # Warm resume is always active for a reuse repair (no env gate).
        c = self._conductor({})
        c._claude_session_resumable = lambda s: True  # type: ignore[assignment]
        repair = {"repair_strategy": "reuse", "repair_target_agent_run_id": "child-1"}
        self.assertEqual(c._resolve_reuse_resume(repair, "generate", "generate"), "child-1")

    def test_resolve_reuse_resume_falls_back_cold_when_unresumable(self) -> None:
        c = self._conductor({})
        c._claude_session_resumable = lambda s: False  # type: ignore[assignment]
        emitted: list[str] = []
        c.emit = lambda ev, **k: emitted.append(ev)  # type: ignore[assignment]
        repair = {"repair_strategy": "reuse", "repair_target_agent_run_id": "child-1"}
        self.assertIsNone(c._resolve_reuse_resume(repair, "generate", "generate"))
        self.assertIn("resume_session_unavailable", emitted)

    def test_resolve_reuse_resume_none_for_restart_strategy(self) -> None:
        # restart stays cold (no resume) to avoid anchoring on the defective reasoning — this
        # strategy-driven warm/cold selection is preserved (LLM verify-attributed restarts stay
        # cold; only reuse repairs warm-resume).
        c = self._conductor({})
        c._claude_session_resumable = lambda s: True  # type: ignore[assignment]
        repair = {"repair_strategy": "restart", "repair_target_agent_run_id": "child-1"}
        self.assertIsNone(c._resolve_reuse_resume(repair, "generate", "generate"))

    def test_resolve_reuse_resume_none_for_non_claude_backend(self) -> None:
        # Warm --resume is a claude-only capability.
        c = _FakeConductor(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
                           orchestration_agent_run_id="ORCH", backend="codex", env={})
        c.calls = []
        c.emit = lambda *a, **k: None  # type: ignore[assignment]
        c._claude_session_resumable = lambda s: True  # type: ignore[assignment]
        repair = {"repair_strategy": "reuse", "repair_target_agent_run_id": "child-1"}
        self.assertIsNone(c._resolve_reuse_resume(repair, "generate", "generate"))

    def test_resolve_reuse_resume_none_when_no_repair(self) -> None:
        c = self._conductor({})
        c._claude_session_resumable = lambda s: True  # type: ignore[assignment]
        self.assertIsNone(c._resolve_reuse_resume(None, "generate", "generate"))

    def test_resolve_reuse_resume_none_when_target_placeholder(self) -> None:
        # A reuse repair with no concrete producer arid (literal "none") cannot resume.
        c = self._conductor({})
        c._claude_session_resumable = lambda s: True  # type: ignore[assignment]
        repair = {"repair_strategy": "reuse", "repair_target_agent_run_id": "none"}
        self.assertIsNone(c._resolve_reuse_resume(repair, "generate", "generate"))

    def test_read_repair_findings_reads_lint_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x",
                               ir_id="x_1", pipeline_id="x_1", source_id="src_1")
            meta_dir = repo / refs.source_dir()
            meta_dir.mkdir(parents=True)
            (meta_dir / "lint_meta.json").write_text(
                json.dumps({"failure_excerpt": "C061 argument 'u_l'"}), encoding="utf-8")
            c = _FakeConductor(repo_root=repo, orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertEqual(
                c._read_repair_findings(refs, "lint_lint_findings"), "C061 argument 'u_l'")
            # verify_* reason -> reads the phase's verify meta last_fail_reason. Absent -> None;
            # present -> returned (generate phase reads source_meta.json).
            self.assertIsNone(c._read_repair_findings(refs, "verify_minor", "generate"))
            (meta_dir / "source_meta.json").write_text(
                json.dumps({"last_fail_reason": "responsibility split violated"}),
                encoding="utf-8")
            self.assertEqual(
                c._read_repair_findings(refs, "verify_minor", "generate"),
                "responsibility split violated")
            # compile phase reads ir_meta.json#last_fail_reason instead.
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "ir_meta.json").write_text(
                json.dumps({"last_fail_reason": "io_contract recompute-insufficient"}),
                encoding="utf-8")
            self.assertEqual(
                c._read_repair_findings(refs, "verify_minor", "compile"),
                "io_contract recompute-insufficient")
            # Missing meta file -> None (falls back to full prompt).
            refs2 = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                                spec_path="spec/component/spec_x",
                                ir_id="x_1", pipeline_id="x_1", source_id="src_missing")
            self.assertIsNone(c._read_repair_findings(refs2, "static_post_generate_violation"))


class NodeRefsTest(unittest.TestCase):
    def test_safe_and_spec_id(self) -> None:
        refs = wc.NodeRefs(node_key="component/dynamics_advdiff_flux_1d_upwind_center2@0.1.0",
                           spec_path="spec/...", ir_id="a_1_1", pipeline_id="a_1_1")
        self.assertEqual(refs.safe, "component__dynamics_advdiff_flux_1d_upwind_center2__0.1.0")
        self.assertEqual(refs.spec_id, "dynamics_advdiff_flux_1d_upwind_center2")


class PhaseStructureTest(unittest.TestCase):
    def test_substeps_and_roles(self) -> None:
        self.assertEqual(wc.SUBSTEPS["compile"], ("generate", "static", "verify"))
        self.assertEqual(wc.SUBSTEPS["generate"], ("generate", "lint", "static", "verify"))
        self.assertEqual(wc.SUBSTEPS["build"], (None,))
        self.assertEqual(wc.SUBSTEPS["validate"],
                         ("pre_judge", "execute", "judge", "post_judge"))
        self.assertEqual(wc.child_agent_role("build"), "step")
        self.assertEqual(wc.child_agent_role("compile"), "substep")

    def test_phases_through(self) -> None:
        self.assertEqual(wc.phases_through("generate"), ("compile", "generate"))
        self.assertEqual(wc.phases_through("validate"), wc.PHASE_ORDER)


class DecisionTableTest(unittest.TestCase):
    def test_build_failure_routing(self) -> None:
        d = wc.classify_build_failure("compile_error")
        self.assertEqual((d.action, d.target_phase, d.repair_strategy), ("retry", "generate", "reuse"))
        d = wc.classify_build_failure("make_error")
        self.assertEqual((d.action, d.target_phase, d.repair_strategy), ("retry", "generate", "restart"))
        self.assertEqual(wc.classify_build_failure("weird").action, "escalate")
        self.assertEqual(wc.classify_build_failure(None).action, "escalate")

    def test_validate_judge_routing(self) -> None:
        self.assertEqual(wc.classify_validate_judge("pass", None).action, "advance")
        d = wc.classify_validate_judge("structural_violation", "ir")
        self.assertEqual((d.action, d.target_phase), ("reopen", "compile"))
        d = wc.classify_validate_judge("physics_fail", "code")
        self.assertEqual((d.action, d.target_phase, d.repair_strategy), ("retry", "generate", "reuse"))
        d = wc.classify_validate_judge("physics_fail", "spec")
        self.assertEqual(d.action, "fail_closed")
        d = wc.classify_validate_judge("evidence_mismatch", "evidence")
        self.assertEqual((d.action, d.target_phase, d.repair_strategy), ("retry", "validate", "re_execute"))
        self.assertEqual(wc.classify_validate_judge("novel_class", "code").action, "escalate")

    def test_dev_verify_severity_gate(self) -> None:
        self.assertEqual(wc.classify_verify_severity("none", "dev").action, "advance")
        # minor (both modes): warm (reuse) SAME-PHASE producer repair — not tolerated, not fail.
        # (A same-phase target + repair_strategy is what conduct keys the producer reopen on.)
        for mode in ("dev", "prod"):
            d = wc.classify_verify_severity("minor", mode)
            self.assertEqual((d.action, d.target_phase, d.repair_strategy),
                             ("retry", None, "reuse"), mode)
        # major/critical: dev hard-fails (fast feedback); prod escalates to the diagnostician.
        self.assertEqual(wc.classify_verify_severity("major", "dev").action, "fail_closed")
        self.assertEqual(wc.classify_verify_severity("critical", "dev").action, "fail_closed")
        self.assertEqual(wc.classify_verify_severity("major", "prod").action, "escalate")
        self.assertEqual(wc.classify_verify_severity("critical", "prod").action, "escalate")


class _FakeConductor(wc.Conductor):
    """Conductor with all I/O (runtime CLI, leaf spawn, artifact reads) stubbed,
    so the happy-path control flow + bookkeeping wiring can be asserted offline."""

    def runtime(self, args):  # type: ignore[override]
        sub = args[0]
        # capture the call (subcommand + parsed result-json/agent-run-json if present)
        captured: dict = {}
        for flag in ("--result-json", "--agent-run-json", "--request-json"):
            if flag in args:
                captured[flag] = json.loads(args[args.index(flag) + 1])
        for flag in ("--node-key", "--step", "--agent-run-id", "--status",
                     "--from-phase", "--reason", "--trigger-agent-run-id",
                     "--reason-code", "--reason-detail"):
            if flag in args:
                captured[flag] = args[args.index(flag) + 1]
        if "--run-ids" in args:  # nargs="+": collect until the next --flag or end
            start = args.index("--run-ids") + 1
            vals = []
            for tok in args[start:]:
                if tok.startswith("--"):
                    break
                vals.append(tok)
            captured["--run-ids"] = vals
        self.calls.append((sub, captured))
        if sub == "check-step-completed":
            return {}
        if sub == "workflow-launch-check":
            return {"status": "pass"}
        if sub == "record-launch":
            return {"launch_prompt_text": "PROMPT", "capability_token": "tok"}
        return {}

    def new_agent_run_id(self):  # type: ignore[override]
        self._n = getattr(self, "_n", 0) + 1
        return f"child-{self._n}"

    def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
        return wc.ProcResult(0, "", "")

    # Configurable meta payloads the fake writes for the deterministic validate gate substeps
    # (pre_judge / post_judge), so run_phase's gate-fail branch + the warm-resume mini-loop
    # read realistic metas without spawning the real subprocess bodies. Default None -> no
    # write (the stubbed determine_substep_status/status_fn drives the outcome). The real
    # bodies are exercised by the dedicated Validate gate tests.
    pre_judge_meta_fn = None   # (n) -> dict
    post_judge_meta_fn = None  # (n) -> dict

    def _run_deterministic_substep(self, refs, phase, substep, child_arid, request):  # type: ignore[override]
        # Build / Validate.{pre_judge,execute,post_judge} run in-process; the fake body is a
        # clean success. When a *_meta_fn is configured, author the corresponding gate meta so
        # run_phase's gate-fail branch + mini-loop see it.
        self._detn = getattr(self, "_detn", 0) + 1
        if phase == "validate" and substep == "pre_judge" and self.pre_judge_meta_fn:
            self._write_run_node_meta(refs, "pre_judge_meta.json",
                                      self.pre_judge_meta_fn(self._detn))
        if phase == "validate" and substep == "post_judge" and self.post_judge_meta_fn:
            self._write_run_node_meta(refs, "post_judge_meta.json",
                                      self.post_judge_meta_fn(self._detn))
        return wc.ProcResult(0, "", "")

    def read_parent_return_token(self, child_arid):  # type: ignore[override]
        return "rtok"

    def read_case_ids(self, refs):  # type: ignore[override]
        return ()

    # A no-op DAG readiness check so the fake's pre_judge substep passes by default (a real
    # _judge_pre_spawn_dag_block would read a nonexistent IR under the fake repo_root).
    def _judge_pre_spawn_dag_block(self, refs):  # type: ignore[override]
        return None

    # A no-op dependency-graph sidecar author so the Compile phase passes under the fake
    # repo_root (a real _write_dependency_graph would read a nonexistent deps.yaml and
    # fail_closed). The real builder is covered by test_dependency_graph.py and the
    # dedicated conductor tests that seed a real deps.yaml + catalog.
    def _write_dependency_graph(self, refs):  # type: ignore[override]
        return None

    # configurable hooks (default: everything passes)
    status_fn = None  # (phase, substep, n) -> "pass"|"fail"
    decision_fn = None  # (phase, outcomes) -> RouteDecision

    def determine_substep_status(self, refs, phase, substep, allowed_output_paths,
                                 min_mtime=0.0):  # type: ignore[override]
        if self.status_fn is not None:
            self._sn = getattr(self, "_sn", 0) + 1
            return self.status_fn(phase, substep, self._sn), ["out.json"]
        return "pass", ["out.json"]

    def classify_failure(self, refs, phase, outcomes):  # type: ignore[override]
        if self.decision_fn is not None:
            return self.decision_fn(phase, outcomes)
        return super().classify_failure(refs, phase, outcomes)


class ConductHappyPathTest(unittest.TestCase):
    def _conductor(self) -> _FakeConductor:
        c = _FakeConductor(
            repo_root=Path("/tmp/repo"),
            orchestration_id="orch_x",
            orchestration_agent_run_id="ORCH",
            backend="claude",
            env={},
        )
        c.calls = []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_20260101_001", pipeline_id="x_20260101_001",
            source_id="src_20260101_001", binary_id="bin_20260101_001",
            run_id="run_20260101_001", source_binary_id="bin_20260101_001",
        )

    def test_full_run_sequence_and_executors(self) -> None:
        c = self._conductor()
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "pass")
        subs = [s for s, _ in c.calls]

        # per phase: check-step-completed, workflow-launch-check, then per substep
        # (record-launch, [record-child-return if deterministic], finalize-child),
        # then write-step-result. Build and Validate.execute are deterministic (the
        # conductor issues their record-child-return); compile/generate/judge are leaves.
        expected = (
            ["check-step-completed", "workflow-launch-check",
             "record-launch", "finalize-child",  # compile.generate (leaf)
             "record-launch", "record-child-return", "finalize-child",  # compile.static (deterministic)
             "record-launch", "finalize-child",  # compile.verify (leaf)
             "write-step-result"]  # compile (2 leaf + 1 deterministic substep)
            + ["check-step-completed", "workflow-launch-check",
               "record-launch", "finalize-child",  # generate.generate (leaf)
               "record-launch", "record-child-return", "finalize-child",  # generate.lint (deterministic)
               "record-launch", "record-child-return", "finalize-child",  # generate.static (deterministic)
               "record-launch", "finalize-child",  # generate.verify (leaf)
               "write-step-result"]  # generate (2 leaf + 2 deterministic substeps)
            + ["check-step-completed", "workflow-launch-check",
               "record-launch", "record-child-return", "finalize-child",
               "write-step-result"]  # build (1 deterministic step)
            + ["check-step-completed", "workflow-launch-check",
               "record-launch", "record-child-return", "finalize-child",  # pre_judge (deterministic)
               "record-launch", "record-child-return", "finalize-child",  # execute (deterministic)
               "record-launch", "finalize-child",  # judge (leaf)
               "record-launch", "record-child-return", "finalize-child",  # post_judge (deterministic)
               "write-step-result"]  # validate (3 deterministic + 1 leaf substep)
            + ["set-status"]
        )
        self.assertEqual(subs, expected)

        # executor roles per phase
        wsr = [cap for s, cap in c.calls if s == "write-step-result"]
        by_step = {cap["--step"]: cap for cap in wsr}
        for substep_aware in ("compile", "generate", "validate"):
            self.assertEqual(by_step[substep_aware]["--agent-run-id"], "ORCH")
            # generate has 4 substeps (generate, lint, static, verify); compile has 3
            # (generate, static, verify); validate has 4 (pre_judge, execute, judge, post_judge).
            expected_substeps = {"generate": 4, "compile": 3, "validate": 4}[substep_aware]
            self.assertEqual(
                len(by_step[substep_aware]["--result-json"]["substep_agent_run_ids"]),
                expected_substeps)
            self.assertEqual(by_step[substep_aware]["--result-json"]["status"], "pass")
        # build executor is the (child) step agent, no substeps
        self.assertNotEqual(by_step["build"]["--agent-run-id"], "ORCH")
        self.assertEqual(by_step["build"]["--result-json"]["substep_agent_run_ids"], [])
        # validation_stage per phase
        self.assertEqual(by_step["compile"]["--result-json"]["validation_stage"], "compile")
        self.assertEqual(by_step["validate"]["--result-json"]["validation_stage"], "pre_judge")

        # final set-status is pass
        self.assertEqual(c.calls[-1][1]["--status"], "pass")

    def test_emits_phase_start_and_complete_with_elapsed(self) -> None:
        c = self._conductor()
        buf = io.StringIO()
        with redirect_stdout(buf):
            status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "pass")
        events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        starts = [e for e in events if e["event"] == "phase_start"]
        completes = [e for e in events if e["event"] == "phase_complete"]
        # one start + one complete per phase, in order
        self.assertEqual([e["phase"] for e in starts],
                         ["compile", "generate", "build", "validate"])
        self.assertEqual([e["phase"] for e in completes],
                         ["compile", "generate", "build", "validate"])
        for e in completes:
            self.assertEqual(e["result"], "pass")
            self.assertIsInstance(e["elapsed_seconds"], (int, float))
            self.assertEqual(e["node_key"], "component/spec_x@0.1.0")
            self.assertEqual(e["orchestration_id"], "orch_x")
        for e in starts:
            self.assertEqual(e["attempt"], 1)

    def test_resume_skipped_phase_reports_skipped_without_elapsed(self) -> None:
        c = self._conductor()
        # compile is already checkpointed complete -> run_phase short-circuits.
        c.check_step_completed = (  # type: ignore[method-assign]
            lambda node_key, step: {"integrity": "ok", "agent_run_id": "prev"}
            if step == "compile" else None
        )
        c._completed_producer_arid = lambda nk, ph, arid: ""  # type: ignore[method-assign]
        buf = io.StringIO()
        with redirect_stdout(buf):
            status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "pass")
        completes = [
            json.loads(line) for line in buf.getvalue().splitlines()
            if line.strip() and json.loads(line)["event"] == "phase_complete"
        ]
        self.assertEqual(len(completes), 1)
        self.assertEqual(completes[0]["result"], "skipped")
        self.assertNotIn("elapsed_seconds", completes[0])

    def test_run_conductor_falls_back_to_backend_alias(self) -> None:
        """run_conductor with no explicit agent_model uses the backend's unpinned
        alias (claude -> 'opus' default / codex -> 'codex'), never a pinned version."""
        from unittest.mock import patch
        seen: dict[str, str] = {}
        orig_init = wc.Conductor.__init__

        def _capture_init(self, **kw):  # type: ignore[no-untyped-def]
            seen["agent_model"] = kw.get("agent_model", "")
            orig_init(self, **kw)

        for backend, expected in (("codex", "codex"), ("claude", "opus")):
            seen.clear()
            with patch.object(wc, "resolve_node", return_value=("c/x@0.1.0", "spec/c/x")), \
                 patch.object(wc, "prepare_node",
                              return_value=wc.NodeRefs(node_key="c/x@0.1.0", spec_path="spec/c/x",
                                                       ir_id="x_1", pipeline_id="x_1")), \
                 patch.object(wc.Conductor, "__init__", _capture_init), \
                 patch.object(wc.Conductor, "conduct", return_value="pass"), \
                 patch("tools.orchestration_runtime.resolve_claude_model_alias",
                       return_value="opus"):
                status = wc.run_conductor(
                    repo_root="/tmp/repo", orchestration_id="o",
                    orchestration_agent_run_id="O", spec_ref="spec/c/x",
                    source_dependency_ref="d", until_phase="compile", backend=backend,
                    agent_model="", workflow_mode="dev", env={})
            self.assertEqual(status, "pass")
            self.assertEqual(seen["agent_model"], expected)
            self.assertNotRegex(seen["agent_model"], r"-\d+-\d+$")

    def test_agent_run_json_records_transcript_resolved_model(self) -> None:
        from unittest.mock import patch
        c = self._conductor()  # backend="claude"
        refs = self._refs()
        with patch(
            "tools.orchestration_runtime.resolve_claude_model_from_transcript",
            return_value="claude-opus-4-8",
        ) as m:
            payload = c._agent_run_json(
                refs, "compile", "generate", "child-arid-1", "pass", [])
        # the leaf's session id == its agent_run_id, so resolution keys on it
        m.assert_called_once_with("child-arid-1")
        self.assertEqual(payload["agent_model"], "claude-opus-4-8")

    def test_agent_run_json_omits_model_when_transcript_unresolved(self) -> None:
        from unittest.mock import patch
        c = self._conductor()  # backend="claude"
        refs = self._refs()
        with patch(
            "tools.orchestration_runtime.resolve_claude_model_from_transcript",
            return_value=None,
        ):
            payload = c._agent_run_json(
                refs, "compile", "generate", "child-arid-2", "pass", [])
        # unresolved -> left absent so record_agent_run backfills the launch alias
        self.assertNotIn("agent_model", payload)

    def test_agent_run_json_carries_step_and_substep(self) -> None:
        c = self._conductor()
        c.conduct(self._refs(), "validate")
        runs = [cap["--agent-run-json"] for s, cap in c.calls if s == "finalize-child"]
        self.assertTrue(runs)
        # every recorded run carries step; substep-aware leaves also carry substep
        self.assertTrue(all(r.get("step") for r in runs))
        substeps = {(r["step"], r.get("substep")) for r in runs}
        self.assertIn(("compile", "generate"), substeps)
        self.assertIn(("validate", "judge"), substeps)
        self.assertIn(("build", None), substeps)  # build step has no substep

    def test_validate_step_result_has_judge_launch_request_ref(self) -> None:
        c = self._conductor()
        c.conduct(self._refs(), "validate")
        wsr = {cap["--step"]: cap["--result-json"]
               for s, cap in c.calls if s == "write-step-result"}
        self.assertIn("launch_request_ref", wsr["validate"])
        self.assertTrue(wsr["validate"]["launch_request_ref"].endswith(".request.json"))
        # non-validate phases must NOT carry it (matches real step_result.json)
        self.assertNotIn("launch_request_ref", wsr["compile"])
        self.assertNotIn("launch_request_ref", wsr["build"])

    def test_validate_execute_failure_still_carries_launch_request_ref(self) -> None:
        # On an execute failure the judge never runs, but the terminal Validate
        # step_result must still carry a launch_request_ref (from the execute
        # substep) for the runtime pre_phase_complete hook.
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "execute") else "pass")
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("fail_closed", reason="exec_fail")
        c.conduct(self._refs(), "validate")
        wsr = {cap["--step"]: cap["--result-json"]
               for s, cap in c.calls if s == "write-step-result"}
        self.assertEqual(wsr["validate"]["status"], "fail")
        self.assertIn("launch_request_ref", wsr["validate"])
        self.assertTrue(wsr["validate"]["launch_request_ref"].endswith(".request.json"))

    def test_stops_at_until_phase(self) -> None:
        c = self._conductor()
        c.conduct(self._refs(), "compile")
        steps = {cap.get("--step") for s, cap in c.calls if s == "write-step-result"}
        self.assertEqual(steps, {"compile"})

    def test_failure_terminalises(self) -> None:
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail"  # compile.generate fails
        status = c.conduct(self._refs(), "validate")
        self.assertIn(status, ("fail", "fail_closed"))
        self.assertEqual(c.calls[-1][0], "set-status")
        self.assertIn(c.calls[-1][1]["--status"], ("fail", "fail_closed"))
        # only the first phase (compile) should have been attempted
        steps = [cap.get("--step") for s, cap in c.calls if s == "write-step-result"]
        self.assertEqual(steps, ["compile"])


class ConductRoutingTest(unittest.TestCase):
    """M3: deterministic failure routing (reopen / in-place retry / fail_closed)."""

    def _conductor(self) -> _FakeConductor:
        c = _FakeConductor(
            repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
            orchestration_agent_run_id="ORCH", backend="claude", env={},
        )
        c.calls = []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
            binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001",
        )

    def test_conductor_fail_closed_codes_are_allowlisted(self) -> None:
        # Every reason_code the conductor uses for set-status fail_closed must be in the
        # runtime's FAIL_CLOSED_REASON_CODES, or set-status rejects it (→ crash).
        from tools.orchestration_runtime import FAIL_CLOSED_REASON_CODES
        for code in ("leaf_transport_error", "retry_budget_exhausted",
                     "conductor_phase_fail_closed", "sandbox_enforcement_violation",
                     "dev_phase_rollback"):
            self.assertIn(code, FAIL_CLOSED_REASON_CODES)

    def test_generic_fail_closed_uses_allowlisted_reason_code(self) -> None:
        # A generic phase fail_closed decision (e.g. judge spec-attribution) maps to the
        # allowlisted conductor_phase_fail_closed code, with the specific reason in detail.
        from tools.orchestration_runtime import FAIL_CLOSED_REASON_CODES
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail" if phase == "compile" else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "fail_closed", reason="judge_physics_fail_spec")
        status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "fail_closed")
        ss = [cap for s, cap in c.calls if s == "set-status"][-1]
        self.assertEqual(ss["--reason-code"], "conductor_phase_fail_closed")
        self.assertIn(ss["--reason-code"], FAIL_CLOSED_REASON_CODES)
        self.assertEqual(ss["--reason-detail"], "judge_physics_fail_spec")

    def test_conduct_terminalizes_sandbox_enforcement_as_fail_closed(self) -> None:
        # A SandboxEnforcementError from a substep (bwrap on, no profile) must
        # terminalize as fail_closed(sandbox_not_enforced), not bubble up as a generic
        # conductor error / plain fail.
        c = self._conductor()

        def boom_run_phase(refs, phase, repair=None):  # type: ignore[no-untyped-def]
            raise wc.SandboxEnforcementError("no usable sandbox profile for child")

        c.run_phase = boom_run_phase  # type: ignore[assignment]
        status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "fail_closed")
        ss = [cap for s, cap in c.calls if s == "set-status"][-1]
        self.assertEqual(ss["--status"], "fail_closed")
        # must be an allowlisted FAIL_CLOSED_REASON_CODES value (runtime rejects others)
        self.assertEqual(ss["--reason-code"], "sandbox_enforcement_violation")

    def test_reopen_compile_on_ir_then_succeed(self) -> None:
        c = self._conductor()
        c.workflow_mode = "prod"  # cross-phase reopen is prod-only (dev fail_closes; see F1 tests)
        state = {"validate_fail_used": False}

        def status_fn(phase, substep, n):
            if phase == "validate" and substep == "judge" and not state["validate_fail_used"]:
                state["validate_fail_used"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "reopen", target_phase="compile", reason="judge_structural_violation_ir")

        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "compile")
        self.assertEqual(reopens[0]["--reason"], "judge_structural_violation_ir")
        # trigger is the failed (judge) substep arid
        self.assertTrue(reopens[0]["--trigger-agent-run-id"].startswith("child-"))
        # validate ran twice (once failed, once after reopen)
        validate_writes = [cap for s, cap in c.calls
                           if s == "write-step-result" and cap["--step"] == "validate"]
        self.assertEqual(len(validate_writes), 2)

    def test_lint_finding_warm_reopens_generate_same_phase(self) -> None:
        # A generate.lint finding routes retry/generate/reuse(lint_*); conduct must do a
        # SAME-PHASE warm reopen (reopen-phase --from-phase generate) and re-run generate,
        # not terminalize like the generic same/downstream branch.
        c = self._conductor()
        state = {"lint_failed": False}

        def status_fn(phase, substep, n):
            if phase == "generate" and substep == "lint" and not state["lint_failed"]:
                state["lint_failed"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="reuse",
            reason="lint_lint_findings")
        # Stub the on-disk excerpt read so the threading assertion does not need a real
        # lint_meta.json (the disk read itself is covered by ReuseResumeAndFindingsTest).
        c._read_repair_findings = lambda refs, reason, phase=None: "C061 argument 'u_l'"  # type: ignore[assignment]
        status = c.conduct(self._refs(), "generate")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "generate")
        self.assertEqual(reopens[0]["--reason"], "lint_lint_findings")
        # generate ran twice (lint-fail attempt, then clean attempt)
        gen_writes = [cap for s, cap in c.calls
                      if s == "write-step-result" and cap["--step"] == "generate"]
        self.assertEqual(len(gen_writes), 2)
        # The repair (2nd) generate.generate launch carries the findings excerpt; the
        # first (cold) launch does not.
        gen_launches = [cap["--request-json"] for s, cap in c.calls
                        if s == "record-launch"
                        and cap.get("--request-json", {}).get("step") == "generate"
                        and cap["--request-json"].get("substep") == "generate"]
        self.assertEqual(len(gen_launches), 2)
        self.assertNotIn("repair_findings", gen_launches[0])
        self.assertEqual(gen_launches[1].get("repair_findings"), "C061 argument 'u_l'")

    def test_static_finding_warm_reopens_generate_same_phase(self) -> None:
        # A generate.static finding routes retry/generate/reuse(static_*); conduct must do a
        # SAME-PHASE warm reopen exactly like a lint finding (same-phase target + reuse),
        # not terminalize like the generic same/downstream branch.
        c = self._conductor()
        state = {"static_failed": False}

        def status_fn(phase, substep, n):
            if phase == "generate" and substep == "static" and not state["static_failed"]:
                state["static_failed"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="reuse",
            reason="static_post_generate_violation")
        status = c.conduct(self._refs(), "generate")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "generate")
        self.assertEqual(reopens[0]["--reason"], "static_post_generate_violation")
        # generate ran twice (static-fail attempt, then clean attempt)
        gen_writes = [cap for s, cap in c.calls
                      if s == "write-step-result" and cap["--step"] == "generate"]
        self.assertEqual(len(gen_writes), 2)

    def test_compile_static_finding_warm_reopens_compile_same_phase(self) -> None:
        # A compile.static finding routes retry/compile/reuse (same-phase); conduct
        # must do a SAME-PHASE warm reopen (reopen-phase --from-phase compile) and re-run
        # compile, exactly like a generate.static finding reopens generate.
        c = self._conductor()
        state = {"static_failed": False}

        def status_fn(phase, substep, n):
            if phase == "compile" and substep == "static" and not state["static_failed"]:
                state["static_failed"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="compile", repair_strategy="reuse",
            reason="compile_static_compile_static_violation")
        status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "compile")
        # compile ran twice (static-fail attempt, then clean attempt)
        compile_writes = [cap for s, cap in c.calls
                          if s == "write-step-result" and cap["--step"] == "compile"]
        self.assertEqual(len(compile_writes), 2)

    def test_verify_minor_finding_warm_reopens_same_phase(self) -> None:
        # A minor verify finding is NOT tolerated: it routes retry/reuse (same-phase)
        # (via classify_verify_severity), so conduct warm-reopens the phase and re-runs the
        # producer (compile.generate) to fix it — instead of passing/terminalizing.
        c = self._conductor()
        state = {"verify_failed": False}

        def status_fn(phase, substep, n):
            if phase == "compile" and substep == "verify" and not state["verify_failed"]:
                state["verify_failed"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.classify_verify_severity("minor", "prod")
        status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "compile")
        compile_writes = [cap for s, cap in c.calls
                          if s == "write-step-result" and cap["--step"] == "compile"]
        self.assertEqual(len(compile_writes), 2)  # verify-fail attempt, then clean attempt

    def test_escalate_same_phase_producer_reopens(self) -> None:
        # The escalate diagnostician routes a same-phase producer re-run. G5: escalate() runs
        # resolve_severity_directive, so its directive arrives with a concrete repair_strategy
        # derived from severity (major -> reuse here); conduct's same-phase producer-reopen
        # branch then fires (no conduct-side strategy normalization). The strategy derivation
        # itself is unit-tested in DiagnosticianTest.test_resolve_severity_directive.
        c = self._conductor()
        state = {"verify_failed": False}

        def status_fn(phase, substep, n):
            if phase == "compile" and substep == "verify" and not state["verify_failed"]:
                state["verify_failed"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("escalate", reason="unclassified")
        # The diagnostician's resolved directive: re-run this phase's producer with a severity-
        # derived strategy (as escalate()/resolve_severity_directive produces in the real flow).
        c.escalate = lambda refs, phase, outcome: wc.RouteDecision(  # type: ignore[assignment]
            "retry", target_phase="compile", repair_strategy="reuse", severity="major",
            reason="diagnostician_regenerate_ir")
        status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "compile")
        compile_writes = [cap for s, cap in c.calls
                          if s == "write-step-result" and cap["--step"] == "compile"]
        self.assertEqual(len(compile_writes), 2)

    def test_escalate_ambiguous_null_target_terminalizes(self) -> None:
        # An ambiguous diagnostician directive (target_phase=None — the schema permits null) must
        # NOT be normalized into a same-phase producer restart; it terminalizes as malformed.
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "compile" and substep == "verify") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("escalate", reason="unclassified")
        c.escalate = lambda refs, phase, outcome: wc.RouteDecision(  # type: ignore[assignment]
            "retry", target_phase=None, reason="diag_ambiguous")
        status = c.conduct(self._refs(), "compile")
        self.assertIn(status, ("fail", "fail_closed"))
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_escalate_same_phase_build_validate_does_not_reopen(self) -> None:
        # The same-phase producer reopen is scoped to compile/generate (the only phases with a
        # re-runnable LLM producer + reopen_phase carve-out). A diagnostician same-phase decision
        # for validate (even with an explicit restart) must NOT fire the producer-reopen branch
        # (which would crash reopen_phase) — it terminalizes.
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "validate" and substep == "judge") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("escalate", reason="unclassified")
        c.escalate = lambda refs, phase, outcome: wc.RouteDecision(  # type: ignore[assignment]
            "retry", target_phase="validate", repair_strategy="restart", reason="diag")
        status = c.conduct(self._refs(), "validate")
        self.assertIn(status, ("fail", "fail_closed"))
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_fail_closed_on_spec_attribution(self) -> None:
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "judge") else "pass")
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("fail_closed", reason="physics_fail_spec")
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "fail_closed")
        self.assertEqual(c.calls[-1][1]["--status"], "fail_closed")

    def test_reopen_budget_exhausts_to_fail_closed(self) -> None:
        c = self._conductor()
        c.workflow_mode = "prod"  # cross-phase reopen budget is prod-only (dev fail_closes; F1)
        # validate always fails and always routes to reopen compile -> budget caps it.
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "judge") else "pass")
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "reopen", target_phase="compile", reason="judge_ir")
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "fail_closed")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), wc.MAX_ATTEMPTS_PER_PHASE)

    def test_same_phase_retry_terminalises_without_retry_decisions(self) -> None:
        # In-place retry is intentionally not done; a same-phase "retry" decision with NO
        # repair_strategy (a malformed/unflagged retry) terminalizes via conduct rather than
        # emitting the error-prone retry_decisions bookkeeping. (A real verify-minor carries
        # repair_strategy=reuse and warm-reopens the producer — covered separately.)
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "compile" and substep == "verify") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("retry", reason="unflagged_retry")
        status = c.conduct(self._refs(), "compile")
        self.assertIn(status, ("fail", "fail_closed"))
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])  # no cross-phase reopen
        compile_wsr = [cap for s, cap in c.calls
                       if s == "write-step-result" and cap["--step"] == "compile"]
        self.assertEqual(len(compile_wsr), 1)  # single attempt, one step_result
        rj = compile_wsr[0]["--result-json"]
        self.assertEqual(rj["status"], "fail")
        self.assertIsNone(rj["retry_decisions"])  # never emits retry_decisions
        # one attempt: generate + static + verify (verify failed, so all 3 ran)
        self.assertEqual(len(rj["substep_agent_run_ids"]), 3)


class DevPhaseRollbackTest(unittest.TestCase):
    """F1: in dev mode a cross-phase backward rollback (reopen, or a retry/reopen targeting
    an earlier phase) fail_closes immediately instead of auto-retrying; prod is unchanged."""

    def _conductor(self, mode: str = "dev") -> _FakeConductor:
        c = _FakeConductor(
            repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
            orchestration_agent_run_id="ORCH", backend="claude", env={},
            workflow_mode=mode,
        )
        c.calls = []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
            binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001")

    def _last_set_status(self, c: _FakeConductor) -> dict:
        return [cap for s, cap in c.calls if s == "set-status"][-1]

    def test_dev_validate_to_generate_rollback_fails_closed_on_first(self) -> None:
        # validate.judge fails and routes a cross-phase retry back to generate (target_idx <
        # idx). In dev this must fail_closed immediately with no reopen.
        c = self._conductor("dev")
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "validate" and substep == "judge") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="restart", reason="code_defect")
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "fail_closed")
        ss = self._last_set_status(c)
        self.assertEqual(ss["--status"], "fail_closed")
        self.assertEqual(ss["--reason-code"], "dev_phase_rollback")
        self.assertEqual(ss["--reason-detail"], "code_defect")
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])  # no reopen in dev

    def test_dev_reopen_decision_fails_closed(self) -> None:
        # A reopen decision (target compile) is a backward rollback by construction.
        c = self._conductor("dev")
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "validate" and substep == "judge") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "reopen", target_phase="compile", reason="judge_structural_violation_ir")
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "fail_closed")
        ss = self._last_set_status(c)
        self.assertEqual(ss["--reason-code"], "dev_phase_rollback")
        self.assertEqual(ss["--reason-detail"], "judge_structural_violation_ir")
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_dev_execute_no_verdict_routes_generate_fails_closed(self) -> None:
        # The deterministic execute-no-verdict route (classify_failure) returns retry->generate;
        # in dev that backward rollback fail_closes on the FIRST occurrence (the D2 motivating
        # case), rather than looping generate->build->validate to budget exhaustion.
        c = self._conductor("dev")
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "execute") else "pass")
        # use the real classify_failure (no verdict.json -> retry generate)
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "fail_closed")
        self.assertEqual(self._last_set_status(c)["--reason-code"], "dev_phase_rollback")
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_prod_same_rollback_reopens_as_today(self) -> None:
        # Identical scenario in prod: the cross-phase reopen still happens (F1 is dev-only).
        c = self._conductor("prod")
        state = {"used": False}

        def status_fn(phase, substep, n):
            if phase == "validate" and substep == "judge" and not state["used"]:
                state["used"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "reopen", target_phase="compile", reason="judge_structural_violation_ir")
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "compile")

    def test_dev_same_phase_reopen_is_not_rollback(self) -> None:
        # Boundary: a (malformed) reopen whose target is NOT upstream — here a reopen of the
        # current phase (target_idx == idx) — is not a backward rollback. The dev gate keys on
        # target_idx < idx only, so this falls through to the same terminal-fail branch as prod
        # (plain `fail`, reason_code <phase>_fail), NOT a dev_phase_rollback fail_closed.
        c = self._conductor("dev")
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "compile" and substep == "verify") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "reopen", target_phase="compile", reason="malformed_same_phase_reopen")
        status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "fail")  # terminal fail, not dev_phase_rollback
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])
        ss = self._last_set_status(c)
        self.assertEqual(ss["--reason-code"], "compile_fail")
        self.assertNotEqual(ss.get("--reason-code"), "dev_phase_rollback")

    def test_dev_intra_phase_same_phase_retry_not_rollback(self) -> None:
        # A same-phase decision (target == current phase) WITHOUT a repair_strategy (so it is not
        # a producer reopen) is intra-phase, not a backward rollback: dev terminalizes it as plain
        # `fail` (no in-place retry at
        # conduct level), NOT a dev_phase_rollback fail_closed. The within-phase substep loop
        # (generate.generate -> generate.verify -> regenerate) is what dev keeps; this asserts
        # the conduct gate does not mistake a same-phase route for a cross-phase rollback.
        c = self._conductor("dev")
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "compile" and substep == "verify") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("retry", reason="unflagged_retry")
        status = c.conduct(self._refs(), "compile")
        self.assertEqual(status, "fail")  # same-phase terminal, not fail_closed
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])
        ss = self._last_set_status(c)
        self.assertNotEqual(ss.get("--reason-code"), "dev_phase_rollback")


class TransportFailureTest(unittest.TestCase):
    """A leaf transport failure (e.g. judge session limit, rc!=0) must route to a clean,
    resumable fail_closed WITHOUT calling write_step_result (which would crash on the judge
    semantic_review.json gate), and must tombstone the attempt's terminalized substep arids
    so a later --resume can reach pass (orphaned-arid completion guard)."""

    class _C(_FakeConductor):
        def _write_lineage(self, refs):  # type: ignore[override]
            return []  # avoid writing to the (fake) repo_root

    def _conductor(self) -> "_FakeConductor":
        c = self._C(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
                    orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
            binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001")

    def test_judge_transport_failure_fails_closed_and_tombstones(self) -> None:
        c = self._conductor()
        # pre_judge + execute are deterministic (rc 0, pass); the judge leaf hits a session limit.
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(1, "", "Claude usage limit reached")  # type: ignore[assignment]
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertTrue(oc.decision.reason.startswith("leaf_transport_error: leaf_exit=1"))
        subs = [s for s, _ in c.calls]
        # the core Bug-2 assertion: no write-step-result (so the judge gate never crashes)
        self.assertNotIn("write-step-result", subs)
        # the Bug-1 tombstone: the three substep arids that ran (pre_judge, execute, judge)
        sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup[0]["--run-ids"], ["child-1", "child-2", "child-3"])
        self.assertIn("leaf_transport_error_orphan", sup[0]["--reason"])

    def test_non_transport_content_fail_writes_and_routes(self) -> None:
        # judge returns rc 0 but content-fails -> normal classify_failure routing, NOT transport:
        # write-step-result IS called and no tombstone is written.
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail" if substep == "judge" else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="restart", reason="x")
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.decision.action, "retry")
        subs = [s for s, _ in c.calls]
        self.assertIn("write-step-result", subs)
        self.assertNotIn("add-superseded-runs", subs)

    def test_pass_path_unchanged(self) -> None:
        c = self._conductor()
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.decision.action, "advance")
        subs = [s for s, _ in c.calls]
        self.assertIn("write-step-result", subs)
        self.assertNotIn("add-superseded-runs", subs)

    def test_pre_spawn_dag_guard_fails_closed_before_any_launch(self) -> None:
        # A not-built+validated dependency closure fails the validate phase closed at the
        # PRE-LAUNCH guard (before workflow_launch_check and before any substep's record-launch).
        # This is load-bearing: record-launch is itself dependency-gated, so the readiness check
        # must fire here — otherwise workflow_launch_check would raise `dependency_not_ready` as
        # an uncaught RuntimeError before the pre_judge substep could run. No launch is recorded.
        class _C(self._C):  # type: ignore[misc]
            def _judge_pre_spawn_dag_block(self, refs):  # type: ignore[override]
                return "dependency closure not built+validated ... missing ['component/dep']"
        c = _C(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
               orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertEqual(oc.decision.reason, "validate_pre_judge_dag_incomplete")
        subs = [s for s, _ in c.calls]
        self.assertNotIn("record-launch", subs)  # guard fires before any substep launch
        self.assertNotIn("write-step-result", subs)

    def test_pre_judge_substep_dag_incomplete_fails_closed(self) -> None:
        # Defensive in-substep path (a TOCTOU where the closure becomes unready between the
        # pre-launch guard and the substep, or the fake bypasses the guard): the pre_judge
        # substep (index 0) authors a FAIL pre_judge_meta, the loop breaks there (no
        # execute/judge/post_judge), and the phase terminalizes fail_closed with NO
        # write-step-result (skip-write + tombstone).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()

            class _C(_FakeConductor):
                def _write_lineage(self, r):  # type: ignore[override]
                    return []
                def _ensure_fresh_producer_id(self, r, phase):  # type: ignore[override]
                    return None
                # Guard passes (None); drive the failure at the substep instead.
                def _judge_pre_spawn_dag_block(self, r):  # type: ignore[override]
                    return None
            c = _C(repo_root=repo, orchestration_id="orch_x",
                   orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            c.pre_judge_meta_fn = lambda n: {
                "status": "fail", "failure_category": "pre_judge_dag_incomplete",
                "failure_excerpt": "missing ['component/dep']"}
            c.status_fn = lambda phase, substep, n: (
                "fail" if (phase == "validate" and substep == "pre_judge") else "pass")
            oc = c.run_phase(refs, "validate")
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "fail_closed")
            self.assertEqual(oc.decision.reason, "validate_pre_judge_dag_incomplete")
            subs = [s for s, _ in c.calls]
            self.assertNotIn("write-step-result", subs)
            sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
            self.assertEqual(len(sup), 1)
            self.assertEqual(sup[0]["--run-ids"], ["child-1"])  # only pre_judge ran
            self.assertIn("validate_gate_fail_orphan", sup[0]["--reason"])

    def test_post_gate_pre_judge_violation_fails_closed_and_tombstones(self) -> None:
        # G3: a PASSING judge (aggregate_verdict=pass) but the deterministic post_judge gate
        # failed on a record-integrity violation classified UNRECOVERABLE (disposition
        # fail_closed) -> a non-physics integrity blocker terminalized fail_closed WITHOUT
        # write-step-result (so the judge pre_phase_complete hook never rejects a fail
        # step_result atop a pass semantic_review), tombstoning the attempt's arids.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()

            class _C(_FakeConductor):
                def _write_lineage(self, r):  # type: ignore[override]
                    return []
                def _ensure_fresh_producer_id(self, r, phase):  # type: ignore[override]
                    return None  # keep run_id stable so the seeded run-node dir is read back

            c = _C(repo_root=repo, orchestration_id="orch_x",
                   orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
            # post_judge substep authors a FAIL post_judge_meta with an UNRECOVERABLE
            # disposition; the mini-loop skips it and run_phase terminalizes fail_closed.
            c.post_judge_meta_fn = lambda n: {
                "status": "fail", "failure_category": "pre_judge_violation",
                "failure_excerpt": "record-integrity boom",
                "violations": ["workspace/.../agent_graph.json: dangling edge"],
                "disposition": "fail_closed"}
            c.status_fn = lambda phase, substep, n: (
                "fail" if (phase == "validate" and substep == "post_judge") else "pass")
            oc = c.run_phase(refs, "validate")
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "fail_closed")
            self.assertEqual(oc.decision.reason, "validate_pre_judge_violation")
            subs = [s for s, _ in c.calls]
            self.assertNotIn("write-step-result", subs)
            sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
            self.assertEqual(len(sup), 1)
            self.assertIn("validate_gate_fail_orphan", sup[0]["--reason"])

    def test_post_gate_recoverable_violation_warm_resumes_judge_to_pass(self) -> None:
        # G4: a RECOVERABLE post_judge conformance violation (disposition warm_resume) is NOT
        # terminal — the mini-loop warm-resumes the judge, which re-authors semantic_review, and
        # the re-run post_judge passes -> the phase certifies PASS (write-step-result, advance).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()

            class _C(_FakeConductor):
                def _write_lineage(self, r):  # type: ignore[override]
                    return []
                def _ensure_fresh_producer_id(self, r, phase):  # type: ignore[override]
                    return None

            c = _C(repo_root=repo, orchestration_id="orch_x",
                   orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
            # post_judge fails RECOVERABLE on the first attempt (detn 1..3 == pre_judge/execute/
            # post_judge of the first pass), then passes on the mini-loop's re-run.
            state = {"post_attempts": 0}
            def _post_meta(n):
                state["post_attempts"] += 1
                if state["post_attempts"] == 1:
                    return {"status": "fail", "failure_category": "pre_judge_violation",
                            "failure_excerpt": "semantic_review.json: review_method must be "
                                               "llm_semantic_review",
                            "violations": ["workspace/.../semantic_review.json: review_method "
                                           "must be llm_semantic_review"],
                            "disposition": "warm_resume"}
                return {"status": "pass", "failure_category": None, "failure_excerpt": None,
                        "violations": [], "disposition": None}
            c.post_judge_meta_fn = _post_meta
            # First pass: post_judge fails. Mini-loop re-runs judge (pass) + post_judge (pass).
            calls = {"n": 0}
            def _status(phase, substep, n):
                if phase == "validate" and substep == "post_judge":
                    calls["n"] += 1
                    return "fail" if calls["n"] == 1 else "pass"
                return "pass"
            c.status_fn = _status
            oc = c.run_phase(refs, "validate")
            self.assertEqual(oc.status, "pass")
            self.assertEqual(oc.decision.action, "advance")
            subs = [s for s, _ in c.calls]
            self.assertIn("write-step-result", subs)
            # the superseded (first) judge + post_judge arids were tombstoned by the mini-loop
            sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
            self.assertEqual(len(sup), 1)
            self.assertIn("validate_post_judge_warm_resume_orphan", sup[0]["--reason"])

    def test_warm_resumed_judge_physics_fail_routes_not_fail_closed(self) -> None:
        # Subtle: after a warm-resume attempt the ON-DISK post_judge_meta is STALE (status fail
        # from the superseded attempt). If the warm-resumed judge itself physics-fails, run_phase
        # must route via classify_failure (judge physics) — NOT fail_closed on the stale meta.
        # The fail_closed branch is gated on the ACTUALLY-failed substep (judge here), not the
        # meta file, so the stale post_judge_meta is ignored.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()

            class _C(_FakeConductor):
                def _write_lineage(self, r):  # type: ignore[override]
                    return []
                def _ensure_fresh_producer_id(self, r, phase):  # type: ignore[override]
                    return None

            c = _C(repo_root=repo, orchestration_id="orch_x",
                   orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
            # First post_judge run fails RECOVERABLE (warm_resume). On the mini-loop's re-run the
            # judge physics-fails, so post_judge never runs a second time and its meta stays stale.
            c.post_judge_meta_fn = lambda n: {
                "status": "fail", "failure_category": "pre_judge_violation",
                "failure_excerpt": "semantic_review.json: review_method must be llm_semantic_review",
                "violations": ["workspace/runs/n/semantic_review.json: review_method must be "
                               "llm_semantic_review"],
                "disposition": "warm_resume"}
            judge_runs = {"n": 0}
            def _status(phase, substep, n):
                if phase == "validate" and substep == "judge":
                    judge_runs["n"] += 1
                    return "pass" if judge_runs["n"] == 1 else "fail"  # re-run judge fails
                if phase == "validate" and substep == "post_judge":
                    return "fail"  # first post_judge fails (warm_resume)
                return "pass"
            c.status_fn = _status
            c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
                "retry", target_phase="generate", repair_strategy="reuse", reason="judge_physics")
            oc = c.run_phase(refs, "validate")
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "retry")  # routed, NOT fail_closed
            self.assertEqual(oc.decision.target_phase, "generate")
            self.assertIn("write-step-result", [s for s, _ in c.calls])

    def _post_judge_unknown_conductor(self, repo, mode):
        class _C(_FakeConductor):
            def _write_lineage(self, r):  # type: ignore[override]
                return []
            def _ensure_fresh_producer_id(self, r, phase):  # type: ignore[override]
                return None
        c = _C(repo_root=repo, orchestration_id="orch_x",
               orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.workflow_mode = mode
        c.calls = []
        rn = repo / self._refs().run_node_dir()
        rn.mkdir(parents=True, exist_ok=True)
        (rn / "aggregate_verdict.json").write_text(
            json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
        # post_judge fails with an UNKNOWN violation -> disposition="escalate".
        c.post_judge_meta_fn = lambda n: {
            "status": "fail", "failure_category": "pre_judge_violation",
            "failure_excerpt": "workspace/runs/n/diagnostics.json: weird evidence violation",
            "violations": ["workspace/runs/n/diagnostics.json: weird evidence violation"],
            "disposition": "escalate"}
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "post_judge") else "pass")
        return c

    def test_post_gate_unknown_escalates_in_prod(self) -> None:
        # G5: a post_judge `unknown` disposition routes to the unified escalate LLM in PROD —
        # run_phase returns a RouteDecision("escalate", reason="validate_post_judge_unknown"),
        # writes no step_result, and does NOT fail_closed itself. It must NOT pre-tombstone the
        # failed post_judge arid: conduct's diagnostician reopen uses it as the trigger, and
        # reopen_phase no-ops on an already-superseded trigger, so the trigger stays live.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._post_judge_unknown_conductor(repo, "prod")
            oc = c.run_phase(self._refs(), "validate")
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "escalate")
            self.assertEqual(oc.decision.reason, "validate_post_judge_unknown")
            subs = [s for s, _ in c.calls]
            self.assertNotIn("write-step-result", subs)
            # No pre-tombstone on the escalate path (the trigger must drive the upstream reopen).
            self.assertNotIn("add-superseded-runs", subs)

    def test_post_gate_unknown_fails_closed_in_dev(self) -> None:
        # G5 sign-off #3: in DEV a post_judge `unknown` keeps the fail-fast fail_closed
        # (no billed escalate leaf), with reason validate_post_judge_unknown for observability.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._post_judge_unknown_conductor(repo, "dev")
            oc = c.run_phase(self._refs(), "validate")
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "fail_closed")
            self.assertEqual(oc.decision.reason, "validate_post_judge_unknown")
            subs = [s for s, _ in c.calls]
            self.assertNotIn("write-step-result", subs)
            # Terminal fail_closed DOES tombstone (no reopen will consume the arids).
            self.assertIn("add-superseded-runs", subs)

    def test_post_gate_unknown_escalate_terminal_tombstones(self) -> None:
        # G5: when a prod post_judge unknown escalates and the diagnostician TERMINALIZES (no
        # upstream reopen), conduct must tombstone the orphaned validate arids (run_phase
        # deliberately left them live for a potential reopen trigger). Otherwise a later
        # resume/pass completion vouch trips on the orphaned, step_result-less arids. This must
        # cover EVERY terminal non-reopen outcome, not just fail_closed:
        import tempfile
        # (a) an explicit fail_closed directive, and
        # (b) a parse-valid null-target retry that falls to conduct's terminal `fail` branch.
        for stub in (
            lambda refs, phase, outcome: wc.RouteDecision("fail_closed", reason="diag_unrecoverable"),
            lambda refs, phase, outcome: wc.RouteDecision("retry", target_phase=None, reason="ambig"),
        ):
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                c = self._post_judge_unknown_conductor(repo, "prod")
                c.escalate = stub  # type: ignore[assignment]
                status = c.conduct(self._refs(), "validate")
                self.assertIn(status, ("fail", "fail_closed"))
                sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
                self.assertTrue(
                    any("validate_post_judge_escalate_terminal_orphan" in cap["--reason"]
                        for cap in sup),
                    f"expected terminal-orphan tombstone for {stub}")

    def test_post_gate_unknown_escalate_reopen_does_not_terminal_tombstone(self) -> None:
        # G5: when the diagnostician routes an upstream REOPEN (with budget remaining), conduct
        # must NOT terminal-tombstone — doing so would pre-supersede the reopen trigger and make
        # reopen_phase no-op. The reopen fires; reopen_phase supersedes the attempt instead.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()

            class _C(_FakeConductor):
                def _write_lineage(self, r):  # type: ignore[override]
                    return []
                def _ensure_fresh_producer_id(self, r, phase):  # type: ignore[override]
                    return None
            c = _C(repo_root=repo, orchestration_id="orch_x",
                   orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.workflow_mode = "prod"
            c.calls = []
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
            # post_judge fails (escalate) on the FIRST validate attempt, then passes after the
            # reopen — so the reopen resolves cleanly (no loop to budget exhaustion).
            st = {"n": 0}
            def _post_meta(n):
                st["n"] += 1
                if st["n"] == 1:
                    return {"status": "fail", "failure_category": "pre_judge_violation",
                            "violations": ["x"], "disposition": "escalate"}
                return {"status": "pass", "disposition": None}
            c.post_judge_meta_fn = _post_meta
            fp = {"n": 0}
            def _status(phase, substep, n):
                if phase == "validate" and substep == "post_judge":
                    fp["n"] += 1
                    return "fail" if fp["n"] == 1 else "pass"
                return "pass"
            c.status_fn = _status
            c.escalate = lambda refs, phase, outcome: wc.RouteDecision(  # type: ignore[assignment]
                "reopen", target_phase="compile", repair_strategy="restart", severity="critical",
                reason="diag_ir")
            c.conduct(refs, "validate")
            subs = [s for s, _ in c.calls]
            self.assertIn("reopen-phase", subs)  # the upstream reopen fired
            sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
            self.assertFalse(
                any("validate_post_judge_escalate_terminal_orphan" in cap["--reason"] for cap in sup),
                "reopen resolution must not terminal-tombstone (reopen_phase supersedes)")

    def test_gather_failure_context_includes_gate_metas(self) -> None:
        # G5: _gather_failure_context embeds post_judge_meta.json / pre_judge_meta.json so the
        # read-only escalate leaf reasons over the violations without touching the FS.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = _FakeConductor(repo_root=repo, orchestration_id="orch_x",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "post_judge_meta.json").write_text(
                json.dumps({"disposition": "escalate", "violations": ["v"]}), encoding="utf-8")
            (rn / "pre_judge_meta.json").write_text(
                json.dumps({"status": "pass"}), encoding="utf-8")
            ctx = c._gather_failure_context(refs, "validate")
            self.assertIn("post_judge_meta.json", ctx)
            self.assertIn("pre_judge_meta.json", ctx)
            self.assertEqual(ctx["post_judge_meta.json"]["disposition"], "escalate")

    def test_failing_judge_verdict_skips_gate_and_routes_normally(self) -> None:
        # G3 regression guard (Codex P1): a legitimate physics/evidence FAIL judge
        # (aggregate_verdict=fail) must NOT run the post_judge gate — that gate flags
        # semantic_review.decision != pass as a violation, which would mislabel the routeable
        # failure as an integrity blocker. In the substep model this is STRUCTURAL: a failing
        # judge substep breaks the run_phase loop before post_judge runs. run_phase writes the
        # step_result + routes via classify_failure (NOT fail_closed).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            gate_calls = {"n": 0}

            class _C(_FakeConductor):
                def _write_lineage(self, r):  # type: ignore[override]
                    return []
                def _ensure_fresh_producer_id(self, r, phase):  # type: ignore[override]
                    return None

            c = _C(repo_root=repo, orchestration_id="orch_x",
                   orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": "fail"}), encoding="utf-8")
            # A post_judge run would call this; a failing judge must break the loop first.
            def _post(n):
                gate_calls["n"] += 1
                return {"status": "pass", "disposition": None}
            c.post_judge_meta_fn = _post
            c.status_fn = lambda phase, substep, n: (
                "fail" if (phase == "validate" and substep == "judge") else "pass")
            c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
                "retry", target_phase="generate", repair_strategy="reuse", reason="judge_physics_fail_code")
            oc = c.run_phase(refs, "validate")
            self.assertEqual(gate_calls["n"], 0)  # post_judge never ran (loop broke at judge)
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "retry")  # routed, NOT fail_closed
            self.assertEqual(oc.decision.target_phase, "generate")
            subs = [s for s, _ in c.calls]
            self.assertIn("write-step-result", subs)  # routeable fail writes a step_result
            self.assertNotIn("add-superseded-runs", subs)  # not tombstoned

    def test_build_transport_failure_tombstones_step_agent(self) -> None:
        # Build is NOT substep-aware (substep_arids == []); a build in-process exception
        # (rc=1) must still tombstone the step-role executor arid (outcomes[0]) so it is not
        # left as an orphan that blocks a resumed pass.
        c = self._conductor()
        c._run_deterministic_substep = (  # type: ignore[assignment]
            lambda refs, phase, substep, child_arid, request: wc.ProcResult(1, "", "mcp error"))
        oc = c.run_phase(self._refs(), "build")
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertTrue(oc.decision.reason.startswith("leaf_transport_error: leaf_exit=1"))
        subs = [s for s, _ in c.calls]
        self.assertNotIn("write-step-result", subs)
        sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup[0]["--run-ids"], ["child-1"])  # the build step agent


class NodeAllocationTest(unittest.TestCase):
    """M5: node resolution + deterministic id allocation + reservation."""

    def test_slug_of(self) -> None:
        self.assertEqual(wc._slug_of("dynamics_advdiff_flux_1d_upwind_center2"),
                         "dynamics-advdiff-flux-1d-upwind-center2")
        self.assertEqual(wc._slug_of("X__Y"), "x-y")
        self.assertEqual(wc._slug_of("___"), "node")

    def test_next_seq(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.assertEqual(wc._next_seq(p, "slug_20260101"), "001")
            (p / "slug_20260101_001").mkdir()
            (p / "slug_20260101_004").mkdir()
            (p / "other_20260101_009").mkdir()  # different prefix ignored
            self.assertEqual(wc._next_seq(p, "slug_20260101"), "005")

    def test_resolve_node_from_catalog(self) -> None:
        node_key, spec_path = wc.resolve_node(
            REPO_ROOT,
            "spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2",
        )
        self.assertEqual(node_key, "component/dynamics_advdiff_flux_1d_upwind_center2@0.1.0")
        self.assertTrue(spec_path.endswith("dynamics_advdiff_flux_1d_upwind_center2"))

    def test_resolve_node_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            wc.resolve_node(REPO_ROOT, "spec/component/does/not/exist_spec_zzz")

    def test_resolve_node_accepts_file_style_spec_ref(self) -> None:
        base = "spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2"
        expected = ("component/dynamics_advdiff_flux_1d_upwind_center2@0.1.0",)
        for ref in (base + "/controlled_spec.md", base + "/tests.md",
                    base + "/deps.yaml", base + "/"):
            node_key, _ = wc.resolve_node(REPO_ROOT, ref)
            self.assertEqual(node_key, expected[0], f"failed for {ref}")

    def test_prepare_node_allocates_and_reserves(self) -> None:
        c = _FakeConductor(
            repo_root=Path("/tmp/_conductor_nonexistent_repo"), orchestration_id="o",
            orchestration_agent_run_id="ORCH", backend="claude", env={},
        )
        c.calls = []
        refs = wc.prepare_node(c, "component/spec_x@0.1.0", "spec/component/spec_x")
        self.assertTrue(refs.ir_id.startswith("spec-x_"))
        self.assertTrue(refs.ir_id.endswith("_001"))  # no prior dirs -> seq 001
        self.assertEqual(refs.spec_path, "spec/component/spec_x")
        reserves = [cap for s, cap in c.calls if s == "reserve-phase-root"]
        self.assertEqual({cap["--step"] for cap in reserves}, {"compile", "generate"})


class DiagnosticianTest(unittest.TestCase):
    """M4: LLM diagnostician escalation for unclassifiable failures."""

    def _conductor(self) -> _FakeConductor:
        c = _FakeConductor(
            repo_root=Path("/tmp/repo"), orchestration_id="o",
            orchestration_agent_run_id="ORCH", backend="claude", env={},
        )
        c.calls = []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
            binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001",
        )

    def test_last_json_object(self) -> None:
        self.assertEqual(wc._last_json_object('x {"a":1} y {"b":2} z')["b"], 2)
        self.assertIsNone(wc._last_json_object("no json here"))
        self.assertEqual(wc._last_json_object('{"nested":{"k":1}}')["nested"]["k"], 1)

    def test_parse_directive_valid(self) -> None:
        d = wc._parse_directive('reason...\n{"action":"reopen","target_phase":"compile","reason":"ir"}')
        self.assertEqual((d.action, d.target_phase, d.reason), ("reopen", "compile", "ir"))
        d = wc._parse_directive('{"action":"retry","target_phase":"generate","repair_strategy":"reuse","reason":"code"}')
        self.assertEqual((d.action, d.target_phase, d.repair_strategy), ("retry", "generate", "reuse"))
        self.assertEqual(wc._parse_directive('{"action":"fail_closed","reason":"spec"}').action, "fail_closed")

    def test_parse_directive_invalid(self) -> None:
        self.assertIsNone(wc._parse_directive('{"action":"nuke"}'))
        self.assertIsNone(wc._parse_directive('{"action":"reopen","target_phase":null}'))
        self.assertIsNone(wc._parse_directive('{"action":"retry","target_phase":"whoops"}'))
        self.assertIsNone(wc._parse_directive("garbage, no object"))

    def test_parse_directive_rejects_non_actionable_targets(self) -> None:
        # G5: build/validate are NOT actionable diagnostician targets (the conductor cannot
        # re-run a deterministic phase in place to fix a defect) -> rejected -> fail_closed,
        # not a wasted reopen. re_execute is likewise dropped (coerced to None).
        self.assertIsNone(wc._parse_directive('{"action":"reopen","target_phase":"build","reason":"x"}'))
        self.assertIsNone(
            wc._parse_directive('{"action":"retry","target_phase":"validate","repair_strategy":"re_execute","reason":"x"}'))
        d = wc._parse_directive('{"action":"retry","target_phase":"generate","repair_strategy":"re_execute","reason":"x"}')
        self.assertEqual((d.target_phase, d.repair_strategy), ("generate", None))

    def test_parse_directive_severity(self) -> None:
        # G5: severity is parsed; absent or out-of-vocab -> default `major` (back-compat).
        for sev in ("minor", "major", "critical"):
            d = wc._parse_directive(
                '{"action":"reopen","target_phase":"compile","severity":"%s","reason":"r"}' % sev)
            self.assertEqual(d.severity, sev)
        self.assertEqual(  # absent -> major
            wc._parse_directive('{"action":"retry","target_phase":"generate","reason":"r"}').severity,
            "major")
        self.assertEqual(  # out-of-vocab -> major (not a whole-directive reject)
            wc._parse_directive(
                '{"action":"retry","target_phase":"generate","severity":"apocalyptic","reason":"r"}'
            ).severity, "major")

    def test_resolve_severity_directive(self) -> None:
        # G5 policy: minor forces reuse; major default reuse (honors LLM restart override);
        # critical forces restart; re_execute passthrough; fail_closed/no-severity untouched;
        # target_phase is NEVER clamped.
        def r(action, target, strat, sev):
            return wc.resolve_severity_directive(
                wc.RouteDecision(action, target_phase=target, repair_strategy=strat, severity=sev))
        self.assertEqual(r("reopen", "compile", "restart", "minor").repair_strategy, "reuse")
        self.assertEqual(r("reopen", "compile", None, "major").repair_strategy, "reuse")
        self.assertEqual(r("reopen", "compile", "restart", "major").repair_strategy, "restart")
        self.assertEqual(r("reopen", "compile", "reuse", "critical").repair_strategy, "restart")
        self.assertEqual(r("retry", "validate", "re_execute", "critical").repair_strategy, "re_execute")
        # null target_phase -> no-op: an ambiguous/incomplete directive (no explicit target) must
        # NOT be synthesized into a strategy (which conduct would turn into a same-phase producer
        # reopen); it stays strategy-less so conduct terminalizes it.
        self.assertIsNone(r("retry", None, None, "major").repair_strategy)
        # target_phase is honored as-is under every severity (no clamp).
        self.assertEqual(r("reopen", "compile", None, "minor").target_phase, "compile")
        # fail_closed and no-severity decisions are untouched.
        self.assertEqual(
            wc.resolve_severity_directive(
                wc.RouteDecision("fail_closed", reason="x", severity="critical")).repair_strategy,
            None)
        self.assertEqual(
            wc.resolve_severity_directive(
                wc.RouteDecision("retry", target_phase="generate", repair_strategy="reuse")
            ).repair_strategy, "reuse")

    def test_escalate_severity_governs_reuse_discard(self) -> None:
        # End-to-end through escalate(): a critical directive that asks to reuse is forced to
        # restart (discard) by resolve_severity_directive.
        c = self._conductor()
        c.spawn_leaf = lambda prompt, env, **kw: wc.ProcResult(  # type: ignore[assignment]
            0, '{"action":"reopen","target_phase":"compile","severity":"critical",'
               '"repair_strategy":"reuse","reason":"ir_rot"}', "")
        d = c.escalate(self._refs(), "validate", wc.PhaseOutcome("validate", "fail"))
        self.assertEqual((d.action, d.target_phase, d.repair_strategy, d.severity),
                         ("reopen", "compile", "restart", "critical"))

    def test_diagnosis_prompt_renders_escalate_skill(self) -> None:
        # G5: the persona is the workflow-escalate SKILL body (host-rendered), with a
        # missing-file fallback to the inline default.
        repo = Path(__file__).resolve().parents[2]
        persona = wc._load_escalate_persona(repo)
        self.assertIn("Failure Diagnostician", persona)
        self.assertFalse(persona.startswith("---"))  # frontmatter stripped
        prompt = wc._diagnosis_prompt("n", "validate", [], {}, "prod", persona=persona)
        self.assertIn("Failure Diagnostician", prompt)
        self.assertIn("severity", prompt)  # directive schema appended
        # Missing SKILL -> inline fallback (never crashes escalate).
        self.assertEqual(
            wc._load_escalate_persona(Path("/no/such/repo")), wc._ESCALATE_PERSONA_FALLBACK)

    def test_escalate_routes_from_diagnostician(self) -> None:
        c = self._conductor()
        c.spawn_leaf = lambda prompt, env, **kw: wc.ProcResult(  # type: ignore[assignment]
            0, 'analysis\n{"action":"reopen","target_phase":"compile","reason":"diag_ir"}', "")
        d = c.escalate(self._refs(), "validate",
                       wc.PhaseOutcome("validate", "fail", failed_substeps=["child-9"]))
        self.assertEqual((d.action, d.target_phase, d.reason), ("reopen", "compile", "diag_ir"))

    def test_escalate_unparsable_is_fail_closed(self) -> None:
        c = self._conductor()
        c.spawn_leaf = lambda prompt, env, **kw: wc.ProcResult(0, "I am unsure; no directive", "")  # type: ignore[assignment]
        d = c.escalate(self._refs(), "build", wc.PhaseOutcome("build", "fail"))
        self.assertEqual(d.action, "fail_closed")

    def test_escalate_fail_closed_when_diagnostician_unsandboxable(self) -> None:
        # Under bwrap-enforced mode, if the host cannot build the read-only diagnostician
        # profile, escalate must convert that to a conservative fail_closed, not crash.
        c = self._conductor()

        def boom():  # type: ignore[no-untyped-def]
            raise wc.SandboxEnforcementError("no bwrap on this host")

        c._readonly_sandbox_profile = boom  # type: ignore[assignment]
        d = c.escalate(self._refs(), "validate", wc.PhaseOutcome("validate", "fail"))
        self.assertEqual(d.action, "fail_closed")
        self.assertIn("sandbox_unavailable", d.reason or "")

    def test_escalate_spawns_diagnostician_with_readonly_profile(self) -> None:
        # P2-4b: under bwrap-enforced mode the diagnostician runs sandboxed with a
        # dedicated read-only profile (no write_roots) instead of fail-closing.
        # Uses a TemporaryDirectory repo_root because this exercises the REAL
        # _readonly_sandbox_profile() (which mkdir's sandbox/tmp/hooks/audit dirs).
        with tempfile.TemporaryDirectory() as tmp:
            c = _FakeConductor(
                repo_root=Path(tmp), orchestration_id="o",
                orchestration_agent_run_id="ORCH", backend="claude", env={},
            )
            c.calls = []
            self.assertTrue(c._bwrap_enabled())  # the test conductor enforces bwrap
            captured: dict[str, object] = {}

            def spawn(prompt, env, **kw):  # type: ignore[no-untyped-def]
                captured["profile"] = kw.get("profile")
                return wc.ProcResult(
                    0, '{"action":"reopen","target_phase":"compile","reason":"diag"}', "")

            c.spawn_leaf = spawn  # type: ignore[assignment]
            d = c.escalate(self._refs(), "validate", wc.PhaseOutcome("validate", "fail"))
        self.assertEqual(d.action, "reopen")
        profile = captured["profile"]
        self.assertIsInstance(profile, dict)
        assert isinstance(profile, dict)
        self.assertTrue(profile.get("readonly"))
        self.assertEqual(profile.get("write_roots"), [])
        self.assertEqual(profile.get("read_roots"), [])

    def test_conduct_escalates_then_reopens(self) -> None:
        c = self._conductor()
        c.workflow_mode = "prod"  # the diagnostician's cross-phase reopen is prod-only (F1)
        state = {"used": False}

        def status_fn(phase, substep, n):
            if phase == "validate" and substep == "judge" and not state["used"]:
                state["used"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("escalate", reason="novel")

        def spawn(prompt, env, **kw):
            if "diagnostician" in prompt:
                return wc.ProcResult(
                    0, '{"action":"reopen","target_phase":"compile","reason":"diag"}', "")
            return wc.ProcResult(0, "", "")

        c.spawn_leaf = spawn  # type: ignore[assignment]
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "compile")


class SubstepStatusAndResumeTest(unittest.TestCase):
    """Codex follow-ups: producer substep requires ALL its own outputs; resume
    reuses existing ids instead of allocating fresh ones."""

    def _real_conductor(self, root: Path) -> wc.Conductor:
        return wc.Conductor(repo_root=root, orchestration_id="o",
                            orchestration_agent_run_id="O", backend="claude", env={})

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
                           binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001")

    def _seed_binary_meta(self, root: Path, refs: wc.NodeRefs, status: str = "pass") -> None:
        # Build status now reads binary_meta.verification_status (deterministic build);
        # seed it so the freshness/all-outputs producer logic is what these tests exercise.
        mp = root / refs.binary_dir() / "binary_meta.json"
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"verification_status": status}), encoding="utf-8")

    def test_producer_requires_all_its_outputs(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self._real_conductor(root)
            self._seed_binary_meta(root, self._refs())
            paths = ["a/runner.bin", "a/binary_meta.json", "a/command_log.jsonl"]
            # none exist -> fail
            self.assertEqual(c.determine_substep_status(self._refs(), "build", None, paths)[0], "fail")
            # all exist -> pass
            for p in paths:
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                (root / p).write_text("x", encoding="utf-8")
            st, refs_out = c.determine_substep_status(self._refs(), "build", None, paths)
            self.assertEqual(st, "pass")
            self.assertEqual(len(refs_out), 3)
            # a partial write (one missing) must NOT pass
            (root / paths[0]).unlink()
            self.assertEqual(c.determine_substep_status(self._refs(), "build", None, paths)[0], "fail")

    def test_build_fails_when_binary_meta_verification_fail(self) -> None:
        # A post_build content failure (binary built but verification_status=fail) must
        # fail the substep even though all deliverables exist (rc 0 content-failure route).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self._real_conductor(root)
            self._seed_binary_meta(root, self._refs(), status="fail")
            allowed = ["b/bin/runner", "b/binary_meta.json"]
            for p in allowed:
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                (root / p).write_text("x", encoding="utf-8")
            self.assertEqual(
                c.determine_substep_status(self._refs(), "build", None, allowed)[0], "fail")

    def test_build_passes_without_binary_side_mcp_log(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self._real_conductor(root)
            self._seed_binary_meta(root, self._refs())
            allowed = ["b/bin/runner", "b/binary_meta.json", "b/command_log.jsonl"]
            for p in allowed[:2]:  # write the deliverables, NOT the audit log
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                (root / p).write_text("x", encoding="utf-8")
            status, _ = c.determine_substep_status(self._refs(), "build", None, allowed)
            self.assertEqual(status, "pass")  # binary-side command_log not required

    def test_ensure_fresh_producer_id_reallocates_when_outputs_exist(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self._real_conductor(root)
            refs = wc.NodeRefs(
                node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                ir_id="ir1", pipeline_id="p1", source_id="s1", binary_id="b1",
                run_id="run_20260101_001", source_binary_id="b1")
            runs = root / refs.pipeline_ref / "runs"
            # no run dir yet -> first run keeps the prepared id
            c._ensure_fresh_producer_id(refs, "validate")
            self.assertEqual(refs.run_id, "run_20260101_001")
            # the run dir exists (a prior attempt) -> allocate a fresh run_id
            (runs / "run_20260101_001").mkdir(parents=True)
            c._ensure_fresh_producer_id(refs, "validate")
            self.assertNotEqual(refs.run_id, "run_20260101_001")
            self.assertTrue(refs.run_id.startswith("run_"))

    def test_producer_stale_outputs_fail_freshness_gate(self) -> None:
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            c = self._real_conductor(root)
            self._seed_binary_meta(root, self._refs())
            paths = ["a/runner.bin", "a/binary_meta.json"]
            for p in paths:
                (root / p).parent.mkdir(parents=True, exist_ok=True)
                (root / p).write_text("x", encoding="utf-8")
            now = time.time()
            # all written before `now` => stale => fail (a retry that did not rewrite)
            self.assertEqual(
                c.determine_substep_status(self._refs(), "build", None, paths, min_mtime=now + 5)[0],
                "fail")
            # min_mtime in the past => fresh => pass
            self.assertEqual(
                c.determine_substep_status(self._refs(), "build", None, paths, min_mtime=now - 5)[0],
                "pass")

    def test_resume_node_refs_from_orchestration_records(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            oid = "orch_test"
            safe = "component__spec_x__0.1.0"
            orch = root / "workspace" / "orchestrations" / oid
            res = orch / "reservations" / safe
            res.mkdir(parents=True)
            (res / "compile.json").write_text(
                json.dumps({"reserved_ir_id": "slug_20260101_007"}), encoding="utf-8")
            (res / "generate.json").write_text(
                json.dumps({"reserved_ir_id": "slug_20260101_009"}), encoding="utf-8")
            ckpt = {"completed_steps": [{
                "node_key": "component/spec_x@0.1.0",
                "output_refs": [
                    f"workspace/pipelines/{safe}/slug_20260101_009/source/src_20260101_003/source_meta.json",
                    f"workspace/pipelines/{safe}/slug_20260101_009/binary/bin_20260101_004/binary_meta.json",
                ],
            }]}
            (orch / "orchestration_checkpoint.json").write_text(
                json.dumps(ckpt), encoding="utf-8")
            c = wc.Conductor(repo_root=root, orchestration_id=oid,
                            orchestration_agent_run_id="O", backend="claude", env={})
            refs = wc.resume_node_refs(c, "component/spec_x@0.1.0", "spec/component/spec_x")
            # ir/pipeline from THIS orchestration's reservations (not global-latest)
            self.assertEqual(refs.ir_id, "slug_20260101_007")
            self.assertEqual(refs.pipeline_id, "slug_20260101_009")
            # source/binary from this orchestration's checkpoint outputs
            self.assertEqual(refs.source_id, "src_20260101_003")
            self.assertEqual(refs.binary_id, "bin_20260101_004")
            self.assertEqual(refs.source_binary_id, "bin_20260101_004")
            # run not yet produced -> freshly allocated
            self.assertTrue(refs.run_id.startswith("run_"))

    def test_resume_node_refs_raises_without_reservation(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            c = wc.Conductor(repo_root=Path(d), orchestration_id="orch_x",
                            orchestration_agent_run_id="O", backend="claude", env={})
            with self.assertRaises(ValueError):
                wc.resume_node_refs(c, "component/spec_x@0.1.0", "spec/component/spec_x")


class RunWorkflowConductorGuardTest(unittest.TestCase):
    """Orchestration is conductor-only; unsupported backends are rejected up front."""

    def test_rejects_cursor_backend(self) -> None:
        # cursor was removed with the LLM-orchestrator driver; argparse rejects it
        # as an invalid --llm choice (SystemExit) before any orchestration state.
        import tools.run_workflow as rw
        with self.assertRaises(SystemExit):
            rw._parse_args(["spec/component/x", "validate", "--llm", "cursor"])


class ResumeRecoveryTest(unittest.TestCase):
    """Codex follow-ups: recover the repair target for skipped phases; restore the
    conductor driver on resume via a marker."""

    def test_completed_producer_arid_from_step_result(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            oid, safe = "o", "component__spec_x__0.1.0"
            sr_dir = root / "workspace" / "orchestrations" / oid / "steps" / safe / "generate" / "EXEC"
            sr_dir.mkdir(parents=True)
            (sr_dir / "step_result.json").write_text(
                json.dumps({"substep_agent_run_ids": ["GEN", "VER"],
                            "executor_agent_run_id": "EXEC"}), encoding="utf-8")
            c = wc.Conductor(repo_root=root, orchestration_id=oid,
                            orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertEqual(
                c._completed_producer_arid("component/spec_x@0.1.0", "generate", "EXEC"), "GEN")
            # build (no substeps) -> the step executor arid
            bdir = root / "workspace" / "orchestrations" / oid / "steps" / safe / "build" / "BLD"
            bdir.mkdir(parents=True)
            (bdir / "step_result.json").write_text(
                json.dumps({"substep_agent_run_ids": [], "executor_agent_run_id": "BLD"}),
                encoding="utf-8")
            self.assertEqual(
                c._completed_producer_arid("component/spec_x@0.1.0", "build", "BLD"), "BLD")

    def test_run_phase_skip_populates_producer_arid(self) -> None:
        c = _FakeConductor(repo_root=Path("/tmp/repo"), orchestration_id="o",
                           orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        c.check_step_completed = lambda nk, phase: ({"integrity": "ok", "agent_run_id": "EXEC"}
                                                    if phase == "generate" else None)
        c._completed_producer_arid = lambda nk, phase, ex: "GEN" if phase == "generate" else None
        refs = wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1_001", pipeline_id="x_1_001")
        po = c.run_phase(refs, "generate")
        self.assertEqual(po.status, "pass")  # skipped (completed)
        self.assertEqual(c._producer_arid.get("generate"), "GEN")


class LeafSpawnTest(unittest.TestCase):
    """Codex follow-ups: honor custom llm_command; gate substep on leaf returncode."""

    @staticmethod
    def _c(**kw) -> wc.Conductor:
        base = dict(repo_root=Path("/tmp/repo"), orchestration_id="o",
                    orchestration_agent_run_id="O", backend="claude", env={})
        base.update(kw)
        return wc.Conductor(**base)

    def test_leaf_command_honors_custom_llm_command(self) -> None:
        c = self._c(backend="claude", llm_command="mywrap --model Z")
        self.assertEqual(c.leaf_command("PROMPT"), ["mywrap", "--model", "Z", "-p", "PROMPT"])
        c2 = self._c(backend="codex", llm_command="codexwrap --x")
        self.assertEqual(c2.leaf_command("P"), ["codexwrap", "--x", "exec", "P"])

    def test_leaf_command_defaults_to_backend(self) -> None:
        self.assertEqual(self._c(backend="claude").leaf_command("P"), ["claude", "-p", "P"])
        self.assertEqual(self._c(backend="codex").leaf_command("P"), ["codex", "exec", "P"])

    def test_leaf_command_pins_session_id_for_claude(self) -> None:
        c = self._c(backend="claude")
        self.assertEqual(
            c.leaf_command("P", session_id="arid-1"),
            ["claude", "--session-id", "arid-1", "-p", "P"],
        )
        # codex has no per-session flag; session_id is ignored.
        self.assertEqual(
            self._c(backend="codex").leaf_command("P", session_id="arid-1"),
            ["codex", "exec", "P"],
        )

    def test_leaf_command_reuse_resume_forks_producer_session(self) -> None:
        c = self._c(backend="claude")
        self.assertEqual(
            c.leaf_command("P", session_id="new-arid", resume_session_id="producer-arid"),
            ["claude", "--resume", "producer-arid", "--fork-session",
             "--session-id", "new-arid", "-p", "P"],
        )

    def test_nonzero_leaf_exit_fails_substep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = _FakeConductor(repo_root=Path(tmp), orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            c.status_fn = lambda phase, substep, n: "pass"  # artifacts claim pass
            # leaf crashed (e.g. token limit), emitting a diagnostic to stderr
            c.spawn_leaf = lambda prompt, env, **kw: wc.ProcResult(1, "", "context limit exceeded")
            refs = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x",
                               ir_id="x_1_001", pipeline_id="x_1_001")
            status = c.conduct(refs, "compile")

            # 1. a leaf transport failure routes straight to fail_closed (no diagnostician)
            self.assertEqual(status, "fail_closed")
            set_status = [cap for s, cap in c.calls if s == "set-status"][-1]
            self.assertEqual(set_status["--status"], "fail_closed")

            # 2. the fail summary carries result_summary so finalize-child won't crash,
            #    and the returncode gate overrides the artifact "pass"
            runs = [cap["--agent-run-json"] for s, cap in c.calls if s == "finalize-child"]
            self.assertEqual(runs[0]["status"], "fail")
            self.assertIn("context limit exceeded", runs[0]["result_summary"])
            self.assertTrue(runs[0]["result_summary"].startswith("leaf_exit=1"))

            # 3. the leaf's verbatim stderr is persisted durably (was lost before)
            child = runs[0]["agent_run_id"]
            stderr_log = (Path(tmp) / "workspace" / "orchestrations" / "o" / "agents"
                          / child / "dialogs" / "leaf.stderr.log")
            self.assertEqual(stderr_log.read_text(encoding="utf-8"), "context limit exceeded")

    def test_set_status_reason_code_names_leaf_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = _FakeConductor(repo_root=Path(tmp), orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            c.spawn_leaf = lambda prompt, env, **kw: wc.ProcResult(1, "", "boom")
            refs = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x",
                               ir_id="x_1_001", pipeline_id="x_1_001")
            c.conduct(refs, "compile")
            set_status = [cap for s, cap in c.calls if s == "set-status"][-1]
            self.assertEqual(set_status["--status"], "fail_closed")
            self.assertEqual(set_status["--reason-code"], "leaf_transport_error")
            self.assertIn("leaf_transport_error", set_status["--reason-detail"])

    def test_leaf_stdout_persisted_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = _FakeConductor(repo_root=Path(tmp), orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            c.spawn_leaf = lambda prompt, env, **kw: wc.ProcResult(0, "all good", "")
            refs = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x",
                               ir_id="x_1_001", pipeline_id="x_1_001")
            c.conduct(refs, "compile")
            runs = [cap["--agent-run-json"] for s, cap in c.calls if s == "finalize-child"]
            child = runs[0]["agent_run_id"]
            stdout_log = (Path(tmp) / "workspace" / "orchestrations" / "o" / "agents"
                          / child / "dialogs" / "leaf.stdout.log")
            self.assertEqual(stdout_log.read_text(encoding="utf-8"), "all good")
            # a passing substep carries no result_summary
            self.assertNotIn("result_summary", runs[0])

    def test_run_substep_reuse_resume_gated_by_flag(self) -> None:
        refs = wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1")
        reuse = {"repair_strategy": "reuse", "repair_target_agent_run_id": "producer-arid"}

        def run(env, repair):
            cap: dict = {}
            with tempfile.TemporaryDirectory() as tmp:
                c = _FakeConductor(repo_root=Path(tmp), orchestration_id="o",
                                   orchestration_agent_run_id="ORCH", backend="claude", env=env)
                c.calls = []

                def spawn(prompt, env_, **kw):
                    cap.update(kw)
                    return wc.ProcResult(0, "", "")

                c.spawn_leaf = spawn  # type: ignore[assignment]
                # The producer session transcript is assumed resumable here; the
                # cold-fallback-when-missing case is covered separately below.
                c._claude_session_resumable = lambda sid: True  # type: ignore[assignment]
                c.run_substep(refs, "generate", "generate", repair=repair)
            return cap

        # reuse → always resume the producer session (no env gate); new arid pinned as session_id.
        cap = run({}, reuse)
        self.assertEqual(cap.get("session_id"), "child-1")
        self.assertEqual(cap.get("resume_session_id"), "producer-arid")
        # restart never resumes (avoid anchoring on the defective reasoning) — the strategy-driven
        # warm/cold selection is preserved.
        restart = run({},
                      {"repair_strategy": "restart", "repair_target_agent_run_id": "producer-arid"})
        self.assertIsNone(restart.get("resume_session_id"))

    def test_run_substep_reuse_resume_cold_fallback_when_session_missing(self) -> None:
        """reuse but the producer session transcript is gone → cold launch
        (drop resume_session_id) instead of failing the leaf with `--resume <missing>`."""
        refs = wc.NodeRefs(node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                           ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1")
        reuse = {"repair_strategy": "reuse", "repair_target_agent_run_id": "producer-arid"}
        cap: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            c = _FakeConductor(repo_root=Path(tmp), orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []

            def spawn(prompt, env_, **kw):
                cap.update(kw)
                return wc.ProcResult(0, "", "")

            c.spawn_leaf = spawn  # type: ignore[assignment]
            c._claude_session_resumable = lambda sid: False  # type: ignore[assignment]
            c.run_substep(refs, "generate", "generate", repair=reuse)
        self.assertEqual(cap.get("session_id"), "child-1")
        self.assertIsNone(cap.get("resume_session_id"))

    def test_bwrap_always_enforced(self) -> None:
        # Phase-2 (Linux+bwrap-only): bwrap leaf sandboxing is unconditionally mandatory;
        # there is no opt-out env value. _bwrap_enabled() always returns True.
        self.assertTrue(self._c(env={})._bwrap_enabled())
        self.assertTrue(self._c(env={"METDSL_CONDUCTOR_BWRAP": "off"})._bwrap_enabled())

    def test_spawn_leaf_wraps_in_bwrap(self) -> None:
        # With a recorded sandbox profile, the leaf argv is wrapped in
        # `bwrap ... -- <leaf command>`. bwrap is unconditionally enforced (no opt-out),
        # so a leaf without a usable profile fails closed rather than launching bare.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ws_tmp = repo / "workspace" / "tmp" / "A"
            ws_tmp.mkdir(parents=True)
            prof_dir = repo / "workspace" / "orchestrations" / "o" / "sandbox_profiles"
            prof_dir.mkdir(parents=True)
            (prof_dir / "A.json").write_text(json.dumps({
                "repo_root": str(repo), "tmp_dir": str(ws_tmp),
                "workspace_tmp_rw_abs": str(ws_tmp),
                "read_roots": [], "write_roots": [],
                "runtime_ro_bind_paths": [], "runtime_rw_bind_paths": [],
            }), encoding="utf-8")

            captured: dict = {}

            def fake_run(argv, **kw):  # type: ignore[no-untyped-def]
                captured["argv"] = argv

                class _R:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                return _R()

            orig = wc.subprocess.run
            try:
                wc.subprocess.run = fake_run  # type: ignore[assignment]
                # profile present → bwrap-wrapped (claude)
                self._c(repo_root=repo, env={}).spawn_leaf(
                    "P", {"HOME": "/h"}, session_id="A", child_arid="A")
                self.assertEqual(captured["argv"][0], "bwrap")
                self.assertIn("claude", captured["argv"])
                self.assertIn("--", captured["argv"])
                # codex backend is also wrapped (it gets a profile + sandbox_enforced too).
                # Certify the codex hooks feature so _ensure_codex_feature_cache passes and
                # spawn_leaf reaches the bwrap-wrapping path under test (the cert itself has
                # dedicated coverage elsewhere).
                captured.clear()
                from unittest.mock import patch
                with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                           return_value=(True, "hooks=true")):
                    self._c(repo_root=repo, backend="codex", env={}).spawn_leaf(
                        "P", {"HOME": "/h"}, child_arid="A")
                self.assertEqual(captured["argv"][0], "bwrap")
                self.assertIn("codex", captured["argv"])
                # profile missing → fail closed (never launch unconfined)
                captured.clear()
                with self.assertRaises(RuntimeError):
                    self._c(repo_root=repo, env={}).spawn_leaf(
                        "P", {"HOME": "/h"}, session_id="Z", child_arid="Z")
                self.assertNotIn("argv", captured)
                # no child_arid (e.g. diagnostician) → also fail closed
                with self.assertRaises(RuntimeError):
                    self._c(repo_root=repo, env={}).spawn_leaf("P", {"HOME": "/h"})
                self.assertNotIn("argv", captured)
                # structurally invalid profile (missing repo_root/tmp_dir) →
                # SandboxEnforcementError (so conduct terminalizes fail_closed), not a
                # bare ValueError bubbling up as a generic conductor error.
                (prof_dir / "BAD.json").write_text(json.dumps({"read_roots": []}),
                                                   encoding="utf-8")
                with self.assertRaises(wc.SandboxEnforcementError):
                    self._c(repo_root=repo, env={}).spawn_leaf(
                        "P", {"HOME": "/h"}, session_id="BAD", child_arid="BAD")
                self.assertNotIn("argv", captured)
            finally:
                wc.subprocess.run = orig  # type: ignore[assignment]

    def test_spawn_leaf_missing_bwrap_binary_fails_closed(self) -> None:
        # D3: when the bwrap binary is absent (e.g. preflight bypassed via
        # ASSUME_BWRAP on a host without bwrap), subprocess.run raises
        # FileNotFoundError. With a valid profile present, spawn_leaf must convert
        # that to a SandboxEnforcementError so it routes to the unified fail-closed
        # path instead of bubbling up as a generic conductor_error.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ws_tmp = repo / "workspace" / "tmp" / "A"
            ws_tmp.mkdir(parents=True)
            prof_dir = repo / "workspace" / "orchestrations" / "o" / "sandbox_profiles"
            prof_dir.mkdir(parents=True)
            (prof_dir / "A.json").write_text(json.dumps({
                "repo_root": str(repo), "tmp_dir": str(ws_tmp),
                "workspace_tmp_rw_abs": str(ws_tmp),
                "read_roots": [], "write_roots": [],
                "runtime_ro_bind_paths": [], "runtime_rw_bind_paths": [],
            }), encoding="utf-8")

            def fake_run(argv, **kw):  # type: ignore[no-untyped-def]
                raise FileNotFoundError(2, "No such file or directory", "bwrap")

            orig = wc.subprocess.run
            try:
                wc.subprocess.run = fake_run  # type: ignore[assignment]
                with self.assertRaises(wc.SandboxEnforcementError):
                    self._c(repo_root=repo, env={}).spawn_leaf(
                        "P", {"HOME": "/h"}, session_id="A", child_arid="A")
            finally:
                wc.subprocess.run = orig  # type: ignore[assignment]


class FailSummaryContractTest(unittest.TestCase):
    """Every non-pass substep payload must satisfy the REAL runtime summary
    validator so finalize-child never crashes (the bug that aborted runs as
    conductor_error). The other tests stub runtime(), so these feed the produced
    payload through orchestration_runtime's actual validator end-to-end."""

    def _run_one_substep(self, proc, status_fn, phase="compile", substep="verify"):
        import tools.orchestration_runtime as rt
        with tempfile.TemporaryDirectory() as tmp:
            c = _FakeConductor(repo_root=Path(tmp), orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            c.status_fn = status_fn
            c.spawn_leaf = lambda prompt, env, **kw: proc
            refs = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x",
                               ir_id="x_1_001", pipeline_id="x_1_001")
            oc = c.run_substep(refs, phase, substep)
            payload = [cap["--agent-run-json"] for s, cap in c.calls
                       if s == "finalize-child"][0]
        text = rt._extract_agent_summary_text(payload)
        rt._validate_agent_summary_text(payload, text)  # must NOT raise
        return oc, payload, text

    def test_nonzero_exit_payload_passes_real_validator(self) -> None:
        oc, payload, text = self._run_one_substep(
            wc.ProcResult(1, "", "context limit exceeded"), lambda p, s, n: "pass")
        self.assertEqual(payload["status"], "fail")
        self.assertIn("context limit exceeded", payload["result_summary"])
        self.assertIn("result_summary:", text)

    def test_returncode0_content_fail_payload_passes_real_validator(self) -> None:
        # The path the first fix missed: leaf exited 0 but artifacts say fail.
        oc, payload, text = self._run_one_substep(
            wc.ProcResult(0, "ok", ""), lambda p, s, n: "fail")
        self.assertEqual(oc.leaf_returncode, 0)
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["result_summary"], "substep_fail: compile.verify")
        self.assertIn("result_summary:", text)

    def test_build_phase_substep_none_content_fail_validates(self) -> None:
        # Build is a single step with substep=None: exercise the `if substep else ""`
        # branch so the generic tag stays `substep_fail: build` (not `build.None`).
        oc, payload, text = self._run_one_substep(
            wc.ProcResult(0, "ok", ""), lambda p, s, n: "fail",
            phase="build", substep=None)
        self.assertEqual(payload["status"], "fail")
        self.assertEqual(payload["result_summary"], "substep_fail: build")
        self.assertIn("result_summary:", text)

    def test_pass_payload_carries_output_refs_not_summary(self) -> None:
        oc, payload, text = self._run_one_substep(
            wc.ProcResult(0, "ok", ""), lambda p, s, n: "pass")
        self.assertEqual(payload["status"], "pass")
        self.assertNotIn("result_summary", payload)
        self.assertIn("output_refs:", text)


class WriteDependencyGraphTest(unittest.TestCase):
    """The conductor authors <ir_ref>/dependency_graph.json host-side at Compile start from
    deps.yaml + spec_catalog.yaml (the derived closure/topo graph moved out of the IR)."""

    def _conductor(self, repo: Path) -> _FakeConductor:
        return _FakeConductor(
            repo_root=repo, orchestration_id="o",
            orchestration_agent_run_id="ORCH", backend="claude", env={})

    def _seed(self, repo: Path) -> None:
        from tools.orchestration_runtime import _load_spec_catalog
        _load_spec_catalog.cache_clear()
        (repo / "spec" / "registry").mkdir(parents=True, exist_ok=True)
        (repo / "spec" / "registry" / "spec_catalog.yaml").write_text(
            "catalog_version: 0.2.0\nupdated_at: 2026-06-18\nspecs:\n"
            "  - spec_kind: component\n    spec_id: top\n    spec_version: \"0.1.0\"\n"
            "    deps_path: spec/component/top/deps.yaml\n"
            "  - spec_kind: component\n    spec_id: base\n    spec_version: \"0.1.0\"\n"
            "    deps_path: spec/component/base/deps.yaml\n", encoding="utf-8")
        (repo / "spec" / "component" / "top").mkdir(parents=True, exist_ok=True)
        (repo / "spec" / "component" / "top" / "deps.yaml").write_text(
            "spec_id: top\nspec_kind: component\ndependencies:\n"
            "  components:\n    - component_id: base\n      version_constraint: \">=0.1.0 <1.0.0\"\n"
            "  profiles: []\n", encoding="utf-8")
        (repo / "spec" / "component" / "base").mkdir(parents=True, exist_ok=True)
        (repo / "spec" / "component" / "base" / "deps.yaml").write_text(
            "spec_id: base\nspec_kind: component\ndependencies:\n"
            "  components: []\n  profiles: []\n", encoding="utf-8")

    def test_authors_sidecar_from_deps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._seed(repo)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            c = self._conductor(repo)
            # Call the REAL method (the fake no-ops it for the run_phase happy path).
            err = wc.Conductor._write_dependency_graph(c, refs)
            self.assertIsNone(err)
            graph = json.loads(
                (repo / refs.ir_ref / "dependency_graph.json").read_text(encoding="utf-8"))
            self.assertEqual(graph["node_key"], "component/top@0.1.0")
            self.assertEqual(graph["generated_by"], "conductor")
            self.assertEqual(graph["all_nodes"], [
                {"node_key": "component/base@0.1.0", "topo_level": 0},
                {"node_key": "component/top@0.1.0", "topo_level": 1}])
            self.assertNotIn("operations", json.dumps(graph))

    def test_fail_closed_on_missing_deps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)  # no deps.yaml anywhere
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            c = self._conductor(repo)
            err = wc.Conductor._write_dependency_graph(c, refs)
            self.assertIsInstance(err, dict)
            self.assertEqual(err["reason"], "dependency_deps_unreadable")
            self.assertFalse((repo / refs.ir_ref / "dependency_graph.json").exists())


class WriteLineageTest(unittest.TestCase):
    """P2: lineage.json is authored host-side by the conductor (it lives at the pipeline
    root, which must stay non-writable to the sandboxed leaf)."""

    def _conductor(self, repo: Path) -> _FakeConductor:
        c = _FakeConductor(
            repo_root=repo, orchestration_id="o",
            orchestration_agent_run_id="ORCH", backend="claude", env={},
        )
        c.calls = []
        return c

    def test_authors_pipeline_lineage_for_leaf_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(
                node_key="component/dynamics_advdiff_flux_1d_upwind_center2@0.1.0",
                spec_path="spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2",
                ir_id="advdiff_20260622_001",
                pipeline_id="advdiff_20260622_002",
                source_id="src_20260622_001",
            )
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True)
            (ir_dir / "spec.ir.yaml").write_text(
                'dependency:\n'
                '  node_key: "component/dynamics_advdiff_flux_1d_upwind_center2@0.1.0"\n'
                '  direct_deps: []\n',
                encoding="utf-8")
            self._conductor(repo)._write_lineage(refs)
            lin_path = repo / refs.pipeline_ref / "lineage.json"
            self.assertTrue(lin_path.exists())
            lin = json.loads(lin_path.read_text(encoding="utf-8"))
            self.assertEqual(lin["node_key"], refs.node_key)
            self.assertEqual(lin["pipeline_id"], refs.pipeline_id)
            self.assertEqual(lin["ir_ref"], refs.ir_ref)
            self.assertEqual(lin["dependency_ref"], refs.ir_ref)
            self.assertEqual(lin["spec_ref"], refs.spec_path)
            self.assertEqual(lin["source_id"], refs.source_id)
            self.assertIsNone(lin["binary_id"])
            self.assertIsNone(lin["run_id"])
            self.assertEqual(lin["direct_dependency_status"], {})
            # Leaf node: no resolved dependency facts.
            self.assertEqual(lin["resolved_dependencies"], [])

    def test_accumulates_stage_ids_and_marks_direct_deps_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(
                node_key="component/x@0.1.0", spec_path="spec/component/x",
                ir_id="x_20260622_001", pipeline_id="x_20260622_002",
                source_id="src_001", binary_id="bin_001", run_id="run_001")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True)
            (ir_dir / "spec.ir.yaml").write_text(
                'dependency:\n'
                '  direct_deps:\n'
                '    - node_key: "component/dep@0.1.0"\n',
                encoding="utf-8")
            self._conductor(repo)._write_lineage(refs)
            lin = json.loads((repo / refs.pipeline_ref / "lineage.json").read_text(encoding="utf-8"))
            self.assertEqual(lin["source_id"], "src_001")
            self.assertEqual(lin["binary_id"], "bin_001")
            self.assertEqual(lin["run_id"], "run_001")
            self.assertEqual(lin["direct_dependency_status"], {"component/dep@0.1.0": "ready"})
            # The dep has no on-disk pipeline in this fixture → resolved facts empty.
            self.assertEqual(lin["resolved_dependencies"], [])

    def test_records_resolved_dependencies_when_dep_pipeline_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(
                node_key="component/top@0.1.0", spec_path="spec/component/top",
                ir_id="top_20260622_001", pipeline_id="top_20260622_002",
                source_id="src_001", binary_id="bin_001", run_id="run_001")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True)
            (ir_dir / "spec.ir.yaml").write_text(
                'dependency:\n'
                '  direct_deps:\n'
                '    - node_key: "component/base@0.1.0"\n',
                encoding="utf-8")
            # Materialize the dependency's on-disk pipeline (binary pass + bound verdict).
            safe = "component__base__0.1.0"
            pipe = repo / "workspace" / "pipelines" / safe / "base_20260622_003"
            b = pipe / "binary" / "bin_20260622_001"
            b.mkdir(parents=True)
            (b / "binary_meta.json").write_text(
                json.dumps({"verification_status": "pass"}), encoding="utf-8")
            rd = pipe / "runs" / "run_20260622_001" / safe
            rd.mkdir(parents=True)
            (rd / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
            (rd / "trial_meta.json").write_text(
                json.dumps({"source_binary_id": "bin_20260622_001"}), encoding="utf-8")

            facts = self._conductor(repo)._write_lineage(refs)
            self.assertEqual(len(facts), 1)
            self.assertEqual(facts[0]["node_key"], "component/base@0.1.0")
            self.assertEqual(facts[0]["run_id"], "run_20260622_001")
            # _write_lineage returns the same list it persists.
            lin = json.loads((repo / refs.pipeline_ref / "lineage.json").read_text(encoding="utf-8"))
            self.assertEqual(lin["resolved_dependencies"], facts)
            self.assertEqual(
                facts[0]["aggregate_verdict_ref"],
                f"workspace/pipelines/{safe}/base_20260622_003/runs/run_20260622_001/{safe}/"
                "aggregate_verdict.json")

    def test_persists_published_operations_for_fortran_consumer(self) -> None:
        # D5: _write_lineage surfaces the dependency call-site argument order (from the
        # certified source) into resolved_dependencies/lineage so the consumer's Generate
        # need not guess it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(
                node_key="component/top@0.1.0", spec_path="spec/component/top",
                ir_id="top_20260622_001", pipeline_id="top_20260622_002",
                source_id="src_001", binary_id="bin_001", run_id="run_001")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True)
            (ir_dir / "spec.ir.yaml").write_text(
                'impl_defaults:\n  toolchain:\n    language: fortran\n'
                'dependency:\n'
                '  direct_deps:\n'
                '    - node_key: "component/base@0.1.0"\n'
                '      operations: ["base__scale"]\n',
                encoding="utf-8")
            safe = "component__base__0.1.0"
            pipe = repo / "workspace" / "pipelines" / safe / "base_20260622_003"
            b = pipe / "binary" / "bin_20260622_001"
            b.mkdir(parents=True)
            (b / "binary_meta.json").write_text(
                json.dumps({"verification_status": "pass",
                            "source_source_id": "src_b_001"}), encoding="utf-8")
            src_dir = pipe / "source" / "src_b_001" / "src"
            src_dir.mkdir(parents=True)
            (src_dir / "base_model.f90").write_text(
                "module base_model\ncontains\n"
                "  subroutine base__scale(x, n, y)\n  end subroutine\n"
                "end module base_model\n", encoding="utf-8")
            rd = pipe / "runs" / "run_20260622_001" / safe
            rd.mkdir(parents=True)
            (rd / "aggregate_verdict.json").write_text(
                json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
            (rd / "trial_meta.json").write_text(
                json.dumps({"source_binary_id": "bin_20260622_001"}), encoding="utf-8")

            facts = self._conductor(repo)._write_lineage(refs)
            self.assertEqual(len(facts), 1)
            pub = facts[0]["published_operations"]
            self.assertEqual(pub[0]["operation"], "base__scale")
            self.assertEqual(pub[0]["argument_order"], ["x", "n", "y"])
            lin = json.loads((repo / refs.pipeline_ref / "lineage.json").read_text(encoding="utf-8"))
            self.assertEqual(lin["resolved_dependencies"], facts)


class BuildLaunchRequestResolvedDependenciesTest(unittest.TestCase):
    """build_launch_request attaches resolved_dependencies only for the LLM phases that
    benefit (generate / validate.judge) and only when the kwarg is non-empty."""

    DEP = {
        "node_key": "component/base@0.1.0",
        "pipeline_ref": "workspace/pipelines/component__base__0.1.0/p1",
        "run_id": "run_b_001",
        "aggregate_verdict_ref":
            "workspace/pipelines/component__base__0.1.0/p1/runs/run_b_001/"
            "component__base__0.1.0/aggregate_verdict.json",
    }

    def _refs(self) -> "wc.NodeRefs":
        return wc.NodeRefs(
            node_key="component/top@0.1.0", spec_path="spec/component/top",
            ir_id="top_001", pipeline_id="top_002",
            source_id="src_001", binary_id="bin_001",
            source_binary_id="bin_001", run_id="run_001")

    def _build(self, step: str, substep: str | None, deps) -> dict:
        return wc.build_launch_request(
            self._refs(), step=step, substep=substep, orchestration_id="o",
            orchestration_agent_run_id="parent", child_agent_run_id="child",
            agent_model="opus", workflow_mode="dev",
            case_ids=("l0_pass",) if step == "validate" else (),
            resolved_dependencies=deps)

    def test_judge_includes_when_present(self) -> None:
        req = self._build("validate", "judge", (self.DEP,))
        self.assertEqual(req["resolved_dependencies"], [self.DEP])

    def test_generate_includes_when_present(self) -> None:
        req = self._build("generate", "generate", (self.DEP,))
        self.assertEqual(req["resolved_dependencies"], [self.DEP])

    def test_omitted_when_kwarg_empty(self) -> None:
        self.assertNotIn("resolved_dependencies", self._build("validate", "judge", ()))

    def test_omitted_for_deterministic_execute_and_build(self) -> None:
        self.assertNotIn(
            "resolved_dependencies", self._build("validate", "execute", (self.DEP,)))
        self.assertNotIn(
            "resolved_dependencies", self._build("build", None, (self.DEP,)))
        # generate.lint is deterministic too: no resolved_dependencies / skill, and the
        # deterministic flag is set with lint-only allowed_output_paths.
        lint_req = self._build("generate", "lint", (self.DEP,))
        self.assertNotIn("resolved_dependencies", lint_req)
        self.assertNotIn("skill_name", lint_req)
        self.assertTrue(lint_req["deterministic"])
        outs = lint_req["allowed_output_paths"]
        self.assertTrue(any(p.endswith("/lint_meta.json") for p in outs))
        # lint does not author model/runner sources
        self.assertFalse(any(p.endswith("_model.f90") for p in outs))
        # generate.static is deterministic too: no resolved_dependencies / skill, and its
        # only allowed output is static_meta.json (no sources, no command_log).
        static_req = self._build("generate", "static", (self.DEP,))
        self.assertNotIn("resolved_dependencies", static_req)
        self.assertNotIn("skill_name", static_req)
        self.assertTrue(static_req["deterministic"])
        static_outs = static_req["allowed_output_paths"]
        self.assertEqual(
            [p for p in static_outs if p.endswith("/static_meta.json")], static_outs)
        self.assertFalse(any(p.endswith("_model.f90") for p in static_outs))

    def test_omitted_for_compile(self) -> None:
        self.assertNotIn(
            "resolved_dependencies", self._build("compile", "verify", (self.DEP,)))
        # compile.static is deterministic: no resolved_dependencies / skill, deterministic flag
        # set, and its only allowed output is compile_static_meta.json (under the IR dir, no
        # spec.ir.yaml / ir_meta.json authoring).
        cs_req = self._build("compile", "static", (self.DEP,))
        self.assertNotIn("resolved_dependencies", cs_req)
        self.assertNotIn("skill_name", cs_req)
        self.assertTrue(cs_req["deterministic"])
        cs_outs = cs_req["allowed_output_paths"]
        self.assertEqual(
            [p for p in cs_outs if p.endswith("/compile_static_meta.json")], cs_outs)
        self.assertEqual(len(cs_outs), 1)
        # compile.static is deterministic -> empty must-read (no leaf reads the NL spec/tests).
        self.assertEqual(cs_req["skill_must_read_refs"], "")
        # compile.generate authors the IR (spec.ir.yaml) + ir_meta.json.
        gen_outs = self._build("compile", "generate", ())["allowed_output_paths"]
        self.assertTrue(any(p.endswith("/spec.ir.yaml") for p in gen_outs))
        self.assertTrue(any(p.endswith("/ir_meta.json") for p in gen_outs))
        # compile.verify authors NOTHING in the IR (io_contract moved to generate; Compile.static
        # gated it): its sole write is ir_meta.json, so it cannot mutate spec.ir.yaml post-gate.
        ver_outs = self._build("compile", "verify", ())["allowed_output_paths"]
        self.assertEqual([p for p in ver_outs if p.endswith("/ir_meta.json")], ver_outs)
        self.assertFalse(any(p.endswith("/spec.ir.yaml") for p in ver_outs))
        # but verify still READS spec.ir.yaml to check it (must-read, not a write target).
        self.assertIn("/spec.ir.yaml",
                      self._build("compile", "verify", ())["skill_must_read_refs"])


class SnapshotDeliverableGapTest(unittest.TestCase):
    """D4: the execute-stage backstop produces an actionable diagnostic when the
    runner names snapshots off the per-case <case_id>.json contract, instead of an
    opaque deliverable-missing fail."""

    def _conductor(self, repo: Path) -> _FakeConductor:
        return _FakeConductor(
            repo_root=repo, orchestration_id="o",
            orchestration_agent_run_id="ORCH", backend="claude", env={},
        )

    def test_mismatch_yields_actionable_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "raw" / "state_snapshots"
            sdir.mkdir(parents=True)
            # Runner wrote a single combined file; the per-case files are absent.
            (sdir / "snapshot_0001.json").write_text("{}", encoding="utf-8")
            (sdir / "snapshot_schema.json").write_text("{}", encoding="utf-8")
            msg = self._conductor(Path(tmp))._snapshot_deliverable_gap(
                sdir, ["l0_pass", "l0_xfail"], ["state_snapshots"])
            self.assertIn("snapshot deliverable mismatch", msg)
            self.assertIn("l0_pass.json", msg)
            self.assertIn("l0_xfail.json", msg)
            self.assertIn("snapshot_0001.json", msg)  # what the runner actually wrote
            self.assertNotIn("snapshot_schema.json", msg)  # metadata excluded

    def test_all_present_no_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "raw" / "state_snapshots"
            sdir.mkdir(parents=True)
            for cid in ("l0_pass", "l0_xfail"):
                (sdir / f"{cid}.json").write_text("{}", encoding="utf-8")
            msg = self._conductor(Path(tmp))._snapshot_deliverable_gap(
                sdir, ["l0_pass", "l0_xfail"], ["state_snapshots"])
            self.assertEqual(msg, "")

    def test_no_gap_when_snapshots_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sdir = Path(tmp) / "raw" / "state_snapshots"
            c = self._conductor(Path(tmp))
            self.assertEqual(
                c._snapshot_deliverable_gap(sdir, ["l0_pass"], ["execution_trace.json"]),
                "")
            # No case_ids -> nothing to require.
            self.assertEqual(
                c._snapshot_deliverable_gap(sdir, [], ["state_snapshots"]), "")


class WriteMakefileTest(unittest.TestCase):
    """The conductor authors a leaf node's src/Makefile deterministically (runtime-owned,
    like lineage.json), for build_system=make + language=fortran."""

    def _conductor(self, repo: Path) -> _FakeConductor:
        c = _FakeConductor(repo_root=repo, orchestration_id="o",
                           orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/foo_bar@0.1.0", spec_path="spec/component/foo_bar",
            ir_id="i1", pipeline_id="p1", source_id="s1", binary_id="b1")

    def _write_ir(self, repo: Path, refs: wc.NodeRefs, *, language="fortran",
                  build_system="make", backend="openmp", direct_deps="[]") -> None:
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "spec.ir.yaml").write_text(
            "impl_defaults:\n"
            "  toolchain:\n"
            f"    language: {language}\n"
            "    standard: f2008\n"
            f"    build_system: {build_system}\n"
            "  target:\n"
            f"    backend: {backend}\n"
            "dependency:\n"
            f"  direct_deps: {direct_deps}\n",
            encoding="utf-8")

    def test_authors_makefile_for_leaf_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs)
            c = self._conductor(repo)
            self.assertTrue(c._is_leaf_node(refs))
            c._write_makefile(refs)
            mk = repo / refs.source_dir() / "src" / "Makefile"
            self.assertTrue(mk.is_file())
            text = mk.read_text(encoding="utf-8")
            self.assertIn("BIN ?= foo_bar_runner", text)
            # SPEC/CASES are ?= overridable (Validate.execute injects the authoritative
            # values; the defaults keep a local `make all test` runnable).
            self.assertIn("SPEC ?= spec.ir.yaml", text)
            self.assertIn("CASES ?=", text)
            # the test recipe invokes the runner with --cases (same argv as run_program)
            self.assertIn("$(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)", text)
            self.assertIn("-std=f2008 -O2 -fopenmp -J$(OBJDIR) -I$(OBJDIR)", text)
            self.assertIn("$(RUNNER_OBJ): $(RUNNER_SRC) $(MODEL_OBJ)", text)
            self.assertIn(
                'test -x $(BINDIR)/$(BIN) || { echo "error: $(BINDIR)/$(BIN) not built',
                text)
            # recipe lines must be tab-indented
            self.assertIn("\n\t$(FC) $(FFLAGS) -c $(MODEL_SRC)", text)
            # L1: the dir rule dedups its targets via $(sort ...) so OBJDIR==BINDIR=="."
            # (in-source make) collapses to one target — no `target '.' given more than
            # once` warning. The bare two-target form must not appear.
            self.assertIn("$(sort $(OBJDIR) $(BINDIR)):", text)
            self.assertNotIn("\n$(OBJDIR) $(BINDIR):", text)

    def test_authored_makefile_passes_post_generate_validators(self) -> None:
        from tools.validate_pipeline_semantics import (
            _validate_fortran_makefile_src_dir, _validate_makefile_bin_overridable,
            _validate_makefile_test_invokes_cases, _validate_makefile_test_no_relink)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs)
            c = self._conductor(repo)
            c._write_makefile(refs)
            src = repo / refs.source_dir() / "src"
            (src / "foo_bar_model.f90").write_text(
                "module foo_bar_model\nimplicit none\nend module foo_bar_model\n", encoding="utf-8")
            (src / "foo_bar_runner.f90").write_text(
                "program foo_bar_runner\nuse foo_bar_model\nimplicit none\nend program foo_bar_runner\n",
                encoding="utf-8")
            mk = src / "Makefile"
            violations: list[str] = []
            _validate_makefile_bin_overridable(mk, mk.read_text(encoding="utf-8"), violations)
            _validate_fortran_makefile_src_dir(src, violations)
            _validate_makefile_test_no_relink(src, violations, build_system="make", language="fortran")
            _validate_makefile_test_invokes_cases(src, violations, build_system="make", language="fortran")
            self.assertEqual(violations, [])

    def test_no_fopenmp_when_backend_not_openmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs, backend="serial")
            c = self._conductor(repo)
            c._write_makefile(refs)
            text = (repo / refs.source_dir() / "src" / "Makefile").read_text(encoding="utf-8")
            self.assertNotIn("-fopenmp", text)
            self.assertIn("-std=f2008 -O2 -J$(OBJDIR) -I$(OBJDIR)", text)

    def test_skips_non_fortran_and_non_make(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            # language=c -> out of scope (keep LLM authoring)
            self._write_ir(repo, refs, language="c")
            self._conductor(repo)._write_makefile(refs)
            self.assertFalse((repo / refs.source_dir() / "src" / "Makefile").exists())
            # build_system=cmake -> out of scope
            self._write_ir(repo, refs, build_system="cmake")
            self._conductor(repo)._write_makefile(refs)
            self.assertFalse((repo / refs.source_dir() / "src" / "Makefile").exists())

    def test_is_leaf_node_false_for_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs, direct_deps="[component/dep@0.1.0]")
            self.assertFalse(self._conductor(repo)._is_leaf_node(refs))

    def test_is_leaf_node_false_when_direct_deps_absent(self) -> None:
        # Undeterminable leaf-ness (no dependency block / no direct_deps key) -> False, to
        # agree with the runtime's _impl_is_leaf_node (None -> treated as non-leaf). A
        # disagreement would author the Makefile but still pin it (or vice versa).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(
                "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n",
                encoding="utf-8")
            self.assertFalse(self._conductor(repo)._is_leaf_node(refs))

    def test_conductor_authors_makefile_requires_make_fortran(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            c = self._conductor(repo)
            self._write_ir(repo, refs)
            self.assertTrue(c._conductor_authors_makefile(refs))
            self._write_ir(repo, refs, language="c")
            self.assertFalse(c._conductor_authors_makefile(refs))  # non-fortran -> no author
            self._write_ir(repo, refs, build_system="cmake")
            self.assertFalse(c._conductor_authors_makefile(refs))  # non-make -> no author
            # A dependency make+fortran node is ALSO conductor-authored (Model B): the
            # dependency Makefile is as IR-determined as the leaf one.
            self._write_ir(repo, refs, direct_deps="[component/dep@0.1.0]")
            self.assertTrue(c._conductor_authors_makefile(refs))

    def test_conductor_runtime_makefile_authorship_agree(self) -> None:
        # The conductor (_conductor_authors_makefile) and the runtime
        # (_resolved_makefile_host_authored, computed in record_launch) must agree on whether
        # the Makefile is host-authored, else a launch is double-owned (pinned + dropped) or
        # orphaned (neither authors). Authorship keys off make+fortran for BOTH leaf and
        # dependency nodes (Model B); covers absent keys (build_system/language), where the
        # two sides must apply the SAME defaults. The reconstruction below mirrors the runtime
        # computation in orchestration_runtime.record_launch verbatim.
        from tools.orchestration_runtime import (
            _impl_resolved_build_system, _impl_resolved_language)
        cases = [
            # (label, spec.ir.yaml text)
            ("leaf+make+fortran",
             "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
             "dependency:\n  direct_deps: []\n"),
            ("leaf+c",
             "impl_defaults:\n  toolchain:\n    language: c\n    build_system: make\n"
             "dependency:\n  direct_deps: []\n"),
            ("leaf+cmake",
             "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: cmake\n"
             "dependency:\n  direct_deps: []\n"),
            ("dependency",
             "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
             "dependency:\n  direct_deps:\n    - node_key: component/dep@0.1.0\n"),
            ("leaf+build_system absent",
             "impl_defaults:\n  toolchain:\n    language: fortran\n"
             "dependency:\n  direct_deps: []\n"),
            ("leaf+language absent",
             "impl_defaults:\n  toolchain:\n    build_system: make\n"
             "dependency:\n  direct_deps: []\n"),
            ("direct_deps absent",
             "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
             "dependency:\n  node_key: component/x@0.1.0\n"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            ir_path = repo / refs.ir_ref / "spec.ir.yaml"
            ir_path.parent.mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            for label, ir_text in cases:
                ir_path.write_text(ir_text, encoding="utf-8")
                conductor_authors = c._conductor_authors_makefile(refs)
                # reconstruct the runtime's _resolved_makefile_host_authored verbatim
                bs = (_impl_resolved_build_system(repo, refs.ir_ref) or "")
                lang = _impl_resolved_language(repo, refs.ir_ref)
                runtime_host_authored = (
                    (bs or "make") == "make"
                    and (lang or "fortran") == "fortran")
                self.assertEqual(conductor_authors, runtime_host_authored,
                                 f"conductor/runtime disagree for {label!r}")

    # --- Part 2 (Model B): dependency Makefile rendering. The non-leaf branch DOES run live —
    # run_phase authors for every make+fortran node (leaf OR dependency; _conductor_authors_
    # makefile has no leaf gate). E2E-UNVERIFIED only in that no real dependency spec has run
    # the full compile->validate path yet; these synthetic-IR tests pin the generated structure. ---

    def _write_dep_graph_sidecar(self, repo: Path, refs: wc.NodeRefs, *,
                                 all_nodes: list, transitive_deps: list) -> None:
        """Author the conductor-authored dependency-graph sidecar the consumers now read
        (the derived closure/topo graph moved out of the IR; see _write_dependency_graph)."""
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "dependency_graph.json").write_text(json.dumps({
            "node_key": refs.node_key,
            "all_nodes": all_nodes,
            "transitive_deps": transitive_deps,
            "generated_by": "conductor",
        }, indent=2) + "\n", encoding="utf-8")

    def _write_dep_ir(self, repo: Path, refs: wc.NodeRefs) -> None:
        # The IR keeps only node_key + direct_deps (the low-mutation directly-read edge);
        # `mid` is the direct dep. The derived closure/topo graph (all_nodes / transitive_deps
        # with topo_level / via) lives in the conductor-authored sidecar dependency_graph.json:
        # `base` is reached transitively `via` mid. The build closure is the sidecar's all_nodes
        # minus self.
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "spec.ir.yaml").write_text(
            "impl_defaults:\n  toolchain:\n    language: fortran\n    standard: f2008\n"
            "    build_system: make\n  target:\n    backend: openmp\n"
            "dependency:\n"
            '  node_key: "component/top@0.1.0"\n'
            "  direct_deps:\n    - node_key: \"component/mid@0.1.0\"\n",
            encoding="utf-8")
        self._write_dep_graph_sidecar(repo, refs, all_nodes=[
            {"node_key": "component/base@0.1.0", "topo_level": 0},
            {"node_key": "component/mid@0.1.0", "topo_level": 1},
            {"node_key": "component/top@0.1.0", "topo_level": 2},
        ], transitive_deps=[
            {"node_key": "component/base@0.1.0", "via": ["component/mid@0.1.0"]},
        ])

    def test_dependency_closure_is_deepest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            self._write_dep_ir(repo, refs)
            self.assertEqual(self._conductor(repo)._dependency_closure(refs), ["base", "mid"])

    def test_dependency_closure_one_hop_direct_only(self) -> None:
        # A one-hop chain (top -> base, base a leaf) has a non-empty direct_deps and an EMPTY
        # transitive_deps (no indirect deps). The closure must still resolve to [base] — the
        # union of direct+transitive, not transitive alone (regression: an empty result here
        # would fail-close the whole dependency build for the common single-edge case).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(
                "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
                "dependency:\n"
                '  node_key: "component/top@0.1.0"\n'
                "  direct_deps:\n    - node_key: \"component/base@0.1.0\"\n",
                encoding="utf-8")
            self._write_dep_graph_sidecar(repo, refs, all_nodes=[
                {"node_key": "component/base@0.1.0", "topo_level": 0},
                {"node_key": "component/top@0.1.0", "topo_level": 1},
            ], transitive_deps=[])
            c = self._conductor(repo)
            self.assertEqual(c._dependency_closure_nodes(refs), ["component/base@0.1.0"])
            self.assertEqual(c._dependency_closure(refs), ["base"])

    def test_dependency_closure_raises_on_spec_id_basename_collision(self) -> None:
        # L6: two distinct closure node_keys sharing a spec_id (a diamond on `foo`: two
        # versions) collide on the bare-spec_id staged source `foo_model.f90` / object
        # `foo_model.o` / `module foo_model`. The closure must fail closed with an actionable
        # cause rather than silently clobber (last-write-wins). Guarded at the shared
        # `_dependency_closure_nodes` chokepoint, so both _dependency_closure (Makefile rules)
        # and _stage_dependency_sources inherit it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(
                "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
                "dependency:\n"
                '  node_key: "component/top@0.1.0"\n'
                "  direct_deps:\n"
                '    - node_key: "component/foo@1.0.0"\n'
                '    - node_key: "component/foo@2.0.0"\n',
                encoding="utf-8")
            self._write_dep_graph_sidecar(repo, refs, all_nodes=[
                {"node_key": "component/foo@1.0.0", "topo_level": 0},
                {"node_key": "component/foo@2.0.0", "topo_level": 0},
                {"node_key": "component/top@0.1.0", "topo_level": 1},
            ], transitive_deps=[])
            c = self._conductor(repo)
            with self.assertRaisesRegex(RuntimeError, "spec_id basename collision"):
                c._dependency_closure_nodes(refs)
            # both named consumers inherit the guard at the shared chokepoint
            with self.assertRaisesRegex(RuntimeError, "spec_id basename collision"):
                c._dependency_closure(refs)
            with self.assertRaisesRegex(RuntimeError, "spec_id basename collision"):
                c._stage_dependency_sources(refs, repo / "obj")

    def test_dependency_makefile_emits_closure_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            self._write_dep_ir(repo, refs)
            self._conductor(repo)._write_makefile(refs)
            text = (repo / refs.source_dir() / "src" / "Makefile").read_text(encoding="utf-8")
            self.assertIn("DEP_OBJS = $(OBJDIR)/base_model.o $(OBJDIR)/mid_model.o", text)
            # deepest-first: base before mid, and mid depends on base
            self.assertIn("$(OBJDIR)/base_model.o: $(OBJDIR)/base_model.f90 | $(OBJDIR)", text)
            self.assertIn(
                "$(OBJDIR)/mid_model.o: $(OBJDIR)/mid_model.f90 $(OBJDIR)/base_model.o | $(OBJDIR)",
                text)
            self.assertIn("$(MODEL_OBJ): $(MODEL_SRC) $(DEP_OBJS) | $(OBJDIR)", text)
            self.assertIn("$(BINDIR)/$(BIN): $(DEP_OBJS) $(MODEL_OBJ) $(RUNNER_OBJ)", text)

    def _seed_dep_pipeline(self, repo: Path, node_key: str, pipeline_id: str,
                           source_id: str, body: str, *, binary_id: str = "bin_20260101_001",
                           binary_source_id: str | None = None,
                           lineage_source_id: str | None = None) -> Path:
        """Create a ready dependency pipeline under the canonical version-pinned workspace
        path so _stage_dependency_sources resolves it: the <dep>_model.f90 source, a
        binary_meta.json whose `source_source_id` is the CERTIFIED source (what staging binds
        to), and lineage.json. `binary_source_id`/`lineage_source_id` default to `source_id`;
        override them separately to model a lineage advanced past the certified binary's
        source. Returns the certified-source model path."""
        safe = wc.node_key_safe(node_key)
        sid = wc.spec_id_of(node_key)
        binary_source_id = binary_source_id or source_id
        lineage_source_id = lineage_source_id or source_id
        pipe = repo / "workspace" / "pipelines" / safe / pipeline_id
        (pipe / "source" / binary_source_id / "src").mkdir(parents=True, exist_ok=True)
        (pipe / "binary" / binary_id).mkdir(parents=True, exist_ok=True)
        (pipe / "binary" / binary_id / "binary_meta.json").write_text(
            wc.json.dumps({"verification_status": "pass",
                           "source_source_id": binary_source_id}) + "\n", encoding="utf-8")
        (pipe / "lineage.json").write_text(
            wc.json.dumps({"source_id": lineage_source_id}) + "\n", encoding="utf-8")
        model = pipe / "source" / binary_source_id / "src" / f"{sid}_model.f90"
        model.write_text(body, encoding="utf-8")
        return model

    def test_stage_dependency_sources_copies_closure_into_objdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            self._write_dep_ir(repo, refs)
            self._seed_dep_pipeline(repo, "component/base@0.1.0", "base_20260101_001",
                                    "src_base", "module base_model\nend module base_model\n")
            self._seed_dep_pipeline(repo, "component/mid@0.1.0", "mid_20260101_001",
                                    "src_mid", "module mid_model\nend module mid_model\n")
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            staged = self._conductor(repo)._stage_dependency_sources(refs, obj_dir)
            # deepest-first (base before mid), matching the Makefile object order
            self.assertEqual(len(staged), 2)
            self.assertTrue(staged[0].endswith("base_model.f90"))
            self.assertTrue(staged[1].endswith("mid_model.f90"))
            self.assertEqual((obj_dir / "base_model.f90").read_text(encoding="utf-8"),
                             "module base_model\nend module base_model\n")
            self.assertEqual((obj_dir / "mid_model.f90").read_text(encoding="utf-8"),
                             "module mid_model\nend module mid_model\n")
            # canonical src/ of the depending node is never touched (no top model written)
            self.assertFalse((repo / refs.source_dir() / "src").exists())

    def test_stage_dependency_sources_binds_to_certified_binary_source(self) -> None:
        # Regression (Codex P2): when lineage.json has advanced to a NEWER source than the
        # certified binary was built from, staging must use the CERTIFIED binary's
        # source_source_id, not the latest lineage source (which is unverified).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(
                "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
                "dependency:\n  node_key: \"component/top@0.1.0\"\n"
                "  direct_deps:\n    - node_key: \"component/base@0.1.0\"\n",
                encoding="utf-8")
            self._write_dep_graph_sidecar(repo, refs, all_nodes=[
                {"node_key": "component/base@0.1.0", "topo_level": 0},
                {"node_key": "component/top@0.1.0", "topo_level": 1},
            ], transitive_deps=[])
            # certified binary built from src_cert; lineage advanced to src_new (unverified).
            self._seed_dep_pipeline(
                repo, "component/base@0.1.0", "base_20260101_001", "src_cert",
                "module base_model ! CERTIFIED\nend module base_model\n",
                lineage_source_id="src_new")
            # the newer, unverified source the lineage points at — must NOT be staged.
            new_src = (repo / "workspace" / "pipelines" / "component__base__0.1.0"
                       / "base_20260101_001" / "source" / "src_new" / "src")
            new_src.mkdir(parents=True, exist_ok=True)
            (new_src / "base_model.f90").write_text(
                "module base_model ! NEWER UNVERIFIED\nend module base_model\n", encoding="utf-8")
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            staged = self._conductor(repo)._stage_dependency_sources(refs, obj_dir)
            self.assertEqual(len(staged), 1)
            self.assertIn("CERTIFIED", (obj_dir / "base_model.f90").read_text(encoding="utf-8"))
            self.assertIn("src_cert", staged[0])

    def test_stage_dependency_sources_noop_for_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs)  # leaf (direct_deps: [])
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            self.assertEqual(self._conductor(repo)._stage_dependency_sources(refs, obj_dir), [])

    def test_stage_dependency_sources_raises_when_dep_unbuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            self._write_dep_ir(repo, refs)
            # only base is built; mid is missing -> fail-closed (build precondition)
            self._seed_dep_pipeline(repo, "component/base@0.1.0", "base_20260101_001",
                                    "src_base", "module base_model\nend module base_model\n")
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            with self.assertRaises(RuntimeError):
                self._conductor(repo)._stage_dependency_sources(refs, obj_dir)

    def test_stage_dependency_sources_raises_on_empty_closure_with_direct_deps(self) -> None:
        # IR declares direct_deps but the closure (now from the dependency_graph.json sidecar's
        # all_nodes) resolves empty — a missing/leaf-shaped sidecar. Fail closed instead of
        # staging a leaf-shaped build.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs, direct_deps="[{operations: [x]}]")  # no sidecar authored
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            with self.assertRaisesRegex(RuntimeError, "empty build closure"):
                self._conductor(repo)._stage_dependency_sources(refs, obj_dir)

    def test_stage_dependency_sources_fails_closed_on_version_mismatch(self) -> None:
        # The sidecar pins base@0.2.0, but only base@0.1.0 is built. Staging must FAIL CLOSED
        # (not substitute the sibling version, which could link stale/constraint-incompatible
        # code) — the L6-deferred multi-version case. (Unreachable for single-version specs.)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/top@0.1.0", spec_path="spec/component/top",
                               ir_id="i", pipeline_id="p", source_id="s", binary_id="b")
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(
                "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
                "dependency:\n  node_key: \"component/top@0.1.0\"\n"
                "  direct_deps:\n    - node_key: \"component/base@0.2.0\"\n",
                encoding="utf-8")
            self._write_dep_graph_sidecar(repo, refs, all_nodes=[
                {"node_key": "component/base@0.2.0", "topo_level": 0},
                {"node_key": "component/top@0.1.0", "topo_level": 1},
            ], transitive_deps=[])
            # Only base@0.1.0 is built — NOT the pinned 0.2.0.
            self._seed_dep_pipeline(repo, "component/base@0.1.0", "base_20260101_001",
                                    "src_base", "module base_model\nend module base_model\n")
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            with self.assertRaisesRegex(RuntimeError, "no ready pipeline"):
                self._conductor(repo)._stage_dependency_sources(refs, obj_dir)

    def test_stage_dependency_sources_noop_for_non_fortran(self) -> None:
        # A c/cpp/mixed dependency node keeps its LLM-authored Makefile and owns its own
        # dependency build; the conductor must NOT stage Fortran <dep>_model.f90 (they do not
        # exist under that name) — staging is a no-op, not a fail-closed.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs, language="c", direct_deps="[component/dep@0.1.0]")
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            self.assertEqual(self._conductor(repo)._stage_dependency_sources(refs, obj_dir), [])


class GenerateLeafAuthorizationTest(unittest.TestCase):
    """For a leaf node, src/Makefile is conductor-authored, so it is dropped from the leaf's
    generate allowed_output_paths and required_outputs (it must not author it)."""

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/foo_bar@0.1.0", spec_path="spec/component/foo_bar",
            ir_id="i1", pipeline_id="p1", source_id="s1", binary_id="b1")

    def _launch(self, refs: wc.NodeRefs, substep: str, *, host_authored: bool) -> dict:
        return wc.build_launch_request(
            refs, step="generate", substep=substep, orchestration_id="o",
            orchestration_agent_run_id="p", child_agent_run_id="c", agent_model="m",
            workflow_mode="dev", makefile_host_authored=host_authored)

    def test_leaf_generate_launch_omits_makefile(self) -> None:
        refs = self._refs()
        mk = f"{refs.source_dir()}/src/Makefile"
        for substep in ("generate", "verify"):
            req = self._launch(refs, substep, host_authored=True)
            self.assertNotIn(mk, req["allowed_output_paths"], f"{substep} should omit Makefile")

    def test_dependency_generate_launch_keeps_makefile(self) -> None:
        refs = self._refs()
        mk = f"{refs.source_dir()}/src/Makefile"
        for substep in ("generate", "verify"):
            req = self._launch(refs, substep, host_authored=False)
            self.assertIn(mk, req["allowed_output_paths"], f"{substep} should keep Makefile")

    def test_phase_required_outputs_leaf_omits_makefile(self) -> None:
        refs = self._refs()
        mk = f"{refs.source_dir()}/src/Makefile"
        self.assertNotIn(mk, wc.phase_required_outputs(refs, "generate", makefile_required=False))
        self.assertIn(mk, wc.phase_required_outputs(refs, "generate", makefile_required=True))


class DeterministicBuildTest(unittest.TestCase):
    """WS-A/C: build runs in-process (no leaf) yet reuses the same bookkeeping."""

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_20260101_001", pipeline_id="x_20260101_001",
            source_id="src_20260101_001", binary_id="bin_20260101_001",
            run_id="run_20260101_001", source_binary_id="bin_20260101_001",
        )

    def test_build_required_outputs_use_resolved_exe_name(self) -> None:
        # B1: the build step_result records the binary at the imposed exe basename;
        # the default (no exe_name threaded) falls back to <spec_id>_runner.
        refs = self._refs()
        bdir = refs.binary_dir()
        resolved = wc.phase_required_outputs(refs, "build", exe_name="foo")
        self.assertIn(f"{bdir}/bin/foo", resolved)
        self.assertNotIn(f"{bdir}/bin/{refs.spec_id}_runner", resolved)
        default = wc.phase_required_outputs(refs, "build")
        self.assertIn(f"{bdir}/bin/{refs.spec_id}_runner", default)

    def test_build_runs_in_process_without_leaf(self) -> None:
        captured: dict = {}

        class C(_FakeConductor):
            # exercise the REAL deterministic dispatch (not the _FakeConductor stub)
            def _run_deterministic_substep(self, *a, **k):  # type: ignore[override]
                return wc.Conductor._run_deterministic_substep(self, *a, **k)

            def spawn_leaf(self, *a, **k):  # type: ignore[override]
                raise AssertionError("leaf must not spawn for deterministic build")

            def _capability_token(self, child_arid):  # type: ignore[override]
                return "captok"

            def _build_inproc(self, refs, child_arid, cap_token):  # type: ignore[override]
                captured["cap_token"] = cap_token
                captured["child_arid"] = child_arid
                return {"stdout": "compiled", "stderr": ""}

            def _persist_leaf_output(self, child_arid, proc, prefix="leaf"):  # type: ignore[override]
                captured["prefix"] = prefix

        c = C(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
              orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        oc = c.run_substep(self._refs(), "build", None)

        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.leaf_returncode, 0)
        self.assertEqual(captured["cap_token"], "captok")
        self.assertEqual(captured["prefix"], "deterministic")
        subs = [s for s, _ in c.calls]
        # SAME bookkeeping as a leaf run, but the conductor issues the child-return.
        self.assertIn("record-launch", subs)
        self.assertIn("record-child-return", subs)
        self.assertIn("finalize-child", subs)

    def test_build_infra_failure_nonzero_returncode_fails_substep(self) -> None:
        # A conductor/MCP INFRA failure (the _run_deterministic_substep except clause,
        # e.g. missing capability) returns rc != 0 -> transport fail (leaf_returncode 1).
        # Content failures (compile/gate) instead return rc 0 and route via the tables.
        class C(_FakeConductor):
            def _run_deterministic_substep(self, *a, **k):  # type: ignore[override]
                return wc.Conductor._run_deterministic_substep(self, *a, **k)

            def _capability_token(self, child_arid):  # type: ignore[override]
                raise RuntimeError("missing capability token")  # infra failure

            def _persist_leaf_output(self, *a, **k):  # type: ignore[override]
                pass

        c = C(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
              orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        oc = c.run_substep(self._refs(), "build", None)
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 1)

    def test_build_inproc_fails_when_binary_not_at_contract_path(self) -> None:
        # E2E regression: a compile that succeeds but produces no binary at the contract
        # path (bin/<spec_id>_runner) must produce a clean fail (verification_status=fail,
        # make_error -> regenerate), NOT a pass binary_meta pointing at a missing file
        # (which escalated/fail_closed).
        import sys
        import tempfile
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="t",
                             orchestration_agent_run_id="x", backend="claude", env={})
            refs = wc.NodeRefs(
                node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1")
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)

            def fake_compile(args):  # ok, but produces NO binary
                return {"ok": True, "return_code": 0, "command_id": "cid"}

            with mock.patch.object(build_runtime_server, "tool_compile_project", fake_compile):
                out = c._build_inproc(refs, "child-1", "captok")

            self.assertEqual(out["returncode"], 0)  # content fail, not transport
            meta = json.loads((repo / refs.binary_dir() / "binary_meta.json").read_text())
            self.assertEqual(meta["verification_status"], "fail")
            self.assertEqual(meta["failure_category"], "make_error")
            self.assertTrue(meta["failure_source_refs"][0].endswith("/Makefile"))

    def test_build_inproc_imposes_canonical_bin_override(self) -> None:
        # The binary name is imposed (not derived from the Makefile): Build passes
        # BIN=<spec_id>_runner on the make command line and produces the binary there.
        import sys
        import tempfile
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="t",
                             orchestration_agent_run_id="x", backend="claude", env={})
            refs = wc.NodeRefs(
                node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1")
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)

            captured: dict = {}

            def fake_compile(args):
                captured["extra_args"] = args.get("extra_args")
                # honor the BIN override: write the binary where Build expects it
                (repo / refs.binary_dir() / "bin").mkdir(parents=True, exist_ok=True)
                (repo / refs.binary_dir() / "bin" / "spec_x_runner").write_text("x")
                return {"ok": True, "return_code": 0, "command_id": "cid"}

            with mock.patch.object(build_runtime_server, "tool_compile_project", fake_compile):
                c._build_inproc(refs, "child-1", "captok")

            self.assertIn("BIN=spec_x_runner", captured["extra_args"])

    def test_execute_inproc_injects_spec_and_cases_env(self) -> None:
        # Validate.execute must run `make test` with the SAME runner argv run_program uses
        # (--cases <spec> <case_id>...), so the make-test re-run's diagnostics match for the
        # quality_check value comparison. The conductor imposes SPEC/CASES via the make-test
        # env (the test target invokes `$(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`).
        import sys
        import tempfile
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="t",
                             orchestration_agent_run_id="x", backend="claude", env={})
            refs = wc.NodeRefs(
                node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
                run_id="run_1", source_binary_id="bin_1")
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.ir_ref / "spec.ir.yaml").write_text(
                "impl_defaults:\n"
                "  toolchain:\n    language: fortran\n    standard: f2008\n"
                "    build_system: make\n"
                "  target:\n    class: cpu\n    backend: openmp\n"
                "case:\n  test_case_set:\n    - case_id: c_alpha\n    - case_id: c_beta\n",
                encoding="utf-8")
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)

            captured: dict = {}

            def fake_run_program(args):
                return {"ok": True, "command_id": "R"}

            def fake_run_quality_checks(args):
                captured["env"] = dict(args.get("env") or {})
                return {"ok": True, "command_id": "Q"}

            with mock.patch.object(build_runtime_server, "tool_run_program", fake_run_program), \
                 mock.patch.object(build_runtime_server, "tool_run_quality_checks", fake_run_quality_checks):
                try:
                    c._execute_inproc(refs, "child-1", "captok")
                except Exception:
                    pass  # downstream promotion/gates are irrelevant; env is captured above

            env = captured["env"]
            self.assertEqual(
                env["SPEC"], str((repo / refs.ir_ref / "spec.ir.yaml").resolve()))
            self.assertEqual(env["CASES"], "c_alpha c_beta")  # read_case_ids is sorted
            self.assertEqual(env["BIN"], "spec_x_runner")

    def test_build_content_failure_routes_to_generate_not_transport(self) -> None:
        # Codex finding 1: a build content failure (rc 0 + binary_meta verification_status
        # =fail) must route via classify_build_failure -> Generate, NOT leaf_transport_error
        # fail_closed. Drive run_phase with a stubbed deterministic body + binary_meta.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()

            class C(_FakeConductor):
                def _run_deterministic_substep(self, refs2, phase, substep, child_arid, request):  # type: ignore[override]
                    # content failure: rc 0, and author a failing binary_meta
                    mp = self.repo_root / refs2.binary_dir() / "binary_meta.json"
                    mp.parent.mkdir(parents=True, exist_ok=True)
                    mp.write_text(json.dumps({"verification_status": "fail",
                                              "failure_category": "compile_error"}),
                                  encoding="utf-8")
                    return wc.ProcResult(0, "", "compile error")

                def determine_substep_status(self, *a, **k):  # type: ignore[override]
                    return "fail", []

                def _write_lineage(self, *a, **k):  # type: ignore[override]
                    return []

            c = C(repo_root=repo, orchestration_id="orch_x",
                  orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls = []
            outcome = c.run_phase(refs, "build")
            self.assertEqual(outcome.status, "fail")
            # routed by the decision table to Generate (retry), NOT fail_closed/transport
            self.assertEqual(outcome.decision.action, "retry")
            self.assertEqual(outcome.decision.target_phase, "generate")
            self.assertNotIn("transport", (outcome.decision.reason or ""))

    def test_require_make_build_system_rejects_non_make(self) -> None:
        c = wc.Conductor(repo_root=Path("/tmp/r"), orchestration_id="o",
                         orchestration_agent_run_id="O", backend="claude", env={})
        c._require_make_build_system("make", "build")  # no raise
        for bs in ("cmake", "meson", "ninja"):
            with self.assertRaisesRegex(RuntimeError, "build_system=make only"):
                c._require_make_build_system(bs, "build")

    def test_execute_failure_routes_to_generate(self) -> None:
        # An execute-substep failure (no verdict.json, judge never ran) is a runner code
        # defect -> retry Generate (restart), not escalate/fail_closed.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="O", backend="claude", env={})
            refs = self._refs()
            # execute is the failed substep (index 1): outcomes == [pre_judge(pass), execute(fail)].
            # No verdict.json under the run node dir -> runner code defect.
            ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                       wc.SubstepOutcome("ex", "fail", [], 0)]
            decision = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual(decision.action, "retry")
            self.assertEqual(decision.target_phase, "generate")
            self.assertEqual(decision.repair_strategy, "restart")

    def test_semantic_review_fail_on_clean_verdict_escalates(self) -> None:
        # G6 (Codex P2): the judge substep fails on semantic_review.decision=="fail" even when
        # the mechanical per_test is clean (failure_class stays "pass"). classify_validate_judge
        # would treat failure_class=="pass" as `advance`, silently dropping the finding; the
        # classify_failure judge branch must route it to the diagnostician (escalate) instead.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="O", backend="claude", env={})
            refs = self._refs()
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "verdict.json").write_text(json.dumps(
                {"per_test": [{"test_id": "t1", "status": "pass"}], "failure_class": "pass"}),
                encoding="utf-8")
            (rn / "semantic_review.json").write_text(json.dumps(
                {"decision": "fail",
                 "findings": [{"attribution": "code", "description": "fabrication suspected"}]}),
                encoding="utf-8")
            # judge is the failed substep (index 2): [pre_judge(pass), execute(pass), judge(fail)].
            judge_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                          wc.SubstepOutcome("ex", "pass", [], 0),
                          wc.SubstepOutcome("jd", "fail", [], 0)]
            decision = c.classify_failure(refs, "validate", judge_fail)
            self.assertEqual(decision.action, "escalate")
            self.assertEqual(decision.reason, "judge_semantic_review_fail")
            # a genuine physics failure_class still routes via the decision table (not escalate).
            (rn / "verdict.json").write_text(json.dumps(
                {"per_test": [{"test_id": "t1", "status": "fail"}],
                 "failure_class": "physics_fail"}), encoding="utf-8")
            decision2 = c.classify_failure(refs, "validate", judge_fail)
            self.assertEqual((decision2.action, decision2.target_phase), ("retry", "generate"))

    def test_recurring_execute_failure_escalates_to_compile(self) -> None:
        # C2 backstop: a first execute failure (no verdict.json) routes to Generate
        # restart; a second consecutive one on the same node escalates to a Compile
        # reopen (the IR is the likely wrong side once a Generate restart did not fix it).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="O", backend="claude", env={})
            refs = self._refs()
            # execute is the failed substep (index 1): [pre_judge(pass), execute(fail)].
            ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                       wc.SubstepOutcome("ex", "fail", [], 0)]
            first = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual((first.action, first.target_phase), ("retry", "generate"))
            second = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual((second.action, second.target_phase), ("reopen", "compile"))
            self.assertEqual(second.reason, "validate_execute_fail_ir")
            # After escalating to Compile the counter resets: the Compile reopen
            # regenerates the IR, so the next execute failure (fresh artifacts) gets its
            # own Generate-retry-first cycle rather than immediately re-escalating.
            third = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual((third.action, third.target_phase), ("retry", "generate"))
            fourth = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual((fourth.action, fourth.target_phase), ("reopen", "compile"))


class DeterministicLintTest(unittest.TestCase):
    """generate.lint runs in-process (no leaf): conductor authors lint_meta.json + the
    host-side lint evidence; findings are a content failure routed to generate.generate."""

    def _conductor(self, repo: Path) -> "wc.Conductor":
        return wc.Conductor(repo_root=repo, orchestration_id="t",
                            orchestration_agent_run_id="x", backend="claude", env={})

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1")

    def _seed(self, repo: Path, refs: wc.NodeRefs, language: str | None = None) -> None:
        (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        if language is not None:
            (ir_dir / "spec.ir.yaml").write_text(
                f"impl_defaults:\n  toolchain:\n    language: {language}\n", encoding="utf-8")

    def _patch_linter(self, fn):
        import sys
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore
        return mock.patch.object(build_runtime_server, "tool_run_linter", fn)

    def test_lint_inproc_pass_writes_meta_and_evidence(self) -> None:
        import tempfile
        from tools.hooks.lint_evidence import read_lint_evidence
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)  # default language fortran -> fortitude
            c = self._conductor(repo)
            with self._patch_linter(
                lambda args: {"ok": True, "return_code": 0, "command_id": "cid",
                              "preset": "fortitude"}):
                out = c._lint_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)
            meta = json.loads((repo / refs.source_dir() / "lint_meta.json").read_text())
            self.assertEqual(meta["lint_status"], "pass")
            self.assertEqual(meta["preset"], "fortitude")
            self.assertIsNone(meta["failure_category"])
            ev = read_lint_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertTrue(ev["ok"])
            self.assertEqual(ev["run_linter"][0]["command_id"], "cid")
            self.assertTrue(
                ev["run_linter"][0]["command_log_ref"].endswith("/src/command_log.jsonl"))

    def test_lint_inproc_findings_is_content_fail(self) -> None:
        import tempfile
        from tools.hooks.lint_evidence import read_lint_evidence
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_linter(
                lambda args: {"ok": False, "return_code": 1, "command_id": "cid",
                              "preset": "fortitude", "stdout": "S001 line too long"}):
                out = c._lint_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)  # content fail, not transport
            meta = json.loads((repo / refs.source_dir() / "lint_meta.json").read_text())
            self.assertEqual(meta["lint_status"], "fail")
            self.assertEqual(meta["failure_category"], "lint_findings")
            self.assertIn("S001", meta["failure_excerpt"])
            ev = read_lint_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertFalse(ev["ok"])

    def test_lint_inproc_mixed_records_two_entries(self) -> None:
        import tempfile
        from tools.hooks.lint_evidence import read_lint_evidence
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs, language="mixed")
            c = self._conductor(repo)
            mixed = {
                "ok": True, "preset": "mixed",
                "runs": [
                    {"sub_preset": "fortitude", "ok": True, "command_id": "f1"},
                    {"sub_preset": "cppcheck", "ok": True, "command_id": "c1"},
                ],
            }
            with self._patch_linter(lambda args: mixed):
                c._lint_inproc(refs, "child-1", "captok")
            ev = read_lint_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertEqual(ev["preset"], "mixed")
            self.assertEqual({e["preset"] for e in ev["run_linter"]}, {"fortitude", "cppcheck"})

    def test_lint_inproc_unknown_language_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs, language="brainfuck")
            c = self._conductor(repo)
            with self.assertRaises(RuntimeError):
                c._lint_inproc(refs, "child-1", "captok")

    def test_determine_substep_status_lint_branch(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            (repo / refs.source_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            meta_path = repo / refs.source_dir() / "lint_meta.json"
            paths = [refs.source_dir() + "/lint_meta.json"]
            meta_path.write_text(json.dumps({"lint_status": "pass"}), encoding="utf-8")
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "lint", paths)[0], "pass")
            meta_path.write_text(json.dumps({"lint_status": "fail"}), encoding="utf-8")
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "lint", paths)[0], "fail")

    def test_classify_failure_routes_lint_findings_to_generate_reuse(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            (repo / refs.source_dir()).mkdir(parents=True, exist_ok=True)
            (repo / refs.source_dir() / "lint_meta.json").write_text(
                json.dumps({"failure_category": "lint_findings"}), encoding="utf-8")
            c = self._conductor(repo)
            # outcomes models generate.generate(pass), generate.lint(fail) — lint is index 1.
            outcomes = [wc.SubstepOutcome("g", "pass", [], 0),
                        wc.SubstepOutcome("l", "fail", [], 0)]
            d = c.classify_failure(refs, "generate", outcomes)
            self.assertEqual((d.action, d.target_phase, d.repair_strategy), ("retry", "generate", "reuse"))
            self.assertTrue(d.reason.startswith("lint_"))


class DeterministicStaticTest(unittest.TestCase):
    """generate.static runs in-process (no leaf): the conductor runs validate_workspace_root +
    validate_pipeline_semantics --stage post_generate and authors static_meta.json; a violation
    is a content failure routed to generate.generate (warm resume)."""

    def _conductor(self, repo: Path) -> "wc.Conductor":
        return wc.Conductor(repo_root=repo, orchestration_id="t",
                            orchestration_agent_run_id="x", backend="claude", env={})

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1")

    def _seed(self, repo: Path, refs: wc.NodeRefs) -> None:
        (repo / refs.source_dir()).mkdir(parents=True, exist_ok=True)

    def _patch_run(self, fn):
        from unittest import mock
        return mock.patch.object(wc.subprocess, "run", fn)

    @staticmethod
    def _fake_run(ws_rc: int, pg_rc: int):
        def run(cmd, **kwargs):
            script = next((c for c in cmd if c.endswith(".py")), "")
            if script.endswith("validate_workspace_root.py"):
                return wc.subprocess.CompletedProcess(cmd, ws_rc, "ws-out", "ws-err")
            if script.endswith("validate_pipeline_semantics.py"):
                return wc.subprocess.CompletedProcess(cmd, pg_rc, "pg-out", "pg-err")
            raise AssertionError(f"unexpected subprocess: {cmd}")
        return run

    def test_static_inproc_pass_writes_meta(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_run(self._fake_run(0, 0)):
                out = c._static_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)
            meta = json.loads((repo / refs.source_dir() / "static_meta.json").read_text())
            self.assertEqual(meta["status"], "pass")
            self.assertIsNone(meta["failure_category"])

    def test_static_inproc_post_generate_violation_is_content_fail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_run(self._fake_run(0, 1)):
                out = c._static_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)  # content fail, not transport
            meta = json.loads((repo / refs.source_dir() / "static_meta.json").read_text())
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "post_generate_violation")
            self.assertIn("pg-out", meta["failure_excerpt"])

    def test_static_inproc_workspace_root_violation_short_circuits(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            # workspace_root fails first; post_generate must NOT run (pg_rc would also fail,
            # but the category proves the short-circuit picked workspace_root).
            with self._patch_run(self._fake_run(1, 1)):
                out = c._static_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)
            meta = json.loads((repo / refs.source_dir() / "static_meta.json").read_text())
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "workspace_root_violation")

    def test_static_inproc_exception_is_transport_fail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def boom(cmd, **kwargs):
                raise OSError("python3 not found")

            # Routed through _run_deterministic_substep, an unexpected error becomes a
            # transport failure (rc != 0), NOT a content failure.
            request = {"step": "generate", "substep": "static"}
            with self._patch_run(boom), \
                    __import__("unittest").mock.patch.object(
                        c, "_capability_token", lambda arid: "captok"):
                proc = c._run_deterministic_substep(refs, "generate", "static", "child-1", request)
            self.assertNotEqual(proc.returncode, 0)

    def test_determine_substep_status_static_branch(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            (repo / refs.source_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            meta_path = repo / refs.source_dir() / "static_meta.json"
            paths = [refs.source_dir() + "/static_meta.json"]
            meta_path.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "static", paths)[0], "pass")
            meta_path.write_text(json.dumps({"status": "fail"}), encoding="utf-8")
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "static", paths)[0], "fail")

    def test_classify_failure_routes_static_violation_to_generate_reuse(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            (repo / refs.source_dir()).mkdir(parents=True, exist_ok=True)
            (repo / refs.source_dir() / "static_meta.json").write_text(
                json.dumps({"failure_category": "post_generate_violation"}), encoding="utf-8")
            c = self._conductor(repo)
            # outcomes models generate(pass), lint(pass), static(fail) — static is index 2.
            outcomes = [wc.SubstepOutcome("g", "pass", [], 0),
                        wc.SubstepOutcome("l", "pass", [], 0),
                        wc.SubstepOutcome("s", "fail", [], 0)]
            d = c.classify_failure(refs, "generate", outcomes)
            self.assertEqual((d.action, d.target_phase, d.repair_strategy), ("retry", "generate", "reuse"))
            self.assertTrue(d.reason.startswith("static_"))

    def test_generate_verify_requires_fresh_source_meta_scoped(self) -> None:
        # generate.verify (pure semantic pass post-G1) must RE-AUTHOR source_meta.json this
        # attempt to pass; a no-op verify reading a stale verification_status=pass from
        # generate.generate must NOT pass. The freshness gate is scoped to source_meta.json ONLY:
        # generate.verify's allowed_output_paths also lists the producer sources (model/runner.f90)
        # it does not rewrite, so a STALE source must NOT cause a false-fail when source_meta is fresh.
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            src = repo / refs.source_dir()
            (src / "src").mkdir(parents=True, exist_ok=True)
            model = src / "src" / f"{refs.spec_id}_model.f90"
            model.write_text("module m\nend module\n", encoding="utf-8")
            # Make the producer source OLD so it would fail a whole-set freshness check.
            os.utime(model, (1_000.0, 1_000.0))
            meta_path = src / "source_meta.json"
            meta_path.write_text(json.dumps({"verification_status": "pass"}), encoding="utf-8")
            c = self._conductor(repo)
            allowed = [f"{refs.source_dir()}/src/{refs.spec_id}_model.f90",
                       f"{refs.source_dir()}/source_meta.json"]
            mtime = meta_path.stat().st_mtime
            # Fresh source_meta + STALE source -> pass (gate scoped to source_meta, ignores source).
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "verify", allowed,
                                           min_mtime=mtime - 100)[0], "pass")
            # Stale source_meta (no-op verify) -> fail.
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "verify", allowed,
                                           min_mtime=mtime + 100)[0], "fail")


class DeterministicCompileStaticTest(unittest.TestCase):
    """compile.static runs in-process (no leaf): the conductor runs validate_workspace_root +
    check_artifact_syntax + validate_pipeline_semantics --stage compile and authors
    compile_static_meta.json under the IR dir; a violation is a content failure routed to
    compile.generate (warm resume)."""

    def _conductor(self, repo: Path) -> "wc.Conductor":
        return wc.Conductor(repo_root=repo, orchestration_id="t",
                            orchestration_agent_run_id="x", backend="claude", env={})

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1")

    def _seed(self, repo: Path, refs: wc.NodeRefs) -> None:
        (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)

    def _patch_run(self, fn):
        from unittest import mock
        return mock.patch.object(wc.subprocess, "run", fn)

    @staticmethod
    def _fake_run(ws_rc: int, syntax_rc: int, compile_rc: int):
        def run(cmd, **kwargs):
            script = next((c for c in cmd if c.endswith(".py")), "")
            if script.endswith("validate_workspace_root.py"):
                return wc.subprocess.CompletedProcess(cmd, ws_rc, "ws-out", "ws-err")
            if script.endswith("check_artifact_syntax.py"):
                return wc.subprocess.CompletedProcess(cmd, syntax_rc, "syn-out", "syn-err")
            if script.endswith("validate_pipeline_semantics.py"):
                return wc.subprocess.CompletedProcess(cmd, compile_rc, "cmp-out", "cmp-err")
            raise AssertionError(f"unexpected subprocess: {cmd}")
        return run

    def _meta(self, repo: Path, refs: wc.NodeRefs) -> dict:
        return json.loads((repo / refs.ir_ref / "compile_static_meta.json").read_text())

    def test_compile_static_inproc_pass_writes_meta(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_run(self._fake_run(0, 0, 0)):
                out = c._compile_static_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)
            meta = self._meta(repo, refs)
            self.assertEqual(meta["status"], "pass")
            self.assertIsNone(meta["failure_category"])

    def test_compile_static_inproc_compile_stage_violation_is_content_fail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_run(self._fake_run(0, 0, 1)):
                out = c._compile_static_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)  # content fail, not transport
            meta = self._meta(repo, refs)
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "compile_static_violation")
            self.assertIn("cmp-out", meta["failure_excerpt"])

    def test_compile_static_inproc_workspace_root_short_circuits(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            # workspace_root fails first; syntax + --stage compile must NOT run.
            with self._patch_run(self._fake_run(1, 1, 1)):
                out = c._compile_static_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)
            meta = self._meta(repo, refs)
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "compile_static_violation")
            self.assertIn("ws-out", meta["failure_excerpt"])

    def test_compile_static_inproc_exception_is_transport_fail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def boom(cmd, **kwargs):
                raise OSError("python3 not found")

            request = {"step": "compile", "substep": "static"}
            with self._patch_run(boom), \
                    __import__("unittest").mock.patch.object(
                        c, "_capability_token", lambda arid: "captok"):
                proc = c._run_deterministic_substep(refs, "compile", "static", "child-1", request)
            self.assertNotEqual(proc.returncode, 0)

    def test_determine_substep_status_compile_static_branch(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            meta_path = repo / refs.ir_ref / "compile_static_meta.json"
            paths = [refs.ir_ref + "/compile_static_meta.json"]
            meta_path.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
            self.assertEqual(
                c.determine_substep_status(refs, "compile", "static", paths)[0], "pass")
            meta_path.write_text(json.dumps({"status": "fail"}), encoding="utf-8")
            self.assertEqual(
                c.determine_substep_status(refs, "compile", "static", paths)[0], "fail")

    def test_classify_failure_routes_compile_static_violation_to_compile_reuse(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            (repo / refs.ir_ref / "compile_static_meta.json").write_text(
                json.dumps({"failure_category": "compile_static_violation"}), encoding="utf-8")
            c = self._conductor(repo)
            # outcomes models compile.generate(pass), compile.static(fail) — static is index 1.
            outcomes = [wc.SubstepOutcome("g", "pass", [], 0),
                        wc.SubstepOutcome("s", "fail", [], 0)]
            d = c.classify_failure(refs, "compile", outcomes)
            self.assertEqual((d.action, d.target_phase, d.repair_strategy),
                             ("retry", "compile", "reuse"))
            self.assertTrue(d.reason.startswith("compile_static_"))

    def test_compile_verify_requires_fresh_ir_meta(self) -> None:
        # compile.verify is a pure-semantic pass whose sole deliverable is ir_meta.json. It must
        # RE-AUTHOR ir_meta this attempt to pass; a no-op verify that reads a stale
        # verification_status=pass left by Compile.generate (the IR author) must NOT pass — the
        # freshness gate (mtime >= this substep's launch time) enforces "an inspect-only verify
        # that writes nothing cannot terminate pass".
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            meta_path = repo / refs.ir_ref / "ir_meta.json"
            meta_path.write_text(json.dumps({"verification_status": "pass"}), encoding="utf-8")
            paths = [refs.ir_ref + "/ir_meta.json"]
            mtime = meta_path.stat().st_mtime
            # Fresh: ir_meta was (re)authored at/after the substep launch -> pass.
            self.assertEqual(
                c.determine_substep_status(refs, "compile", "verify", paths,
                                           min_mtime=mtime - 100)[0], "pass")
            # Stale: a no-op verify did not rewrite ir_meta (its mtime predates this substep's
            # launch) -> fail, even though verification_status is still "pass".
            self.assertEqual(
                c.determine_substep_status(refs, "compile", "verify", paths,
                                           min_mtime=mtime + 100)[0], "fail")


class PostJudgeClassifierTest(unittest.TestCase):
    """G4: post_judge severity classification of free-text `--stage pre_judge` violations."""

    def test_recoverable_is_judge_authored_only(self) -> None:
        # ONLY the judge's own deliverables are warm-resume-recoverable.
        for base in ("semantic_review.json", "verdict.json", "aggregate_verdict.json",
                     "summary.json", "validate_meta.json"):
            self.assertEqual(
                wc.classify_post_judge_violations(
                    [f"workspace/pipelines/x/runs/r/n/{base}: review_method must be llm_semantic_review"]),
                "recoverable", base)
        # Execute-authored evidence is NOT judge-fixable -> unknown (fail_closed), no wasted
        # warm-resume: the judge re-run cannot rewrite diagnostics/perf/trial_meta.
        for base in ("perf.json", "diagnostics.json", "trial_meta.json"):
            self.assertEqual(
                wc.classify_post_judge_violations(
                    [f"workspace/pipelines/x/runs/r/n/{base}: some execute-evidence violation"]),
                "unknown", base)

    def test_unrecoverable_integrity(self) -> None:
        for line in (
            "workspace/orchestrations/orch_x/agent_graph.json:edges[3] child not found",
            "workspace/orchestrations/orch_x/steps/n/validate/x/step_result.json: missing",
            "workspace/pipelines/x/lineage.json: node pipelines not issued",
            "copy_based_artifact_reuse detected: workspace/...",
            "dependency DAG incomplete for x; missing node workflows ['a']",
            "workspace/orchestrations/orch_x: agent_runs.jsonl must include substep role",
        ):
            self.assertEqual(wc.classify_post_judge_violations([line]), "unrecoverable", line)

    def test_unknown_and_precedence(self) -> None:
        self.assertEqual(wc.classify_post_judge_violations([]), "unknown")
        self.assertEqual(wc.classify_post_judge_violations(["something/weird.txt: huh"]), "unknown")
        # Precedence: any unrecoverable dominates a recoverable in the same batch.
        self.assertEqual(
            wc.classify_post_judge_violations([
                "workspace/runs/n/semantic_review.json: review_method must be llm_semantic_review",
                "workspace/orchestrations/o/agent_graph.json: dangling edge",
            ]), "unrecoverable")
        # A single unknown forces escalation over a recoverable (no optimistic warm resume).
        self.assertEqual(
            wc.classify_post_judge_violations([
                "workspace/runs/n/semantic_review.json: review_method must be llm_semantic_review",
                "something/weird.txt: huh",
            ]), "unknown")


class G3JudgeGateSubstepTest(unittest.TestCase):
    """G3/G4: the `--stage pre_judge` gate is two deterministic substeps wrapping the judge —
    pre_judge (pre-spawn DAG readiness) authoring pre_judge_meta.json and post_judge (the gate +
    severity classifier) authoring post_judge_meta.json."""

    def _conductor(self, repo: Path) -> "wc.Conductor":
        return wc.Conductor(repo_root=repo, orchestration_id="t",
                            orchestration_agent_run_id="x", backend="claude", env={})

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
            run_id="run_1")

    def _patch_run(self, fn):
        from unittest import mock
        return mock.patch.object(wc.subprocess, "run", fn)

    def _seed_judge(self, repo: Path, refs: wc.NodeRefs, *, per_test: list,
                    decision: str) -> None:
        """G6: seed the judge's OWN two deliverables (verdict.json#per_test +
        semantic_review.json#decision) — aggregate_verdict.json no longer exists at
        judge-completion (post_judge authors it)."""
        rn = repo / refs.run_node_dir()
        rn.mkdir(parents=True, exist_ok=True)
        (rn / "verdict.json").write_text(
            json.dumps({"per_test": per_test}), encoding="utf-8")
        (rn / "semantic_review.json").write_text(
            json.dumps({"decision": decision}), encoding="utf-8")

    # -- determine_substep_status: judge (verdict+semantic_review), pre/post_judge (meta) --
    def test_determine_judge_passes_on_verdict_alone(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            # G6: pass iff per_test non-empty with no `fail` AND decision == pass.
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "pass"}], decision="pass")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "pass")
            # all-xfail node still passes ("no fail" admits it)
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "xfail"}], decision="pass")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "pass")
            # a per_test fail -> judge fail
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "fail"}], decision="pass")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "fail")
            # a semantic_review fail on clean per_test -> judge fail
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "pass"}], decision="fail")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "fail")
            # empty per_test -> judge fail
            self._seed_judge(repo, refs, per_test=[], decision="pass")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "fail")

    # -- G6: _author_derived_validate_artifacts (conductor-authored aggregate/summary/meta) --
    def _seed_verdict(self, repo: Path, refs: wc.NodeRefs, per_test: list,
                      *, failure_class="pass", quality_check: dict | None = None) -> None:
        rn = repo / refs.run_node_dir()
        rn.mkdir(parents=True, exist_ok=True)
        (rn / "verdict.json").write_text(
            json.dumps({"per_test": per_test, "failure_class": failure_class}),
            encoding="utf-8")
        (rn / "semantic_review.json").write_text(
            json.dumps({"decision": "pass"}), encoding="utf-8")
        if quality_check is not None:
            (rn / "quality_check.json").write_text(
                json.dumps(quality_check), encoding="utf-8")

    def test_author_derived_single_node(self) -> None:
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            self._seed_verdict(
                repo, refs,
                [{"test_id": "t1", "status": "pass"}, {"test_id": "t2", "status": "xfail"}],
                quality_check={"target_class": "cpu", "status": "pass",
                               "checks": {"diagnostics_match": True, "verdict_match": True}})
            with mock.patch("tools.orchestration_runtime._resolve_dependency_facts",
                            return_value=[]):
                c._author_derived_validate_artifacts(refs)
            rn = repo / refs.run_node_dir()
            agg = json.loads((rn / "aggregate_verdict.json").read_text())
            self.assertEqual(agg["aggregate_verdict"], "pass")
            self.assertEqual(agg["self_verdict"], "pass")
            self.assertFalse(agg["blocked"])
            self.assertEqual(agg["dependency_nodes"], [])
            summary = json.loads((rn / "summary.json").read_text())
            # counts equal the verdict.per_test aggregate (what the gate cross-checks)
            self.assertEqual(summary["counts"],
                             {"pass": 1, "fail": 0, "xfail": 1, "skipped": 0, "blocked": 0})
            self.assertEqual(summary["self_summary"]["verdict"], "pass")
            self.assertEqual(summary["self_summary"]["total"], 2)
            self.assertEqual(summary["dependency_summary"],
                             {"total": 0, "pass": 0, "xfail": 0, "fail": 0, "blocked": 0})
            self.assertEqual(summary["quality_check"]["status"], "pass")
            vmeta = json.loads((rn / "validate_meta.json").read_text())
            self.assertEqual(vmeta["verification_status"], "pass")
            self.assertTrue(vmeta["context_isolated"])
            self.assertIsNone(vmeta["last_fail_reason"])
            self.assertTrue(vmeta["judge_command_ref"].endswith("/semantic_review.json"))

    def test_author_derived_all_xfail_self_verdict(self) -> None:
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            self._seed_verdict(
                repo, refs,
                [{"test_id": "t1", "status": "xfail"}, {"test_id": "t2", "status": "skipped"}])
            with mock.patch("tools.orchestration_runtime._resolve_dependency_facts",
                            return_value=[]):
                c._author_derived_validate_artifacts(refs)
            agg = json.loads((repo / refs.run_node_dir() / "aggregate_verdict.json").read_text())
            # every non-skipped entry is xfail -> self_verdict xfail (no fail admits it)
            self.assertEqual(agg["self_verdict"], "xfail")
            self.assertEqual(agg["aggregate_verdict"], "xfail")

    def _seed_ir_with_dep(self, repo: Path, refs: wc.NodeRefs, dep_node_key: str) -> None:
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "spec.ir.yaml").write_text(
            json.dumps({"dependency": {"direct_deps": [{"node_key": dep_node_key}],
                                       "all_nodes": [dep_node_key, refs.node_key]}}),
            encoding="utf-8")

    def test_author_derived_deps_present(self) -> None:
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            self._seed_verdict(repo, refs, [{"test_id": "t1", "status": "pass"}])
            self._seed_ir_with_dep(repo, refs, "component/dep@0.1.0")
            # display verdict comes from the resolved fact; blocking uses the readiness predicate.
            dep_agg = repo / "dep_agg.json"
            dep_agg.write_text(json.dumps({"aggregate_verdict": "pass"}), encoding="utf-8")
            fact = {"node_key": "component/dep@0.1.0", "pipeline_ref": "workspace/pipelines/dep",
                    "run_id": "run_dep_001", "aggregate_verdict_ref": "dep_agg.json"}
            with mock.patch("tools.orchestration_runtime._resolve_dependency_facts",
                            return_value=[fact]), \
                 mock.patch("tools.validate_pipeline_semantics._closure_node_validated_in_own_pipeline",
                            return_value=True):
                c._author_derived_validate_artifacts(refs)
            rn = repo / refs.run_node_dir()
            agg = json.loads((rn / "aggregate_verdict.json").read_text())
            self.assertFalse(agg["blocked"])
            self.assertEqual(agg["aggregate_verdict"], "pass")
            self.assertEqual(agg["dependency_nodes"][0]["node_key"], "component/dep@0.1.0")
            self.assertEqual(agg["dependency_nodes"][0]["aggregate_verdict"], "pass")
            self.assertTrue(agg["dependency_nodes"][0]["ready"])
            summary = json.loads((rn / "summary.json").read_text())
            self.assertEqual(summary["dependency_summary"]["total"], 1)
            self.assertEqual(summary["dependency_summary"]["pass"], 1)

    def test_author_derived_blocked_case(self) -> None:
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            self._seed_verdict(repo, refs, [{"test_id": "t1", "status": "pass"}])
            self._seed_ir_with_dep(repo, refs, "component/dep@0.1.0")
            fact = {"node_key": "component/dep@0.1.0", "pipeline_ref": "workspace/pipelines/dep",
                    "run_id": "run_dep_001", "aggregate_verdict_ref": None}
            # a direct dep that is NOT validated in its own pipeline (readiness predicate=False)
            # -> node blocked, regardless of any latest-verdict display value.
            with mock.patch("tools.orchestration_runtime._resolve_dependency_facts",
                            return_value=[fact]), \
                 mock.patch("tools.validate_pipeline_semantics._closure_node_validated_in_own_pipeline",
                            return_value=False):
                c._author_derived_validate_artifacts(refs)
            rn = repo / refs.run_node_dir()
            agg = json.loads((rn / "aggregate_verdict.json").read_text())
            self.assertTrue(agg["blocked"])
            self.assertEqual(agg["aggregate_verdict"], "blocked")
            self.assertEqual(agg["blocking_direct_deps"], ["component/dep@0.1.0"])
            self.assertFalse(agg["dependency_nodes"][0]["ready"])
            summary = json.loads((rn / "summary.json").read_text())
            self.assertEqual(summary["dependency_summary"]["blocked"], 1)

    def test_author_derived_regressed_dep_not_blocked(self) -> None:
        """G6 review finding #3: a dep whose LATEST verdict is `fail` but that is still
        validated-in-own-pipeline (an older bound pass) must NOT block — the blocking
        decision uses the same readiness predicate pre_judge/the gate use, so the derived
        aggregate can never contradict a readiness gate that already passed."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            self._seed_verdict(repo, refs, [{"test_id": "t1", "status": "pass"}])
            self._seed_ir_with_dep(repo, refs, "component/dep@0.1.0")
            dep_agg = repo / "dep_agg.json"
            dep_agg.write_text(json.dumps({"aggregate_verdict": "fail"}), encoding="utf-8")
            fact = {"node_key": "component/dep@0.1.0", "pipeline_ref": "workspace/pipelines/dep",
                    "run_id": "run_dep_001", "aggregate_verdict_ref": "dep_agg.json"}
            with mock.patch("tools.orchestration_runtime._resolve_dependency_facts",
                            return_value=[fact]), \
                 mock.patch("tools.validate_pipeline_semantics._closure_node_validated_in_own_pipeline",
                            return_value=True):
                c._author_derived_validate_artifacts(refs)
            agg = json.loads((repo / refs.run_node_dir() / "aggregate_verdict.json").read_text())
            self.assertFalse(agg["blocked"])
            # aggregate stays pass (ready dep folds pass; its regressed latest verdict is
            # display-only), never contradicting the pass phase.
            self.assertEqual(agg["aggregate_verdict"], "pass")
            self.assertEqual(agg["dependency_nodes"][0]["aggregate_verdict"], "fail")
            self.assertTrue(agg["dependency_nodes"][0]["ready"])

    def test_author_derived_blocked_per_test_not_certifiable(self) -> None:
        """G6 review finding #2: an all-`blocked` per_test must not certify as pass."""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            self._seed_verdict(repo, refs, [{"test_id": "t1", "status": "blocked"}])
            with mock.patch("tools.orchestration_runtime._resolve_dependency_facts",
                            return_value=[]):
                c._author_derived_validate_artifacts(refs)
            rn = repo / refs.run_node_dir()
            agg = json.loads((rn / "aggregate_verdict.json").read_text())
            self.assertEqual(agg["self_verdict"], "fail")
            summary = json.loads((rn / "summary.json").read_text())
            self.assertEqual(summary["counts"]["blocked"], 1)
            # judge criterion also rejects a per_test `blocked`.
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "fail")

    def test_determine_pre_and_post_judge_meta_branches(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            for sub, fname in (("pre_judge", "pre_judge_meta.json"),
                               ("post_judge", "post_judge_meta.json")):
                p = rn / fname
                paths = [refs.run_node_dir() + "/" + fname]
                p.write_text(json.dumps({"status": "pass"}), encoding="utf-8")
                mtime = p.stat().st_mtime
                self.assertEqual(
                    c.determine_substep_status(refs, "validate", sub, paths,
                                               min_mtime=mtime - 100)[0], "pass")
                p.write_text(json.dumps({"status": "fail"}), encoding="utf-8")
                self.assertEqual(
                    c.determine_substep_status(refs, "validate", sub, paths,
                                               min_mtime=p.stat().st_mtime - 100)[0], "fail")

    # -- _pre_judge_inproc -----------------------------------------------------
    def test_pre_judge_inproc_pass_and_fail(self) -> None:
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            (repo / refs.run_node_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            # empty closure -> pass
            with mock.patch.object(c, "_judge_pre_spawn_dag_block", lambda r: None):
                out = c._pre_judge_inproc(refs, "child-pj", "captok")
            self.assertEqual(out["returncode"], 0)
            meta = json.loads((repo / refs.run_node_dir() / "pre_judge_meta.json").read_text())
            self.assertEqual(meta["status"], "pass")
            # incomplete closure -> fail with dag category
            with mock.patch.object(c, "_judge_pre_spawn_dag_block",
                                   lambda r: "missing ['component/dep']"):
                out = c._pre_judge_inproc(refs, "child-pj", "captok")
            self.assertEqual(out["returncode"], 0)  # content failure, not transport
            meta = json.loads((repo / refs.run_node_dir() / "pre_judge_meta.json").read_text())
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "pre_judge_dag_incomplete")

    # -- _post_judge_inproc (subprocess -> post_judge_meta + disposition) ------
    def test_post_judge_pass_writes_pass_meta(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            (repo / refs.run_node_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            with self._patch_run(lambda cmd, **k: wc.subprocess.CompletedProcess(cmd, 0, "", "")):
                out = c._post_judge_inproc(refs, "child-post", "captok")
            self.assertEqual(out["returncode"], 0)
            meta = json.loads((repo / refs.run_node_dir() / "post_judge_meta.json").read_text())
            self.assertEqual(meta["status"], "pass")
            self.assertIsNone(meta["disposition"])

    def test_post_judge_recoverable_violation_sets_warm_resume(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            (repo / refs.run_node_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            out = ("pipeline semantic validation: FAIL\n"
                   "- workspace/runs/n/semantic_review.json: review_method must be "
                   "llm_semantic_review\n")
            with self._patch_run(lambda cmd, **k: wc.subprocess.CompletedProcess(cmd, 1, out, "")):
                res = c._post_judge_inproc(refs, "child-post", "captok")
            self.assertEqual(res["returncode"], 0)  # content failure, not transport
            meta = json.loads((repo / refs.run_node_dir() / "post_judge_meta.json").read_text())
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "pre_judge_violation")
            self.assertEqual(meta["disposition"], "warm_resume")
            self.assertTrue(any("review_method" in v for v in meta["violations"]))

    def test_post_judge_unrecoverable_violation_sets_fail_closed(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            (repo / refs.run_node_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            out = ("pipeline semantic validation: FAIL\n"
                   "- workspace/orchestrations/o/agent_graph.json:edges[1] child not found\n")
            with self._patch_run(lambda cmd, **k: wc.subprocess.CompletedProcess(cmd, 1, out, "")):
                c._post_judge_inproc(refs, "child-post", "captok")
            meta = json.loads((repo / refs.run_node_dir() / "post_judge_meta.json").read_text())
            self.assertEqual(meta["disposition"], "fail_closed")

    def test_post_judge_subprocess_launch_failure_records_gate_error(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            (repo / refs.run_node_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)

            def boom(cmd, **k):
                raise OSError("python3 not found")

            with self._patch_run(boom):
                res = c._post_judge_inproc(refs, "child-post", "captok")
            self.assertEqual(res["returncode"], 0)
            meta = json.loads((repo / refs.run_node_dir() / "post_judge_meta.json").read_text())
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "post_judge_gate_error")
            self.assertEqual(meta["disposition"], "fail_closed")

    def test_post_judge_emits_scoped_args_with_both_in_flight(self) -> None:
        # post_judge runs --stage pre_judge scoped to its own run, declaring BOTH the judge and
        # the post_judge (self) arids in-flight (post_judge's own graph edge is dangling).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            (repo / refs.run_node_dir()).mkdir(parents=True, exist_ok=True)
            c = self._conductor(repo)
            c._pending_judge_arid = {refs.node_key: "child-judge"}
            seen = {}

            def run(cmd, **k):
                seen["cmd"] = cmd
                return wc.subprocess.CompletedProcess(cmd, 0, "", "")

            with self._patch_run(run):
                c._post_judge_inproc(refs, "child-post", "captok")
            cmd = seen["cmd"]
            self.assertEqual(cmd[cmd.index("--stage") + 1], "pre_judge")
            self.assertEqual(cmd[cmd.index("--pipeline-root") + 1], refs.pipeline_ref)
            self.assertEqual(cmd[cmd.index("--run-id") + 1], "run_1")
            inflight = [cmd[i + 1] for i, t in enumerate(cmd) if t == "--in-flight-agent-run-id"]
            self.assertIn("child-post", inflight)
            self.assertIn("child-judge", inflight)

    # -- _judge_pre_spawn_dag_block (multi-node fail / single-node skip) -------
    def _patch_closure_validated(self, fn):
        from unittest import mock
        import tools.validate_pipeline_semantics as vps
        return mock.patch.object(vps, "_closure_node_validated_in_own_pipeline", fn)

    def _seed_ir_closure(self, repo: Path, refs: wc.NodeRefs, closure: list[str]) -> None:
        # Seed the conductor-authored sidecar dependency_graph.json with all_nodes = self +
        # closure (the SAME source the post-gate pre_judge DAG check reads now — the derived
        # graph moved out of spec.ir.yaml).
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        all_nodes = [{"node_key": refs.node_key}] + [{"node_key": nk} for nk in closure]
        (ir_dir / "dependency_graph.json").write_text(
            json.dumps({"node_key": refs.node_key, "all_nodes": all_nodes,
                        "transitive_deps": [], "generated_by": "conductor"}),
            encoding="utf-8")

    def test_pre_spawn_single_node_skips(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            self._seed_ir_closure(repo, refs, [])  # only self in all_nodes
            c = self._conductor(repo)
            # Empty closure -> None without ever consulting the (unpatched) validator.
            self.assertIsNone(c._judge_pre_spawn_dag_block(refs))

    def test_pre_spawn_absent_ir_skips(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # No spec.ir.yaml at all (defensive) -> empty dep -> None.
            c = self._conductor(Path(td))
            self.assertIsNone(c._judge_pre_spawn_dag_block(self._refs()))

    def test_pre_spawn_missing_sidecar_falls_back_to_ir_direct_deps(self) -> None:
        # No dependency_graph.json sidecar (resumed pre-sidecar / corrupt), but the IR still
        # declares direct_deps: the pre-spawn gate must fall back to the IR block and BLOCK on
        # the unbuilt dep rather than wave the run through with an empty closure.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(
                json.dumps({"dependency": {
                    "node_key": refs.node_key,
                    "direct_deps": [{"node_key": "component/base@0.1.0"}]}}),
                encoding="utf-8")  # NOTE: no dependency_graph.json authored
            c = self._conductor(repo)
            with self._patch_closure_validated(lambda repo_root, tok: False):
                block = c._judge_pre_spawn_dag_block(refs)
            self.assertIsInstance(block, str)
            self.assertIn("component/base", block)

    def test_pre_spawn_multi_node_all_ready_proceeds(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            self._seed_ir_closure(repo, refs, ["component/base@0.1.0", "component/mid@0.2.0"])
            c = self._conductor(repo)
            with self._patch_closure_validated(lambda repo_root, tok: True):
                self.assertIsNone(c._judge_pre_spawn_dag_block(refs))

    def test_pre_spawn_multi_node_incomplete_blocks(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            self._seed_ir_closure(repo, refs, ["component/base@0.1.0", "component/mid@0.2.0"])
            c = self._conductor(repo)
            # base ready, mid not -> block, and the excerpt names the missing normalized token.
            with self._patch_closure_validated(
                    lambda repo_root, tok: tok == "component/base"):
                block = c._judge_pre_spawn_dag_block(refs)
            self.assertIsInstance(block, str)
            self.assertIn("component/mid", block)
            self.assertNotIn("component/base", block)


class ExecutePromoterTest(unittest.TestCase):
    """WS-B: artifact-type-driven evidence promotion + metadata authoring."""

    def _conductor(self, repo: Path) -> wc.Conductor:
        return wc.Conductor(repo_root=repo, orchestration_id="t",
                            orchestration_agent_run_id="x", backend="claude", env={})

    def _write(self, p: Path, obj) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj), encoding="utf-8")

    def test_execute_allowed_paths_are_evidence_artifact_driven(self) -> None:
        refs = wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_20260101_001", pipeline_id="x_20260101_001",
            source_id="src_20260101_001", binary_id="bin_20260101_001",
            run_id="run_20260101_001", source_binary_id="bin_20260101_001")
        common = dict(step="validate", substep="execute", orchestration_id="o",
                      orchestration_agent_run_id="p", child_agent_run_id="c",
                      agent_model="m", workflow_mode="dev", case_ids=("a", "b"))
        snap = wc.build_launch_request(refs, evidence_artifacts=("state_snapshots",), **common)
        snap_outs = snap["allowed_output_paths"]
        self.assertTrue(any("/raw/state_snapshots/a.json" in p for p in snap_outs))
        self.assertTrue(any("snapshot_schema.json" in p for p in snap_outs))
        self.assertFalse(any("execution_trace.json" in p for p in snap_outs))

        trace = wc.build_launch_request(
            refs, evidence_artifacts=("execution_trace.json",), **common)
        trace_outs = trace["allowed_output_paths"]
        self.assertTrue(any(p.endswith("/raw/execution_trace.json") for p in trace_outs))
        self.assertFalse(any("/raw/state_snapshots/" in p for p in trace_outs))

    def test_required_evidence_artifacts(self) -> None:
        c = self._conductor(Path("/tmp/repo"))
        ir = {"io_contract": {"raw_requirements": {"required_evidence": [
            {"artifact": "state_snapshots", "required": True},
            {"artifact": "metrics_basis.json", "required": False},
        ]}}}
        self.assertEqual(c._required_evidence_artifacts(ir), ["state_snapshots"])

    def test_promote_state_snapshots(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor(repo)
            run = repo / "run"
            self._write(run / "diagnostics.json", {"verdict": {"overall": "pass"}})
            self._write(run / "perf.json", {"case_id": "a"})
            self._write(run / "raw" / "metrics_basis.json", {"x": 1})
            self._write(run / "raw" / "state_snapshots" / "caseA.json", {"u": [1]})
            self._write(run / "raw" / "state_snapshots" / "caseB.json", {"u": [2]})
            node = repo / "node"
            refs = c._promote_run_evidence(run, node, ["state_snapshots"])
            self.assertTrue((node / "diagnostics.json").exists())
            self.assertTrue((node / "perf.json").exists())
            self.assertTrue((node / "raw" / "metrics_basis.json").exists())
            self.assertTrue((node / "raw" / "state_snapshots" / "caseA.json").exists())
            self.assertTrue((node / "raw" / "state_snapshots" / "caseB.json").exists())
            self.assertIn("node/raw/metrics_basis.json", refs)

    def test_promote_execution_trace_drops_per_case_aux(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor(repo)
            run = repo / "run"
            self._write(run / "diagnostics.json", {"verdict": {"overall": "pass"}})
            self._write(run / "perf.json", {"case_id": "a"})
            self._write(run / "raw" / "execution_trace.json", {"trace": []})
            # auxiliary per-case files the runner also emits -> must be DROPPED
            self._write(run / "raw" / "execution_trace_caseA.json", {"trace": ["a"]})
            node = repo / "node"
            c._promote_run_evidence(run, node, ["execution_trace.json"])
            self.assertTrue((node / "raw" / "execution_trace.json").exists())
            self.assertFalse((node / "raw" / "execution_trace_caseA.json").exists())

    def test_author_snapshot_schema_orders_by_ir_case(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor(repo)
            node = repo / "node"
            sdir = node / "raw" / "state_snapshots"
            sdir.mkdir(parents=True)
            for cid in ("left", "right", "invalid"):
                (sdir / f"{cid}.json").write_text("{}", encoding="utf-8")
            ir = {
                "io_contract": {"raw_requirements": {"required_evidence": [
                    {"artifact": "state_snapshots", "required": True, "min_samples": 1,
                     "schema": {"variables": [{"name": "u", "shape_expr": "[n]"}],
                                "time_variable": "t", "time_shape_expr": "scalar"}},
                ]}},
                "case": {"test_case_set": [
                    {"case_id": "left"}, {"case_id": "right"}, {"case_id": "invalid"}]},
            }
            ref = c._author_snapshot_schema(ir, node)
            self.assertIsNotNone(ref)
            doc = json.loads((sdir / "snapshot_schema.json").read_text())
            self.assertEqual(doc["samples"], ["left.json", "right.json", "invalid.json"])
            self.assertEqual(doc["time_variable"], "t")
            self.assertEqual(doc["min_samples"], 1)

    def test_author_quality_check_pass_and_mismatch(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            node = Path(td)
            run_diag = {"checks": {"c1": {"status": "pass"}},
                        "verdict": {"overall": "pass", "failed_checks": []},
                        "cases": [{"case_id": "a", "verdict": {"overall": "pass"}}]}
            c = self._conductor(node)
            status = c._author_quality_check(node, run_diag, run_diag, "R", "Q", "make_test", 1)
            self.assertEqual(status, "pass")
            doc = json.loads((node / "quality_check.json").read_text())
            self.assertTrue(all(doc["checks"].values()))
            # mismatched verdict -> fail
            qc_diag = {"checks": {"c1": {"status": "fail"}},
                       "verdict": {"overall": "fail", "failed_checks": ["c1"]},
                       "cases": [{"case_id": "a", "verdict": {"overall": "fail"}}]}
            status2 = c._author_quality_check(node, run_diag, qc_diag, "R", "Q", "make_test", 1)
            self.assertEqual(status2, "fail")


class TransportTombstoneRealCliTest(unittest.TestCase):
    """T1: integration coverage of the conductor -> REAL runtime CLI -> completion-exemption
    seam for the leaf-transport tombstone. The unit layers stub `runtime()`
    (`TransportFailureTest`) or call the runtime helper in-process
    (`test_orchestration_runtime.TransportOrphanCompletionTest`); this drives the actual
    `Conductor.runtime()` subprocess against a real `orchestration_runtime.py` and asserts the
    persisted superseded set is what the completion check consults via `_load_superseded_run_ids`.
    """

    def _repo_with_real_tools(self, tmp: str) -> Path:
        # Symlink the real tools/ into the temp repo so `runtime()` (cwd=repo_root,
        # `python3 tools/orchestration_runtime.py`) resolves the real script while all
        # orchestration state (`--repo-root .`) lives under the temp repo. The script's
        # imports resolve via the symlink target (real repo), so nothing leaks into the
        # real workspace.
        repo = Path(tmp)
        real_tools = Path(wc.__file__).resolve().parent
        os.symlink(real_tools, repo / "tools")
        return repo

    def test_add_superseded_runs_persists_via_real_cli_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_real_tools(tmp)
            oid = "orch_t1"
            (repo / "workspace" / "orchestrations" / oid).mkdir(parents=True)
            c = wc.Conductor(repo_root=repo, orchestration_id=oid,
                             orchestration_agent_run_id="ORCH", backend="claude",
                             env=os.environ.copy())
            # the conductor shells out to the REAL add-superseded-runs CLI
            c._add_superseded_run_ids(
                ["child-1", "child-2"],
                reason="leaf_transport_error_orphan: leaf_exit=1")
            # the exact reader the completion check consults sees both orphans tombstoned
            from tools.orchestration_runtime import _load_superseded_run_ids
            self.assertEqual(
                _load_superseded_run_ids(repo, oid), {"child-1", "child-2"})
            # idempotent: re-tombstoning the same ids does not duplicate/lose them
            c._add_superseded_run_ids(["child-2"], reason="leaf_transport_error_orphan: leaf_exit=1")
            self.assertEqual(
                _load_superseded_run_ids(repo, oid), {"child-1", "child-2"})


class CodexFeatureCacheTest(unittest.TestCase):
    """The conductor host-certifies the codex hooks feature into a leaf-unwritable cache
    (orchestration-dir root) before launching codex leaves, so the in-sandbox hook reads a
    value it cannot forge. No-op for claude; probed once per orchestration."""

    def _conductor(self, repo: Path, backend: str, llm_command: str = "",
                   env: dict | None = None) -> wc.Conductor:
        return wc.Conductor(repo_root=repo, orchestration_id="orch_cfc",
                            orchestration_agent_run_id="ORCH", backend=backend,
                            env=env if env is not None else {}, llm_command=llm_command)

    def test_codex_probes_once_and_writes_unwritable_cache(self) -> None:
        from unittest.mock import patch
        from tools.hooks.codex_feature import codex_feature_cache_path
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / "orch_cfc").mkdir(parents=True)
            c = self._conductor(repo, "codex")
            with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                       return_value=(True, "hooks=true")) as probe:
                c._ensure_codex_feature_cache()
                c._ensure_codex_feature_cache()  # memoized -> still one probe
            self.assertEqual(probe.call_count, 1)
            # bare backend -> probe the bare `codex` executable
            self.assertEqual(probe.call_args.kwargs["command"], ["codex"])
            path = codex_feature_cache_path(repo_root=repo, orchestration_id="orch_cfc")
            self.assertTrue(path.is_file())
            # the cache must NOT live under the leaf-writable hooks/ (or audit/) bind
            self.assertNotIn("/hooks/", str(path))
            self.assertNotIn("/audit/", str(path))
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(doc["enabled"], True)

    def test_codex_probe_uses_custom_llm_command(self) -> None:
        # A custom --llm-command wrapper must be probed verbatim (same prefix the leaf
        # runs via leaf_command), not the hardcoded `codex` — else the host certifies a
        # different executable than the leaf will use.
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / "orch_cfc").mkdir(parents=True)
            c = self._conductor(repo, "codex", llm_command="codexwrap --profile x")
            with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                       return_value=(True, "hooks=true")) as probe:
                c._ensure_codex_feature_cache()
            self.assertEqual(probe.call_args.kwargs["command"], ["codexwrap", "--profile", "x"])

    def test_codex_fails_closed_when_hooks_not_certified(self) -> None:
        # hooks=false / probe error → the leaf's hooks would not fire, so the in-sandbox
        # fail-closed read never happens. The conductor must fail closed BEFORE launch
        # (SandboxEnforcementError → conduct terminalizes as sandbox_enforcement_violation),
        # and must NOT memoize (so a retry cannot degrade into an allow).
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / "orch_cfc").mkdir(parents=True)
            c = self._conductor(repo, "codex")
            with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                       return_value=(False, "hooks=false")):
                with self.assertRaises(wc.SandboxEnforcementError):
                    c._ensure_codex_feature_cache()
            self.assertFalse(getattr(c, "_codex_feature_cache_written", False))

    def test_certification_fails_closed_before_record_launch(self) -> None:
        # The cert runs at the top of run_substep, BEFORE record_launch — so a fail-closed
        # cert never orphans a recorded launch (phantom child_running active run).
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / "orch_cfc").mkdir(parents=True)
            c = self._conductor(repo, "codex")

            def _boom_record_launch(*a, **k):
                raise AssertionError("record_launch ran before codex cert")

            c.record_launch = _boom_record_launch  # type: ignore[assignment]
            refs = wc.NodeRefs(
                node_key="component/x@0.1.0", spec_path="spec/component/x",
                ir_id="i", pipeline_id="p", source_id="s", binary_id="b",
                run_id="r", source_binary_id="b")
            with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                       return_value=(False, "hooks=false")):
                # SandboxEnforcementError (cert), NOT AssertionError (record_launch) —
                # proves the cert short-circuits before the launch is recorded.
                with self.assertRaises(wc.SandboxEnforcementError):
                    c.run_substep(refs, "compile", "generate")

    def test_codex_disabled_requirement_opt_out_does_not_fail_closed(self) -> None:
        # With METDSL_REQUIRE_CODEX_HOOKS_FEATURE=0 (same opt-out the hook honours), an
        # uncertified feature is recorded but does NOT fail closed.
        from unittest.mock import patch
        from tools.hooks.codex_feature import codex_feature_cache_path
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / "orch_cfc").mkdir(parents=True)
            c = self._conductor(repo, "codex",
                                env={"METDSL_REQUIRE_CODEX_HOOKS_FEATURE": "0"})
            with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                       return_value=(False, "hooks=false")):
                c._ensure_codex_feature_cache()  # no raise
            self.assertTrue(getattr(c, "_codex_feature_cache_written", False))
            doc = json.loads(codex_feature_cache_path(
                repo_root=repo, orchestration_id="orch_cfc").read_text(encoding="utf-8"))
            self.assertIs(doc["enabled"], False)

    def test_claude_backend_is_noop(self) -> None:
        from unittest.mock import patch
        from tools.hooks.codex_feature import codex_feature_cache_path
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "workspace" / "orchestrations" / "orch_cfc").mkdir(parents=True)
            c = self._conductor(repo, "claude")
            with patch("tools.hooks.codex_feature.codex_hooks_feature_enabled",
                       side_effect=AssertionError("claude must not probe codex")):
                c._ensure_codex_feature_cache()
            path = codex_feature_cache_path(repo_root=repo, orchestration_id="orch_cfc")
            self.assertFalse(path.is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
