"""Tests for tools/workflow_conductor.py.

The payload-builder tests validate build_launch_request() field-for-field against
real, working launches/*.request.json artifacts captured from a successful run, so
the deterministic conductor reproduces exactly what the LLM orchestration agent
assembled. The decision-table tests pin the deterministic failure routing.
"""

from __future__ import annotations

import copy
import glob
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import tools.orchestration_runtime as wc_runtime
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


def _conformant_stage_meta(status: str = "pass", **overrides: object) -> dict:
    """A stage meta (ir_meta / source_meta) that satisfies the canonical contract
    (tools/meta_contracts). Fixtures that mean to exercise some OTHER gate must start from a
    contract-clean meta, or the stage-meta gate is what fails them."""
    meta = {
        "attempt_count": 1,
        "verification_status": status,
        "last_fail_reason": None,
        "debug_mode": False,
        "context_isolated": True,
    }
    meta.update(overrides)
    return meta


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

    def test_m3d_runner_contract_narrowing_survives_record_launch(self) -> None:
        # M3d node-aware must-read: an M3c physics generate leaf (runner host-rendered)
        # drops RUNNER_OUTPUT_CONTRACT and keeps the checks ABI; a non-M3c leaf keeps
        # RUNNER. The conductor stamps `runner_host_authored` into the payload so the
        # record-launch security-boundary recompute derives the SAME set — end-to-end
        # proof (beyond the synthetic-payload drift test) that the two paths cannot drift.
        from tools.orchestration_runtime import build_skill_must_read_refs
        RUN = "docs/workflow/RUNNER_OUTPUT_CONTRACT.md"
        CHK = "docs/workflow/CHECKS_MODULE_CONTRACT.md"

        m3c = wc.build_launch_request(
            self._generate_refs(), step="generate", substep="generate",
            orchestration_id="o", orchestration_agent_run_id="p",
            child_agent_run_id="c", agent_model="m", workflow_mode="dev",
            runner_host_authored=True)
        self.assertTrue(m3c.get("runner_host_authored"))
        self.assertNotIn(RUN, m3c["skill_must_read_refs"])
        self.assertIn(CHK, m3c["skill_must_read_refs"])
        # The record-launch recompute reads runner_host_authored off the SAME payload,
        # so it must NOT re-add RUNNER (a drift would leak it back in).
        self.assertNotIn(RUN, build_skill_must_read_refs(m3c))

        legacy = wc.build_launch_request(
            self._generate_refs(), step="generate", substep="generate",
            orchestration_id="o", orchestration_agent_run_id="p",
            child_agent_run_id="c", agent_model="m", workflow_mode="dev",
            runner_host_authored=False)
        self.assertNotIn("runner_host_authored", legacy)  # non-M3c: not stamped
        self.assertIn(RUN, legacy["skill_must_read_refs"])
        self.assertIn(RUN, build_skill_must_read_refs(legacy))


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

    def test_read_repair_findings_reads_gate_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x",
                               ir_id="x_1", pipeline_id="x_1", source_id="src_1")
            meta_dir = repo / refs.source_dir()
            meta_dir.mkdir(parents=True)
            # The gate union verdict: gate_meta.json#failure_excerpt (composed per-checker
            # sections). Any `gate_*` reason reads it.
            (meta_dir / "gate_meta.json").write_text(
                json.dumps({"failure_excerpt": "[syntax]\nErr\n[lint]\nC061 argument 'u_l'"}),
                encoding="utf-8")
            c = _FakeConductor(repo_root=repo, orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertEqual(
                c._read_repair_findings(refs, "gate_syntax_error+lint_findings"),
                "[syntax]\nErr\n[lint]\nC061 argument 'u_l'")
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
            self.assertIsNone(c._read_repair_findings(refs2, "gate_post_generate_violation"))

    def test_read_repair_findings_reads_execute_trial_meta_excerpt(self) -> None:
        # B1: a structural validate.execute failure keeps its excerpt in the failed RUN's
        # trial_meta.json. Matched on the category suffix — the reasons that merely share the
        # `validate_execute_` prefix (the cold restart, the per-test predicate classes) must NOT
        # pick it up, since their repairs are cold / not a Generate repair at all.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = wc.NodeRefs(node_key="component/spec_x@0.1.0",
                               spec_path="spec/component/spec_x",
                               ir_id="x_1", pipeline_id="x_1", source_id="src_1",
                               run_id="run_1", binary_id="bin_1", source_binary_id="bin_1")
            node_dir = repo / refs.run_node_dir()
            node_dir.mkdir(parents=True)
            (node_dir / "trial_meta.json").write_text(
                json.dumps({"status": "fail", "failure_category": "post_execute_violation",
                            "failure_excerpt": "missing required_raw_variables: a1"}),
                encoding="utf-8")
            c = _FakeConductor(repo_root=repo, orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertEqual(
                c._read_repair_findings(refs, "validate_execute_post_execute_violation",
                                        "validate"),
                "missing required_raw_variables: a1")
            for reason in ("validate_execute_fail", "validate_execute_physics_fail",
                           "validate_execute_structural_violation"):
                self.assertIsNone(c._read_repair_findings(refs, reason, "validate"), reason)


class NodeRefsTest(unittest.TestCase):
    def test_safe_and_spec_id(self) -> None:
        refs = wc.NodeRefs(node_key="component/dynamics_advdiff_flux_1d_upwind_center2@0.1.0",
                           spec_path="spec/...", ir_id="a_1_1", pipeline_id="a_1_1")
        self.assertEqual(refs.safe, "component__dynamics_advdiff_flux_1d_upwind_center2__0.1.0")
        self.assertEqual(refs.spec_id, "dynamics_advdiff_flux_1d_upwind_center2")


class PhaseStructureTest(unittest.TestCase):
    def test_substeps_and_roles(self) -> None:
        self.assertEqual(wc.SUBSTEPS["compile"], ("generate", "static", "verify"))
        self.assertEqual(wc.SUBSTEPS["generate"],
                         ("generate", "gate", "verify"))
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

    def test_gate_failure_routing(self) -> None:
        # Ordinary content violations warm-retry generate.generate. Every known non-terminal
        # gate category (lint/syntax/static family) routes ("retry","generate","reuse").
        for cat in ("post_generate_violation", "workspace_root_violation",
                    "lint_findings", "syntax_error"):
            d = wc.classify_gate_failure([cat])
            self.assertEqual((d.action, d.target_phase, d.repair_strategy),
                             ("retry", "generate", "reuse"), cat)
        # A UNION of several known categories -> one warm reuse; reason lists them canonically.
        d = wc.classify_gate_failure(["lint_findings", "syntax_error"])
        self.assertEqual((d.action, d.target_phase, d.repair_strategy),
                         ("retry", "generate", "reuse"))
        self.assertEqual(d.reason, "gate_syntax_error+lint_findings")
        # A stale certified dependency IR is TERMINAL — the leaf cannot repair it, so no warm
        # retry, and it DOMINATES a co-occurring warm category (reachability note: static runs
        # only when lint+syntax passed, so this multi-category input cannot arise in practice —
        # the test defends the classifier's totality).
        d = wc.classify_gate_failure(["stale_dependency_ir"])
        self.assertEqual(d.action, "fail_closed")
        d = wc.classify_gate_failure(["lint_findings", "stale_dependency_ir"])
        self.assertEqual(d.action, "fail_closed")
        self.assertIn("stale_dependency_ir", wc.GATE_FAILURE_TERMINAL)
        # An unknown category escalates; an empty list escalates with the no-category reason.
        self.assertEqual(wc.classify_gate_failure(["mystery"]).action, "escalate")
        self.assertEqual(wc.classify_gate_failure([]).reason, "gate_fail_no_category")
        self.assertEqual(wc.classify_gate_failure(None).reason, "gate_fail_no_category")

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

    # Normalized semantic_review.json#decision for a failed judge substep. Default "fail"
    # represents a genuine physics/semantic judge fail (the routeable case: run_phase writes
    # the step_result and routes via classify_failure). A test that exercises the
    # judge-conformance guard (decision != "fail" -> skip-write + escalate/fail_closed) sets
    # this to "pass"/"" to model a malformed/inconsistent verdict atop a pass semantic_review.
    judge_semantic_decision_value = "fail"

    def _judge_semantic_decision(self, refs):  # type: ignore[override]
        return self.judge_semantic_decision_value

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
               "record-launch", "record-child-return", "finalize-child",  # generate.gate (deterministic)
               "record-launch", "finalize-child",  # generate.verify (leaf)
               "write-step-result"]  # generate (2 leaf + 1 deterministic substep)
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
            # generate has 3 substeps (generate, gate, verify); compile has 3
            # (generate, static, verify); validate has 4 (pre_judge, execute, judge, post_judge).
            expected_substeps = {"generate": 3, "compile": 3, "validate": 4}[substep_aware]
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


class ConsumeResumeDirectiveTest(unittest.TestCase):
    """B2: `conduct` honors the dev structural-validate.execute `resume_directive` by
    reopening Generate and seeding a warm repair carrying the gate's findings."""

    NODE_KEY = "component/spec_x@0.1.0"
    OID = "orch_resume"
    TRIGGER = "validate-exec-fail-1"
    PRODUCER = "generate-sub-1"
    FINDINGS = "[execute fail]\npost_execute: missing required_raw_variables {'a1'}"

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key=self.NODE_KEY, spec_path="spec/component/spec_x",
            ir_id="x_20260101_001", pipeline_id="x_20260101_001",
            source_id="src_20260101_001", binary_id="bin_20260101_001",
            run_id="run_20260101_001", source_binary_id="bin_20260101_001",
        )

    def _conductor(self, repo_root: Path, directive: dict | None, *,
                   generate_completed: bool = True,
                   reopen_raises: bool = False,
                   reopen_noop: bool = False) -> _FakeConductor:
        root = repo_root / "workspace" / "orchestrations" / self.OID
        root.mkdir(parents=True, exist_ok=True)
        meta: dict = {"orchestration_id": self.OID}
        if directive is not None:
            meta["resume_directive"] = directive
        (root / "orchestration_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        sr = root / "steps" / wc.node_key_safe(self.NODE_KEY) / "generate" / "ORCH"
        sr.mkdir(parents=True, exist_ok=True)
        (sr / "step_result.json").write_text(
            json.dumps({"status": "pass", "executor_agent_run_id": "ORCH",
                        "substep_agent_run_ids": [self.PRODUCER, "generate-sub-2"]}),
            encoding="utf-8")

        class _C(_FakeConductor):
            def runtime(self, args):  # type: ignore[override]
                if args[0] == "check-step-completed":
                    if not generate_completed and "generate" in args:
                        return {}
                    return {"integrity": "ok", "agent_run_id": "ORCH"}
                if args[0] == "reopen-phase":
                    if reopen_raises:
                        raise RuntimeError("reopen-phase: trigger not found")
                    if reopen_noop:
                        super().runtime(args)  # still record the call
                        return {"status": "noop"}
                return super().runtime(args)

        c = _C(repo_root=repo_root, orchestration_id=self.OID,
               orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        return c

    def _directive(self, **over) -> dict:
        base = {
            "reopen_from": "generate",
            "node_key": self.NODE_KEY,
            "trigger_agent_run_id": self.TRIGGER,
            "reason_code": "dev_phase_rollback",
            "failure_category": "post_execute_violation",
            "source": wc_runtime.DEV_VALIDATE_EXECUTE_RESUME_SOURCE,
            "repair_findings": self.FINDINGS,
        }
        base.update(over)
        return base

    def test_directive_reopens_generate_and_seeds_warm_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            c = self._conductor(repo_root, self._directive())
            repair = c._consume_resume_directive(self._refs(), ["compile", "generate",
                                                               "build", "validate"])
            reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
            self.assertEqual(len(reopens), 1)
            self.assertEqual(reopens[0]["--from-phase"], "generate")
            self.assertEqual(reopens[0]["--trigger-agent-run-id"], self.TRIGGER)
            self.assertEqual(reopens[0]["--reason"], "dev_resume_validate_execute_structural")
            self.assertEqual(repair, {"generate": {
                "issue_severity": "major",
                "repair_strategy": "reuse",
                # recovered from the checkpointed step_result BEFORE reopen dropped it
                "repair_target_agent_run_id": self.PRODUCER,
                "repair_reason": "validate_execute_structural_resume",
                "repair_findings": self.FINDINGS,
            }})

    def test_no_directive_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            for directive in (None,
                              self._directive(source="failure_analysis.original_finding"),
                              self._directive(node_key="component/other@0.1.0"),
                              self._directive(trigger_agent_run_id=""),
                              self._directive(reopen_from="compile")):
                c = self._conductor(repo_root, directive)
                self.assertEqual(
                    c._consume_resume_directive(self._refs(),
                                                ["compile", "generate", "build", "validate"]),
                    {})
                self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_generate_out_of_scope_is_a_noop(self) -> None:
        """A `--until compile` run never reaches Generate; nothing to reopen."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            c = self._conductor(repo_root, self._directive())
            self.assertEqual(c._consume_resume_directive(self._refs(), ["compile"]), {})
            self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_incomplete_generate_is_a_noop(self) -> None:
        """Generate is not checkpointed (a prior reopen already dropped it): the plain
        resume re-runs it, and reopening would archive the in-progress attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            c = self._conductor(repo_root, self._directive(), generate_completed=False)
            self.assertEqual(
                c._consume_resume_directive(self._refs(),
                                            ["compile", "generate", "build", "validate"]),
                {})
            self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_reopen_failure_degrades_to_plain_resume(self) -> None:
        """A rejected reopen must not crash the run and must not seed a repair."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            c = self._conductor(repo_root, self._directive(), reopen_raises=True)
            self.assertEqual(
                c._consume_resume_directive(self._refs(),
                                            ["compile", "generate", "build", "validate"]),
                {})

    def test_reopen_noop_seeds_no_repair(self) -> None:
        """A trigger a prior reopen already consumed leaves Generate checkpointed, so
        run_phase would skip it — seeding a repair there would drop it silently."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            c = self._conductor(repo_root, self._directive(), reopen_noop=True)
            self.assertEqual(
                c._consume_resume_directive(self._refs(),
                                            ["compile", "generate", "build", "validate"]),
                {})

    def test_missing_findings_still_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            d = self._directive()
            d.pop("repair_findings")
            c = self._conductor(repo_root, d)
            repair = c._consume_resume_directive(self._refs(), ["compile", "generate",
                                                                "build", "validate"])
            self.assertNotIn("repair_findings", repair["generate"])
            self.assertEqual(repair["generate"]["repair_strategy"], "reuse")

    def test_conduct_repairs_generate_from_the_directive(self) -> None:
        """End-to-end at the conduct level: the reopened Generate's producer substep is
        launched with the warm repair payload (findings in the request)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            c = self._conductor(repo_root, self._directive())
            # Generate is reopened by the directive; every phase then passes. The fake's
            # check-step-completed reports "completed" for every phase, so only the reopened
            # Generate is re-run... which the fake cannot model. Assert on the payload the
            # conductor hands run_phase instead.
            captured: list = []
            orig = c.run_phase

            def _run_phase(refs, phase, repair=None):
                captured.append((phase, repair))
                return wc.PhaseOutcome(phase, "pass", decision=wc.RouteDecision("advance"),
                                       skipped=True)

            c.run_phase = _run_phase  # type: ignore[assignment]
            self.assertEqual(c.conduct(self._refs(), "validate"), "pass")
            del orig
            repairs = {phase: rep for phase, rep in captured if rep}
            self.assertEqual(list(repairs), ["generate"])
            self.assertEqual(repairs["generate"]["repair_findings"], self.FINDINGS)
            self.assertEqual(repairs["generate"]["repair_target_agent_run_id"], self.PRODUCER)


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

    def test_gate_finding_warm_reopens_generate_same_phase(self) -> None:
        # A generate.gate finding routes retry/generate/reuse(gate_*); conduct must do a
        # SAME-PHASE warm reopen (reopen-phase --from-phase generate) and re-run generate,
        # not terminalize like the generic same/downstream branch.
        c = self._conductor()
        state = {"gate_failed": False}

        def status_fn(phase, substep, n):
            if phase == "generate" and substep == "gate" and not state["gate_failed"]:
                state["gate_failed"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="reuse",
            reason="gate_syntax_error+lint_findings")
        # Stub the on-disk excerpt read so the threading assertion does not need a real
        # gate_meta.json (the disk read itself is covered by ReuseResumeAndFindingsTest).
        c._read_repair_findings = lambda refs, reason, phase=None: "C061 argument 'u_l'"  # type: ignore[assignment]
        status = c.conduct(self._refs(), "generate")
        self.assertEqual(status, "pass")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "generate")
        self.assertEqual(reopens[0]["--reason"], "gate_syntax_error+lint_findings")
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

    def test_structural_execute_failure_warm_reopens_generate_cross_phase(self) -> None:
        # B1 end to end (prod): a structural validate.execute failure cross-phase reopens
        # generate with a WARM reuse repair carrying the gate's findings — the same treatment
        # the judge's ("structural_violation","code") route already gets. The findings must be
        # read BEFORE reopen-phase, while refs still names the failed run.
        c = self._conductor()
        c.workflow_mode = "prod"  # cross-phase reopen is prod-only (dev fail_closes; see F1)
        c._claude_session_resumable = lambda s: True  # type: ignore[assignment]
        state = {"execute_failed": False}

        def status_fn(phase, substep, n):
            if phase == "validate" and substep == "execute" and not state["execute_failed"]:
                state["execute_failed"] = True
                return "fail"
            return "pass"

        c.status_fn = status_fn
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="reuse",
            reason="validate_execute_post_execute_violation")
        # Stub the on-disk excerpt read (covered by ReuseResumeAndFindingsTest) and record how
        # many reopens had happened when it ran: reopen rotates the run id, so a read after it
        # would look at a fresh (empty) run node dir.
        seen: list[tuple[int, str | None, str | None, str | None]] = []

        def fake_findings(refs, reason, phase=None):
            seen.append((len([s for s, _ in c.calls if s == "reopen-phase"]),
                         refs.run_id, reason, phase))
            return "missing required_raw_variables: a1 (wrapper key 'values')"

        c._read_repair_findings = fake_findings  # type: ignore[assignment]
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "pass")
        # Read exactly once, BEFORE any reopen, with refs still naming the FAILED run and the
        # route reason that selects the trial_meta branch.
        self.assertEqual(
            seen, [(0, "run_1_001", "validate_execute_post_execute_violation", "validate")])

        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), 1)
        self.assertEqual(reopens[0]["--from-phase"], "generate")
        self.assertEqual(reopens[0]["--reason"], "validate_execute_post_execute_violation")

        gen_launches = [cap["--request-json"] for s, cap in c.calls
                        if s == "record-launch"
                        and cap.get("--request-json", {}).get("step") == "generate"
                        and cap["--request-json"].get("substep") == "generate"]
        self.assertEqual(len(gen_launches), 2)
        self.assertNotIn("repair_findings", gen_launches[0])
        self.assertEqual(gen_launches[1]["repair_strategy"], "reuse")
        self.assertEqual(gen_launches[1]["repair_findings"],
                         "missing required_raw_variables: a1 (wrapper key 'values')")
        # reuse + findings + a resumable producer session -> the slim warm-resume repair turn.
        self.assertTrue(gen_launches[1]["warm_resume"])
        self.assertEqual(gen_launches[1]["skill_must_read_refs"], "")

    def test_dev_structural_execute_failure_fails_closed_with_category_detail(self) -> None:
        # F1 is unchanged by B1: dev still fail_closes the cross-phase rollback rather than
        # auto-retrying. The category rides in reason_detail, which is what the B2 dev-resume
        # deriver keys on.
        c = self._conductor()
        c.workflow_mode = "dev"
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "execute") else "pass")
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="reuse",
            reason="validate_execute_post_execute_violation")
        self.assertEqual(c.conduct(self._refs(), "validate"), "fail_closed")
        ss = [cap for s, cap in c.calls if s == "set-status"][-1]
        self.assertEqual(ss["--reason-code"], "dev_phase_rollback")
        self.assertEqual(ss["--reason-detail"], "validate_execute_post_execute_violation")
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])

    def test_compile_static_finding_warm_reopens_compile_same_phase(self) -> None:
        # A compile.static finding routes retry/compile/reuse (same-phase); conduct
        # must do a SAME-PHASE warm reopen (reopen-phase --from-phase compile) and re-run
        # compile, exactly like a generate.gate finding reopens generate.
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
        # A usage limit is NOT retryable (and, with --wait-usage-reset OFF as here, not waited
        # either), so this stub also serves as the tripwire for that: adding `llm_usage_limit` to
        # _RETRYABLE_LEAF_INFRA_TAGS would spawn 3 judge leaves and break the single-tombstone /
        # three-arid assertions below.
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

    def test_transport_failure_reason_names_an_llm_usage_limit(self) -> None:
        """A leaf killed by an LLM usage limit exits 1 with no artifacts, and the conductor could
        only report `leaf_transport_error: leaf_exit=1` — which reads as a crash and sends the
        operator hunting for a bug that is not there. The cause is in the leaf's captured output,
        so the fail_closed reason (and the orphan tombstone reason) must carry it."""
        c = self._conductor()
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(  # type: ignore[assignment]
            1, "", "Claude AI usage limit reached|1752200000")
        oc = c.run_phase(self._refs(), "validate")
        reason = oc.decision.reason
        # The prefix is load-bearing: set_status maps fail_closed reasons to an allowlisted
        # reason_code by prefix match, so the tag may only ever append.
        self.assertTrue(reason.startswith("leaf_transport_error: leaf_exit=1"))
        self.assertIn("llm_usage_limit", reason)
        self.assertIn("usage limit reached", reason)
        # reason_detail is truncated at 200 chars by set_status; the tag must survive that.
        self.assertLessEqual(len(reason), 200)
        sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertIn("llm_usage_limit", sup[0]["--reason"])

    def test_transport_failure_reason_is_unchanged_without_an_infra_marker(self) -> None:
        """Negative twin: an ordinary crash carries no tag, so the reason keeps its current form."""
        c = self._conductor()
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(  # type: ignore[assignment]
            1, "", "Traceback (most recent call last): ValueError: boom")
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.decision.reason, "leaf_transport_error: leaf_exit=1")
        self.assertNotIn("tag:", oc.decision.reason)

    def test_classify_leaf_infra_error_tags_each_known_cause(self) -> None:
        cases = [
            ("Claude AI usage limit reached", "llm_usage_limit"),
            ("You've hit your session limit; resets at 5pm", "llm_usage_limit"),
            ("API Error: rate limit exceeded", "llm_rate_limit"),
            ("API Error: 429 Too Many Requests", "llm_rate_limit"),
            ("Error: Overloaded", "llm_overloaded"),
            ("API Error: 529 overloaded_error", "llm_overloaded"),
            ("Permission checking is temporarily unavailable, so auto mode cannot determine "
             "whether this is safe", "llm_permission_probe_unavailable"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                got = wc._classify_leaf_infra_error(text)
                self.assertIsNotNone(got, f"expected a tag for {text!r}")
                self.assertEqual(got[0], expected)
                self.assertTrue(got[1], "the evidence line must be non-empty")
        self.assertIsNone(wc._classify_leaf_infra_error("Traceback: ValueError: boom"))
        self.assertIsNone(wc._classify_leaf_infra_error(""))

    def test_classify_leaf_infra_error_does_not_fire_on_ordinary_leaf_output(self) -> None:
        """A bare `429` / `529` substring matches a traceback frame or any duration/token count.
        Mislabelling a routine crash as a quota event is worse than not labelling it at all — it
        sends the operator hunting an outage that never happened. An HTTP-ish status code counts
        only next to an error/status word, and the word patterns must survive being fed a leaf's
        own prose and a C++ compiler's diagnostics."""
        benign = [
            '  File "/repo/tools/thing.py", line 429, in run',
            "  File \"/repo/x.py\", line 529, in main",
            "stats: duration_ms: 5291 cost_usd 0.02",
            "total input tokens 4293",
            "gfortran: error at line 429 of model.f90",   # a compiler line number, not HTTP
            "I analysed the scheme: diffusion is the rate-limiting process here.",
            "error: call of overloaded 'update(double)' is ambiguous",
            "error: call of overloaded ‘advance’ is ambiguous",   # gcc's unicode quotes
            # ...and the same for the transport tag: a 5xx-looking integer next to a bare
            # `error` is a subscript or a line number, never an HTTP status.
            "error: index 502 out of bounds for array u(500)",
            "gfortran: error at line 504 of model.f90",
        ]
        for text in benign:
            with self.subTest(text=text):
                self.assertIsNone(
                    wc._classify_leaf_infra_error(text),
                    f"must not tag ordinary leaf output: {text!r}")

    def test_classify_leaf_infra_error_ignores_a_recovered_retry_notice(self) -> None:
        """The CLI prints `API Error (429 ...) - Retrying in 1s... (attempt 1/10)` and then
        RECOVERS. Tagging that would blame the quota for a leaf that actually died of a hook
        denial — the very misdiagnosis this classifier exists to prevent, inverted. A TERMINAL
        message may still mention retries, and must stay classifiable."""
        recovered = ("API Error (429 rate_limit_error) - Retrying in 1 seconds... (attempt 1/10)\n"
                     "API Error (Overloaded) - Retrying in 2 seconds... (attempt 2/10)\n"
                     "RuntimeError: hook denied write outside write_roots")
        self.assertIsNone(wc._classify_leaf_infra_error(recovered))
        # ...but codex's terminal give-up, which names retries, still classifies.
        terminal = "stream error: exceeded retry limit, last status: 429 Too Many Requests"
        got = wc._classify_leaf_infra_error(terminal)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "llm_rate_limit")

    def test_classify_leaf_infra_error_prefers_stderr_over_the_leafs_own_prose(self) -> None:
        """On a `claude -p` leaf stdout carries the model's final message, which can discuss the
        "rate-limiting step" of a scheme. stderr carries the real transport error, so it wins."""
        got = wc._classify_leaf_infra_error(
            "Claude AI usage limit reached|1752200000",                     # stderr
            "the rate limit of the reaction is set by diffusion")           # stdout
        self.assertEqual(got[0], "llm_usage_limit")

    def test_classify_leaf_infra_error_covers_the_other_real_quota_messages(self) -> None:
        cases = [
            ("Your credit balance is too low to make this request", "llm_usage_limit"),
            ("5-hour limit reached; resets at 12pm", "llm_usage_limit"),
            ("quota exceeded for this organization", "llm_usage_limit"),
            ("stream disconnected: Too Many Requests", "llm_rate_limit"),
            ('API Error: 529 {"type":"overloaded_error","message":"Overloaded"}', "llm_overloaded"),
            # Strings carried verbatim by the shipped claude CLI:
            ("Request rejected (429) · this may be a temporary capacity issue.", "llm_rate_limit"),
            ("rate limited — wait and retry", "llm_rate_limit"),
            ("Opus is experiencing high load, please use another model", "llm_overloaded"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                got = wc._classify_leaf_infra_error(text)
                self.assertIsNotNone(got, f"expected a tag for {text!r}")
                self.assertEqual(got[0], expected)

    def test_classify_leaf_infra_error_does_not_invert_the_clis_not_your_usage_limit(self) -> None:
        """The CLI's own 429 message reads "Server is temporarily limiting requests (not your usage
        limit)". Tagging that `llm_usage_limit` is exactly backwards: the operator would sit out a
        5-hour quota reset that never happened. It is a rate limit, and must be reported as one."""
        got = wc._classify_leaf_infra_error(
            "Server is temporarily limiting requests (not your usage limit) · this may be a "
            "temporary capacity issue.")
        self.assertIsNotNone(got)
        self.assertEqual(got[0], "llm_rate_limit")

    def test_classify_leaf_infra_error_does_not_fire_on_the_leafs_own_prose(self) -> None:
        """A failed `claude -p` leaf writes its output to STDOUT, so the classifier is fed the
        model's own prose and the compiler's diagnostics. Generic words must not tag them."""
        benign = [
            "Newton iteration limit reached; the solver did not converge",
            "Context limit reached",                       # a prompt-size failure, not a quota
            "Subagent nesting limit reached (depth 3)",
            "The generic __box interface is overloaded across ranks 0..3",
            "Overloaded the `__box` generic so ranks 0..3 share one writer.",
            "the rate-limiting step of the reaction is diffusion",
            # The transport tag is fed the same prose, and its vocabulary (connection, stream,
            # network, timed out) is exactly the vocabulary of a numerical model's own writing.
            "the solver timed out after 500 iterations without converging",
            "the connection between cells 3 and 4 carries the upwind flux",
            "open(unit=10, file=snap, access='stream', form='unformatted')",
            "the MPI network topology is a 2D torus",
            "Context limit exceeded while reading the IR",
        ]
        for text in benign:
            with self.subTest(text=text):
                self.assertIsNone(wc._classify_leaf_infra_error(text),
                                  f"must not tag the leaf's own prose: {text!r}")

    def test_classify_leaf_infra_error_prefers_the_most_severe_tag(self) -> None:
        """A usage limit is a hard stop costing hours; a rate limit is transient. When a run logged
        both, reporting the transient one sends the operator back to a run that cannot start."""
        got = wc._classify_leaf_infra_error(
            "Claude AI usage limit reached|1752307200\nAPI Error: 429 rate_limit_error")
        self.assertEqual(got[0], "llm_usage_limit")

    def test_classify_leaf_infra_error_ignores_a_wrapped_retry_banner(self) -> None:
        """The retry banner is sometimes wrapped across two lines, putting the error on one and the
        `Retrying...` on the next. A line-scoped skip alone would still tag the recovered retry."""
        self.assertIsNone(wc._classify_leaf_infra_error(
            "API Error (429 rate_limit_error)\n"
            "  · Retrying in 1 seconds... (attempt 1/10)\n"
            "RuntimeError: hook denied write outside write_roots"))

    def test_classify_leaf_infra_error_tags_a_transient_transport_flake(self) -> None:
        """The line that cost E2E #4 6.8 hours: a `compile.verify` leaf died leaving ONLY this,
        it matched no pattern, and the run fail-closed until a human `--resume`d. It — and the
        other shapes a dropped connection takes — must tag `llm_transport_flake`, which is what
        makes the substep retryable."""
        cases = [
            # the real incident, verbatim from agents/<arid>/dialogs/leaf.stdout.log
            "API Error: Connection closed mid-response. The response above may be incomplete.",
            "Error: socket hang up",
            "read ECONNRESET",
            "connect ETIMEDOUT 160.79.104.10:443",
            "API Error: 502 Bad Gateway",
            "stream disconnected",
            "TypeError: terminated",
            "TypeError: fetch failed",
            "Error: Premature close",
            "API Error: 503 Service Unavailable",
            "Error: request timed out",
            # the catch-all: a transport wording we have not seen yet still self-heals, because
            # the CLI only ever opens a LINE with `API Error:` for an API/transport fault.
            "API Error: something entirely new went wrong at the edge",
        ]
        for text in cases:
            with self.subTest(text=text):
                got = wc._classify_leaf_infra_error(text)
                self.assertIsNotNone(got, f"expected a tag for {text!r}")
                self.assertEqual(got[0], "llm_transport_flake")
                self.assertTrue(got[1], "the evidence line must be non-empty")
        self.assertIn("llm_transport_flake", wc._RETRYABLE_LEAF_INFRA_TAGS)

    def test_classify_leaf_infra_error_transport_does_not_fire_on_numerics_prose(self) -> None:
        """A false transport tag is not a cosmetic mislabel: it ARMS A RETRY, so a deterministic
        failure (a crash, a hook denial, a compiler error) would be re-run three times and cost
        three times the wall-clock to reach the same dead end. The tag's whole vocabulary —
        connection, stream, network, timed out, 5xx — is also a numerical model's vocabulary."""
        benign = [
            "the solver timed out after 500 iterations",
            "the connection between cells 3 and 4",
            "error: index 502 out of bounds",
            "gfortran: error at line 504 of model.f90",
            "open(unit=7, access='stream')",
            "the MPI network is a fat tree",
            "context limit exceeded",
            "Traceback (most recent call last): ValueError: boom",
            # the transport library's phrases, but continued into a sentence: the leaf is
            # DESCRIBING something, not reporting a dropped connection. A comma, a semicolon or a
            # colon CONTINUES a sentence — only the end of the line ends one, which is why the
            # phrase patterns anchor there and not on punctuation generally.
            "write(*,*) 'stream error estimate', err",
            "write(*,*) 'stream error: ', err_est",
            "The premature close of the file unit truncated the snapshot.",
            "A premature close, or a missing flush, truncates the snapshot.",
            "the fetch failed for the dependency facts, so I re-read the IR",
            "If the fetch failed, I fall back to the IR dependency facts.",
            "Internal server error: out of scope for this node, see the checks module.",
            "A bad gateway: not applicable here; the harness is in-process.",
            "gfortran Error: interface is overloaded; use a specific binding.",
            # `rate limit` unqualified is ordinary technical English; only the bare form that ENDS
            # the line (or opens the CLI's `rate limited — wait and retry` dash clause) counts.
            "The CFL condition sets the rate limit.",
            "The scheme is rate-limited, so dt shrinks.",
            "The stream function psi is diagnosed from the vorticity.",
            "iostat = 5001 on unit 10",   # a gfortran iostat code, not an HTTP 5xx
            "The service unavailable state is represented by flag X.",
            "Internal server error handling is outside the scope of this module.",
            "The bad gateway approximation is not used in this scheme.",
            # `status` is THIS REPO'S vocabulary (gate status, verdict status, step_result status),
            # so a 5xx-looking number beside it is not an HTTP status...
            "status: 500 checks failed",
            "status 500 iterations completed",
            # ...and the port of a URL is not one either
            "endpoint https://mcp.internal:443/sse is configured",
            # `API Error` INSIDE a sentence is the model talking about one, not the CLI
            # reporting one — only a line that OPENS with it is the CLI's own banner.
            "I hit an API error while reading the docs, but recovered and continued.",
        ]
        for text in benign:
            with self.subTest(text=text):
                self.assertIsNone(
                    wc._classify_leaf_infra_error(text),
                    f"must not arm a retry on ordinary leaf output: {text!r}")

    def test_classify_leaf_infra_error_prefers_a_quota_tag_over_the_generic_transport_tag(
            self) -> None:
        """Severity order is a contract, not an accident: the transport tag sits LAST, so a
        message that names a quota keeps its specific tag. It decides the retry policy — a usage
        limit is never retried, a transport flake always is — so a quota message demoted to
        `llm_transport_flake` would spend the budget re-launching into a hard stop."""
        cases = [
            # each of these ALSO matches the generic transport pattern
            ("API Error: 429 Too Many Requests", "llm_rate_limit"),
            ("stream disconnected: Too Many Requests", "llm_rate_limit"),
            ('API Error: 529 {"type":"overloaded_error"}', "llm_overloaded"),
            ("API Error: Claude AI usage limit reached|1752200000", "llm_usage_limit"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(wc._classify_leaf_infra_error(text)[0], expected)
        # and across lines, the most severe still wins
        self.assertEqual(
            wc._classify_leaf_infra_error(
                "API Error: Connection closed mid-response.\n"
                "Claude AI usage limit reached|1752307200")[0],
            "llm_usage_limit")

    def test_classify_leaf_infra_error_promotes_only_the_terminal_tags_out_of_stdout(self) -> None:
        """The tag now decides the RETRY POLICY, which makes the stream priority load-bearing in
        both directions.

        The CLI reports an infrastructure failure as its RESULT TEXT — on stdout, often with an
        empty stderr (that is exactly how the E2E #4 incident line arrived). So the two
        NON-RETRYABLE tags must be able to override a stderr transport match: retrying a usage
        limit re-launches into a multi-hour hard stop, and retrying a 4xx re-sends a request the
        API rejects identically every time. Promoting them can only ever REMOVE a launch.

        Nothing else from stdout may override stderr, because stdout is also the model's own
        prose. If any prose match could outrank stderr, a leaf that happened to write "rate limits
        the timestep" while the connection actually dropped would silently retag — or worse,
        suppress — the retry, re-arming the 6.8-hour incident with the leaf's own writing."""
        self.assertEqual(wc._CROSS_STREAM_PROMOTING_TAGS,
                         {"llm_usage_limit", "llm_client_error"})
        for stdout_line, expected in (
                ("Claude AI usage limit reached|1752307200", "llm_usage_limit"),
                ('API Error: 400 {"type":"invalid_request_error"}', "llm_client_error")):
            with self.subTest(stdout=stdout_line):
                got = wc._classify_leaf_infra_error(
                    "connect ETIMEDOUT 160.79.104.10:443", stdout_line)   # stderr: transient
                self.assertEqual(got[0], expected)
                self.assertNotIn(got[0], wc._RETRYABLE_LEAF_INFRA_TAGS)
        # Any other stdout match leaves stderr's verdict — and its retry — standing.
        for prose in ("Newton's method diverged in 1200 cells",
                      "diffusion rate limits the timestep",
                      "Opus is experiencing high load"):
            with self.subTest(prose=prose):
                got = wc._classify_leaf_infra_error(
                    "API Error: Connection closed mid-response.", prose)
                self.assertEqual(got[0], "llm_transport_flake")
                self.assertIn(got[0], wc._RETRYABLE_LEAF_INFRA_TAGS)
        # On equal severity stderr also wins.
        got = wc._classify_leaf_infra_error("Error: socket hang up", "API Error: 502 Bad Gateway")
        self.assertEqual(got[1], "Error: socket hang up")

    def test_classify_leaf_infra_error_tags_a_4xx_as_a_non_retryable_client_error(self) -> None:
        """A 4xx means the REQUEST is wrong (bad credential, unsupported parameter, oversized
        prompt): every re-launch sends the same request and gets the same rejection. Without its
        own tag the `^api error` catch-all would swallow it into `llm_transport_flake`, retry a
        deterministic misconfiguration three times, and then report it to the operator as a
        provider outage to wait out. The case this repo can cause itself is the first one: a leaf
        model whose output ceiling is below LEAF_MAX_OUTPUT_TOKENS."""
        cases = [
            'API Error: 400 {"type":"invalid_request_error","message":"max_tokens: 128000 > '
            '64000, which is the maximum allowed number of output tokens"}',
            'API Error: 401 {"type":"authentication_error","message":"invalid x-api-key"}',
            "API Error: 403 Forbidden",
            "API Error: 413 request_too_large",
            # The CLI also renders a 4xx with NO status code at all. Without these the `^api error`
            # catch-all would take the line and RETRY a deterministic rejection. The first is the
            # other failure this repo can inflict on itself: the conductor injects the R5 exemplar,
            # the dependency facts and the must-read docs into a cold generate prompt.
            "API Error: prompt is too long: 235000 tokens > 200000 maximum",
            "API Error: Invalid API key · Please run /login",
            "API Error: OAuth token has expired.",
            "API Error: Request body too large",
        ]
        for text in cases:
            with self.subTest(text=text):
                got = wc._classify_leaf_infra_error(text)
                self.assertEqual(got[0], "llm_client_error")
                self.assertNotIn("llm_client_error", wc._RETRYABLE_LEAF_INFRA_TAGS)
        # Two 4xx are NOT client errors and must stay RETRYABLE: 429 (a rate limit) and 408
        # (Request Timeout — a genuinely transient fault, the very thing the retry exists for).
        # Excluding them from this tag is only half the job: they must also MATCH a retryable one,
        # in every rendering — not just the `API Error:`-prefixed line that the catch-all covers.
        for text, expected in (("API Error: 429 Too Many Requests", "llm_rate_limit"),
                               ("Request rejected (429)", "llm_rate_limit"),
                               ("API Error: 408 Request Timeout", "llm_transport_flake"),
                               ("HTTP 408 Request Timeout", "llm_transport_flake"),
                               ("status_code: 408 request timeout", "llm_transport_flake"),
                               ("stream error: exceeded retry limit, last status: 408 Request "
                                "Timeout", "llm_transport_flake")):
            with self.subTest(text=text):
                got = wc._classify_leaf_infra_error(text)
                self.assertEqual(got[0], expected)
                self.assertIn(got[0], wc._RETRYABLE_LEAF_INFRA_TAGS)
        # ...and a leaf's own 4xx-looking numbers are not API status codes
        for benign in ("error: index 404 out of bounds", "the loop runs 400 steps"):
            self.assertIsNone(wc._classify_leaf_infra_error(benign), benign)

    def test_leaf_failure_summary_keeps_the_real_error_and_adds_the_marker(self) -> None:
        """The marker can land on either stream, so it is lifted out of whichever carries it — but
        it is PREPENDED to the stderr tail, never substituted for it. Substituting would let a
        misfiring classifier destroy the leaf's actual error, which is the opposite of the point."""
        proc = wc.ProcResult(1, "Claude AI usage limit reached|1752200000",
                             "RuntimeError: hook denied write to src/foo.f90")
        summary = wc.Conductor._leaf_failure_summary(proc)
        # The `leaf_exit=` prefix is relied on by the agent_runs result_summary assertions.
        self.assertTrue(summary.startswith("leaf_exit=1"))
        self.assertIn("usage limit reached", summary)   # the stdout-side marker survives...
        self.assertIn("hook denied write", summary)     # ...and so does the real stderr error

    def test_transport_tag_survives_a_marker_beyond_the_summary_truncation(self) -> None:
        """The tag is carried structurally on the outcome, not re-derived from the (already
        truncated) summary text: a backend that emits the limit message deep inside one long line
        would otherwise lose the tag exactly when it is needed."""
        long_line = "codex exec failed: " + "context " * 40 + "Claude AI usage limit reached"
        c = self._conductor()
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(1, "", long_line)  # type: ignore[assignment]
        oc = c.run_phase(self._refs(), "validate")
        self.assertIn("llm_usage_limit", oc.decision.reason)
        self.assertTrue(oc.decision.reason.startswith("leaf_transport_error: leaf_exit=1"))

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

    def test_judge_conformance_block_escalates_in_prod(self) -> None:
        # Fix: a judge substep that fails determine_substep_status while its semantic_review
        # decision != "fail" (e.g. decision=pass with a malformed per_test using the `result`
        # key) is a judge-AUTHORED conformance violation. The pre_phase_complete hook forbids a
        # `fail` step_result atop a non-`fail` semantic_review, so run_phase must SKIP the write
        # and route to the escalate diagnostician in prod — NOT crash the runtime
        # write-step-result (orch_20260702T041436Z_a901797b). No tombstone here: the escalate
        # trigger must stay live for a possible upstream reopen.
        c = self._C(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
                    orchestration_agent_run_id="ORCH", backend="claude", env={},
                    workflow_mode="prod")
        c.calls = []
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "judge") else "pass")
        c.judge_semantic_decision_value = "pass"
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.decision.action, "escalate")
        self.assertEqual(oc.decision.reason, "validate_judge_conformance_violation")
        subs = [s for s, _ in c.calls]
        self.assertNotIn("write-step-result", subs)    # no crash on the judge gate
        self.assertNotIn("add-superseded-runs", subs)  # escalate keeps the trigger live

    def test_judge_conformance_block_fails_closed_in_dev(self) -> None:
        # Same conformance violation in dev: fail-fast (no billed escalate leaf) -> skip-write +
        # tombstone the orphan arids (pre_judge/execute/judge) + fail_closed.
        c = self._conductor()  # default workflow_mode="dev"
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "judge") else "pass")
        c.judge_semantic_decision_value = "pass"
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertEqual(oc.decision.reason, "validate_judge_conformance_violation")
        subs = [s for s, _ in c.calls]
        self.assertNotIn("write-step-result", subs)
        sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup[0]["--run-ids"], ["child-1", "child-2", "child-3"])
        self.assertIn("validate_gate_fail_orphan", sup[0]["--reason"])

    def test_judge_missing_decision_also_blocks_not_crashes(self) -> None:
        # A judge fail with an ABSENT/empty semantic_review decision ("" != "fail") is treated
        # like decision=pass: the hook would reject the `fail` step_result (or require
        # semantic_review.json), so run_phase skips the write and conformance-blocks rather than
        # crash.
        c = self._conductor()  # dev
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "judge") else "pass")
        c.judge_semantic_decision_value = ""
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertEqual(oc.decision.reason, "validate_judge_conformance_violation")
        self.assertNotIn("write-step-result", [s for s, _ in c.calls])

    def test_judge_physics_fail_with_decision_fail_still_routes(self) -> None:
        # Guard scoping: a GENUINE physics/semantic judge fail (decision=="fail") is a routeable
        # failure — run_phase writes the step_result (the hook allows fail+fail) and routes via
        # classify_failure, unchanged by the conformance guard.
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: (
            "fail" if (phase == "validate" and substep == "judge") else "pass")
        c.judge_semantic_decision_value = "fail"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "retry", target_phase="generate", repair_strategy="restart", reason="physics_fail")
        oc = c.run_phase(self._refs(), "validate")
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

    def test_judge_conformance_escalate_terminal_tombstones(self) -> None:
        # Conduct-level counterpart of test_post_gate_unknown_escalate_terminal_tombstones for
        # the judge-conformance escalate: a prod judge-substep fail with decision != "fail"
        # escalates (run_phase leaves the orphan arids live for a possible reopen); when the
        # diagnostician TERMINALIZES, conduct must tombstone them with the judge-specific reason,
        # and never crash on write-step-result.
        import tempfile
        for stub in (
            lambda refs, phase, outcome: wc.RouteDecision("fail_closed", reason="diag_unrecoverable"),
            lambda refs, phase, outcome: wc.RouteDecision("retry", target_phase=None, reason="ambig"),
        ):
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
                c.judge_semantic_decision_value = "pass"
                c.status_fn = lambda phase, substep, n: (
                    "fail" if (phase == "validate" and substep == "judge") else "pass")
                c.escalate = stub  # type: ignore[assignment]
                status = c.conduct(refs, "validate")
                self.assertIn(status, ("fail", "fail_closed"))
                # The validate step_result is never written (the crash we fixed); earlier phases
                # (compile/generate/build) legitimately write their own.
                self.assertEqual(
                    [cap for s, cap in c.calls
                     if s == "write-step-result" and cap.get("--step") == "validate"], [])
                sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
                self.assertTrue(
                    any("validate_judge_conformance_escalate_terminal_orphan" in cap["--reason"]
                        for cap in sup),
                    f"expected judge-conformance terminal-orphan tombstone for {stub}")

    def test_judge_semantic_decision_reads_and_normalizes(self) -> None:
        # Direct coverage of the REAL helper (the _FakeConductor override is bypassed here): it
        # reads semantic_review.json#decision, normalizes case/whitespace, and returns "" when
        # the file or the field is absent (the "" path is what makes a missing/empty decision a
        # conformance block rather than a crash).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = wc.Conductor(repo_root=repo, orchestration_id="orch_x",
                             orchestration_agent_run_id="ORCH", backend="claude", env={})
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            self.assertEqual(c._judge_semantic_decision(refs), "")  # missing file
            (rn / "semantic_review.json").write_text(
                json.dumps({"decision": "  PASS "}), encoding="utf-8")
            self.assertEqual(c._judge_semantic_decision(refs), "pass")
            (rn / "semantic_review.json").write_text(
                json.dumps({"decision": "Fail"}), encoding="utf-8")
            self.assertEqual(c._judge_semantic_decision(refs), "fail")
            (rn / "semantic_review.json").write_text(
                json.dumps({}), encoding="utf-8")
            self.assertEqual(c._judge_semantic_decision(refs), "")  # missing field

    def test_read_case_ids_drops_path_traversal_tokens(self) -> None:
        # Runtime defense-in-depth: read_case_ids builds the runner argv (--cases ...), from
        # which the snapshot path raw/state_snapshots/<case_id>.json is formed. A `/` or `..`
        # must never reach the argv — even from a hand-crafted IR that bypassed the Compile gate
        # — or the honest runner writes outside its directory. Safe ids survive; unsafe are dropped.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = wc.Conductor(repo_root=repo, orchestration_id="orch_x",
                             orchestration_agent_run_id="ORCH", backend="claude", env={})
            ir_dir = repo / refs.ir_ref
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text(json.dumps({"case": {"test_case_set": [
                {"case_id": "c_ok"}, {"case_id": "l0.v1-2"},
                {"case_id": "../../evil"}, {"case_id": "a/b"}, {"case_id": ".."},
            ]}}), encoding="utf-8")
            self.assertEqual(c.read_case_ids(refs), ("c_ok", "l0.v1-2"))

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


class UsageResetEpochParseTests(unittest.TestCase):
    """`_parse_usage_reset_epoch` reads the MACHINE-FORM reset epoch a usage-limit leaf may carry
    as a trailing `|<10-digit>`. It is a separate scan from the classifier (whose evidence is
    clipped), it only trusts an epoch on a line the `llm_usage_limit` pattern itself matches (so a
    stray `|<number>` is never mistaken for a reset time), and it reads ONLY stderr — the trusted CLI
    error channel — never the leaf's untrusted stdout output surface."""

    def test_machine_form_reset_yields_the_epoch(self) -> None:
        self.assertEqual(
            wc._parse_usage_reset_epoch("Claude AI usage limit reached|1752200000"),
            1752200000)

    def test_human_worded_reset_has_no_epoch(self) -> None:
        # The CLI's human form ("resets 6:10pm") carries no machine time -> the caller must not wait.
        self.assertIsNone(
            wc._parse_usage_reset_epoch("You've hit your session limit; resets 6:10pm"))

    def test_stdout_is_never_trusted_to_arm_a_wait(self) -> None:
        # SECURITY: stdout is the leaf's own output surface (untrusted). The wait ADDS a multi-hour
        # sleep+relaunch, so a usage-limit epoch that appears only on stdout — whether the real CLI's
        # result text or a crashed leaf's prose containing `usage limit reached|<epoch>` — must NOT
        # arm the wait. `_parse_usage_reset_epoch` takes stderr ONLY, so there is no stdout argument
        # a caller could pass to arm it.
        import inspect
        params = list(inspect.signature(wc._parse_usage_reset_epoch).parameters)
        self.assertEqual(params, ["stderr"])  # no stdout parameter at all

    def test_pipe_epoch_on_a_non_usage_line_is_ignored(self) -> None:
        # A `|<10-digit>` that is NOT on a usage-limit line (here the leaf's own numerical prose)
        # must not be read as a reset time.
        self.assertIsNone(
            wc._parse_usage_reset_epoch("the solver step is |1752200000 in code units"))
        # Nothing at all.
        self.assertIsNone(wc._parse_usage_reset_epoch(""))

    def test_terminal_usage_line_epoch_wins_over_an_earlier_one(self) -> None:
        # Multiple usage-limit records: the LAST (terminal) line governs the wait, mirroring the
        # classifier's most-severe-then-last discipline. Returning the FIRST would wait on a stale,
        # already-survived epoch.
        stderr = ("Claude AI usage limit reached|1752200000\n"
                  "the run continued...\n"
                  "Claude AI usage limit reached|1799999999")
        self.assertEqual(wc._parse_usage_reset_epoch(stderr), 1799999999)

    def test_terminal_usage_line_without_epoch_supersedes_an_earlier_epoch(self) -> None:
        # Codex P2: an earlier session message with an in-window epoch, then a TERMINAL weekly limit
        # with no machine epoch -> None (do not wait on the stale epoch; the weekly limit fired).
        stderr = ("session limit reached|1752200000\n"
                  "Claude AI weekly limit reached; resets Monday")
        self.assertIsNone(wc._parse_usage_reset_epoch(stderr))

    def test_recovered_retry_notice_epoch_is_not_taken_as_terminal(self) -> None:
        # A recovered `Retrying...` banner that mentions a usage limit + epoch is skipped (as the
        # classifier skips it); only the genuine terminal line's epoch is returned.
        stderr = ("API Error (usage limit reached|1752200000) Retrying in 1s (attempt 1/10)\n"
                  "Claude AI usage limit reached|1799999999")
        self.assertEqual(wc._parse_usage_reset_epoch(stderr), 1799999999)


class LeafChildEnvTest(unittest.TestCase):
    """WI-A: the leaf's output ceiling is part of the CONDUCTOR'S leaf contract (it lives in
    _child_env, not in `.claude/settings.json`, so it cannot leak into the operator's own
    interactive sessions). Thinking tokens are billed against max_tokens, so the CLI default
    truncates a hard leaf mid-think — a fully billed turn that emits nothing at all."""

    def _conductor(self, backend: str) -> wc.Conductor:
        return wc.Conductor(repo_root=Path("/tmp/repo"), orchestration_id="orch_x",
                            orchestration_agent_run_id="ORCH", backend=backend, env={})

    def test_child_env_sets_leaf_max_output_tokens_for_claude(self) -> None:
        env = self._conductor("claude")._child_env("child-1")
        self.assertEqual(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], str(wc.LEAF_MAX_OUTPUT_TOKENS))
        self.assertEqual(wc.LEAF_MAX_OUTPUT_TOKENS, 128000)  # the Opus 4.8 synchronous ceiling
        # the pre-existing leaf env is untouched
        self.assertEqual(env["METDSL_ORCHESTRATION_ID"], "orch_x")
        self.assertTrue(env["TMPDIR"].endswith("/workspace/tmp/child-1"))

    def test_child_env_does_not_set_leaf_max_output_tokens_for_codex(self) -> None:
        # codex configures its model limits on a different surface; a CLAUDE_* var there is at
        # best inert and at worst confusing.
        env = self._conductor("codex")._child_env("child-1")
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", env)


class LeafTransientRetryTest(unittest.TestCase):
    """WI-B: a leaf killed by a TRANSIENT LLM-infrastructure failure (a connection dropped
    mid-response, an overload, a rate limit) is re-launched in place, bounded and with backoff,
    instead of fail-closing the run for a human to `--resume` hours later — the E2E #4 incident
    cost 6.8 hours of wall-clock waiting for exactly that.

    The retry loop lives INSIDE run_substep (run_phase's `outcomes` is positionally aligned with
    SUBSTEPS[phase]), so these drive run_substep/run_phase directly and assert the launch
    bookkeeping: one arid per attempt, every dead attempt tombstoned (an un-vouched terminal arid
    is an orphan edge that fails the completion check), and the warm-resume target preserved."""

    class _C(_FakeConductor):
        # scripted per-attempt leaf results + a recorded (not slept) backoff schedule
        procs: list = []
        slept: list = []
        spawns: list = []
        resume_target: str | None = None

        def _write_lineage(self, refs):  # type: ignore[override]
            return []

        def _resolve_reuse_resume(self, repair, phase, substep):  # type: ignore[override]
            return self.resume_target

        def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
            self.spawns.append(dict(kwargs))
            idx = len(self.spawns) - 1
            return self.procs[min(idx, len(self.procs) - 1)]

        def _sleep_backoff(self, seconds):  # type: ignore[override]
            self.slept.append(seconds)

    _FLAKE = "API Error: Connection closed mid-response. The response above may be incomplete."

    def _conductor(self, procs: list, repo: Path | None = None, **kw) -> "_C":
        c = self._C(repo_root=repo or Path("/tmp/repo"), orchestration_id="orch_x",
                    orchestration_agent_run_id="ORCH", backend="claude", env={}, **kw)
        c.calls, c.procs, c.slept, c.spawns = [], procs, [], []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
            binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001")

    def _flake(self) -> wc.ProcResult:
        # the real incident's shape: rc!=0, the message on STDOUT, stderr empty
        return wc.ProcResult(1, self._FLAKE, "")

    def test_transient_leaf_failure_is_retried_and_recovers(self) -> None:
        c = self._conductor([self._flake(), wc.ProcResult(0, "done", "")])
        with redirect_stdout(io.StringIO()):
            oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)
        self.assertEqual(len(c.spawns), 2)
        # one arid per attempt, and the outcome carries the SURVIVING one (the arid run_phase
        # will vouch in substep_agent_run_ids)
        launched = [cap["--request-json"]["agent_run_id"]
                    for s, cap in c.calls if s == "record-launch"]
        self.assertEqual(launched, ["child-1", "child-2"])
        self.assertEqual(oc.agent_run_id, "child-2")
        # the dead attempt is tombstoned: terminalized but never vouched, it would otherwise be
        # an orphan edge that fails _validate_orchestration_completion_for_pass at the end of an
        # otherwise-passing run
        sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup[0]["--run-ids"], ["child-1"])
        self.assertIn("leaf_transient_retry_orphan", sup[0]["--reason"])
        self.assertIn("llm_transport_flake", sup[0]["--reason"])
        # Ordering: record-launch -> finalize-child -> add-superseded-runs -> next record-launch.
        # finalize-child MUST come first (see test_tombstone_writes_are_outside_the_leafs_write_
        # window below: the tombstone's own files land in the child's FS diff otherwise, and the
        # dying leaf is rejected for the conductor's writes), and it must precede the next launch
        # (the runtime fail-closes a launch while a child of this parent is still active).
        subs = [s for s, _ in c.calls]
        self.assertLess(subs.index("finalize-child"), subs.index("add-superseded-runs"))
        self.assertLess(subs.index("add-superseded-runs"),
                        len(subs) - 1 - subs[::-1].index("record-launch"))

    def test_recovered_retry_vouches_only_the_survivor_in_the_step_result(self) -> None:
        """The phase-level shape of a recovered retry, and the invariant that makes the whole
        feature safe to leave running: the `step_result.json` vouches ONLY the surviving attempt,
        the dead one is tombstoned instead, and no arid is left in neither set. An un-vouched,
        un-tombstoned terminal arid is an orphan edge — the run would sail through every phase and
        then fail the completion check at the very end."""
        procs = [self._flake(), wc.ProcResult(0, "done", "")]   # compile.generate dies once
        c = self._conductor(procs)
        c.procs = procs + [wc.ProcResult(0, "ok", "")] * 4      # remaining substeps are clean
        with redirect_stdout(io.StringIO()):
            oc = c.run_phase(self._refs(), "compile")
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.decision.action, "advance")
        sr = next(cap["--result-json"] for s, cap in c.calls if s == "write-step-result")
        launched = [cap["--request-json"]["agent_run_id"]
                    for s, cap in c.calls if s == "record-launch"]
        tombstoned = [rid for s, cap in c.calls if s == "add-superseded-runs"
                      for rid in cap["--run-ids"]]
        dead, *survivors = launched
        self.assertEqual(tombstoned, [dead])                    # the dead attempt only...
        self.assertNotIn(dead, sr["substep_agent_run_ids"])     # ...and it vouches nothing
        self.assertEqual(sr["substep_agent_run_ids"], survivors)
        # every arid the loop minted is accounted for by exactly one of the two sets
        self.assertEqual(sorted(sr["substep_agent_run_ids"] + tombstoned), sorted(launched))
        # an infra retry is not a content retry: the phase records no repair decision
        self.assertIsNone(sr["retry_decisions"])

    def test_transient_retry_budget_exhaustion_fails_closed_with_the_attempt_count(self) -> None:
        c = self._conductor([self._flake()])  # every attempt dies
        with redirect_stdout(io.StringIO()):
            oc = c.run_phase(self._refs(), "compile")
        self.assertEqual(len(c.spawns), wc.MAX_LEAF_TRANSIENT_RETRIES + 1)
        self.assertEqual(oc.decision.action, "fail_closed")
        reason = oc.decision.reason
        # the prefix stays load-bearing (set_status maps it to a reason_code), the tag names the
        # cause, and `attempts=3` tells the operator the outage outlasted every backoff — do not
        # `--resume` straight into it
        self.assertTrue(reason.startswith("leaf_transport_error: leaf_exit=1"))
        self.assertIn("llm_transport_flake", reason)
        self.assertIn("[attempts=3]", reason)
        self.assertLessEqual(len(reason), 200)  # set_status truncates reason_detail at 200
        # the orphan-edge invariant: the tombstoned set == every arid the loop minted. The two
        # dead retries are tombstoned by run_substep, the last one by run_phase's transport branch.
        launched = {cap["--request-json"]["agent_run_id"]
                    for s, cap in c.calls if s == "record-launch"}
        tombstoned = {rid for s, cap in c.calls if s == "add-superseded-runs"
                      for rid in cap["--run-ids"]}
        self.assertEqual(tombstoned, launched)
        self.assertEqual(len(launched), wc.MAX_LEAF_TRANSIENT_RETRIES + 1)
        self.assertNotIn("write-step-result", [s for s, _ in c.calls])

    def test_a_retried_leaf_that_then_hits_a_usage_limit_reports_the_usage_limit(self) -> None:
        """Attempts can MIX tags, and the tag — not the `[attempts=N]` suffix — is what the
        operator routes on. A transport flake retried into a quota stop must terminalize as
        `llm_usage_limit` (wait for the reset) even though the suffix is present, and it must stop
        at once rather than spending the remaining budget re-launching into the hard stop."""
        c = self._conductor([self._flake(),
                             wc.ProcResult(1, "", "Claude AI usage limit reached|1752200000")])
        with redirect_stdout(io.StringIO()):
            oc = c.run_phase(self._refs(), "compile")
        self.assertEqual(len(c.spawns), 2)                    # the quota stop is NOT retried
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertIn("llm_usage_limit", oc.decision.reason)  # the LAST attempt's cause
        self.assertNotIn("llm_transport_flake", oc.decision.reason)
        self.assertIn("[attempts=2]", oc.decision.reason)     # ...and the launch count is honest
        self.assertLessEqual(len(oc.decision.reason), 200)

    def test_usage_limit_is_never_retried(self) -> None:
        """A usage limit is a HARD STOP lasting hours. Retrying it burns the budget in seconds
        and only delays the operator's resume. With --wait-usage-reset OFF (the DEFAULT, and what
        this conductor has) it stays terminal even though the leaf carried a machine-form reset
        epoch — the opt-in wait is exercised by the tests below."""
        c = self._conductor([wc.ProcResult(1, "", "Claude AI usage limit reached|1752200000")])
        oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(len(c.spawns), 1)
        self.assertEqual(oc.attempts, 1)
        self.assertEqual(oc.infra_error[0], "llm_usage_limit")
        self.assertEqual(c.slept, [])
        self.assertNotIn("add-superseded-runs", [s for s, _ in c.calls])

    def test_usage_reset_wait_recovers_when_opted_in(self) -> None:
        """--wait-usage-reset ON + a machine-form reset epoch: the usage-limited leaf is waited out
        in place and the substep re-launched, turning a next-day fresh run into a same-run resume.
        The wait sleeps to the epoch + the 120s margin, the dead attempt is tombstoned under its own
        prefix, and a `leaf_usage_limit_wait` event is emitted."""
        now = 1_752_200_000.0
        c = self._conductor(
            [wc.ProcResult(1, "", f"Claude AI usage limit reached|{int(now) + 300}"),
             wc.ProcResult(0, "done", "")],
            wait_usage_reset=True)
        events: list = []
        c.emit = lambda event, **f: events.append((event, f))  # type: ignore[assignment]
        with mock.patch.object(wc.time, "time", return_value=now):
            oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(oc.status, "pass")
        self.assertEqual(len(c.spawns), 2)             # dead attempt + the recovered launch
        self.assertEqual(oc.attempts, 2)               # every launch is counted honestly
        self.assertEqual(c.slept, [420.0])             # 300s to the reset + 120s margin
        # the dead usage attempt is tombstoned under the wait's own prefix (not the transient one)
        sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup[0]["--run-ids"], ["child-1"])
        self.assertIn("leaf_usage_limit_wait_orphan", sup[0]["--reason"])
        waits = [f for e, f in events if e == "leaf_usage_limit_wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["reset_epoch"], int(now) + 300)
        self.assertEqual(waits[0]["wait_seconds"], 420.0)
        self.assertEqual(waits[0]["wait_attempt"], 1)
        self.assertEqual(waits[0]["dead_agent_run_id"], "child-1")

    def test_usage_reset_wait_budget_is_one_then_fails_closed(self) -> None:
        """The wait budget is MAX_USAGE_LIMIT_WAITS (=1) per substep: a second usage limit after the
        first wait is terminal (fail_closed), with the honest launch count in the reason."""
        now = 1_752_200_000.0
        c = self._conductor(
            [wc.ProcResult(1, "", f"usage limit reached|{int(now) + 200}")],
            wait_usage_reset=True)
        with mock.patch.object(wc.time, "time", return_value=now), redirect_stdout(io.StringIO()):
            oc = c.run_phase(self._refs(), "compile")
        self.assertEqual(len(c.spawns), 2)                 # one wait, then the hard stop
        self.assertEqual(c.slept, [320.0])                 # only the single wait
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertIn("llm_usage_limit", oc.decision.reason)
        self.assertIn("[attempts=2]", oc.decision.reason)

    def test_usage_limit_on_stdout_only_does_not_arm_the_wait(self) -> None:
        """SECURITY (Codex P2): the classifier PROMOTES an `llm_usage_limit` tag out of stdout, but
        stdout is the leaf's own untrusted output surface. A usage-limit epoch seen ONLY on stdout —
        e.g. a leaf that crashed for an unrelated reason whose prose contains
        `usage limit reached|<future epoch>` — must NOT arm the (multi-hour) wait, even opted in. It
        is still classified/fail_closed as `llm_usage_limit`, but the wait declines (no sleep, no
        relaunch)."""
        now = 1_752_200_000.0
        c = self._conductor(
            [wc.ProcResult(1, f"Claude AI usage limit reached|{int(now) + 300}", "")],
            wait_usage_reset=True)
        with mock.patch.object(wc.time, "time", return_value=now):
            oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(len(c.spawns), 1)                 # no relaunch
        self.assertEqual(c.slept, [])                      # no wait
        self.assertEqual(oc.infra_error[0], "llm_usage_limit")  # still classified (fail_closed)
        self.assertNotIn("add-superseded-runs", [s for s, _ in c.calls])

    def test_usage_reset_wait_declines_without_a_machine_epoch(self) -> None:
        """Opted in but no machine-form epoch (a human-worded reset) => no guessing: the current
        fail_closed behavior is preserved and nothing is slept or tombstoned."""
        c = self._conductor(
            [wc.ProcResult(1, "", "You've hit your session limit; resets 6:10pm")],
            wait_usage_reset=True)
        oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(len(c.spawns), 1)
        self.assertEqual(oc.attempts, 1)
        self.assertEqual(oc.infra_error[0], "llm_usage_limit")
        self.assertEqual(c.slept, [])
        self.assertNotIn("add-superseded-runs", [s for s, _ in c.calls])

    def test_usage_reset_wait_declines_when_reset_exceeds_the_cap(self) -> None:
        """A reset further out than MAX_USAGE_LIMIT_WAIT_SECONDS (6h) is a weekly limit or a
        misparse, not a session window — do not sit on it; fall back to fail_closed."""
        now = 1_752_200_000.0
        c = self._conductor(
            [wc.ProcResult(1, "", f"usage limit reached|{int(now) + 7 * 3600}")],
            wait_usage_reset=True)
        with mock.patch.object(wc.time, "time", return_value=now):
            oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(len(c.spawns), 1)
        self.assertEqual(c.slept, [])
        self.assertEqual(oc.infra_error[0], "llm_usage_limit")

    def test_usage_wait_budget_is_independent_of_the_transient_budget(self) -> None:
        """A transient flake and a usage-limit wait draw on SEPARATE budgets: a flake retried, then
        a usage limit waited, then a success — all recovered in one substep, with both budgets
        counted precisely in the launch total."""
        now = 1_752_200_000.0
        c = self._conductor(
            [self._flake(),
             wc.ProcResult(1, "", f"usage limit reached|{int(now) + 60}"),
             wc.ProcResult(0, "done", "")],
            wait_usage_reset=True)
        with mock.patch.object(wc.time, "time", return_value=now), redirect_stdout(io.StringIO()):
            oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(oc.status, "pass")
        self.assertEqual(len(c.spawns), 3)
        self.assertEqual(oc.attempts, 3)               # 1 transient retry + 1 usage wait + success
        self.assertEqual(c.slept, [2.0, 180.0])        # transport backoff, then 60s + 120s margin
        # both dead attempts are tombstoned, each under its own prefix
        reasons = [cap["--reason"] for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("leaf_transient_retry_orphan" in r for r in reasons))
        self.assertTrue(any("leaf_usage_limit_wait_orphan" in r for r in reasons))

    def test_client_error_is_never_retried(self) -> None:
        """The WI-A interlock: if the configured leaf model's output ceiling is below
        LEAF_MAX_OUTPUT_TOKENS, EVERY launch is rejected with the same 400. Retrying it would
        triple the wall-clock and then report a local misconfiguration as a provider outage; the
        run must stop at once, with the API's own message in the reason."""
        c = self._conductor([wc.ProcResult(
            1, 'API Error: 400 {"type":"invalid_request_error","message":"max_tokens: 128000 > '
               '64000, which is the maximum allowed number of output tokens"}', "")])
        with redirect_stdout(io.StringIO()):
            oc = c.run_phase(self._refs(), "compile")
        self.assertEqual(len(c.spawns), 1)
        self.assertEqual(c.slept, [])
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertIn("llm_client_error", oc.decision.reason)
        self.assertIn("max_tokens", oc.decision.reason)   # the operator sees the actual cause
        self.assertNotIn("[attempts=", oc.decision.reason)

    def test_unclassifiable_leaf_crash_is_never_retried(self) -> None:
        """A crash / OOM / hook denial is deterministic: retrying it hides the same failure
        behind three times the wall-clock."""
        c = self._conductor([wc.ProcResult(
            1, "", "RuntimeError: hook denied write outside write_roots")])
        oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(len(c.spawns), 1)
        self.assertEqual(oc.attempts, 1)
        self.assertIsNone(oc.infra_error)
        self.assertEqual(c.slept, [])

    def test_deterministic_substep_is_never_retried(self) -> None:
        """A deterministic substep runs in-process — there is no leaf and no transport, so a
        failure there is a real defect (and its "stdout" is a gate report, not an API message)."""
        c = self._conductor([])
        c._run_deterministic_substep = (  # type: ignore[assignment]
            lambda refs, phase, substep, child_arid, request: wc.ProcResult(1, self._FLAKE, ""))
        oc = c.run_substep(self._refs(), "build", None)
        self.assertEqual(oc.attempts, 1)
        self.assertEqual(len([s for s, _ in c.calls if s == "record-launch"]), 1)
        self.assertEqual(c.slept, [])

    def test_transient_retry_backoff_is_per_tag_and_bounded(self) -> None:
        # a transport flake is usually gone on the next connection...
        c = self._conductor([self._flake()])
        with redirect_stdout(io.StringIO()):
            c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(c.slept, list(wc._LEAF_RETRY_BACKOFF_SECONDS["llm_transport_flake"]))
        # ...an overload needs the server side to recover, so it waits materially longer before
        # spending another billed launch
        c = self._conductor([wc.ProcResult(1, "", "API Error: 529 overloaded_error")])
        with redirect_stdout(io.StringIO()):
            c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(c.slept, list(wc._LEAF_RETRY_BACKOFF_SECONDS["llm_overloaded"]))
        # bounded, and no sleep after the LAST attempt (nothing follows it to wait for)
        self.assertEqual(len(c.slept), wc.MAX_LEAF_TRANSIENT_RETRIES)
        self.assertEqual(len(c.spawns), wc.MAX_LEAF_TRANSIENT_RETRIES + 1)
        # ...and a rate limit waits longer still, before spending the next billed launch
        c = self._conductor([wc.ProcResult(1, "", "API Error: 429 Too Many Requests")])
        with redirect_stdout(io.StringIO()):
            c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(c.slept, list(wc._LEAF_RETRY_BACKOFF_SECONDS["llm_rate_limit"]))
        # Every retryable tag must have a schedule. If one is ever added without one, the loop
        # falls back rather than raising KeyError mid-phase — which would crash the conductor
        # AFTER the dead attempt was already finalized and tombstoned, instead of failing closed.
        self.assertEqual(set(wc._LEAF_RETRY_BACKOFF_SECONDS), set(wc._RETRYABLE_LEAF_INFRA_TAGS))
        self.assertEqual(
            wc._LEAF_RETRY_BACKOFF_SECONDS.get("a_tag_with_no_schedule",
                                               wc._DEFAULT_LEAF_RETRY_BACKOFF),
            wc._DEFAULT_LEAF_RETRY_BACKOFF)

    def test_transient_retry_preserves_the_warm_resume_target(self) -> None:
        """The retry must NOT cold-start: build_launch_request only sends the slim findings-only
        repair turn when warm_resume is True, so a cold retry would silently drop the findings the
        repair exists to act on. Forking the producer session again is idempotent."""
        c = self._conductor([self._flake(), wc.ProcResult(0, "done", "")])
        c.resume_target = "SESSION-PRODUCER"
        repair = {"repair_strategy": "reuse", "repair_target_agent_run_id": "child-0",
                  "repair_reason": "generate_static_violation",
                  "repair_findings": "static: diagnostics are constant-heavy"}
        with redirect_stdout(io.StringIO()):
            oc = c.run_substep(self._refs(), "generate", "generate", repair=repair)
        self.assertEqual(oc.status, "pass")
        self.assertEqual([s["resume_session_id"] for s in c.spawns],
                         ["SESSION-PRODUCER", "SESSION-PRODUCER"])
        # ...but each attempt is its own child (its own session to write into)
        self.assertEqual([s["session_id"] for s in c.spawns], ["child-1", "child-2"])
        reqs = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"]
        self.assertEqual(len(reqs), 2)
        for req in reqs:  # both attempts send the slim repair turn, findings included
            self.assertTrue(req["warm_resume"])
            self.assertEqual(req["skill_must_read_refs"], "")
            self.assertIn("constant-heavy", req["repair_findings"])

    def test_transient_retry_uses_a_fresh_launch_request_and_min_mtime_per_attempt(self) -> None:
        """Each attempt re-takes its launch instant, so a half-written artifact left behind by the
        leaf that died is OLDER than the retry's window and cannot fake the retry's pass."""
        seen: list[float] = []

        c = self._conductor([self._flake(), wc.ProcResult(0, "done", "")])

        def _status(refs, phase, substep, allowed, min_mtime=0.0):
            seen.append(min_mtime)
            return ("pass", ["out.json"])

        c.determine_substep_status = _status  # type: ignore[assignment]
        with redirect_stdout(io.StringIO()):
            oc = c.run_substep(self._refs(), "compile", "verify")
        self.assertEqual(len(seen), 2)
        self.assertGreater(seen[1], seen[0])       # the retry's window starts later...
        self.assertEqual(oc.launched_at, seen[1])  # ...and the outcome carries the LIVE one
        # every attempt gets its own request.json (its own arid), so nothing is overwritten
        reqs = [cap["--request-json"] for s, cap in c.calls if s == "record-launch"]
        self.assertEqual([r["agent_run_id"] for r in reqs], ["child-1", "child-2"])

    def test_tombstone_writes_are_outside_the_leafs_write_window(self) -> None:
        """WHY finalize-child must precede add-superseded-runs.

        `record-launch` snapshots an FS baseline for the child, and `record-agent-run` (inside
        finalize-child) re-walks the live workspace and rejects every changed path outside the
        child's write_roots as an unauthorized write. The tombstone writes
        `<orch_root>/reopen/{superseded_runs.json,reopen_log.jsonl}`, and — unlike `launches/`,
        `agents/` and `violations/` — those are NOT runtime-ignored. Tombstoning while the window
        is still open would therefore charge the conductor's own two writes to the dying leaf: the
        attempt is rejected, finalize-child exits nonzero, `runtime()` raises, and the retry never
        launches. The retry loop is the only tombstone caller that runs mid-window, so the
        ordering — not an ignore rule — is what keeps it out of the diff."""
        from tools.orchestration_runtime import _should_ignore_runtime_snapshot_path as ignored
        for path in ("workspace/orchestrations/orch_x/reopen/superseded_runs.json",
                     "workspace/orchestrations/orch_x/reopen/reopen_log.jsonl"):
            self.assertFalse(
                ignored(path, orchestration_id="orch_x", agent_run_id="child-1"),
                f"{path} is visible in a child's FS diff — the tombstone must not run inside "
                f"a child's write window")
        # ...and the loop honours that: no tombstone is issued between a record-launch and its
        # matching finalize-child.
        c = self._conductor([self._flake(), wc.ProcResult(0, "done", "")])
        with redirect_stdout(io.StringIO()):
            c.run_substep(self._refs(), "compile", "verify")
        open_window = False
        for sub, _cap in c.calls:
            if sub == "record-launch":
                open_window = True
            elif sub == "finalize-child":
                open_window = False
            elif sub == "add-superseded-runs":
                self.assertFalse(open_window, "tombstoned inside an open child write window")

    def test_retried_judge_cannot_certify_the_dead_attempts_semantic_review(self) -> None:
        """The retry must not let a leaf that NEVER COMPLETED certify the node.

        The run dir is not rotated between attempts of one phase, so a judge that authored
        `semantic_review.json` with `decision: "pass"` and only THEN died on a dropped connection
        leaves a complete, passing artifact behind. The retry (a cold leaf, same run dir) can find
        it, rewrite nothing and exit 0 — and the phase would certify on an artifact authored by a
        tombstoned attempt and vouch it to an arid that never wrote it. Freshness (`min_mtime`) is
        what forbids that, and `validate.judge` was the one LLM substep not gated on it."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            rundir = repo / refs.run_node_dir()
            rundir.mkdir(parents=True, exist_ok=True)

            class _C(self._C):
                # the REAL freshness-aware status resolver, not the fake's blanket "pass"
                def determine_substep_status(self, refs, phase, substep, allowed,
                                             min_mtime=0.0):  # type: ignore[override]
                    return wc.Conductor.determine_substep_status(
                        self, refs, phase, substep, allowed, min_mtime=min_mtime)

                def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
                    self.spawns.append(dict(kwargs))
                    if len(self.spawns) == 1:
                        # attempt 1: author a PASSING review, then die of a transport fault
                        (rundir / "semantic_review.json").write_text(
                            json.dumps({"decision": "pass", "findings": []}), encoding="utf-8")
                        return wc.ProcResult(1, LeafTransientRetryTest._FLAKE, "")
                    # attempt 2: a cold leaf finds the file already there and writes nothing
                    return wc.ProcResult(0, "nothing to do", "")

            c = _C(repo_root=repo, orchestration_id="orch_x",
                   orchestration_agent_run_id="ORCH", backend="claude", env={})
            c.calls, c.procs, c.slept, c.spawns = [], [], [], []
            with redirect_stdout(io.StringIO()):
                oc = c.run_substep(refs, "validate", "judge")
            self.assertEqual(len(c.spawns), 2)          # it did retry...
            self.assertEqual(oc.status, "fail")         # ...but the stale artifact cannot pass
            self.assertEqual(oc.attempts, 2)
            # sanity: the same review REWRITTEN inside the retry's window does pass, so the gate
            # is freshness and not the file's content
            def _reauthoring_judge(prompt_text, child_env, **kwargs):
                (rundir / "semantic_review.json").write_text(
                    json.dumps({"decision": "pass", "findings": []}), encoding="utf-8")
                return wc.ProcResult(0, "re-authored", "")

            c2 = _C(repo_root=repo, orchestration_id="orch_x",
                    orchestration_agent_run_id="ORCH", backend="claude", env={})
            c2.calls, c2.procs, c2.slept, c2.spawns = [], [], [], []
            c2.spawn_leaf = _reauthoring_judge  # type: ignore[assignment]
            self.assertEqual(c2.run_substep(refs, "validate", "judge").status, "pass")

    def test_transient_retry_persists_every_attempts_leaf_output(self) -> None:
        """The dead attempt's stdout is the ONLY evidence of what killed it — the diagnosis of the
        E2E #4 incident came from exactly this file. A retry must not overwrite it (each attempt
        has its own arid, hence its own dialogs dir)."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor([self._flake(), wc.ProcResult(0, "done", "")], repo=repo)
            with redirect_stdout(io.StringIO()):
                oc = c.run_substep(self._refs(), "compile", "verify")
            self.assertEqual(oc.status, "pass")
            agents = repo / "workspace" / "orchestrations" / "orch_x" / "agents"
            dead = (agents / "child-1" / "dialogs" / "leaf.stdout.log").read_text()
            live = (agents / "child-2" / "dialogs" / "leaf.stdout.log").read_text()
            self.assertEqual(dead, self._FLAKE)
            self.assertEqual(live, "done")


class TransportSubstepResumeTest(unittest.TestCase):
    """Item C2: a transport-substep resume preseats the surviving producer as outcomes[0] and
    relaunches only the deterministic mids + verify — instead of re-paying the billed producer
    leaf. The consumer arms it defensively (any precondition miss declines to a full re-run)."""

    NODE_KEY = "component/spec_x@0.1.0"

    class _C(_FakeConductor):
        procs: list = []
        spawns: list = []

        def _write_lineage(self, refs):  # type: ignore[override]
            return []

        def _write_dependency_graph(self, refs):  # type: ignore[override]
            return None

        def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
            self.spawns.append(dict(kwargs))
            idx = len(self.spawns) - 1
            return self.procs[min(idx, len(self.procs) - 1)]

    def _conductor(self, procs: list, repo: Path, **kw) -> "_C":
        c = self._C(repo_root=repo, orchestration_id="orch_x",
                    orchestration_agent_run_id="ORCH", backend="claude", env={}, **kw)
        c.calls, c.procs, c.spawns = [], procs, []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key=self.NODE_KEY, spec_path="spec/component/spec_x",
            ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
            binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001")

    def _seed_row(self, repo: Path, arid: str, step: str, substep: str,
                  status: str = "pass", **extra) -> None:
        root = repo / "workspace" / "orchestrations" / "orch_x"
        root.mkdir(parents=True, exist_ok=True)
        with (root / "agent_runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "agent_run_id": arid, "agent_role": "substep", "step": step,
                "substep": substep, "status": status, "node_key": self.NODE_KEY, **extra,
            }) + "\n")

    def _capture_events(self, c) -> list:
        events: list = []
        c.emit = lambda ev, **f: events.append((ev, f))  # type: ignore[assignment]
        return events

    def test_transport_resume_preseats_producer_and_skips_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # The surviving ir dir exists → a normal run would ROTATE the producer id
            # (_ensure_fresh_producer_id calls reserve-phase-root); preseat suppresses that.
            ir_dir = repo / "workspace" / "ir" / wc.node_key_safe(self.NODE_KEY) / "x_1_001"
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text("case: {}\n", encoding="utf-8")
            c = self._conductor([wc.ProcResult(0, "ok", "")], repo=repo)
            c._substep_resume = {"compile": {"producer_arid": "run1-producer",
                                             "artifact_id": "x_1_001"}}
            refs = self._refs()
            events = self._capture_events(c)
            oc = c.run_phase(refs, "compile")

            self.assertEqual(oc.status, "pass")
            self.assertEqual(oc.decision.action, "advance")
            self.assertEqual(refs.ir_id, "x_1_001")  # NOT rotated
            self.assertNotIn("reserve-phase-root", [s for s, _ in c.calls])  # rotation suppressed
            # The producer leaf is NOT relaunched: record-launch fires only for the deterministic
            # static substep and the verify leaf; only verify actually spawns a `claude -p`.
            launched = [cap["--request-json"]["agent_run_id"]
                        for s, cap in c.calls if s == "record-launch"]
            self.assertEqual(launched, ["child-1", "child-2"])
            self.assertEqual(len(c.spawns), 1)
            sr = next(cap["--result-json"] for s, cap in c.calls if s == "write-step-result")
            # step_result spans run-1 producer + the run-2 mids/verify (validator-clean:
            # the superseded producer is vouch-exempt, the fresh rows satisfy the replacement rule).
            self.assertEqual(sr["substep_agent_run_ids"],
                             ["run1-producer", "child-1", "child-2"])
            self.assertEqual(c._producer_arid["compile"], "run1-producer")
            resumed = [f for e, f in events if e == "substep_resumed"]
            self.assertEqual(len(resumed), 1)
            self.assertEqual(resumed[0]["agent_run_id"], "run1-producer")

    def test_consumer_then_run_phase_end_to_end_skips_producer(self) -> None:
        """The full seam: arm the preseat through the REAL consumer (from an agent_runs producer
        pass row + surviving ir dir), then run the phase — the producer is not relaunched and the
        step_result vouches the run-1 producer + the fresh re-run substeps."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._seed_row(repo, "cmp-prod", "compile", "generate")
            ir_dir = repo / "workspace" / "ir" / wc.node_key_safe(self.NODE_KEY) / "x_1_001"
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text("case: {}\n", encoding="utf-8")
            c = self._conductor([wc.ProcResult(0, "ok", "")], repo=repo)
            refs = self._refs()
            with redirect_stdout(io.StringIO()):
                c._consume_transport_resume_directive(
                    refs, ["compile", "generate", "build", "validate"], self._directive())
                self.assertEqual(c._substep_resume["compile"],
                                 {"producer_arid": "cmp-prod", "artifact_id": "x_1_001"})
                oc = c.run_phase(refs, "compile")
            self.assertEqual(oc.status, "pass")
            launched = [cap["--request-json"]["agent_run_id"]
                        for s, cap in c.calls if s == "record-launch"]
            self.assertEqual(launched, ["child-1", "child-2"])  # producer NOT relaunched
            sr = next(cap["--result-json"] for s, cap in c.calls if s == "write-step-result")
            self.assertEqual(sr["substep_agent_run_ids"],
                             ["cmp-prod", "child-1", "child-2"])
            self.assertEqual(c._producer_arid["compile"], "cmp-prod")

    def test_transport_resume_consumer_repoints_generate_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor([], repo=repo)
            self._seed_row(repo, "gen-prod", "generate", "generate")
            refs = self._refs()
            (repo / refs.source_dir("src_recovered") / "src").mkdir(parents=True, exist_ok=True)
            events = self._capture_events(c)
            c._consume_transport_resume_directive(
                refs, ["compile", "generate", "build", "validate"], {
                    "source": wc_runtime.LEAF_TRANSPORT_RESUME_SOURCE, "node_key": self.NODE_KEY,
                    "step": "generate", "resume_substep": "verify",
                    "producer_agent_run_id": "gen-prod", "producer_artifact_id": "src_recovered"})
            self.assertEqual(refs.source_id, "src_recovered")  # day-boundary default corrected
            self.assertEqual(c._substep_resume["generate"],
                             {"producer_arid": "gen-prod", "artifact_id": "src_recovered"})
            self.assertIn("transport_substep_resume", [e for e, _ in events])

    def _directive(self, **over) -> dict:
        d = {"source": wc_runtime.LEAF_TRANSPORT_RESUME_SOURCE, "node_key": self.NODE_KEY,
             "step": "compile", "resume_substep": "verify",
             "producer_agent_run_id": "cmp-prod", "producer_artifact_id": "x_1_001"}
        d.update(over)
        return d

    def test_transport_resume_consumer_declines_gracefully(self) -> None:
        phases = ["compile", "generate", "build", "validate"]
        # (setup, directive-overrides, expected decline reason)
        cases = [
            ("node_mismatch", {"node_key": "component/other@0.1.0"}, "node_key_mismatch"),
            ("bad_step", {"step": "validate"}, "step_out_of_scope"),
            ("not_verify", {"resume_substep": "static"}, "not_verify_resume"),
            ("incomplete", {"producer_agent_run_id": ""}, "incomplete_directive"),
            ("no_producer_row", {}, "producer_row_absent"),
        ]
        for label, over, reason in cases:
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                c = self._conductor([], repo=repo)
                refs = self._refs()
                if label != "no_producer_row":
                    # a valid producer row + artifact so ONLY the intended check fails
                    self._seed_row(repo, "cmp-prod", "compile", "generate")
                    ir = repo / "workspace" / "ir" / wc.node_key_safe(self.NODE_KEY) / "x_1_001"
                    ir.mkdir(parents=True, exist_ok=True)
                    (ir / "spec.ir.yaml").write_text("case: {}\n", encoding="utf-8")
                events = self._capture_events(c)
                c._consume_transport_resume_directive(refs, phases, self._directive(**over))
                self.assertFalse(getattr(c, "_substep_resume", {}), f"{label} must not arm")
                declines = [f["reason"] for e, f in events if e == "transport_resume_declined"]
                self.assertEqual(declines, [reason], f"{label}")

    def test_transport_resume_consumer_requires_pure_bundle(self) -> None:
        """A PURE generate.verify reviewer's input is the producer's codegen_bundle.json; reusing a
        source dir whose bundle is gone would certify blind (empty bundle_document + the re-run
        post_generate gate skips a missing bundle). The consumer must decline when only src/ survives
        and arm once the bundle is present."""
        directive = {"source": wc_runtime.LEAF_TRANSPORT_RESUME_SOURCE, "node_key": self.NODE_KEY,
                     "step": "generate", "resume_substep": "verify",
                     "producer_agent_run_id": "gen-prod", "producer_artifact_id": "src_x"}
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor([], repo=repo)
            c._pure_leaf_substep = lambda refs, phase, substep: True  # force the pure branch
            self._seed_row(repo, "gen-prod", "generate", "generate")
            refs = self._refs()
            src_root = repo / refs.source_dir("src_x")
            (src_root / "src").mkdir(parents=True, exist_ok=True)  # src/ present, bundle absent
            events = self._capture_events(c)
            c._consume_transport_resume_directive(refs, ["compile", "generate"], directive)
            self.assertFalse(getattr(c, "_substep_resume", {}))
            self.assertEqual([f["reason"] for e, f in events
                              if e == "transport_resume_declined"], ["artifact_dir_missing"])
            self.assertEqual(refs.source_id, "src_1_001")  # ref NOT mutated on decline
            # With the canonical bundle present, the same directive arms.
            (src_root / "codegen_bundle.json").write_text("{}", encoding="utf-8")
            self._capture_events(c)
            c._consume_transport_resume_directive(refs, ["compile", "generate"], directive)
            self.assertEqual(c._substep_resume["generate"],
                             {"producer_arid": "gen-prod", "artifact_id": "src_x"})

    def test_transport_resume_consumer_declines_when_phase_already_complete(self) -> None:
        """A phase already checkpointed complete is skipped by run_phase, so preseating it would be
        wrong — the consumer declines when check_step_completed reports it done."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor([], repo=repo)
            self._seed_row(repo, "cmp-prod", "compile", "generate")
            ir = repo / "workspace" / "ir" / wc.node_key_safe(self.NODE_KEY) / "x_1_001"
            ir.mkdir(parents=True, exist_ok=True)
            (ir / "spec.ir.yaml").write_text("case: {}\n", encoding="utf-8")
            c.check_step_completed = lambda nk, st: {"integrity": "ok"}  # type: ignore[assignment]
            refs = self._refs()
            events = self._capture_events(c)
            c._consume_transport_resume_directive(
                refs, ["compile", "generate"], self._directive())
            self.assertFalse(getattr(c, "_substep_resume", {}))
            self.assertEqual([f["reason"] for e, f in events
                              if e == "transport_resume_declined"], ["phase_already_complete"])

    def test_transport_resume_consumer_declines_when_artifact_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._conductor([], repo=repo)
            self._seed_row(repo, "cmp-prod", "compile", "generate")  # row exists, no ir dir
            refs = self._refs()
            events = self._capture_events(c)
            c._consume_transport_resume_directive(
                refs, ["compile", "generate"], self._directive())
            self.assertFalse(getattr(c, "_substep_resume", {}))
            self.assertEqual([f["reason"] for e, f in events
                              if e == "transport_resume_declined"], ["artifact_dir_missing"])

    def test_second_transport_death_after_preseat_retombstones_idempotently(self) -> None:
        """A C-resumed verify that transport-dies AGAIN: run_phase's transport branch supersedes
        every outcome arid — including the already-superseded run-1 producer — which is a no-op
        set-union, not an error, and the next derive re-fires."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            ir_dir = repo / "workspace" / "ir" / wc.node_key_safe(self.NODE_KEY) / "x_1_001"
            ir_dir.mkdir(parents=True, exist_ok=True)
            (ir_dir / "spec.ir.yaml").write_text("case: {}\n", encoding="utf-8")
            # verify leaf dies of transport (nonzero exit) — static passed in-process first.
            c = self._conductor([wc.ProcResult(1, "", "Claude AI usage limit reached")], repo=repo)
            c._substep_resume = {"compile": {"producer_arid": "run1-producer",
                                             "artifact_id": "x_1_001"}}
            with redirect_stdout(io.StringIO()):
                oc = c.run_phase(self._refs(), "compile")
            self.assertEqual(oc.decision.action, "fail_closed")
            self.assertTrue(oc.decision.reason.startswith("leaf_transport_error"))
            tombstoned = [rid for s, cap in c.calls if s == "add-superseded-runs"
                          for rid in cap["--run-ids"]]
            # the preseated run-1 producer is re-superseded alongside the fresh static+verify.
            self.assertIn("run1-producer", tombstoned)
            self.assertNotIn("write-step-result", [s for s, _ in c.calls])

    def test_transport_resume_is_mode_independent(self) -> None:
        for mode in ("dev", "prod"):
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                c = self._conductor([], repo=repo, workflow_mode=mode)
                self._seed_row(repo, "cmp-prod", "compile", "generate")
                ir = repo / "workspace" / "ir" / wc.node_key_safe(self.NODE_KEY) / "x_1_001"
                ir.mkdir(parents=True, exist_ok=True)
                (ir / "spec.ir.yaml").write_text("case: {}\n", encoding="utf-8")
                refs = self._refs()
                with redirect_stdout(io.StringIO()):
                    c._consume_transport_resume_directive(
                        refs, ["compile", "generate"], self._directive())
                self.assertIn("compile", getattr(c, "_substep_resume", {}), mode)


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

    def test_resolve_node_rejects_overlong_spec_id(self) -> None:
        # M3d spec-input gate: a spec_id over MAX_SPEC_ID_LEN is rejected before the
        # catalog lookup (the length check precedes _read_yaml), so this needs no real
        # spec on disk — the message names spec-input and the char count.
        from tools.runner_renderer import MAX_SPEC_ID_LEN
        overlong = "component/dynamics/" + "z" * (MAX_SPEC_ID_LEN + 3)
        with self.assertRaises(ValueError) as ctx:
            wc.resolve_node(REPO_ROOT, "spec/" + overlong)
        self.assertIn("spec-input rejected", str(ctx.exception))
        self.assertIn(str(MAX_SPEC_ID_LEN + 3), str(ctx.exception))

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

    def test_diagnosis_prompt_keeps_every_artifact_under_a_large_context(self) -> None:
        """Regression (E2E #4 run 1): the prompt truncated the whole context dump at 6000 chars,
        which silently dropped whichever artifacts sorted last — including source_meta.json, the
        primary evidence of the failed generate phase. The diagnostician then reported
        "insufficient evidence" and fail-closed a leaf whose decline was correct. Every artifact
        must survive, bounded individually."""
        # ir_meta alone busts the old 6000-char budget, as it did in the real run.
        context = {
            "source_meta.json": {"decline_reason": "lake_at_rest cannot be satisfied",
                                 "evidence": "max_velocity 3.2e-2 with a non-well-balanced scheme"},
            "ir_meta.json": {"filler": ["x" * 200 for _ in range(60)]},
            "verdict.json": {"overall": "fail"},
        }
        prompt = wc._diagnosis_prompt("n", "generate", [], context, "dev")
        for name in context:
            self.assertIn(name, prompt, f"{name} must appear in the diagnosis prompt")
        self.assertIn("lake_at_rest cannot be satisfied", prompt)
        self.assertIn("[truncated]", prompt)  # the oversized artifact is marked, not dropped

    def test_bounded_context_json_is_valid_json_and_bounded(self) -> None:
        big = {"k": "y" * 50000}
        out = wc._bounded_context_json({"a.json": big, "b.json": {"small": 1}})
        parsed = json.loads(out)  # a truncated artifact must not corrupt the JSON
        self.assertEqual(sorted(parsed), ["a.json", "b.json"])
        self.assertEqual(parsed["b.json"], {"small": 1})  # under budget -> kept verbatim
        self.assertIsInstance(parsed["a.json"], str)      # over budget -> truncated string
        self.assertTrue(parsed["a.json"].endswith("[truncated]"))
        self.assertEqual(wc._bounded_context_json({}), "{}")

    def test_bounded_context_json_shrinks_the_allowance_as_artifacts_multiply(self) -> None:
        """Many artifacts share the total budget, but each keeps at least the floor — nothing is
        dropped no matter how many compete. The floor, like the budget, is denominated in SERIALIZED
        characters: that is what the prompt actually pays for a truncated artifact."""
        context = {f"a{i}.json": {"k": "z" * 20000} for i in range(20)}
        parsed = json.loads(wc._bounded_context_json(context))
        self.assertEqual(len(parsed), 20)
        for name, body in parsed.items():
            self.assertTrue(body.endswith("[truncated]"), name)
            # The search takes the largest prefix that FITS, so it lands within a char or two of
            # the floor rather than exactly on it.
            self.assertGreaterEqual(
                wc._serialized_len(body), wc._MIN_ARTIFACT_BUDGET - 2, name)

    def test_bounded_context_json_never_starves_a_lone_artifact(self) -> None:
        """A per-artifact cap that ignores how FEW artifacts there are would hand the diagnostician
        less evidence than the flat 6000-char dump it replaced — a regression dressed as a fix. A
        compile failure whose only artifact is an 11k ir_meta is exactly that case."""
        big = {"k": "y" * 50000}
        lone = json.loads(wc._bounded_context_json({"ir_meta.json": big}))["ir_meta.json"]
        self.assertGreaterEqual(wc._serialized_len(lone), 6000 - 2)

    def test_bounded_context_json_spends_the_budget_in_order(self) -> None:
        """Ordering only means something if the budget is spent greedily in order. The failed
        phase's own artifact leads, so it must get the LARGE slice while the trailing ones keep the
        floor — otherwise `_gather_failure_context`'s reordering is decorative and the primary
        evidence is truncated just as hard as the noise behind it."""
        oversized = {"k": "y" * 50000}
        parsed = json.loads(wc._bounded_context_json(
            {f"a{i}.json": dict(oversized) for i in range(6)}))
        costs = [wc._serialized_len(parsed[f"a{i}.json"]) for i in range(6)]
        self.assertGreater(costs[0], costs[-1],
                           f"the leading artifact must win the budget; got {costs}")
        self.assertGreaterEqual(min(costs), wc._MIN_ARTIFACT_BUDGET - 2)

    def test_bounded_context_json_holds_the_total_budget_against_escaping(self) -> None:
        """A truncated artifact is carried as a STRING, so the outer `json.dumps` escapes it a
        SECOND time: every quote, backslash and newline doubles. Budgeting against the raw slice
        undercounted quote-dense content by ~2x — 8 such artifacts emitted 22k characters against a
        nominal 12k, quietly inflating the diagnosis prompt. Both truncation and accounting must
        therefore work in serialized characters."""
        dense = {f"a{i}.json": {"k": '"\\\n' * 4000} for i in range(8)}
        out = wc._bounded_context_json(dense)
        parsed = json.loads(out)               # escaping must not corrupt the JSON
        self.assertEqual(len(parsed), 8)       # ...and must not drop an artifact either
        plain = wc._bounded_context_json({f"a{i}.json": {"k": "y" * 20000} for i in range(8)})
        # Escape-dense content must cost no more than plain content of the same budget; the only
        # excess over `total` is the wrapper (keys, braces, indent), which is identical for both.
        self.assertLessEqual(len(out), len(plain) + 64,
                             f"escaping blew the budget: {len(out)} vs plain {len(plain)}")
        self.assertLess(len(out), 13000, f"emitted {len(out)} chars against a 12000 budget")

    def test_gather_failure_context_leads_with_the_failed_phase_artifacts(self) -> None:
        """The per-artifact budget is applied in insertion order, so the failed phase's own
        evidence must come first — a generate failure leads with source_meta, not with the 11k
        ir_meta that merely happens to be enumerated earlier."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            for rel, doc in (
                (f"{refs.ir_ref}/ir_meta.json", {"a": 1}),
                (f"{refs.source_dir()}/source_meta.json", {"b": 2}),
                (f"{refs.run_node_dir()}/verdict.json", {"c": 3}),
            ):
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(doc), encoding="utf-8")
            c = _FakeConductor(repo_root=repo, orchestration_id="o",
                               orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertEqual(list(c._gather_failure_context(refs, "generate"))[0],
                             "source_meta.json")
            self.assertEqual(list(c._gather_failure_context(refs, "compile"))[0], "ir_meta.json")
            self.assertEqual(list(c._gather_failure_context(refs, "validate"))[0], "verdict.json")

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

    def test_run_substep_reuse_resume_always_resumes_producer(self) -> None:
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
                "  subroutine base__scale(x, n, y)\n"
                "    integer, intent(in) :: n\n"
                "    real(8), intent(in) :: x(n)\n"
                "    real(8), intent(out) :: y(n)\n"
                "  end subroutine\n"
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
            # Per-argument type/rank/intent is surfaced and persisted into lineage.
            self.assertEqual(pub[0]["arguments"], [
                {"name": "x", "type": "real(8)", "intent": "in",
                 "rank": 1, "dimension": "n"},
                {"name": "n", "type": "integer", "intent": "in",
                 "rank": 0, "dimension": None},
                {"name": "y", "type": "real(8)", "intent": "out",
                 "rank": 1, "dimension": "n"},
            ])
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
        # generate.gate is deterministic too: no resolved_dependencies / skill, and the
        # deterministic flag is set with gate-only allowed_output_paths (gate_meta.json plus the
        # canonical command_log.jsonl the lint/syntax checkers append to).
        gate_req = self._build("generate", "gate", (self.DEP,))
        self.assertNotIn("resolved_dependencies", gate_req)
        self.assertNotIn("skill_name", gate_req)
        self.assertTrue(gate_req["deterministic"])
        outs = gate_req["allowed_output_paths"]
        self.assertTrue(any(p.endswith("/gate_meta.json") for p in outs))
        self.assertTrue(any(p.endswith("/src/command_log.jsonl") for p in outs))
        # gate does not author model/runner sources
        self.assertFalse(any(p.endswith("_model.f90") for p in outs))
        # no retired per-checker meta names are emitted
        self.assertFalse(any(p.endswith(("/lint_meta.json", "/syntax_meta.json",
                                         "/static_meta.json")) for p in outs))

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


class WriteRunnerTest(unittest.TestCase):
    """R1/M3c-β: the conductor host-renders `<spec_id>_runner.f90` for an M3c physics node
    (make+fortran, non-infra, exactly one infrastructure/harness dep), and the Makefile
    compiles the leaf-authored `<spec_id>_checks.f90` between model and runner."""

    SID = "boundary_x"

    def _conductor(self, repo: Path) -> _FakeConductor:
        c = _FakeConductor(repo_root=repo, orchestration_id="o",
                           orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        return c

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key=f"component/{self.SID}@0.1.0", spec_path=f"spec/component/{self.SID}",
            ir_id="i1", pipeline_id="p1", source_id="s1", binary_id="b1")

    def _write_consumer_ir(self, repo: Path, refs: wc.NodeRefs, *, infra=1,
                           bare_string: bool = False) -> None:
        from tools.tests.test_runner_renderer import _boundary_ir
        import yaml as _yaml
        ir = _boundary_ir()
        ids = ["harness_fortran_cpu"] * infra
        if infra == 2:
            ids = ["harness_fortran_cpu", "harness_other_cpu"]
        if bare_string:  # deps.yaml also permits a bare `infrastructure/<id>@ver` string
            deps: list = [f"infrastructure/{i}@0.2.0" for i in ids]
        else:
            deps = [{"node_key": f"infrastructure/{i}@0.2.0"} for i in ids]
        ir["dependency"]["direct_deps"] = deps
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "spec.ir.yaml").write_text(_yaml.safe_dump(ir), encoding="utf-8")

    def _seed_harness_pipeline(
            self, repo: Path, *, tamper_source: bool = False,
            ir_dirname: str = "harness-fortran-cpu_20260707_002",
            ir_meta_status: str | None = "pass", write_ir_meta: bool = True,
            extra_source_meta: dict | None = None, signatures: object = None,
            no_public_api_signatures: bool = False,
            binary_source_ir_id: str | None = "harness-fortran-cpu_20260707_002",
            write_source_ir_id: bool = True) -> None:
        """Seed a ready certified-harness pipeline + its certified IR dir.

        `source_meta.json` is written CONTRACT-MINIMAL (no `ir_ref`): the pin resolves the IR
        structurally, never from source_meta. `binary_meta.source_ir_id` (host-authored) binds the
        certified binary to its origin IR dir under `workspace/ir/<safe>/<binary_source_ir_id>`;
        `write_source_ir_id=False` omits it to exercise the legacy `_certified_ir_dir` fallback.
        `ir_dirname` (the seeded IR dir) may diverge from the pipeline dir name (compile reopen
        re-numbers ir_id independently). `extra_source_meta` merges extra keys into the (still
        ir_ref-free) source_meta to prove they are ignored."""
        from tools.tests.test_runner_renderer import (
            _HARNESS_STUB, _harness_signatures)
        import yaml as _yaml
        safe = "infrastructure__harness_fortran_cpu__0.2.0"
        pipe = repo / "workspace" / "pipelines" / safe / "harness-fortran-cpu_20260707_002"
        src_dir = pipe / "source" / "src_20260707_002" / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        source = _HARNESS_STUB
        if tamper_source:
            source = source.replace(
                "function harness_fortran_cpu__box(name, json) result(nv)",
                "function harness_fortran_cpu__box(key, json) result(nv)")
        (src_dir / "harness_fortran_cpu_model.f90").write_text(source, encoding="utf-8")
        smeta: dict = dict(extra_source_meta or {})  # contract-minimal: NO ir_ref
        (src_dir.parent / "source_meta.json").write_text(
            json.dumps(smeta), encoding="utf-8")
        (pipe / "binary" / "bin_20260707_001").mkdir(parents=True, exist_ok=True)
        bmeta: dict = {"source_source_id": "src_20260707_002"}
        if write_source_ir_id and binary_source_ir_id is not None:
            bmeta["source_ir_id"] = binary_source_ir_id
        (pipe / "binary" / "bin_20260707_001" / "binary_meta.json").write_text(
            json.dumps(bmeta), encoding="utf-8")
        hir_dir = repo / "workspace" / "ir" / safe / ir_dirname
        hir_dir.mkdir(parents=True, exist_ok=True)
        sigs = _harness_signatures() if signatures is None else signatures
        pub = {} if no_public_api_signatures else {"signatures": sigs}
        (hir_dir / "spec.ir.yaml").write_text(
            _yaml.safe_dump({"public_api": pub}), encoding="utf-8")
        if write_ir_meta:
            (hir_dir / "ir_meta.json").write_text(
                json.dumps({"verification_status": ir_meta_status}), encoding="utf-8")

    def test_conductor_authors_runner_truth_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            c = self._conductor(repo)
            self._write_consumer_ir(repo, refs, infra=1)
            self.assertTrue(c._conductor_authors_runner(refs))
            # zero infra deps -> legacy path (leaf-authored runner)
            self._write_consumer_ir(repo, refs, infra=0)
            self.assertFalse(c._conductor_authors_runner(refs))
            # two infra deps -> not M3c
            self._write_consumer_ir(repo, refs, infra=2)
            self.assertFalse(c._conductor_authors_runner(refs))

    def test_conductor_authors_runner_bare_string_dep_parity(self) -> None:
        # deps.yaml permits a bare-string infra dep as well as the dict form; the conductor
        # predicate must count it identically to the gate (a divergence = host-render vs
        # checks-gate skew = fail-open). Locks the prior HIGH parity bug's fix.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            c = self._conductor(repo)
            self._write_consumer_ir(repo, refs, infra=1, bare_string=True)
            self.assertTrue(c._conductor_authors_runner(refs))
            self._write_consumer_ir(repo, refs, infra=2, bare_string=True)
            self.assertFalse(c._conductor_authors_runner(refs))

    def test_conductor_authors_runner_false_for_infra_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            from tools.tests.test_runner_renderer import _boundary_ir
            import yaml as _yaml
            ir = _boundary_ir()
            ir["meta"]["spec_kind"] = "infrastructure"
            ir["dependency"]["direct_deps"] = [
                {"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.ir_ref / "spec.ir.yaml").write_text(_yaml.safe_dump(ir))
            self.assertFalse(self._conductor(repo)._conductor_authors_runner(refs))

    def test_write_runner_renders_and_pins(self) -> None:
        from tools.runner_renderer import render_runner
        from tools.tests.test_runner_renderer import _boundary_ir
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(repo)
            c = self._conductor(repo)
            c._write_runner(refs)
            runner = repo / refs.source_dir() / "src" / f"{self.SID}_runner.f90"
            self.assertTrue(runner.is_file())
            # host-rendered output == render_runner(ir, spec_id, harness) exactly
            ir = _boundary_ir()
            ir["dependency"]["direct_deps"] = [
                {"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]
            expected = render_runner(ir, self.SID, "harness_fortran_cpu")
            self.assertEqual(runner.read_text(encoding="utf-8"), expected)

    def test_write_runner_fail_closed_on_missing_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            # no harness pipeline seeded -> build precondition failure
            with self.assertRaises(RuntimeError):
                self._conductor(repo)._write_runner(refs)

    def test_write_runner_fail_closed_on_pin_drift(self) -> None:
        from tools.runner_renderer import RenderError
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(repo, tamper_source=True)
            with self.assertRaises(RenderError):
                self._conductor(repo)._write_runner(refs)

    def test_write_runner_pins_without_source_meta_ir_ref(self) -> None:
        # Regression for E2E #4: a contract-minimal source_meta.json (no `ir_ref`, as a
        # harness-0.3.0 leaf writes) must still resolve the certified IR structurally and pin.
        from tools.runner_renderer import render_runner
        from tools.tests.test_runner_renderer import _boundary_ir
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(repo)  # source_meta has NO ir_ref
            self._conductor(repo)._write_runner(refs)
            runner = repo / refs.source_dir() / "src" / f"{self.SID}_runner.f90"
            ir = _boundary_ir()
            ir["dependency"]["direct_deps"] = [
                {"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]
            self.assertEqual(runner.read_text(encoding="utf-8"),
                             render_runner(ir, self.SID, "harness_fortran_cpu"))

    def test_write_runner_ignores_source_meta_ir_ref(self) -> None:
        # Even a present-but-bogus `ir_ref` must be entirely disregarded: the field is no
        # longer read, so a dangling path cannot break (or steer) resolution.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(
                repo, extra_source_meta={"ir_ref": "workspace/ir/does/not/exist"})
            self._conductor(repo)._write_runner(refs)  # renders successfully regardless
            self.assertTrue(
                (repo / refs.source_dir() / "src" / f"{self.SID}_runner.f90").is_file())

    def test_write_runner_fail_closed_on_uncertified_harness_ir(self) -> None:
        # ir_meta absent or verification_status != pass -> transport fail_closed (RuntimeError),
        # NOT a RenderError (this is a build precondition, not interface drift).
        from tools.runner_renderer import RenderError
        for kwargs in ({"ir_meta_status": "fail"}, {"write_ir_meta": False}):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                refs = self._refs()
                self._write_consumer_ir(repo, refs, infra=1)
                self._seed_harness_pipeline(repo, **kwargs)
                with self.assertRaises(RuntimeError) as cm:
                    self._conductor(repo)._write_runner(refs)
                self.assertNotIsInstance(cm.exception, RenderError)
                self.assertIn("--with-deps", str(cm.exception))

    def test_write_runner_resolves_ir_divergent_from_pipeline_id(self) -> None:
        # Compile reopen re-numbers ir_id independently of pipeline_id: the certified IR dir
        # (`_003`, bound by binary_meta.source_ir_id) need not match the pipeline dir name.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(
                repo, ir_dirname="harness-fortran-cpu_20260707_003",
                binary_source_ir_id="harness-fortran-cpu_20260707_003")
            self._conductor(repo)._write_runner(refs)
            self.assertTrue(
                (repo / refs.source_dir() / "src" / f"{self.SID}_runner.f90").is_file())

    def test_write_runner_pins_ir_bound_to_binary_not_latest(self) -> None:
        # Codex P1 regression: after a same-version compile reopen the globally-LATEST passing IR
        # can diverge from the IR the certified binary's source was built from. The pin must bind
        # to binary_meta.source_ir_id (`_002`), NOT the newer `_004` whose TAMPERED signatures
        # would raise a spurious drift even though source+binary are internally consistent.
        import yaml as _yaml
        from tools.tests.test_runner_renderer import _harness_signatures
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            # binary bound to _002 (default ir_dirname + default binary_source_ir_id).
            self._seed_harness_pipeline(repo)
            # A NEWER passing IR (_004) with corrupt signatures — latest-passing selection would
            # wrongly pick it; the source_ir_id binding must ignore it.
            safe = "infrastructure__harness_fortran_cpu__0.2.0"
            newer = repo / "workspace" / "ir" / safe / "harness-fortran-cpu_20260707_004"
            newer.mkdir(parents=True, exist_ok=True)
            bad_sigs = copy.deepcopy(_harness_signatures())
            bad_sigs[0]["signature"]["name"] = "bogus"
            (newer / "spec.ir.yaml").write_text(
                _yaml.safe_dump({"public_api": {"signatures": bad_sigs}}), encoding="utf-8")
            (newer / "ir_meta.json").write_text(
                json.dumps({"verification_status": "pass"}), encoding="utf-8")
            self._conductor(repo)._write_runner(refs)  # binds to _002 -> renders, no false drift
            self.assertTrue(
                (repo / refs.source_dir() / "src" / f"{self.SID}_runner.f90").is_file())

    def test_write_runner_falls_back_to_latest_ir_without_source_ir_id(self) -> None:
        # A binary predating source_ir_id (legacy / the pending E2E-recovery harness) must still
        # resolve via _certified_ir_dir (latest certified IR), so the pin keeps working.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(repo, write_source_ir_id=False)
            self._conductor(repo)._write_runner(refs)
            self.assertTrue(
                (repo / refs.source_dir() / "src" / f"{self.SID}_runner.f90").is_file())

    def test_write_runner_binds_source_and_ir_from_one_binary_snapshot(self) -> None:
        # TOCTOU guard: the model source (source_source_id) and its provenance (source_ir_id) must
        # come from ONE latest-binary selection, so a binary published between two selections can't
        # pair a source with a mismatched IR lineage. Assert the latest-binary meta is selected
        # exactly once on the bound path (two selections would be the racy pattern).
        from unittest import mock
        from tools import orchestration_runtime as ortime
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(repo)
            real = ortime._latest_meta_under
            binary_selects = {"n": 0}

            def counting(root, pattern):
                if pattern == "binary/*/binary_meta.json":
                    binary_selects["n"] += 1
                return real(root, pattern)

            with mock.patch.object(ortime, "_latest_meta_under", counting):
                self._conductor(repo)._write_runner(refs)
            self.assertEqual(binary_selects["n"], 1)
            self.assertTrue(
                (repo / refs.source_dir() / "src" / f"{self.SID}_runner.f90").is_file())

    def test_write_runner_legacy_fallback_pin_failure_hints_rebuild(self) -> None:
        # On the legacy-fallback path (no source_ir_id) a pin failure must carry the operator
        # hint (rebuild --with-deps to stamp source_ir_id) so a contract-violation-window drift
        # reads as actionable, not a misdiagnosis. Bound (source_ir_id present) failures do not.
        from tools.runner_renderer import RenderError
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            # legacy binary (no source_ir_id) + tampered source -> genuine pin drift.
            self._seed_harness_pipeline(repo, tamper_source=True, write_source_ir_id=False)
            with self.assertRaises(RenderError) as cm:
                self._conductor(repo)._write_runner(refs)
            self.assertIn("source_ir_id", str(cm.exception))
            self.assertIn("--with-deps", str(cm.exception))

    def test_write_runner_bound_pin_failure_has_no_legacy_hint(self) -> None:
        # The reciprocal: with source_ir_id present the pin drift is NOT annotated with the
        # legacy-rebuild hint (it is already bound to the exact origin IR).
        from tools.runner_renderer import RenderError
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(repo, tamper_source=True)  # bound (default source_ir_id)
            with self.assertRaises(RenderError) as cm:
                self._conductor(repo)._write_runner(refs)
            self.assertNotIn("legacy harness binary", str(cm.exception))

    def test_write_runner_fail_closed_on_unresolvable_source_ir_id(self) -> None:
        # A PRESENT-but-unresolvable source_ir_id (dangling dir, or an unsafe token) is corrupt
        # lineage: it must fail closed with RuntimeError, NOT silently fall back to the latest IR
        # (which would reintroduce the false-drift the binding exists to prevent). The valid IR
        # dir (`_002`) is seeded to prove the fallback is NOT taken.
        from tools.runner_renderer import RenderError
        for bad_id in ("harness-fortran-cpu_20260707_999", "../evil"):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                refs = self._refs()
                self._write_consumer_ir(repo, refs, infra=1)
                self._seed_harness_pipeline(repo, binary_source_ir_id=bad_id)
                with self.assertRaises(RuntimeError) as cm:
                    self._conductor(repo)._write_runner(refs)
                self.assertNotIsInstance(cm.exception, RenderError)
                self.assertIn("source_ir_id", str(cm.exception))

    def test_write_runner_present_null_source_ir_id_is_not_legacy_fallback(self) -> None:
        # A PRESENT key with an explicit JSON null is NOT a legacy binary (which lacks the key):
        # it is corrupt lineage and must fail closed, not take the latest-IR fallback. Locks the
        # presence-keyed branch (a value-is-None check would wrongly fall back here).
        from tools.runner_renderer import RenderError
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            self._seed_harness_pipeline(repo)  # valid _002 IR present (fallback would render)
            bpath = (repo / "workspace" / "pipelines"
                     / "infrastructure__harness_fortran_cpu__0.2.0"
                     / "harness-fortran-cpu_20260707_002" / "binary" / "bin_20260707_001"
                     / "binary_meta.json")
            bpath.write_text(
                json.dumps({"source_source_id": "src_20260707_002", "source_ir_id": None}),
                encoding="utf-8")
            with self.assertRaises(RuntimeError) as cm:
                self._conductor(repo)._write_runner(refs)
            self.assertNotIsInstance(cm.exception, RenderError)
            self.assertIn("source_ir_id", str(cm.exception))

    def test_write_runner_no_signatures_in_certified_ir_fails_closed(self) -> None:
        # Certified IR present + pass, but its public_api carries no USABLE signatures
        # (missing list, or a non-empty list of all-malformed entries) -> RuntimeError
        # (re-certify), never a RenderError — so the conductor precondition, not the pin's
        # drift path, classifies an incomplete certified artifact.
        from tools.runner_renderer import RenderError
        for kwargs in ({"no_public_api_signatures": True},
                       {"signatures": [{"garbage": 1}]},
                       {"signatures": [{"symbol": " ", "signature": {}}]}):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                refs = self._refs()
                self._write_consumer_ir(repo, refs, infra=1)
                self._seed_harness_pipeline(repo, **kwargs)
                with self.assertRaises(RuntimeError) as cm:
                    self._conductor(repo)._write_runner(refs)
                self.assertNotIsInstance(cm.exception, RenderError)

    def test_run_phase_routes_render_failure_to_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)  # M3c, but no harness seeded
            c = self._conductor(repo)
            outcome = c.run_phase(refs, "generate")
            self.assertEqual(outcome.status, "fail")
            self.assertEqual(outcome.decision.action, "fail_closed")
            self.assertEqual(outcome.decision.reason, "generate_runner_render_failed")

    def test_makefile_has_checks_rule_for_m3c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=1)
            (repo / refs.ir_ref / "dependency_graph.json").write_text(json.dumps({
                "all_nodes": [
                    {"node_key": f"component/{self.SID}@0.1.0", "topo_level": 1},
                    {"node_key": "infrastructure/harness_fortran_cpu@0.2.0", "topo_level": 0},
                ]}), encoding="utf-8")
            self._conductor(repo)._write_makefile(refs)
            text = (repo / refs.source_dir() / "src" / "Makefile").read_text(encoding="utf-8")
            self.assertIn(f"CHECKS_SRC = {self.SID}_checks.f90", text)
            self.assertIn(f"CHECKS_OBJ = $(OBJDIR)/{self.SID}_checks.o", text)
            self.assertIn("$(CHECKS_OBJ): $(CHECKS_SRC) $(MODEL_OBJ) | $(OBJDIR)", text)
            self.assertIn("$(RUNNER_OBJ): $(RUNNER_SRC) $(CHECKS_OBJ) $(MODEL_OBJ)", text)
            self.assertIn(
                "$(DEP_OBJS) $(MODEL_OBJ) $(CHECKS_OBJ) $(RUNNER_OBJ) -o $(BINDIR)/$(BIN)", text)

    def test_makefile_no_checks_rule_for_legacy_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_consumer_ir(repo, refs, infra=0)  # legacy leaf, no harness dep
            self._conductor(repo)._write_makefile(refs)
            text = (repo / refs.source_dir() / "src" / "Makefile").read_text(encoding="utf-8")
            self.assertNotIn("CHECKS_SRC", text)
            self.assertIn("$(RUNNER_OBJ): $(RUNNER_SRC) $(MODEL_OBJ)", text)

    def test_build_launch_request_swaps_runner_for_checks(self) -> None:
        refs = self._refs()
        # generate.generate authors the sources: on an M3c (runner_host_authored) node the leaf
        # writes <spec_id>_checks.f90 instead of <spec_id>_runner.f90.
        built = wc.build_launch_request(
            refs, step="generate", substep="generate", orchestration_id="o",
            orchestration_agent_run_id="ORCH", child_agent_run_id="c",
            agent_model="m", workflow_mode="prod", runner_host_authored=True)
        outs = built["allowed_output_paths"]
        self.assertIn(f"{refs.source_dir()}/src/{self.SID}_checks.f90", outs)
        self.assertNotIn(f"{refs.source_dir()}/src/{self.SID}_runner.f90", outs)
        self.assertIn(f"{refs.source_dir()}/src/{self.SID}_model.f90", outs)
        # generate.verify writes ONLY source_meta.json — it inspects the sources, never rewrites
        # them — so no runner / checks / model appears in its output set at all.
        ver = wc.build_launch_request(
            refs, step="generate", substep="verify", orchestration_id="o",
            orchestration_agent_run_id="ORCH", child_agent_run_id="c",
            agent_model="m", workflow_mode="prod", runner_host_authored=True)
        self.assertEqual(ver["allowed_output_paths"], [f"{refs.source_dir()}/source_meta.json"])
        # default (legacy) keeps the runner
        legacy = wc.build_launch_request(
            refs, step="generate", substep="generate", orchestration_id="o",
            orchestration_agent_run_id="ORCH", child_agent_run_id="c",
            agent_model="m", workflow_mode="prod")
        self.assertIn(f"{refs.source_dir()}/src/{self.SID}_runner.f90",
                      legacy["allowed_output_paths"])

    def test_phase_required_outputs_symmetry(self) -> None:
        refs = self._refs()
        m3c = wc.phase_required_outputs(refs, "generate", makefile_required=False,
                                        runner_host_authored=True)
        self.assertIn(f"{refs.source_dir()}/src/{self.SID}_checks.f90", m3c)
        self.assertNotIn(f"{refs.source_dir()}/src/{self.SID}_runner.f90", m3c)
        legacy = wc.phase_required_outputs(refs, "generate")
        self.assertIn(f"{refs.source_dir()}/src/{self.SID}_runner.f90", legacy)


class PureLeafSubstepPredicateTests(unittest.TestCase):
    """M-F: `_pure_leaf_substep` dispatch is decided by node SHAPE alone (the generate-executor is
    no longer selectable — legacy execution was removed, `pure` is the only executor). Direct unit
    coverage of the predicate (previously exercised only indirectly). Reuses WriteRunnerTest's
    `_write_consumer_ir` IR-shaping helper (self is ignored by it)."""

    SID = "boundary_x"

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key=f"component/{self.SID}@0.1.0", spec_path=f"spec/component/{self.SID}",
            ir_id="i1", pipeline_id="p1", source_id="s1", binary_id="b1")

    def _conductor(self, repo: Path, backend: str) -> _FakeConductor:
        return _FakeConductor(repo_root=repo, orchestration_id="o",
                              orchestration_agent_run_id="ORCH", backend=backend, env={})

    def test_claude_m3c_generate_substeps_are_pure(self) -> None:
        # (a) claude + M3c: both generate LLM substeps are pure; other (phase, substep) pairs are
        # not (deterministic generate substeps + compile.verify stay agentic).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            WriteRunnerTest._write_consumer_ir(self, repo, refs, infra=1)
            c = self._conductor(repo, "claude")
            self.assertTrue(c._pure_leaf_substep(refs, "generate", "generate"))
            self.assertTrue(c._pure_leaf_substep(refs, "generate", "verify"))
            self.assertFalse(c._pure_leaf_substep(refs, "generate", "static"))
            self.assertFalse(c._pure_leaf_substep(refs, "compile", "verify"))

    def test_codex_m3c_is_agentic_residual(self) -> None:
        # (b) codex + M3c: no pure producer (codex fail-closes in leaf_command), so the node runs
        # the shared agentic leaf loop as a recorded residual.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            WriteRunnerTest._write_consumer_ir(self, repo, refs, infra=1)
            c = self._conductor(repo, "codex")
            self.assertFalse(c._pure_leaf_substep(refs, "generate", "generate"))
            self.assertFalse(c._pure_leaf_substep(refs, "generate", "verify"))

    def test_claude_non_m3c_is_agentic_residual(self) -> None:
        # (c) claude but non-M3c (0 or 2 infra deps): no bundle representation for the runner, so
        # the node keeps the agentic leaf.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            c = self._conductor(repo, "claude")
            WriteRunnerTest._write_consumer_ir(self, repo, refs, infra=0)
            self.assertFalse(c._pure_leaf_substep(refs, "generate", "generate"))
            WriteRunnerTest._write_consumer_ir(self, repo, refs, infra=2)
            self.assertFalse(c._pure_leaf_substep(refs, "generate", "generate"))

    def test_infrastructure_spec_kind_is_not_pure(self) -> None:
        # (d) an infrastructure node authors its own self-test runner (not glue), so it is non-M3c
        # even with exactly one infra dep.
        from tools.tests.test_runner_renderer import _boundary_ir
        import yaml as _yaml
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            ir = _boundary_ir()
            ir["meta"]["spec_kind"] = "infrastructure"
            ir["dependency"]["direct_deps"] = [
                {"node_key": "infrastructure/harness_fortran_cpu@0.2.0"}]
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.ir_ref / "spec.ir.yaml").write_text(_yaml.safe_dump(ir))
            c = self._conductor(repo, "claude")
            self.assertFalse(c._pure_leaf_substep(refs, "generate", "generate"))


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
        # generate.generate (leaf-authored Makefile for a c/cpp/mixed dependency node) keeps it.
        req = self._launch(refs, "generate", host_authored=False)
        self.assertIn(mk, req["allowed_output_paths"], "generate should keep Makefile")
        # generate.verify writes ONLY source_meta.json — it inspects the Makefile, never rewrites
        # it — so the Makefile is absent from its output set regardless of authorship.
        ver = self._launch(refs, "verify", host_authored=False)
        self.assertEqual(ver["allowed_output_paths"], [f"{refs.source_dir()}/source_meta.json"])

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

    def test_build_inproc_stamps_source_ir_id(self) -> None:
        # binary_meta.json records the origin ir_id (refs.ir_id) alongside source_source_id
        # (refs.source_id). `_write_runner` binds the harness pin to source_ir_id, so a wrong
        # constant here (e.g. source_id) would silently break the binding — distinct ir_id vs
        # source_id values catch a swap.
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
                ir_id="ir_20260707_007", pipeline_id="x_1",
                source_id="src_20260707_003", binary_id="bin_1")
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)

            def fake_compile(args):
                (repo / refs.binary_dir() / "bin").mkdir(parents=True, exist_ok=True)
                (repo / refs.binary_dir() / "bin" / "spec_x_runner").write_text("x")
                return {"ok": True, "return_code": 0, "command_id": "cid"}

            with mock.patch.object(build_runtime_server, "tool_compile_project", fake_compile):
                c._build_inproc(refs, "child-1", "captok")

            meta = json.loads((repo / refs.binary_dir() / "binary_meta.json").read_text())
            self.assertEqual(meta["source_ir_id"], "ir_20260707_007")
            self.assertEqual(meta["source_source_id"], "src_20260707_003")

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

    def test_execute_inproc_clears_stale_verdict_on_runtime_error(self) -> None:
        # R2 guard: a structural (runtime-error) execute failure must leave NO verdict.json, so a
        # STALE one from a prior run cannot make classify_failure misroute the runner failure as a
        # predicate failure. Force run_program to fail after seeding a stale failing verdict.
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
                "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
                "  target:\n    class: cpu\n", encoding="utf-8")
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)
            node_dir = repo / refs.run_node_dir()
            node_dir.mkdir(parents=True, exist_ok=True)
            (node_dir / "verdict.json").write_text(
                json.dumps({"self_verdict": "fail", "failure_class": "physics_fail"}),
                encoding="utf-8")

            with mock.patch.object(build_runtime_server, "tool_run_program",
                                   lambda a: {"ok": False, "stderr": "boom"}):
                out = c._execute_inproc(refs, "child-1", "captok")
            # runtime error returns rc 0 (content failure) AND leaves no verdict.json ->
            # classify_failure sees no failure_class -> the Generate/C2 runner-failure path.
            self.assertEqual(out["returncode"], 0)
            self.assertFalse((node_dir / "verdict.json").exists())

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

    def _predicate_ir(self) -> dict:
        return {"io_contract": {"test_predicates": [
            {"test_id": "l0_scale_identity_pass", "expected_outcome": "pass",
             "target_cases": ["l0_scale_identity_pass"],
             "pass_when": {"all": [{"ref": "checks.scale_identity.pass", "op": "eq", "value": True},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass"}]}},
            {"test_id": "l0_invalid_length_xfail", "expected_outcome": "xfail",
             "target_cases": ["l0_invalid_length_xfail"],
             "pass_when": {"all": [{"ref": "checks.input_guard.pass", "op": "eq", "value": True},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass"}]}}]}}

    def test_author_execute_verdict_pass_and_physics(self) -> None:
        # R2: execute authors verdict.json deterministically from the IR predicates + the
        # runner's diagnostics.json — the judge no longer writes it.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="O", backend="claude", env={})
            refs = self._refs()
            ir = self._predicate_ir()
            good = {"checks": {"scale_identity": {"pass": True}, "input_guard": {"pass": True}},
                    "verdict": {"overall": "pass", "failed_checks": []}}
            doc = c._author_execute_verdict(refs, ir, good)
            self.assertEqual(doc["self_verdict"], "pass")
            self.assertEqual(doc["failure_class"], "pass")
            on_disk = json.loads((repo / refs.run_node_dir() / "verdict.json").read_text())
            self.assertEqual([p["status"] for p in on_disk["per_test"]], ["pass", "xfail"])
            # a physics failure (scale check false) -> self_verdict fail / physics_fail
            bad = {"checks": {"scale_identity": {"pass": False}, "input_guard": {"pass": True}},
                   "verdict": {"overall": "fail", "failed_checks": ["scale_identity"]}}
            doc2 = c._author_execute_verdict(refs, ir, bad)
            self.assertEqual(doc2["self_verdict"], "fail")
            self.assertEqual(doc2["failure_class"], "physics_fail")

    def test_author_execute_verdict_missing_predicates_is_structural(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="O", backend="claude", env={})
            refs = self._refs()
            doc = c._author_execute_verdict(refs, {"io_contract": {}}, {"verdict": {"overall": "pass"}})
            self.assertEqual(doc["self_verdict"], "fail")
            self.assertEqual(doc["failure_class"], "structural_violation")
            self.assertIn("predicate_error", doc)

    def test_execute_physics_fail_routes_escalate_prod_failclosed_dev(self) -> None:
        # R2: an execute-authored physics/contract verdict fail routes to the escalate
        # diagnostician in prod (attribution needs reasoning) and fail_closed in dev.
        import tempfile
        for fclass in ("physics_fail", "structural_violation"):
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                refs = self._refs()
                rn = repo / refs.run_node_dir()
                rn.mkdir(parents=True, exist_ok=True)
                (rn / "verdict.json").write_text(json.dumps(
                    {"self_verdict": "fail", "failure_class": fclass, "per_test": []}),
                    encoding="utf-8")
                ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                           wc.SubstepOutcome("ex", "fail", [], 0)]
                prod = wc.Conductor(repo_root=repo, orchestration_id="o",
                                    orchestration_agent_run_id="O", backend="claude",
                                    env={}, workflow_mode="prod")
                d_prod = prod.classify_failure(refs, "validate", ex_fail)
                self.assertEqual(d_prod.action, "escalate", fclass)
                self.assertEqual(d_prod.reason, f"validate_execute_{fclass}")
                dev = wc.Conductor(repo_root=repo, orchestration_id="o",
                                   orchestration_agent_run_id="O", backend="claude",
                                   env={}, workflow_mode="dev")
                d_dev = dev.classify_failure(refs, "validate", ex_fail)
                self.assertEqual(d_dev.action, "fail_closed", fclass)
                self.assertEqual(d_dev.reason, f"validate_execute_{fclass}", fclass)

    def test_execute_predicate_error_verdict_is_attributed_to_the_ir(self) -> None:
        # A `predicate_error` verdict is the missing/malformed test_predicates DSL — a defect in
        # the certified IR, which Generate cannot author. It must carry the `_ir` attribution
        # suffix so the dev `--resume` directive declines it (reopening Generate would rebuild and
        # deterministically re-fail) and the prod diagnostician sees the attribution.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            rn = repo / refs.run_node_dir()
            rn.mkdir(parents=True, exist_ok=True)
            (rn / "verdict.json").write_text(json.dumps(
                {"self_verdict": "fail", "failure_class": "structural_violation", "per_test": [],
                 "predicate_error": "PredicateError: io_contract.test_predicates missing/empty"}),
                encoding="utf-8")
            ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                       wc.SubstepOutcome("ex", "fail", [], 0)]
            for mode, action in (("prod", "escalate"), ("dev", "fail_closed")):
                c = wc.Conductor(repo_root=repo, orchestration_id="o",
                                 orchestration_agent_run_id="O", backend="claude",
                                 env={}, workflow_mode=mode)
                d = c.classify_failure(refs, "validate", ex_fail)
                self.assertEqual((d.action, d.reason),
                                 (action, "validate_execute_structural_violation_ir"), mode)
            # The suffix must not leak onto a physics_fail (no predicate_error there) nor onto a
            # genuine ref_absent structural_violation, which a warm Generate repair CAN fix.
            (rn / "verdict.json").write_text(json.dumps(
                {"self_verdict": "fail", "failure_class": "physics_fail", "per_test": [],
                 "predicate_error": "ignored on a physics verdict"}), encoding="utf-8")
            dev = wc.Conductor(repo_root=repo, orchestration_id="o",
                               orchestration_agent_run_id="O", backend="claude",
                               env={}, workflow_mode="dev")
            self.assertEqual(dev.classify_failure(refs, "validate", ex_fail).reason,
                             "validate_execute_physics_fail")

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

    def _seed_trial_meta(self, repo: Path, refs: "wc.NodeRefs", **fields: object) -> None:
        node_dir = repo / refs.run_node_dir()
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "trial_meta.json").write_text(json.dumps(fields), encoding="utf-8")

    def test_structural_execute_failure_routes_generate_reuse(self) -> None:
        # B1: a no-verdict execute failure whose trial_meta names a recognized structural
        # category is repaired WARM (reuse) with the gate's own text as findings, instead of
        # the blind cold restart the category-less case still gets.
        import tempfile
        ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                   wc.SubstepOutcome("ex", "fail", [], 0)]
        for category in sorted(wc.VALIDATE_EXECUTE_FAILURE_ROUTING):
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                c = wc.Conductor(repo_root=repo, orchestration_id="o",
                                 orchestration_agent_run_id="O", backend="claude", env={})
                refs = self._refs()
                self._seed_trial_meta(repo, refs, status="fail", failure_category=category,
                                      failure_excerpt="[execute fail]\nmissing a1")
                d = c.classify_failure(refs, "validate", ex_fail)
                self.assertEqual(
                    (d.action, d.target_phase, d.repair_strategy),
                    ("retry", "generate", "reuse"), category)
                self.assertEqual(d.reason, f"validate_execute_{category}")

    def _m3c_conductor(self, repo: Path) -> "wc.Conductor":
        """A conductor whose node host-renders its runner (M3c), without seeding a full IR."""
        class _M3c(wc.Conductor):
            def _conductor_authors_runner(self, refs):  # type: ignore[override]
                return True
        return _M3c(repo_root=repo, orchestration_id="o",
                    orchestration_agent_run_id="O", backend="claude", env={})

    def test_snapshot_gap_on_host_rendered_runner_reopens_compile(self) -> None:
        """M3c: `src/<spec_id>_runner.f90` is host-rendered from the IR, and it emits the
        per-case `__write_snapshot` for every declared case. A missing per-case snapshot file
        therefore cannot be fixed by regenerating the leaf's model/checks — attribute it to the
        IR and reopen Compile rather than burning a Generate attempt that cannot converge."""
        import tempfile
        ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                   wc.SubstepOutcome("ex", "fail", [], 0)]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._m3c_conductor(repo)
            refs = self._refs()
            self._seed_trial_meta(repo, refs, status="fail",
                                  failure_category="snapshot_deliverable_gap",
                                  failure_excerpt="[execute fail]\nmissing case_b.json")
            d = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual((d.action, d.target_phase), ("reopen", "compile"))
            self.assertEqual(d.reason, "validate_execute_snapshot_deliverable_gap_ir")
            # The `_ir` suffix is not a routing-table key, so no findings are threaded into a
            # Generate repair that could not apply them.
            self.assertNotIn(
                d.reason[len(wc.VALIDATE_EXECUTE_REASON_PREFIX):],
                wc.VALIDATE_EXECUTE_FAILURE_ROUTING)
            self.assertIsNone(c._read_repair_findings(refs, d.reason, "validate"))

    def test_snapshot_gap_compile_reopen_resets_the_c2_counter(self) -> None:
        """The M3c snapshot-gap Compile reopen must reset the C2 counter exactly like the
        threshold branch does: it regenerates the IR and everything downstream, so the next
        execute failure is against fresh artifacts. With a stale count of 1, the next failure —
        typically a leaf-repairable value defect in the regenerated checks module — would hit
        the C2 threshold and take the findings-less Compile reopen instead of the warm Generate
        repair the routing table exists to provide."""
        import tempfile
        ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                   wc.SubstepOutcome("ex", "fail", [], 0)]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = self._m3c_conductor(repo)
            refs = self._refs()
            self._seed_trial_meta(repo, refs, status="fail",
                                  failure_category="snapshot_deliverable_gap",
                                  failure_excerpt="[execute fail]\nmissing case_b.json")
            first = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual(first.reason, "validate_execute_snapshot_deliverable_gap_ir")
            self.assertEqual(c._validate_execute_fail_count[refs.node_key], 0)

            # Compile+Generate rebuilt; now a value defect the leaf CAN fix.
            self._seed_trial_meta(repo, refs, status="fail",
                                  failure_category="post_execute_violation",
                                  failure_excerpt="[execute fail]\nall-zero basis")
            second = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual(
                (second.action, second.target_phase, second.repair_strategy),
                ("retry", "generate", "reuse"))
            self.assertEqual(second.reason, "validate_execute_post_execute_violation")

    def test_host_rendered_unrepairable_set_holds_only_the_snapshot_gap(self) -> None:
        """Pinned as a LITERAL, not derived from the constant: the value-domain test below must
        not silently shrink its coverage when a category is added to the set."""
        self.assertEqual(wc.HOST_RENDERED_RUNNER_UNREPAIRABLE, frozenset({"snapshot_deliverable_gap"}))
        self.assertTrue(wc.HOST_RENDERED_RUNNER_UNREPAIRABLE
                        <= set(wc.VALIDATE_EXECUTE_FAILURE_ROUTING))

    def test_value_categories_stay_on_generate_even_on_host_rendered_runner(self) -> None:
        """The renderer boxes each of a case's required variables unconditionally (the leaf
        registry's found-flag is discarded), so on an M3c node the snapshot key set and shapes
        are host-fixed from the IR but every VALUE comes from the leaf's checks module. A
        trivial basis / NaN / wrong metric is exactly what the warm repair fixes, so these
        categories keep the Generate route.

        The category list is a LITERAL: deriving it from `HOST_RENDERED_RUNNER_UNREPAIRABLE`
        would make the test vacuous for any category wrongly added to that set.
        """
        import tempfile
        ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                   wc.SubstepOutcome("ex", "fail", [], 0)]
        for category in ("post_execute_violation", "quality_check_mismatch"):
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                c = self._m3c_conductor(repo)
                refs = self._refs()
                self._seed_trial_meta(repo, refs, status="fail", failure_category=category,
                                      failure_excerpt="[execute fail]\nall-zero basis")
                d = c.classify_failure(refs, "validate", ex_fail)
                self.assertEqual(
                    (d.action, d.target_phase, d.repair_strategy),
                    ("retry", "generate", "reuse"), category)
                self.assertEqual(d.reason, f"validate_execute_{category}")

    def test_snapshot_gap_on_leaf_authored_runner_still_routes_generate(self) -> None:
        """A non-M3c node's runner IS leaf-authored, so the gap is a Generate defect."""
        import tempfile
        ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                   wc.SubstepOutcome("ex", "fail", [], 0)]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            # bare Conductor: no IR on disk -> _conductor_authors_runner is False
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="O", backend="claude", env={})
            refs = self._refs()
            self._seed_trial_meta(repo, refs, status="fail",
                                  failure_category="snapshot_deliverable_gap",
                                  failure_excerpt="[execute fail]\nmissing case_b.json")
            d = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual((d.action, d.target_phase, d.repair_strategy),
                             ("retry", "generate", "reuse"))

    def test_structural_execute_failure_keeps_restart_without_category(self) -> None:
        # A passing / absent / unrecognized-category trial_meta is not understood well enough to
        # guide a warm repair -> the cold restart stands (and a missing trial_meta is exactly the
        # runner runtime-error case, which _execute_inproc never writes).
        import tempfile
        ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                   wc.SubstepOutcome("ex", "fail", [], 0)]
        cases: list[dict] = [
            {"status": "pass", "failure_category": "post_execute_violation"},
            {"status": "fail", "failure_category": "some_future_category"},
            {"status": "fail"},
        ]
        for fields in cases:
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                c = wc.Conductor(repo_root=repo, orchestration_id="o",
                                 orchestration_agent_run_id="O", backend="claude", env={})
                refs = self._refs()
                self._seed_trial_meta(repo, refs, **fields)
                d = c.classify_failure(refs, "validate", ex_fail)
                self.assertEqual((d.action, d.target_phase, d.repair_strategy, d.reason),
                                 ("retry", "generate", "restart", "validate_execute_fail"), fields)

    def test_structural_execute_reuse_runs_after_c2_threshold(self) -> None:
        # C2 ordering is load-bearing and unchanged: the second CONSECUTIVE no-verdict execute
        # failure escalates to a Compile reopen even when a reuse-eligible category is present
        # (a Generate repair already failed to fix it, so the IR is the wrong side).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="O", backend="claude", env={})
            refs = self._refs()
            self._seed_trial_meta(repo, refs, status="fail",
                                  failure_category="post_execute_violation",
                                  failure_excerpt="missing a1")
            ex_fail = [wc.SubstepOutcome("pj", "pass", [], 0),
                       wc.SubstepOutcome("ex", "fail", [], 0)]
            first = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual(first.repair_strategy, "reuse")
            second = c.classify_failure(refs, "validate", ex_fail)
            self.assertEqual((second.action, second.target_phase, second.reason),
                             ("reopen", "compile", "validate_execute_fail_ir"))

    # -- B1 producer side: _execute_inproc's structural-failure classification ---------
    #
    # Each category is reached by driving ONE input to failure while the other two stay clean,
    # so the 3-way precedence in _execute_inproc is pinned rather than incidentally satisfied:
    #   post_execute_violation   <- a gate subprocess exits non-zero
    #   snapshot_deliverable_gap <- gates clean, quality_check clean, a per-case snapshot missing
    #   quality_check_mismatch   <- gates clean, no snapshot gap, the make-test re-run disagrees
    _B1_IR_MINIMAL = ("impl_defaults:\n  toolchain:\n    language: fortran\n"
                      "    build_system: make\n  target:\n    class: cpu\n")
    _B1_IR_SNAPSHOTS = (_B1_IR_MINIMAL
                        + "io_contract:\n  raw_requirements:\n    required_evidence:\n"
                          "      - artifact: state_snapshots\n        required: true\n"
                          "case:\n  test_case_set:\n    - case_id: c_alpha\n")
    # A structurally clean run needs a SATISFIABLE predicate too, else _author_execute_verdict
    # authors a structural_violation verdict (the missing-DSL guard) and the run fails anyway.
    # `_b1_execute` seeds diagnostics `verdict.overall == pass`.
    _B1_IR_CLEAN_VERDICT = (_B1_IR_MINIMAL
                            + "case:\n  test_case_set:\n    - case_id: c_alpha\n"
                              "io_contract:\n  test_predicates:\n    - test_id: t_alpha\n"
                              "      expected_outcome: pass\n      target_cases: [c_alpha]\n"
                              "      pass_when:\n        all:\n"
                              "          - ref: verdict.overall\n            op: eq\n"
                              "            value: pass\n")
    # The same predicate, but over a per-case metric ADDRESS the seeded diagnostics never emits:
    # every gate is clean, so execute reaches the verdict and fails it structurally (ref_absent).
    _B1_IR_VERDICT_FAIL = (_B1_IR_MINIMAL
                           + "case:\n  test_case_set:\n    - case_id: c_alpha\n"
                             "io_contract:\n  test_predicates:\n    - test_id: t_alpha\n"
                             "      expected_outcome: pass\n      target_cases: [c_alpha]\n"
                             "      pass_when:\n        all:\n"
                             "          - ref: metrics.max_abs_dev\n            op: le\n"
                             "            value: 1.0e-12\n            per_case: true\n")

    def _b1_refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1",
            run_id="run_1", source_binary_id="bin_1")

    def _b1_execute(self, repo: Path, ir_yaml: str, *, gate_result: tuple[int, str],
                    matching_diagnostics: bool,
                    diagnostics: dict | None = None) -> tuple[dict, dict]:
        """Drive _execute_inproc with the two gate subprocesses stubbed to `gate_result`
        (returncode, stdout) and the runner/make-test diagnostics seeded so the quality_check
        passes (matching_diagnostics) or fails. Returns (result, trial_meta-or-{})."""
        import sys
        import subprocess as _sp
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore

        c = wc.Conductor(repo_root=repo, orchestration_id="t",
                         orchestration_agent_run_id="x", backend="claude", env={})
        refs = self._b1_refs()
        (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
        (repo / refs.ir_ref / "spec.ir.yaml").write_text(ir_yaml, encoding="utf-8")
        (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)

        # The runner's (mocked) output dirs: a matching pair of diagnostics makes
        # _author_quality_check return "pass"; an absent candidate makes it "fail".
        run_tmp = repo / "workspace" / "tmp" / "child-1" / "run"
        qc_tmp = repo / "workspace" / "tmp" / "child-1" / "qc_run"
        run_tmp.mkdir(parents=True, exist_ok=True)
        diag = diagnostics or {"checks": {"k": {"status": "pass"}},
                               "verdict": {"overall": "pass"}}
        (run_tmp / "diagnostics.json").write_text(json.dumps(diag), encoding="utf-8")
        if matching_diagnostics:
            qc_tmp.mkdir(parents=True, exist_ok=True)
            (qc_tmp / "diagnostics.json").write_text(json.dumps(diag), encoding="utf-8")

        rc, out = gate_result

        def fake_subprocess_run(argv, **kwargs):
            # Only the two gates (check_artifact_syntax / validate_pipeline_semantics) run here.
            return _sp.CompletedProcess(argv, rc, stdout=out, stderr="")

        with mock.patch.object(build_runtime_server, "tool_run_program",
                               lambda a: {"ok": True, "command_id": "R"}), \
             mock.patch.object(build_runtime_server, "tool_run_quality_checks",
                               lambda a: {"ok": True, "command_id": "Q"}), \
             mock.patch.object(wc.subprocess, "run", fake_subprocess_run):
            result = c._execute_inproc(refs, "child-1", "captok")

        meta_path = repo / refs.run_node_dir() / "trial_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return result, meta

    def test_execute_inproc_records_post_execute_violation(self) -> None:
        # A genuine gate report (the gate RAN and exited non-zero) becomes the category and its
        # text becomes the excerpt the warm repair leaf receives.
        import tempfile
        violation = ("workspace/.../raw/metrics_basis.json: test 'l0_pass' is missing "
                     "required_raw_variables: a1 — the missing variables are nested under the "
                     "unrecognized wrapper key 'values'")
        with tempfile.TemporaryDirectory() as td:
            out, meta = self._b1_execute(Path(td), self._B1_IR_MINIMAL,
                                         gate_result=(1, violation),
                                         matching_diagnostics=True)
            # rc 0: a structural failure is a CONTENT failure, routed by the validate tables.
            self.assertEqual(out["returncode"], 0)
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "post_execute_violation")
            self.assertIn(meta["failure_category"], wc.VALIDATE_EXECUTE_FAILURE_ROUTING)
            self.assertIn("[execute fail]", meta["failure_excerpt"])
            self.assertIn("unrecognized wrapper key 'values'", meta["failure_excerpt"])
            self.assertLessEqual(len(meta["failure_excerpt"].splitlines()), 50)

    def test_execute_inproc_excerpt_is_bounded_by_characters_not_only_lines(self) -> None:
        # The excerpt is rendered verbatim into the slim repair prompt. A post_execute violation is
        # not line-shaped like compiler stderr — it interpolates whole dict payloads into ONE line
        # (`declared state_variables missing in snapshot files ({...})`), so a 50-LINE cap alone
        # leaves the excerpt unbounded. Both axes must hold.
        import tempfile
        one_huge_line = "post_execute: missing in snapshot files ({})".format("x" * 200_000)
        with tempfile.TemporaryDirectory() as td:
            _o, meta = self._b1_execute(Path(td), self._B1_IR_MINIMAL,
                                        gate_result=(1, one_huge_line),
                                        matching_diagnostics=True)
            excerpt = meta["failure_excerpt"]
            self.assertLessEqual(len(excerpt.splitlines()), wc._EXECUTE_EXCERPT_MAX_LINES)
            # the truncation notice is prepended, so allow it on top of the character budget
            self.assertLess(len(excerpt), wc._EXECUTE_EXCERPT_MAX_CHARS + 200)
            self.assertIn(wc._EXECUTE_EXCERPT_TRUNCATION_MARK, excerpt)
            # the TAIL is kept (a gate prints the offending detail last)
            self.assertTrue(excerpt.endswith("x)"), excerpt[-40:])

    def test_execute_inproc_short_excerpt_is_not_truncated(self) -> None:
        # The common case is well under both bounds and must pass through byte-for-byte.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _o, meta = self._b1_execute(Path(td), self._B1_IR_MINIMAL,
                                        gate_result=(1, "post_execute: bad shape"),
                                        matching_diagnostics=True)
            self.assertNotIn(wc._EXECUTE_EXCERPT_TRUNCATION_MARK, meta["failure_excerpt"])
            self.assertIn("post_execute: bad shape", meta["failure_excerpt"])

    def test_execute_inproc_records_quality_check_mismatch(self) -> None:
        # Gates clean, no snapshot requirement -> the only failing input is the make-test re-run
        # (no candidate diagnostics), so the category must be quality_check_mismatch.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out, meta = self._b1_execute(Path(td), self._B1_IR_MINIMAL, gate_result=(0, ""),
                                         matching_diagnostics=False)
            self.assertEqual(out["returncode"], 0)
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "quality_check_mismatch")
            self.assertIn("[execute fail]", meta["failure_excerpt"])

    def test_execute_inproc_stamps_the_repo_revision(self) -> None:
        """B4: the revision that produced this run's evidence is recorded beside the excerpt, so
        the dev resume directive can refuse to inject findings a later source change invalidated.
        Stamped on the failing RUN (not read from orchestration_meta, which freezes at first
        start), which is what lets the guard self-correct after a re-run."""
        import tempfile
        from unittest import mock
        rev = {"commit": "c" * 40, "dirty": False}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("tools.orchestration_runtime._capture_repo_revision",
                            return_value=dict(rev)):
                _o, meta = self._b1_execute(Path(td), self._B1_IR_MINIMAL,
                                            gate_result=(1, "post_execute: bad shape"),
                                            matching_diagnostics=True)
            self.assertEqual(meta["repo_revision"], rev)
            self.assertEqual(meta["failure_category"], "post_execute_violation")

    def test_execute_inproc_records_snapshot_deliverable_gap(self) -> None:
        # Gates clean and quality_check clean; the IR requires a per-case state snapshot the
        # runner never wrote -> snapshot_deliverable_gap, and the gap message is the excerpt.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out, meta = self._b1_execute(Path(td), self._B1_IR_SNAPSHOTS, gate_result=(0, ""),
                                         matching_diagnostics=True)
            self.assertEqual(out["returncode"], 0)
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "snapshot_deliverable_gap")
            self.assertIn("c_alpha", meta["failure_excerpt"])

    def test_execute_inproc_category_precedence_when_inputs_fail_together(self) -> None:
        # The categories differ only in report quality (all three route to generate/reuse), so the
        # precedence is what the leaf reads first: the most specific report wins. A gate report
        # beats a snapshot gap; a snapshot gap beats a bare quality_check mismatch.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _o, meta = self._b1_execute(Path(td), self._B1_IR_SNAPSHOTS,
                                        gate_result=(1, "post_execute: bad shape"),
                                        matching_diagnostics=False)
            self.assertEqual(meta["failure_category"], "post_execute_violation")
        with tempfile.TemporaryDirectory() as td:
            _o, meta = self._b1_execute(Path(td), self._B1_IR_SNAPSHOTS, gate_result=(0, ""),
                                        matching_diagnostics=False)
            self.assertEqual(meta["failure_category"], "snapshot_deliverable_gap")

    def test_execute_inproc_structural_pass_writes_no_failure_fields(self) -> None:
        # The mirror of the three above: every input clean (gates, quality_check, AND the
        # per-test verdict) -> no failure at all, so the category/excerpt fields must be ABSENT.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _out, meta = self._b1_execute(Path(td), self._B1_IR_CLEAN_VERDICT,
                                          gate_result=(0, ""), matching_diagnostics=True)
            self.assertEqual(meta["status"], "pass")
            self.assertNotIn("failure_category", meta)
            self.assertNotIn("failure_excerpt", meta)

    # -- verdict-fail side: the per-test predicate failure authors findings, not a category ----

    def test_execute_inproc_verdict_fail_authors_a_failure_excerpt(self) -> None:
        # Every structural gate is clean, so execute reaches the deterministic verdict and the
        # predicate fails. The excerpt is what a dev `--resume` threads into the reopened
        # Generate, so it must name the failing predicate (test / ref / op / case / reason).
        import tempfile
        diag = {"checks": {"k": {"status": "pass"}}, "verdict": {"overall": "pass"},
                "per_case": {"c_alpha": {"metrics": {"metrics.other": 0.0}}}}
        with tempfile.TemporaryDirectory() as td:
            out, meta = self._b1_execute(Path(td), self._B1_IR_VERDICT_FAIL, gate_result=(0, ""),
                                         matching_diagnostics=True, diagnostics=diag)
            self.assertEqual(out["returncode"], 0)
            self.assertEqual(meta["status"], "fail")
            excerpt = meta["failure_excerpt"]
            self.assertIn(wc._VERDICT_FAIL_MARKER, excerpt)
            self.assertIn("failure_class=structural_violation", excerpt)
            self.assertIn("test t_alpha", excerpt)
            self.assertIn("metrics.max_abs_dev", excerpt)
            self.assertIn("reason=ref_absent", excerpt)
            self.assertIn("case='c_alpha'", excerpt)
            self.assertIn(excerpt, out["stderr"])
            # The B1 routing table keys on failure_category; a verdict failure is NOT one of its
            # categories, and classify_failure's no-verdict branch must never see one here.
            self.assertNotIn("failure_category", meta)

    def test_execute_inproc_verdict_fail_excerpt_is_bounded(self) -> None:
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            huge = {"self_verdict": "fail", "failure_class": "physics_fail",
                    "per_test": [{"test_id": f"t{i}", "status": "fail",
                                  "basis": {"conditions": [{"ref": "metrics.x", "op": "le",
                                                            "evaluated": [{"satisfied": False,
                                                                           "lhs": "y" * 500}]}]}}
                                 for i in range(200)]}
            with mock.patch.object(wc.Conductor, "_author_execute_verdict",
                                   lambda self, refs, ir, run_diag: huge):
                _out, meta = self._b1_execute(Path(td), self._B1_IR_CLEAN_VERDICT,
                                              gate_result=(0, ""), matching_diagnostics=True)
            excerpt = meta["failure_excerpt"]
            self.assertLessEqual(len(excerpt.splitlines()), wc._EXECUTE_EXCERPT_MAX_LINES)
            self.assertLess(len(excerpt), wc._EXECUTE_EXCERPT_MAX_CHARS + 200)

    def test_execute_inproc_verdict_fail_excerpt_reports_a_predicate_error(self) -> None:
        # A missing/malformed predicate DSL is authored as a structural_violation verdict with an
        # empty per_test; the excerpt must still carry the reason (the IR defect).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _out, meta = self._b1_execute(Path(td), self._B1_IR_MINIMAL, gate_result=(0, ""),
                                          matching_diagnostics=True)
            self.assertEqual(meta["status"], "fail")
            self.assertIn("predicate_error:", meta["failure_excerpt"])
            self.assertIn("test_predicates missing/empty", meta["failure_excerpt"])
            self.assertNotIn("failure_category", meta)

    def test_execute_inproc_clears_stale_trial_meta(self) -> None:
        # The runtime-error discriminator ("no trial_meta") must not depend on the external
        # run-id rotation invariant: a stale trial_meta in the run node dir is cleared up front,
        # so a runner runtime error cannot be misrouted as a warm structural repair.
        import sys
        import tempfile
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            c = wc.Conductor(repo_root=repo, orchestration_id="t",
                             orchestration_agent_run_id="x", backend="claude", env={})
            refs = self._b1_refs()
            (repo / refs.ir_ref).mkdir(parents=True, exist_ok=True)
            (repo / refs.ir_ref / "spec.ir.yaml").write_text(
                self._B1_IR_MINIMAL, encoding="utf-8")
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)
            node_dir = repo / refs.run_node_dir()
            node_dir.mkdir(parents=True, exist_ok=True)
            (node_dir / "trial_meta.json").write_text(
                json.dumps({"status": "fail", "failure_category": "post_execute_violation",
                            "failure_excerpt": "stale"}), encoding="utf-8")

            with mock.patch.object(build_runtime_server, "tool_run_program",
                                   lambda a: {"ok": False, "stderr": "SIGFPE"}):
                c._execute_inproc(refs, "child-1", "captok")
            self.assertFalse((node_dir / "trial_meta.json").exists())

    def test_execute_inproc_runtime_error_writes_no_trial_meta(self) -> None:
        # The on-disk discriminator for the two no-verdict kinds: a runner runtime error returns
        # before any trial_meta is authored, so classify_failure keeps its cold restart.
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
                "impl_defaults:\n  toolchain:\n    language: fortran\n    build_system: make\n"
                "  target:\n    class: cpu\n", encoding="utf-8")
            (repo / refs.source_dir() / "src").mkdir(parents=True, exist_ok=True)

            with mock.patch.object(build_runtime_server, "tool_run_program",
                                   lambda a: {"ok": False, "stderr": "SIGFPE"}):
                out = c._execute_inproc(refs, "child-1", "captok")
            self.assertEqual(out["returncode"], 0)
            self.assertFalse((repo / refs.run_node_dir() / "trial_meta.json").exists())


class DeterministicLintTest(unittest.TestCase):
    """The generate.gate lint checker (_gate_lint_check) runs in-process: it returns the `lint`
    section of gate_meta and writes the host-side lint evidence (even on a content fail). The
    unioned gate_meta.json + routing is exercised by DeterministicGateTest."""

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

    def test_gate_lint_check_pass_returns_section_and_writes_evidence(self) -> None:
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
                out = c._gate_lint_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "pass")
            meta = out
            self.assertEqual(meta["status"], "pass")
            self.assertEqual(meta["preset"], "fortitude")
            self.assertIsNone(meta["failure_category"])
            ev = read_lint_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertTrue(ev["ok"])
            self.assertEqual(ev["run_linter"][0]["command_id"], "cid")
            self.assertTrue(
                ev["run_linter"][0]["command_log_ref"].endswith("/src/command_log.jsonl"))

    def test_gate_lint_check_findings_is_content_fail(self) -> None:
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
                out = c._gate_lint_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "fail")
            meta = out
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "lint_findings")
            self.assertIn("S001", meta["failure_excerpt"])
            ev = read_lint_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertFalse(ev["ok"])

    def test_gate_lint_check_mixed_records_two_entries(self) -> None:
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
                c._gate_lint_check(refs, "child-1", "captok")
            ev = read_lint_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertEqual(ev["preset"], "mixed")
            self.assertEqual({e["preset"] for e in ev["run_linter"]}, {"fortitude", "cppcheck"})

    def test_gate_lint_check_unknown_language_raises(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs, language="brainfuck")
            c = self._conductor(repo)
            with self.assertRaises(RuntimeError):
                c._gate_lint_check(refs, "child-1", "captok")


class DeterministicSyntaxTest(unittest.TestCase):
    """The generate.gate syntax checker (_gate_syntax_check) runs in-process: it stages the node
    (+ dep closure) sources, runs the MCP run_syntax_check compiler gate (mandatory gfortran,
    optional METDSL_SYNTAX_COMPILERS stages), returns the `syntax` section of gate_meta and
    writes the host-side syntax evidence. An unfixable attribution raises (transport
    fail_closed); the unioned gate_meta.json is exercised by DeterministicGateTest."""

    def _conductor(self, repo: Path, env: dict[str, str] | None = None) -> "wc.Conductor":
        return wc.Conductor(repo_root=repo, orchestration_id="t",
                            orchestration_agent_run_id="x", backend="claude",
                            env=env or {})

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1")

    def _seed(self, repo: Path, refs: wc.NodeRefs, language: str = "fortran",
              sources: dict[str, str] | None = None) -> None:
        src = repo / refs.source_dir() / "src"
        src.mkdir(parents=True, exist_ok=True)
        if sources is None:
            sources = {"spec_x_model.f90": "module spec_x_model\nend module spec_x_model\n"}
        for name, text in sources.items():
            (src / name).write_text(text, encoding="utf-8")
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "spec.ir.yaml").write_text(
            f"impl_defaults:\n  toolchain:\n    language: {language}\n"
            f"    standard: f2008\n  target:\n    backend: openmp\n", encoding="utf-8")

    def _patch_syntax(self, fn):
        import sys
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore
        return mock.patch.object(build_runtime_server, "tool_run_syntax_check", fn)

    def test_gate_syntax_check_pass_returns_section_and_writes_evidence(self) -> None:
        import tempfile
        from tools.hooks.syntax_evidence import read_syntax_evidence
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            seen_args: list[dict] = []

            def fake(args):
                seen_args.append(args)
                return {"ok": True, "return_code": 0, "command_id": "sid",
                        "compiler": args["compiler"], "compiler_version": "GNU Fortran 13",
                        "skipped": False}

            with self._patch_syntax(fake):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "pass")
            self.assertEqual(seen_args[0]["compiler"], "gfortran")
            self.assertEqual(seen_args[0]["std"], "f2008")
            self.assertTrue(seen_args[0]["openmp"])  # target.backend: openmp
            # staging dir is a per-compiler throwaway under workspace/tmp, never src/
            self.assertIn("workspace/tmp/child-1/syntax/gfortran", seen_args[0]["project_dir"])
            # the log still lands at the canonical <src>/command_log.jsonl placement
            self.assertTrue(
                seen_args[0]["command_log_path"].endswith("/src/command_log.jsonl"))
            meta = out
            self.assertEqual(meta["status"], "pass")
            self.assertIsNone(meta["failure_category"])
            ev = read_syntax_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertTrue(ev["ok"])
            self.assertEqual(ev["stages"][0]["compiler"], "gfortran")
            self.assertEqual(ev["stages"][0]["status"], "pass")
            self.assertEqual(ev["stages"][0]["command_id"], "sid")
            self.assertTrue(
                ev["stages"][0]["command_log_ref"].endswith("/src/command_log.jsonl"))

    def test_gate_syntax_check_compile_error_is_content_fail(self) -> None:
        import tempfile
        from tools.hooks.syntax_evidence import read_syntax_evidence
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def fake(args):
                if self._call_kind(args) == "canary":
                    return {"ok": True, "skipped": False, "command_id": "canary"}
                return {"ok": False, "return_code": 1, "command_id": "sid",
                        "skipped": False,
                        "stderr": "Error: IMPLICIT NONE with spec list"}

            with self._patch_syntax(fake):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "fail")
            meta = out
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "syntax_error")
            self.assertIn("IMPLICIT NONE", meta["failure_excerpt"])
            ev = read_syntax_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertFalse(ev["ok"])
            self.assertEqual(ev["stages"][0]["status"], "fail")

    def test_gate_syntax_check_non_fortran_passes_through(self) -> None:
        import tempfile
        from tools.hooks.syntax_evidence import syntax_evidence_path
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs, language="cpp", sources={"m.cpp": "int main(){}\n"})
            c = self._conductor(repo)

            def fake(args):  # must never be called for a non-fortran node
                raise AssertionError("run_syntax_check must not run for language=cpp")

            with self._patch_syntax(fake):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "pass")
            meta = out
            self.assertEqual(meta["status"], "pass")
            self.assertIn("language=cpp", meta["skipped_reason"])
            self.assertEqual(meta["stages"], [])
            self.assertFalse(
                syntax_evidence_path(pipeline_root=repo / refs.pipeline_ref,
                                     source_id="src_1").exists())

    def test_gate_syntax_check_no_sources_is_content_fail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs, sources={})
            c = self._conductor(repo)
            with self._patch_syntax(lambda args: {"ok": True, "skipped": False}):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "fail")  # content fail (no source to check)
            meta = out
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "syntax_error")

    def test_gate_syntax_check_missing_gfortran_is_transport_fail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_syntax(
                lambda args: {"ok": True, "skipped": True,
                              "reason": "compiler not available: gfortran"}):
                with self.assertRaises(RuntimeError):
                    c._gate_syntax_check(refs, "child-1", "captok")

    def test_gate_syntax_check_optional_stage_skipped_records_and_passes(self) -> None:
        import sys
        import tempfile
        from unittest import mock
        from tools.hooks.syntax_evidence import read_syntax_evidence
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            # frt is a REGISTERED adapter here (temporarily) but its binary is not
            # installed: the tool returns skipped, the gate records it and still passes.
            c = self._conductor(repo, env={"METDSL_SYNTAX_COMPILERS": "frt,gfortran"})
            registry = dict(build_runtime_server._SYNTAX_COMPILER_ADAPTERS)
            registry["frt"] = registry["gfortran"]

            def fake(args):
                if args["compiler"] == "frt":
                    return {"ok": True, "skipped": True,
                            "reason": "compiler not available: frt"}
                return {"ok": True, "return_code": 0, "command_id": "sid",
                        "compiler_version": "GNU Fortran 13", "skipped": False}

            with mock.patch.object(
                    build_runtime_server, "_SYNTAX_COMPILER_ADAPTERS", registry), \
                    self._patch_syntax(fake):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "pass")
            meta = out
            self.assertEqual(meta["status"], "pass")
            ev = read_syntax_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            by_compiler = {s["compiler"]: s for s in ev["stages"]}
            # gfortran is forced to run first (the mandatory gate) even though the env
            # var listed frt first.
            self.assertEqual(ev["stages"][0]["compiler"], "gfortran")
            self.assertEqual(by_compiler["gfortran"]["status"], "pass")
            self.assertEqual(by_compiler["frt"]["status"], "skipped")

    def test_gate_syntax_check_nonmake_with_deps_fails_closed_not_loops(self) -> None:
        # A fortran node whose dependency modules cannot be staged (non-make: the conductor
        # does not own its Makefile, so _stage_dependency_sources is a no-op) must fail
        # CLOSED (RuntimeError -> transport) rather than run gfortran, get "Cannot open
        # module file", and misclassify it as a content syntax_error that loops forever.
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with mock.patch.object(c, "_stage_dependency_sources", return_value=[]), \
                    mock.patch.object(c, "_dependency_closure_nodes",
                                      return_value=["component/dep@0.1.0"]):
                with self._patch_syntax(lambda args: {"ok": True, "skipped": False}):
                    with self.assertRaises(RuntimeError):
                        c._gate_syntax_check(refs, "child-1", "captok")

    DEP_REF = "workspace/pipelines/component__dep__0.1.0/p_1/source/s_1/src/dep_model.f90"

    def _with_dep(self, c: "wc.Conductor"):
        """Patch the conductor so one dependency-closure `dep_model.f90` is staged."""
        from unittest import mock

        def stage(_refs, obj_dir):
            obj_dir.mkdir(parents=True, exist_ok=True)
            (obj_dir / "dep_model.f90").write_text(
                "module dep_model\nend module dep_model\n", encoding="utf-8")
            return [self.DEP_REF]

        return (mock.patch.object(c, "_stage_dependency_sources", side_effect=stage),
                mock.patch.object(c, "_dependency_closure_nodes",
                                  return_value=["component/dep@0.1.0"]))

    def _call_kind(self, args: dict) -> str:
        """Classify a run_syntax_check call as the staged run, the invocation canary, or the
        dependency attribution probe — asserting each attribution run's INPUT SET, which is
        its load-bearing property. A probe that also recompiled the node's own src would
        reproduce every node-caused failure and turn each one into a permanent fail_closed
        (the exact over-trigger attribution exists to avoid), and a canary carrying anything
        but the canary source would stop being a viability test — both while still satisfying
        a test that only branched on the directory name."""
        d = Path(args["project_dir"])
        staged = {p.name for p in d.iterdir() if p.is_file() and p.suffix == ".f90"}
        if d.name.endswith("_canary"):
            self.assertEqual(staged, {"metdsl_syntax_canary.f90"})
            return "canary"
        if d.name.endswith("_deps_probe"):
            self.assertEqual(staged, {"dep_model.f90"})
            return "probe"
        return "stage"

    def test_gate_syntax_check_dependency_source_finding_fails_closed_not_loops(self) -> None:
        # The gate compiles the node's src TOGETHER with the certified dependency-closure
        # `<dep>_model.f90`. A failure the DEPENDENCY sources cause is unfixable by this
        # node's leaf (they lie outside its src/ and its write_roots), so routing it as a
        # content syntax_error would warm-resume generate.generate into a futile loop that
        # burns the retry budget and blames the wrong node. It must fail CLOSED naming the
        # dependency to re-certify. Reachable whenever a dependency was certified before a
        # gate rule existed (the promoted -Werror=unused-* classes).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            # The staged run AND the deps-alone attribution probe both fail => the dependency
            # closure is what is broken.
            fail = {"ok": False, "return_code": 1, "command_id": "sid", "skipped": False,
                    "stderr": "dep_model.f90:4:25:\n\nError: Unused dummy argument 'z_b' "
                              "at (1) [-Werror=unused-dummy-argument]\n"}

            def fake(args):
                kind = self._call_kind(args)  # also asserts each run's input set
                if kind == "canary":
                    return {"ok": True, "skipped": False, "command_id": "canary"}
                return fail

            stage_p, closure_p = self._with_dep(c)
            with stage_p, closure_p, self._patch_syntax(fake):
                with self.assertRaises(RuntimeError) as ctx:
                    c._gate_syntax_check(refs, "child-1", "captok")
            self.assertIn(self.DEP_REF, str(ctx.exception))
            self.assertIn("Unused dummy argument", str(ctx.exception))
            # no content-fail deliverable is authored on a transport fail_closed
            self.assertFalse((repo / refs.source_dir() / "syntax_meta.json").exists())

    def test_gate_syntax_check_node_source_finding_with_deps_staged_still_content_fail(self) -> None:
        # The mirror of the test above: with a dependency staged, a finding in the NODE's own
        # source stays a content failure (warm-resume). The attribution probe must not
        # over-trigger and turn every syntax finding on a node with deps into fail_closed.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def fake(args):
                if self._call_kind(args) in ("canary", "probe"):
                    return {"ok": True, "skipped": False, "command_id": "sub"}
                return {"ok": False, "return_code": 1, "command_id": "sid", "skipped": False,
                        "stderr": "spec_x_model.f90:4:25:\n\nError: Unused dummy argument "
                                  "'z_b' at (1) [-Werror=unused-dummy-argument]\n"}

            stage_p, closure_p = self._with_dep(c)
            with stage_p, closure_p, self._patch_syntax(fake):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "fail")
            meta = out
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "syntax_error")

    def test_gate_syntax_check_unviable_invocation_fails_closed_not_blaming_deps(self) -> None:
        # `std` comes from the LLM-authored IR and is passed verbatim as `-std=<value>`. An
        # unknown value (`2008`, the elided-`f` form the IMPL_PLAN_SPEC example once showed)
        # makes the driver reject the COMMAND LINE: no source is parsed and every file fails
        # at once — including the dependency closure, which would otherwise be blamed and sent
        # for a pointless re-certification (it passes its own gate; every --resume would fail
        # again). The canary, valid under every standard, fails too — which is what identifies
        # the invocation as the culprit. The leaf does not author the IR: fail_closed, no retry.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            # every run fails identically — the driver never got as far as reading a source
            unviable = {"ok": False, "return_code": 1, "command_id": "sid", "skipped": False,
                        "stderr": "gfortran: error: unrecognized command-line option "
                                  "'-std=2008'; did you mean '-std=f2008'?\n"}
            stage_p, closure_p = self._with_dep(c)
            with stage_p, closure_p, self._patch_syntax(lambda args: unviable):
                with self.assertRaises(RuntimeError) as ctx:
                    c._gate_syntax_check(refs, "child-1", "captok")
            msg = str(ctx.exception)
            self.assertIn("not viable", msg)
            self.assertIn("toolchain.standard", msg)
            self.assertNotIn(self.DEP_REF, msg)  # the dependency must NOT be blamed

    def test_gate_syntax_check_dep_failure_message_names_both_causes(self) -> None:
        # A closure failing under this node's std has two possible causes, and the leaf can
        # fix NEITHER, so both take the same fail_closed: the dependency's certified source is
        # defective, OR this node's declared standard is narrower than the sound closure needs.
        # The message must name both and attach the compiler's diagnostics (which say which),
        # and must not prescribe one remedy. A permissive-standard re-check would look like a
        # cheap discriminator but is unsound: `-std=gnu` accepts what the gate means to reject
        # (a non-constant STOP code, `implicit none (external)`, GNU extensions), so a
        # genuinely defective dependency would be reported as sound and the operator sent to
        # widen this node's standard to accommodate nonconforming code.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def fake(args):
                if self._call_kind(args) == "canary":
                    return {"ok": True, "skipped": False, "command_id": "canary"}
                return {"ok": False, "return_code": 1, "command_id": "sid", "skipped": False,
                        "stderr": "dep_model.f90:12:5:\n\nError: Fortran 2008: The symbol "
                                  "'real64', referenced at (1), is not in the selected "
                                  "standard\n"}

            stage_p, closure_p = self._with_dep(c)
            with stage_p, closure_p, self._patch_syntax(fake):
                with self.assertRaises(RuntimeError) as ctx:
                    c._gate_syntax_check(refs, "child-1", "captok")
            msg = str(ctx.exception)
            self.assertIn("re-certify", msg)          # cause 1: a defective dependency
            self.assertIn("toolchain.standard", msg)  # cause 2: this node's standard
            self.assertIn(self.DEP_REF, msg)          # what was staged
            self.assertIn("not in the selected standard", msg)  # the diagnostics decide

    def test_gate_syntax_check_dep_warning_beside_node_error_is_content_fail(self) -> None:
        # The attribution probe asks the compiler ("does the dependency closure pass on its
        # own?"), never the diagnostics text. A clean dependency still PRINTS default-on
        # warnings (-Wampersand / -Wtabs / -Wunderflow) that name its file, and gfortran emits
        # them in the same run whose only Error is in the node's own source. Attributing by
        # "the dep's filename appears in the output" would convert that self-repairable
        # finding into a permanent fail_closed (re-certifying the dep would pass, so every
        # --resume would fail again). It must stay a content failure.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def fake(args):
                if self._call_kind(args) == "canary":
                    return {"ok": True, "skipped": False, "command_id": "canary"}
                if self._call_kind(args) == "probe":
                    # the dep compiles clean on its own: the warning does not fail its gate
                    return {"ok": True, "skipped": False, "command_id": "probe",
                            "stderr": "dep_model.f90:8:12:\n\nWarning: Missing '&' in "
                                      "continued character constant [-Wampersand]\n"}
                return {"ok": False, "return_code": 1, "command_id": "sid", "skipped": False,
                        "stderr": "dep_model.f90:8:12:\n\nWarning: Missing '&' in continued "
                                  "character constant [-Wampersand]\n"
                                  "spec_x_model.f90:7:8:\n\nError: Function 'undefined_thing' "
                                  "at (1) has no IMPLICIT type\n"}

            stage_p, closure_p = self._with_dep(c)
            with stage_p, closure_p, self._patch_syntax(fake):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "fail")
            meta = out
            self.assertEqual(meta["status"], "fail")
            self.assertIn("no IMPLICIT type", meta["failure_excerpt"])

    def test_gate_syntax_check_unregistered_optional_compiler_skipped(self) -> None:
        # An optional METDSL_SYNTAX_COMPILERS entry with no registered adapter is recorded
        # skipped, NOT crashed: the tool raises ValueError for an unknown compiler, which
        # would otherwise propagate as a transport fail_closed even though gfortran passed.
        import tempfile
        from tools.hooks.syntax_evidence import read_syntax_evidence
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo, env={"METDSL_SYNTAX_COMPILERS": "gfortran,frtxx"})

            def fake(args):
                # frtxx is unregistered, so the tool must never be called for it.
                self.assertEqual(args["compiler"], "gfortran")
                return {"ok": True, "return_code": 0, "command_id": "sid",
                        "compiler_version": "GNU Fortran 13", "skipped": False}

            with self._patch_syntax(fake):
                out = c._gate_syntax_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "pass")
            ev = read_syntax_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            by_compiler = {s["compiler"]: s for s in ev["stages"]}
            self.assertEqual(by_compiler["gfortran"]["status"], "pass")
            self.assertEqual(by_compiler["frtxx"]["status"], "skipped")
            self.assertIn("no registered", by_compiler["frtxx"]["reason"])


class DeterministicStaticTest(unittest.TestCase):
    """The generate.gate static checker (_gate_static_check) runs in-process: it runs
    validate_workspace_root + validate_pipeline_semantics --stage post_generate and returns the
    `static` section of gate_meta; a violation is a content failure the gate routes to
    generate.generate (warm resume). The unioned gate_meta.json + skip-when-dirty behavior is
    exercised by DeterministicGateTest."""

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

    def test_gate_static_check_pass_returns_section(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_run(self._fake_run(0, 0)):
                out = c._gate_static_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "pass")
            meta = out
            self.assertEqual(meta["status"], "pass")
            self.assertIsNone(meta["failure_category"])

    def test_gate_static_check_post_generate_violation_is_content_fail(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            with self._patch_run(self._fake_run(0, 1)):
                out = c._gate_static_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "fail")
            meta = out
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "post_generate_violation")
            self.assertIn("pg-out", meta["failure_excerpt"])

    def test_gate_static_check_workspace_root_violation_short_circuits(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            # workspace_root fails first; post_generate must NOT run (pg_rc would also fail,
            # but the category proves the short-circuit picked workspace_root).
            with self._patch_run(self._fake_run(1, 1)):
                out = c._gate_static_check(refs, "child-1", "captok")
            self.assertEqual(out["status"], "fail")  # workspace_root short-circuit
            meta = out
            self.assertEqual(meta["status"], "fail")
            self.assertEqual(meta["failure_category"], "workspace_root_violation")

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
            # Contract-conformant meta: this test isolates the FRESHNESS gate, and the
            # stage-meta contract gate (a separate pass condition) must not be what fails it.
            meta_path.write_text(json.dumps(_conformant_stage_meta()), encoding="utf-8")
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


class DeterministicGateTest(unittest.TestCase):
    """generate.gate unions the lint / syntax / static checkers into ONE gate_meta.json
    (_gate_inproc). These tests drive the REAL writer (mocking only the underlying tools /
    validators, never hand-authoring gate_meta) so the on-disk verdict is the production shape,
    then run it through determine_substep_status -> classify_failure -> _read_repair_findings."""

    def _conductor(self, repo: Path, env: dict[str, str] | None = None) -> "wc.Conductor":
        return wc.Conductor(repo_root=repo, orchestration_id="t",
                            orchestration_agent_run_id="x", backend="claude", env=env or {})

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_1", pipeline_id="x_1", source_id="src_1", binary_id="bin_1")

    def _seed(self, repo: Path, refs: wc.NodeRefs, language: str = "fortran") -> None:
        src = repo / refs.source_dir() / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "spec_x_model.f90").write_text(
            "module spec_x_model\nend module spec_x_model\n", encoding="utf-8")
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "spec.ir.yaml").write_text(
            f"impl_defaults:\n  toolchain:\n    language: {language}\n"
            f"    standard: f2008\n  target:\n    backend: openmp\n", encoding="utf-8")

    def _patches(self, linter, syntax, run=None):
        import sys
        from unittest import mock
        sys.path.insert(0, str(Path("mcp_servers").resolve()))
        import build_runtime_server  # type: ignore
        ps = [
            mock.patch.object(build_runtime_server, "tool_run_linter", linter),
            mock.patch.object(build_runtime_server, "tool_run_syntax_check", syntax),
        ]
        if run is not None:
            ps.append(mock.patch.object(wc.subprocess, "run", run))
        return ps

    @staticmethod
    def _syntax_fail(args):
        # main stage fails; the invocation canary passes (isolates the failure to the source).
        if str(args.get("project_dir", "")).endswith("_canary"):
            return {"ok": True, "skipped": False, "command_id": "canary"}
        return {"ok": False, "return_code": 1, "command_id": "sid", "skipped": False,
                "stderr": "Error: IMPLICIT NONE with spec list"}

    @staticmethod
    def _syntax_pass(args):
        return {"ok": True, "return_code": 0, "command_id": "sid",
                "compiler": args["compiler"], "compiler_version": "GNU Fortran 13",
                "skipped": False}

    def test_union_verdict_lint_and_syntax_fail_static_skipped(self) -> None:
        """New test 1 + 3: lint fail + syntax fail -> one gate_meta with failure_categories
        [syntax_error, lint_findings], sectioned excerpt, static skipped; the on-disk verdict
        flows determine -> classify -> read_repair as a single union warm reuse."""
        import contextlib
        import tempfile
        from tools.hooks.lint_evidence import read_lint_evidence
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            ran_static = {"called": False}

            def linter(args):
                return {"ok": False, "return_code": 1, "command_id": "cid",
                        "preset": "fortitude", "stdout": "S001 line too long"}

            def run(cmd, **kwargs):  # static must NOT run when lint/syntax failed
                ran_static["called"] = True
                raise AssertionError("static checker must be skipped on a dirty source")

            with contextlib.ExitStack() as stack:
                for p in self._patches(linter, self._syntax_fail, run):
                    stack.enter_context(p)
                out = c._gate_inproc(refs, "child-1", "captok")

            self.assertEqual(out["returncode"], 0)  # content fail, not transport
            self.assertFalse(ran_static["called"])
            meta = json.loads((repo / refs.source_dir() / "gate_meta.json").read_text())
            self.assertEqual(meta["gate_status"], "fail")
            self.assertEqual(meta["failure_categories"], ["syntax_error", "lint_findings"])
            # Canonical excerpt section order: syntax before lint.
            self.assertLess(meta["failure_excerpt"].index("[syntax]"),
                            meta["failure_excerpt"].index("[lint]"))
            self.assertIn("IMPLICIT NONE", meta["failure_excerpt"])
            self.assertIn("S001", meta["failure_excerpt"])
            self.assertEqual(meta["checkers"]["static"]["status"], "skipped")
            self.assertEqual(meta["checkers"]["static"]["skipped_reason"],
                             "lint_or_syntax_failed")
            # New test 6: evidence written even on a content fail (ok=false).
            ev = read_lint_evidence(pipeline_root=repo / refs.pipeline_ref, source_id="src_1")
            assert ev is not None
            self.assertFalse(ev["ok"])

            # Same on-disk verdict flows the deterministic pipeline as ONE union.
            paths = [refs.source_dir() + "/gate_meta.json"]
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "gate", paths)[0], "fail")
            outcomes = [wc.SubstepOutcome("g", "pass", [], 0),
                        wc.SubstepOutcome("gate", "fail", [], 0)]
            d = c.classify_failure(refs, "generate", outcomes)
            self.assertEqual((d.action, d.target_phase, d.repair_strategy),
                             ("retry", "generate", "reuse"))
            self.assertEqual(d.reason, "gate_syntax_error+lint_findings")
            findings = c._read_repair_findings(refs, d.reason, "generate")
            self.assertIn("[syntax]", findings)
            self.assertIn("[lint]", findings)

    def test_all_clean_runs_static_and_passes(self) -> None:
        """New test 3 (pass path): lint+syntax pass -> static runs; all clean -> gate pass."""
        import contextlib
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)
            static_calls: list[str] = []

            def linter(args):
                return {"ok": True, "return_code": 0, "command_id": "cid", "preset": "fortitude"}

            def run(cmd, **kwargs):
                script = next((x for x in cmd if str(x).endswith(".py")), "")
                static_calls.append(script)
                return wc.subprocess.CompletedProcess(cmd, 0, "ok", "")

            with contextlib.ExitStack() as stack:
                for p in self._patches(linter, self._syntax_pass, run):
                    stack.enter_context(p)
                out = c._gate_inproc(refs, "child-1", "captok")

            self.assertEqual(out["returncode"], 0)
            meta = json.loads((repo / refs.source_dir() / "gate_meta.json").read_text())
            self.assertEqual(meta["gate_status"], "pass")
            self.assertEqual(meta["failure_categories"], [])
            self.assertIsNone(meta["failure_excerpt"])
            self.assertEqual(meta["checkers"]["static"]["status"], "pass")
            # static actually ran the post_generate + workspace_root validators.
            self.assertTrue(any(s.endswith("validate_workspace_root.py") for s in static_calls))
            self.assertTrue(
                any(s.endswith("validate_pipeline_semantics.py") for s in static_calls))
            paths = [refs.source_dir() + "/gate_meta.json"]
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "gate", paths)[0], "pass")

    def test_syntax_runtimeerror_suppresses_gate_meta_and_is_transport_fail(self) -> None:
        """New test 5: a syntax attribution RuntimeError (fail_closed) dominates a co-occurring
        lint content-fail — it propagates, so NO gate_meta is written and the substep is a
        transport failure (rc != 0), routed through _run_deterministic_substep."""
        import contextlib
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def linter(args):  # lint content-fails, but fail_closed must win
                return {"ok": False, "return_code": 1, "command_id": "cid",
                        "preset": "fortitude", "stdout": "S001 line too long"}

            def syntax(args):
                # The invocation canary itself fails -> _gate_syntax_check raises RuntimeError.
                if str(args.get("project_dir", "")).endswith("_canary"):
                    return {"ok": False, "skipped": False, "stderr": "bad -std"}
                return {"ok": False, "return_code": 1, "command_id": "sid", "skipped": False,
                        "stderr": "Error"}

            from unittest import mock
            with contextlib.ExitStack() as stack:
                for p in self._patches(linter, syntax):
                    stack.enter_context(p)
                stack.enter_context(
                    mock.patch.object(c, "_capability_token", lambda arid: "captok"))
                proc = c._run_deterministic_substep(
                    refs, "generate", "gate", "child-1",
                    {"step": "generate", "substep": "gate"})
            self.assertNotEqual(proc.returncode, 0)  # transport fail_closed
            self.assertFalse((repo / refs.source_dir() / "gate_meta.json").exists())

    def test_static_checker_exception_is_transport_fail_not_content_pass(self) -> None:
        """An unexpected error in the static checker (e.g. the validator subprocess raising)
        must propagate out of _gate_inproc — NOT be swallowed into a content pass. lint+syntax
        pass so static runs; its subprocess raises; _run_deterministic_substep converts it to a
        transport failure (rc != 0) with no gate_meta written."""
        import contextlib
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            refs = self._refs()
            self._seed(repo, refs)
            c = self._conductor(repo)

            def linter(args):
                return {"ok": True, "return_code": 0, "command_id": "cid", "preset": "fortitude"}

            def run(cmd, **kwargs):  # the static checker's validator subprocess blows up
                raise OSError("python3 not found")

            from unittest import mock
            with contextlib.ExitStack() as stack:
                for p in self._patches(linter, self._syntax_pass, run):
                    stack.enter_context(p)
                stack.enter_context(
                    mock.patch.object(c, "_capability_token", lambda arid: "captok"))
                proc = c._run_deterministic_substep(
                    refs, "generate", "gate", "child-1",
                    {"step": "generate", "substep": "gate"})
            self.assertNotEqual(proc.returncode, 0)  # transport fail_closed, not content pass
            self.assertFalse((repo / refs.source_dir() / "gate_meta.json").exists())


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
            # Contract-conformant meta: this test isolates the FRESHNESS gate, and the
            # stage-meta contract gate (a separate pass condition) must not be what fails it.
            meta_path.write_text(json.dumps(_conformant_stage_meta()), encoding="utf-8")
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
        # R2: semantic_review.json is the judge's ONLY deliverable, so it is the only
        # warm-resume-recoverable artifact.
        self.assertEqual(
            wc.classify_post_judge_violations(
                ["workspace/pipelines/x/runs/r/n/semantic_review.json: review_method must be llm_semantic_review"]),
            "recoverable")
        # R2: verdict.json is host-authored at execute; the derived aggregate_verdict / summary /
        # validate_meta are host-authored at post_judge from that same verdict. A gate violation
        # naming any of them is a conductor derivation defect the judge cannot fix (re-running it
        # would re-derive identically) -> unrecoverable (no wasted warm-resume).
        for base in ("verdict.json", "aggregate_verdict.json", "summary.json", "validate_meta.json"):
            self.assertEqual(
                wc.classify_post_judge_violations(
                    [f"workspace/pipelines/x/runs/r/n/{base}: counts must equal per_test aggregate"]),
                "unrecoverable", base)
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

    # -- determine_substep_status: judge (semantic_review only), pre/post_judge (meta) --
    def test_determine_judge_passes_on_semantic_decision_only(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = self._conductor(repo)
            # R2: the judge authors semantic_review.json only; verdict.json is host-authored
            # at execute (and is ∈ {pass,xfail} whenever the judge runs). So the judge passes
            # iff semantic_review.decision == "pass", regardless of per_test.
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "pass"}], decision="pass")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "pass")
            # an all-xfail execute verdict still passes the judge on a pass decision
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "xfail"}], decision="pass")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "pass")
            # a semantic_review fail -> judge fail (independent of the execute verdict)
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "pass"}], decision="fail")
            self.assertEqual(
                c.determine_substep_status(refs, "validate", "judge", [])[0], "fail")
            # a missing/empty decision -> judge fail (a judge that produced no clear verdict)
            self._seed_judge(repo, refs,
                             per_test=[{"test_id": "t1", "status": "pass"}], decision="")
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

    def test_transient_retry_tombstone_reaches_the_real_superseded_file(self) -> None:
        """The same seam for the WI-B transient retry, driven through the REAL run_substep loop
        (real `runtime()` subprocess, real `new_agent_run_id`, real FS): a leaf dies of a dropped
        connection, the loop re-launches it, and the DEAD attempt must land in the actual
        `reopen/superseded_runs.json` the completion check reads. If it does not, the recovered
        run passes every phase and then fails at the very end on an orphaned agent_graph edge —
        the failure mode the tombstone exists to prevent."""
        flake = "API Error: Connection closed mid-response. The response above may be incomplete."
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo_with_real_tools(tmp)
            oid = "orch_t2"
            (repo / "workspace" / "orchestrations" / oid).mkdir(parents=True)
            spawned: list[str] = []

            class _C(wc.Conductor):
                # Only the bookkeeping calls that need a fully-provisioned orchestration
                # (capability tokens, prompt rendering, return tokens) are stubbed; the
                # tombstone goes through the real CLI, and the leaf output through the real FS.
                def record_launch(self, child_arid, request):  # type: ignore[override]
                    return {"launch_prompt_text": "PROMPT"}

                def read_parent_return_token(self, child_arid):  # type: ignore[override]
                    return "rtok"

                def finalize_child(self, child_arid, return_token, reply_text,
                                   agent_run_json):  # type: ignore[override]
                    return {}

                def determine_substep_status(self, refs, phase, substep, allowed,
                                             min_mtime=0.0):  # type: ignore[override]
                    return "pass", ["out.json"]

                def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
                    spawned.append(kwargs["child_arid"])
                    return (wc.ProcResult(1, flake, "") if len(spawned) == 1
                            else wc.ProcResult(0, "done", ""))

                def _sleep_backoff(self, seconds):  # type: ignore[override]
                    pass

            c = _C(repo_root=repo, orchestration_id=oid, orchestration_agent_run_id="ORCH",
                   backend="claude", env=os.environ.copy())
            refs = wc.NodeRefs(
                node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
                ir_id="x_1_001", pipeline_id="x_1_001", source_id="src_1_001",
                binary_id="bin_1_001", run_id="run_1_001", source_binary_id="bin_1_001")
            with redirect_stdout(io.StringIO()):
                oc = c.run_substep(refs, "compile", "verify")

            self.assertEqual(oc.status, "pass")
            self.assertEqual(oc.attempts, 2)
            dead, live = spawned
            self.assertNotEqual(dead, live)
            self.assertEqual(oc.agent_run_id, live)
            from tools.orchestration_runtime import _load_superseded_run_ids
            self.assertEqual(_load_superseded_run_ids(repo, oid), {dead})
            # and the dead attempt's output survives — it is the only evidence of what killed it
            agents = repo / "workspace" / "orchestrations" / oid / "agents"
            self.assertEqual((agents / dead / "dialogs" / "leaf.stdout.log").read_text(), flake)
            self.assertEqual((agents / live / "dialogs" / "leaf.stdout.log").read_text(), "done")


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


_INCIDENT_DICT_REASON = {
    "violated_convention": "inert_dependency_call",
    "target_artifact": "src/model.f90",
    "reason": "binding probe invented",
}


class VerifyMetaSchemaWarmResumeTests(unittest.TestCase):
    """A verify leaf that authors a CONTRACT-VIOLATING stage meta (canonically:
    last_fail_reason as a structured dict instead of one plain string) must be caught at the
    write point and warm-resumed to re-author it. The runtime's write gate only checks PASS
    step_results, and a Generate reopen rotates a fresh source dir without deleting anything,
    so a violation that survives the phase is IMMUTABLE and unrepairable (E2E #4)."""

    def _refs(self) -> wc.NodeRefs:
        return wc.NodeRefs(
            node_key="component/spec_x@0.1.0", spec_path="spec/component/spec_x",
            ir_id="x_20260101_001", pipeline_id="x_20260101_001",
            source_id="src_20260101_001", binary_id="bin_20260101_001",
            run_id="run_20260101_001", source_binary_id="bin_20260101_001",
        )

    def _repair_requests(self, c: _FakeConductor) -> list[dict]:
        """The launch requests that carried a repair (the mini-loop's re-run turns). A
        non-repair launch still carries the literal `repair_reason: "none"` the templates use."""
        return [
            cap["--request-json"]
            for sub, cap in c.calls
            if sub == "record-launch"
            and cap.get("--request-json", {}).get("repair_reason") not in (None, "none")
        ]

    def _meta_path(self, repo: Path, refs: wc.NodeRefs, phase: str) -> Path:
        if phase == "compile":
            return repo / refs.ir_ref / "ir_meta.json"
        return repo / refs.source_dir() / "source_meta.json"

    def _write_meta(self, repo: Path, refs: wc.NodeRefs, phase: str, meta: dict) -> Path:
        path = self._meta_path(repo, refs, phase)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta), encoding="utf-8")
        return path

    def _conductor(self, repo: Path, refs: wc.NodeRefs, phase: str,
                   metas_by_verify_attempt: list[dict]) -> _FakeConductor:
        """A fake whose verify leaf authors `metas_by_verify_attempt[n-1]` on its n-th run (an
        empty list models a leaf that writes NOTHING), and whose verify gate mirrors the real
        one (status + freshness + stage-meta contract). Every other substep passes unless
        `status_fn` says otherwise."""
        meta_path = self._meta_path(repo, refs, phase)
        state = {"verify_runs": 0}

        class _C(_FakeConductor):
            verify_leaf_returncode = 0

            def _write_lineage(self, r):  # type: ignore[override]
                return []

            def _ensure_fresh_producer_id(self, r, p):  # type: ignore[override]
                return None

            # The real precondition (a claude session transcript on disk for the failed verify
            # leaf) cannot hold for a fake arid; the loop's resumability guard is pinned
            # separately by test_no_warm_session_skips_loop_and_escalates.
            def _verify_session_resumable(self, verify_arid):  # type: ignore[override]
                return True

            def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
                if self._current_substep != "verify":
                    return wc.ProcResult(0, "", "")
                n = state["verify_runs"]
                state["verify_runs"] += 1
                if metas_by_verify_attempt:
                    meta = metas_by_verify_attempt[
                        min(n, len(metas_by_verify_attempt) - 1)]
                    meta_path.parent.mkdir(parents=True, exist_ok=True)
                    meta_path.write_text(json.dumps(meta), encoding="utf-8")
                return wc.ProcResult(self.verify_leaf_returncode, "", "")

            def run_substep(self, r, p, substep, **kwargs):  # type: ignore[override]
                self._current_substep = substep
                return super().run_substep(r, p, substep, **kwargs)

            def determine_substep_status(self, r, p, substep, allowed, min_mtime=0.0):  # type: ignore[override]
                if self.status_fn is not None:
                    self._sn = getattr(self, "_sn", 0) + 1
                    return self.status_fn(p, substep, self._sn), ["out.json"]
                if substep != "verify":
                    return "pass", ["out.json"]
                # Faithful mirror of the real verify gate: status + freshness + contract. The
                # contract clause delegates to the REAL _stage_meta_contract_findings.
                if not meta_path.exists():
                    return "fail", ["out.json"]
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                ok = (meta.get("verification_status") == "pass"
                      and meta_path.stat().st_mtime >= min_mtime
                      and not self._stage_meta_contract_findings(r, p))
                return ("pass" if ok else "fail"), ["out.json"]

        c = _C(repo_root=repo, orchestration_id="orch_x",
               orchestration_agent_run_id="ORCH", backend="claude", env={})
        c.calls = []
        c._current_substep = None
        c.verify_runs = state
        return c

    # -- 3b: the choke point (real gate) ------------------------------------------------

    def test_verify_with_type_invalid_meta_fails_even_when_status_pass(self) -> None:
        # A verify cannot certify its phase with a schema-violating meta, even when it declares
        # verification_status=pass — the violation would be persisted and become unrepairable.
        for phase, meta_name, ref_attr in (("generate", "source_meta.json", "source_dir"),
                                           ("compile", "ir_meta.json", "ir_ref")):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                repo, refs = Path(td), self._refs()
                path = self._write_meta(
                    repo, refs, phase,
                    _conformant_stage_meta("pass", last_fail_reason=_INCIDENT_DICT_REASON))
                c = wc.Conductor(repo_root=repo, orchestration_id="o",
                                 orchestration_agent_run_id="ORCH", backend="claude", env={})
                ref_dir = getattr(refs, ref_attr)
                allowed = [f"{ref_dir() if callable(ref_dir) else ref_dir}/{meta_name}"]
                mtime = path.stat().st_mtime
                self.assertEqual(
                    c.determine_substep_status(refs, phase, "verify", allowed,
                                               min_mtime=mtime - 100)[0], "fail")
                # Same meta with a plain-string reason and a pass status -> pass (the gate
                # rejects the TYPE violation, not the presence of a reason).
                path.write_text(json.dumps(_conformant_stage_meta("pass")), encoding="utf-8")
                self.assertEqual(
                    c.determine_substep_status(refs, phase, "verify", allowed,
                                               min_mtime=path.stat().st_mtime - 100)[0], "pass")

    def test_pass_status_meta_missing_key_fails_verify_instead_of_crashing(self) -> None:
        # A pass-status meta with a MISSING required key used to reach write_step_result and
        # raise ValueError there (crashing the conductor). It now fails the verify gate, which
        # routes it into the warm-retry loop instead.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            meta = _conformant_stage_meta("pass")
            meta.pop("debug_mode")
            path = self._write_meta(repo, refs, "generate", meta)
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="ORCH", backend="claude", env={})
            allowed = [f"{refs.source_dir()}/source_meta.json"]
            self.assertEqual(
                c.determine_substep_status(refs, "generate", "verify", allowed,
                                           min_mtime=path.stat().st_mtime - 100)[0], "fail")
            self.assertEqual(
                c._stage_meta_contract_findings(refs, "generate"),
                ["source_meta.json missing required key 'debug_mode'"])

    def test_contract_findings_empty_when_meta_absent(self) -> None:
        # An absent meta is an ordinary verify failure (the leaf wrote nothing), NOT this
        # class — the mini-loop must not fire and consume budget on it.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertEqual(c._stage_meta_contract_findings(refs, "generate"), [])
            self.assertEqual(c._stage_meta_contract_findings(refs, "build"), [])

    # -- 3c: the warm-resume mini-loop -------------------------------------------------

    def test_verify_meta_schema_warm_resumes_to_pass(self) -> None:
        # The violating meta is re-authored by the SAME (warm-resumed) verify leaf, and the
        # phase certifies pass. Assert the repair payload is the slim reuse shape and that the
        # findings name the actual violation.
        for phase in ("generate", "compile"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                repo, refs = Path(td), self._refs()
                bad = _conformant_stage_meta("fail", last_fail_reason=_INCIDENT_DICT_REASON)
                good = _conformant_stage_meta("pass")
                self._write_meta(repo, refs, phase, bad)
                c = self._conductor(repo, refs, phase, [bad, good])
                oc = c.run_phase(refs, phase)

                self.assertEqual(oc.status, "pass")
                self.assertEqual(oc.decision.action, "advance")
                self.assertEqual(c.verify_runs["verify_runs"], 2)  # original + one repair
                # The superseded verify attempt was tombstoned so a later --resume can pass.
                sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
                self.assertEqual(len(sup), 1)
                self.assertIn(f"{phase}_verify_meta_schema_warm_resume_orphan",
                              sup[0]["--reason"])
                # The repair is a reuse turn aimed at the VERIFY leaf itself (not the producer),
                # so a resumable session inherits its context.
                repairs = self._repair_requests(c)
                self.assertEqual(len(repairs), 1)
                self.assertEqual(repairs[0]["repair_reason"], "verify_meta_schema")
                self.assertEqual(repairs[0]["repair_strategy"], "reuse")
                self.assertEqual(repairs[0]["issue_severity"], "major")
                # The repair targets the verify leaf's own arid (the last substep of the phase).
                self.assertEqual(repairs[0]["repair_target_agent_run_id"],
                                 f"child-{len(wc.SUBSTEPS[phase])}")
                self.assertEqual(repairs[0]["substep"], "verify")

    def test_verify_meta_schema_findings_name_the_violation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            bad = _conformant_stage_meta("fail", last_fail_reason=_INCIDENT_DICT_REASON)
            self._write_meta(repo, refs, "generate", bad)
            c = self._conductor(repo, refs, "generate", [bad, _conformant_stage_meta("pass")])
            c.run_phase(refs, "generate")

            repairs = self._repair_requests(c)
            self.assertEqual(len(repairs), 1)
            # The findings are PURE gate output — the slim renderer fences them as untrusted
            # data the leaf is told not to obey, so an instruction smuggled in here would be
            # both ignored and self-contradictory.
            self.assertEqual(
                repairs[0]["repair_findings"],
                "source_meta.json last_fail_reason must be string or null")
            # "Re-author only the meta" is imposed STRUCTURALLY instead: the repair turn's
            # writable set is the meta alone, so the producer sources (which generate.verify's
            # normal allowed_output_paths also lists) are not writable on this turn.
            self.assertEqual(repairs[0]["allowed_output_paths"],
                             [f"{refs.source_dir()}/source_meta.json"])

    def test_verify_meta_schema_repair_then_normal_severity_routing(self) -> None:
        # The repaired meta records a GENUINE fail (with a readable string reason). The
        # mini-loop exits after one turn and the ordinary verify-severity gate routes it —
        # repairing the schema must not swallow the real finding.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            bad = _conformant_stage_meta("fail", last_fail_reason=_INCIDENT_DICT_REASON)
            repaired = _conformant_stage_meta(
                "fail", last_fail_reason="model.f90: z_b associate directive missing",
                last_fail_severity="minor")
            self._write_meta(repo, refs, "generate", bad)
            c = self._conductor(repo, refs, "generate", [bad, repaired])
            oc = c.run_phase(refs, "generate")

            self.assertEqual(oc.status, "fail")
            self.assertEqual(c.verify_runs["verify_runs"], 2)  # exactly one repair turn
            self.assertNotEqual(oc.decision.reason, "generate_fail_meta_schema")
            self.assertEqual(oc.decision.action, "retry")  # verify_minor -> same-phase repair

    def test_verify_meta_schema_budget_exhaustion_escalates(self) -> None:
        # A leaf that keeps writing the violating meta exhausts MAX_ATTEMPTS_PER_PHASE and
        # terminalizes as `{phase}_fail_meta_schema` — bounded, never an infinite loop, and
        # never routed by the (untrustworthy) severity field.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            bad = _conformant_stage_meta(
                "fail", last_fail_reason=_INCIDENT_DICT_REASON, last_fail_severity="minor")
            self._write_meta(repo, refs, "generate", bad)
            c = self._conductor(repo, refs, "generate", [bad])
            oc = c.run_phase(refs, "generate")

            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "escalate")
            self.assertEqual(oc.decision.reason, "generate_fail_meta_schema")
            # original + MAX_ATTEMPTS_PER_PHASE repair turns, then stop.
            self.assertEqual(c.verify_runs["verify_runs"], 1 + wc.MAX_ATTEMPTS_PER_PHASE)

    def test_mini_loop_does_not_fire_on_a_conformant_failing_verify(self) -> None:
        # An ordinary semantic verify fail (conformant meta) must not spawn any repair turn
        # here — that is the severity gate's job.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            meta = _conformant_stage_meta("fail", last_fail_reason="physics mismatch",
                                          last_fail_severity="minor")
            self._write_meta(repo, refs, "generate", meta)
            c = self._conductor(repo, refs, "generate", [meta])
            oc = c.run_phase(refs, "generate")
            self.assertEqual(oc.status, "fail")
            self.assertEqual(c.verify_runs["verify_runs"], 1)  # no repair turn
            self.assertEqual([cap for s, cap in c.calls if s == "add-superseded-runs"], [])

    # -- 3d / 3e: routing guard + findings recomputation --------------------------------

    def test_classify_failure_ignores_severity_on_schema_violating_meta(self) -> None:
        # severity=minor on a schema-violating meta must NOT route through the severity table:
        # the meta's fields are not trustworthy inputs to a decision.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            self._write_meta(repo, refs, "generate", _conformant_stage_meta(
                "fail", last_fail_reason=_INCIDENT_DICT_REASON, last_fail_severity="minor"))
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="ORCH", backend="claude", env={})
            # SUBSTEPS["generate"] == ("generate","gate","verify"); verify is index 2.
            outcomes = [wc.SubstepOutcome("g", "pass", [], 0),
                        wc.SubstepOutcome("gate", "pass", [], 0),
                        wc.SubstepOutcome("v", "fail", [], 0)]
            d = c.classify_failure(refs, "generate", outcomes)
            self.assertEqual((d.action, d.reason), ("escalate", "generate_fail_meta_schema"))

    def test_read_repair_findings_never_returns_a_dict_reason(self) -> None:
        # The generic `verify_` route reads the meta's own last_fail_reason — which in this
        # defect class is the field that may be a dict. It must degrade to None (full prompt),
        # never hand a dict downstream.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            self._write_meta(repo, refs, "generate", _conformant_stage_meta(
                "fail", last_fail_reason=_INCIDENT_DICT_REASON))
            c = wc.Conductor(repo_root=repo, orchestration_id="o",
                             orchestration_agent_run_id="ORCH", backend="claude", env={})
            self.assertIsNone(c._read_repair_findings(refs, "verify_minor", "generate"))

    # -- loop preconditions: only repair a meta THIS verify leaf authored -----------------

    def test_transport_failed_verify_is_not_repaired(self) -> None:
        # A leaf that died of an infra/transport error (usage limit, OOM) authored nothing. If
        # the loop repaired it, `outcomes[-1] = oc` would erase the nonzero returncode that
        # run_phase's transport branch fail_closes on — silently certifying a phase whose verify
        # leaf never ran to completion.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            bad = _conformant_stage_meta("fail", last_fail_reason=_INCIDENT_DICT_REASON)
            self._write_meta(repo, refs, "generate", bad)
            c = self._conductor(repo, refs, "generate", [bad, _conformant_stage_meta("pass")])
            # The verify leaf exits nonzero (transport), leaving the producer's dirty meta.
            c.verify_leaf_returncode = 1
            oc = c.run_phase(refs, "generate")

            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.decision.action, "fail_closed")
            self.assertIn("leaf_transport_error", oc.decision.reason)
            self.assertEqual(c.verify_runs["verify_runs"], 1)  # the dead leaf only
            self.assertEqual(self._repair_requests(c), [])  # no repair turn
            # run_phase's own transport branch tombstones; the mini-loop's must not fire.
            self.assertEqual(
                [cap for s, cap in c.calls if s == "add-superseded-runs"
                 and "meta_schema" in cap["--reason"]], [])

    def test_producer_authored_dirty_meta_is_not_attributed_to_a_no_op_verify(self) -> None:
        # The freshness clause exists to reject an inspect-only verify that writes NOTHING. If
        # the loop fired on a meta the PRODUCER left dirty, it would hand that no-op verify a
        # "just fix the meta" turn whose rewrite also satisfies the freshness gate — letting it
        # certify `pass` without doing the verification it skipped. So the loop must only claim
        # a meta whose mtime proves THIS verify leaf wrote it.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            bad = _conformant_stage_meta("pass", last_fail_reason=_INCIDENT_DICT_REASON)
            meta_path = self._write_meta(repo, refs, "generate", bad)
            os.utime(meta_path, (1_000.0, 1_000.0))  # authored long before the verify launch
            c = self._conductor(repo, refs, "generate", [])  # verify leaf writes nothing
            oc = c.run_phase(refs, "generate")

            self.assertEqual(oc.status, "fail")
            self.assertEqual(c.verify_runs["verify_runs"], 1)  # original verify, no repair turn
            self.assertEqual(self._repair_requests(c), [])
            # Routed as the meta-schema class (escalate), NOT silently passed.
            self.assertEqual((oc.decision.action, oc.decision.reason),
                             ("escalate", "generate_fail_meta_schema"))

    def test_no_warm_session_skips_loop_and_escalates(self) -> None:
        # Without a resumable session the launch degrades to a COLD full prompt, which carries
        # no findings at all — the leaf would re-verify blind, 3x, then escalate anyway. Skip
        # straight to the escalate instead of burning the budget.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            bad = _conformant_stage_meta("fail", last_fail_reason=_INCIDENT_DICT_REASON)
            self._write_meta(repo, refs, "generate", bad)
            c = self._conductor(repo, refs, "generate", [bad, _conformant_stage_meta("pass")])
            c._verify_session_resumable = lambda arid: False  # type: ignore[assignment]
            oc = c.run_phase(refs, "generate")

            self.assertEqual(c.verify_runs["verify_runs"], 1)  # the original verify only
            self.assertEqual(self._repair_requests(c), [])
            self.assertEqual((oc.decision.action, oc.decision.reason),
                             ("escalate", "generate_fail_meta_schema"))

    def test_session_lost_mid_loop_stops_instead_of_degrading_to_cold(self) -> None:
        # Resumability is re-checked EVERY iteration: each repair turn is a new session that may
        # itself not be resumable. Without the re-check, iteration 2 would silently launch a COLD
        # full prompt — which carries no findings at all — and re-verify blind.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            bad = _conformant_stage_meta("fail", last_fail_reason=_INCIDENT_DICT_REASON)
            self._write_meta(repo, refs, "generate", bad)
            c = self._conductor(repo, refs, "generate", [bad])  # leaf keeps writing the dict
            # Only the ORIGINAL verify leaf's session survives; the repair turn's does not.
            original_verify_arid = f"child-{len(wc.SUBSTEPS['generate'])}"
            c._verify_session_resumable = (  # type: ignore[assignment]
                lambda arid: arid == original_verify_arid)
            oc = c.run_phase(refs, "generate")

            self.assertEqual(len(self._repair_requests(c)), 1)  # one turn, then stop
            self.assertEqual(c.verify_runs["verify_runs"], 2)
            self.assertEqual((oc.decision.action, oc.decision.reason),
                             ("escalate", "generate_fail_meta_schema"))

    def test_producer_substep_failure_does_not_trigger_the_verify_loop(self) -> None:
        # The loop is scoped to a failed VERIFY substep. Without that scoping a producer failure
        # (index 0) with a dirty meta on disk would spawn 3 spurious verify turns AND overwrite
        # outcomes[-1] — dropping the actually-failing producer from step_result.
        # substep_agent_run_ids and pointing _producer_arid at a verify leaf.
        with tempfile.TemporaryDirectory() as td:
            repo, refs = Path(td), self._refs()
            self._write_meta(repo, refs, "generate", _conformant_stage_meta(
                "fail", last_fail_reason=_INCIDENT_DICT_REASON))
            c = self._conductor(repo, refs, "generate", [])
            c.status_fn = lambda phase, substep, n: (
                "fail" if substep == "generate" else "pass")
            oc = c.run_phase(refs, "generate")

            self.assertEqual(oc.status, "fail")
            self.assertEqual(c.verify_runs["verify_runs"], 0)  # verify never launched
            self.assertEqual(self._repair_requests(c), [])
            self.assertEqual([cap for s, cap in c.calls if s == "add-superseded-runs"], [])
            # The failed producer is still the recorded outcome.
            self.assertEqual(oc.substep_arids, ["child-1"])
            self.assertEqual(c._producer_arid["generate"], "child-1")

    def test_build_launch_request_slim_for_verify_meta_repair(self) -> None:
        # The verify repair renders the findings-only SLIM prompt (warm resume), and that
        # prompt satisfies the launch-integrity marker set for a slim request.
        from tools.orchestration_runtime import (
            _render_slim_repair_launch_prompt,
            _required_launch_prompt_markers,
        )
        refs = self._refs()
        repair = {
            "issue_severity": "major", "repair_strategy": "reuse",
            "repair_target_agent_run_id": "child-1", "repair_reason": "verify_meta_schema",
            "repair_findings": "source_meta.json last_fail_reason must be string or null",
        }
        req = wc.build_launch_request(
            refs, step="generate", substep="verify", orchestration_id="orch_x",
            orchestration_agent_run_id="parent", child_agent_run_id="child-2",
            agent_model="m", workflow_mode="dev", repair=repair, warm_resume=True)
        self.assertTrue(req.get("warm_resume"))
        self.assertEqual(req["skill_must_read_refs"], "")
        # generate.verify's output set is source_meta.json on ALL turns (it never rewrites the
        # producer sources), so the meta-schema repair turn is no different — no special case.
        self.assertEqual(req["allowed_output_paths"],
                         [f"{refs.source_dir()}/source_meta.json"])

        prompt = _render_slim_repair_launch_prompt(req)
        for marker in _required_launch_prompt_markers(req):
            self.assertIn(marker, prompt)
        # The resumed leaf is the VERIFY leaf; the prompt must not tell it it is the producer.
        self.assertIn("generate.verify", prompt)
        self.assertIn(repair["repair_findings"], prompt)
        # The prompt's TRUSTED deliverable list names only the meta, so "re-write your
        # deliverables" cannot be read as license to touch the sources.
        self.assertNotIn("_model.f90", prompt)

    def test_verify_session_resumable_predicate(self) -> None:
        # The mini-loop tests stub this predicate (a fake arid can have no real session
        # transcript), so its BODY needs its own coverage: warm resume requires the claude
        # backend AND a surviving session for the failed verify leaf.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)

            def _conductor(backend: str) -> wc.Conductor:
                return wc.Conductor(repo_root=repo, orchestration_id="o",
                                    orchestration_agent_run_id="ORCH", backend=backend, env={})

            claude = _conductor("claude")
            claude._claude_session_resumable = lambda arid: arid == "live-arid"  # type: ignore[assignment]
            self.assertTrue(claude._verify_session_resumable("live-arid"))
            self.assertFalse(claude._verify_session_resumable("gc-ed-arid"))

            # codex has no session-resume primitive at all -> never warm.
            codex = _conductor("codex")
            codex._claude_session_resumable = lambda arid: True  # type: ignore[assignment]
            self.assertFalse(codex._verify_session_resumable("live-arid"))

    def test_build_launch_request_narrows_a_normal_verify_launch_too(self) -> None:
        # generate.verify's output set is source_meta.json on EVERY turn (not just a meta-schema
        # repair turn): it inspects the producer sources but never rewrites them. So an ordinary
        # verify launch carries exactly the meta and nothing under src/.
        refs = self._refs()
        req = wc.build_launch_request(
            refs, step="generate", substep="verify", orchestration_id="orch_x",
            orchestration_agent_run_id="parent", child_agent_run_id="child-2",
            agent_model="m", workflow_mode="dev")
        self.assertEqual(req["allowed_output_paths"],
                         [f"{refs.source_dir()}/source_meta.json"])
        self.assertFalse([p for p in req["allowed_output_paths"] if p.endswith("_model.f90")])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
