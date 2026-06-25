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


class NodeRefsTest(unittest.TestCase):
    def test_safe_and_spec_id(self) -> None:
        refs = wc.NodeRefs(node_key="component/dynamics_advdiff_flux_1d_upwind_center2@0.1.0",
                           spec_path="spec/...", ir_id="a_1_1", pipeline_id="a_1_1")
        self.assertEqual(refs.safe, "component__dynamics_advdiff_flux_1d_upwind_center2__0.1.0")
        self.assertEqual(refs.spec_id, "dynamics_advdiff_flux_1d_upwind_center2")


class PhaseStructureTest(unittest.TestCase):
    def test_substeps_and_roles(self) -> None:
        self.assertEqual(wc.SUBSTEPS["compile"], ("generate", "verify"))
        self.assertEqual(wc.SUBSTEPS["build"], (None,))
        self.assertEqual(wc.SUBSTEPS["validate"], ("execute", "judge"))
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
        self.assertEqual(wc.classify_verify_severity("minor", "dev").action, "retry")
        self.assertEqual(wc.classify_verify_severity("major", "dev").action, "fail_closed")
        self.assertEqual(wc.classify_verify_severity("critical", "dev").action, "fail_closed")
        # prod does not hard-fail on major (subject to retry limits elsewhere)
        self.assertEqual(wc.classify_verify_severity("major", "prod").action, "escalate")


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

    def _run_deterministic_substep(self, refs, phase, substep, child_arid, request):  # type: ignore[override]
        # Build / Validate.execute always run in-process; the fake body is a clean
        # success so the stubbed determine_substep_status/status_fn drives the outcome.
        return wc.ProcResult(0, "", "")

    def read_parent_return_token(self, child_arid):  # type: ignore[override]
        return "rtok"

    def read_case_ids(self, refs):  # type: ignore[override]
        return ()

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
             "record-launch", "finalize-child", "record-launch", "finalize-child",
             "write-step-result"]  # compile (2 leaf substeps)
            + ["check-step-completed", "workflow-launch-check",
               "record-launch", "finalize-child", "record-launch", "finalize-child",
               "write-step-result"]  # generate (2 leaf substeps)
            + ["check-step-completed", "workflow-launch-check",
               "record-launch", "record-child-return", "finalize-child",
               "write-step-result"]  # build (1 deterministic step)
            + ["check-step-completed", "workflow-launch-check",
               "record-launch", "record-child-return", "finalize-child",  # execute (deterministic)
               "record-launch", "finalize-child",  # judge (leaf)
               "write-step-result"]  # validate
            + ["set-status"]
        )
        self.assertEqual(subs, expected)

        # executor roles per phase
        wsr = [cap for s, cap in c.calls if s == "write-step-result"]
        by_step = {cap["--step"]: cap for cap in wsr}
        for substep_aware in ("compile", "generate", "validate"):
            self.assertEqual(by_step[substep_aware]["--agent-run-id"], "ORCH")
            self.assertEqual(len(by_step[substep_aware]["--result-json"]["substep_agent_run_ids"]), 2)
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
                     "conductor_phase_fail_closed", "sandbox_enforcement_violation"):
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

    def test_fail_closed_on_spec_attribution(self) -> None:
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail" if phase == "validate" else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("fail_closed", reason="physics_fail_spec")
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "fail_closed")
        self.assertEqual(c.calls[-1][1]["--status"], "fail_closed")

    def test_reopen_budget_exhausts_to_fail_closed(self) -> None:
        c = self._conductor()
        # validate always fails and always routes to reopen compile -> budget caps it.
        c.status_fn = lambda phase, substep, n: "fail" if phase == "validate" else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision(
            "reopen", target_phase="compile", reason="judge_ir")
        status = c.conduct(self._refs(), "validate")
        self.assertEqual(status, "fail_closed")
        reopens = [cap for s, cap in c.calls if s == "reopen-phase"]
        self.assertEqual(len(reopens), wc.MAX_ATTEMPTS_PER_PHASE)

    def test_same_phase_retry_terminalises_without_retry_decisions(self) -> None:
        # In-place retry is intentionally not done; a same-phase "retry" decision
        # (e.g. verify-minor) terminalizes via conduct rather than emitting the
        # error-prone retry_decisions bookkeeping.
        c = self._conductor()
        c.status_fn = lambda phase, substep, n: "fail" if (phase == "compile" and substep == "verify") else "pass"
        c.decision_fn = lambda phase, outcomes: wc.RouteDecision("retry", reason="verify_minor")
        status = c.conduct(self._refs(), "compile")
        self.assertIn(status, ("fail", "fail_closed"))
        self.assertEqual([s for s, _ in c.calls if s == "reopen-phase"], [])  # no cross-phase reopen
        compile_wsr = [cap for s, cap in c.calls
                       if s == "write-step-result" and cap["--step"] == "compile"]
        self.assertEqual(len(compile_wsr), 1)  # single attempt, one step_result
        rj = compile_wsr[0]["--result-json"]
        self.assertEqual(rj["status"], "fail")
        self.assertIsNone(rj["retry_decisions"])  # never emits retry_decisions
        self.assertEqual(len(rj["substep_agent_run_ids"]), 2)  # one attempt: generate + verify


class TransportFailureTest(unittest.TestCase):
    """A leaf transport failure (e.g. judge session limit, rc!=0) must route to a clean,
    resumable fail_closed WITHOUT calling write_step_result (which would crash on the judge
    semantic_review.json gate), and must tombstone the attempt's terminalized substep arids
    so a later --resume can reach pass (orphaned-arid completion guard)."""

    class _C(_FakeConductor):
        def _write_lineage(self, refs):  # type: ignore[override]
            pass  # avoid writing to the (fake) repo_root

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
        # validate.execute is deterministic (rc 0, pass); the judge leaf hits a session limit.
        c.spawn_leaf = lambda *a, **k: wc.ProcResult(1, "", "Claude usage limit reached")  # type: ignore[assignment]
        oc = c.run_phase(self._refs(), "validate")
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.decision.action, "fail_closed")
        self.assertTrue(oc.decision.reason.startswith("leaf_transport_error: leaf_exit=1"))
        subs = [s for s, _ in c.calls]
        # the core Bug-2 assertion: no write-step-result (so the judge gate never crashes)
        self.assertNotIn("write-step-result", subs)
        # the Bug-1 tombstone: both substep arids superseded
        sup = [cap for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertEqual(len(sup), 1)
        self.assertEqual(sup[0]["--run-ids"], ["child-1", "child-2"])
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

    def test_reuse_resume_flag_default_off(self) -> None:
        self.assertFalse(self._c(env={})._reuse_resume_enabled())
        self.assertTrue(self._c(env={"METDSL_CONDUCTOR_REUSE_RESUME": "1"})._reuse_resume_enabled())

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
                c.run_substep(refs, "generate", "generate", repair=repair)
            return cap

        # flag ON + reuse → resume the producer session; new arid pinned as session_id.
        cap = run({"METDSL_CONDUCTOR_REUSE_RESUME": "1"}, reuse)
        self.assertEqual(cap.get("session_id"), "child-1")
        self.assertEqual(cap.get("resume_session_id"), "producer-arid")
        # flag OFF → no resume even on reuse (session_id still pinned).
        off = run({}, reuse)
        self.assertEqual(off.get("session_id"), "child-1")
        self.assertIsNone(off.get("resume_session_id"))
        # restart never resumes (avoid anchoring on the defective reasoning), flag or not.
        restart = run({"METDSL_CONDUCTOR_REUSE_RESUME": "1"},
                      {"repair_strategy": "restart", "repair_target_agent_run_id": "producer-arid"})
        self.assertIsNone(restart.get("resume_session_id"))

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
                # codex backend is also wrapped (it gets a profile + sandbox_enforced too)
                captured.clear()
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
            self.assertIn("-std=f2008 -O2 -fopenmp -J$(OBJDIR) -I$(OBJDIR)", text)
            self.assertIn("$(RUNNER_OBJ): $(RUNNER_SRC) $(MODEL_OBJ)", text)
            self.assertIn(
                'test -x $(BINDIR)/$(BIN) || { echo "error: $(BINDIR)/$(BIN) not built',
                text)
            # recipe lines must be tab-indented
            self.assertIn("\n\t$(FC) $(FFLAGS) -c $(MODEL_SRC)", text)

    def test_authored_makefile_passes_post_generate_validators(self) -> None:
        from tools.validate_pipeline_semantics import (
            _validate_fortran_makefile_src_dir, _validate_makefile_bin_overridable,
            _validate_makefile_test_no_relink)
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

    def _write_dep_ir(self, repo: Path, refs: wc.NodeRefs) -> None:
        # Contract-faithful (phase_01 §V4): `transitive_deps` lists ONLY the indirect deps
        # (base, reached `via` mid); the direct dep (mid) is in `direct_deps` only. The build
        # closure is the union of the two — exercises that union (not transitive_deps alone).
        ir_dir = repo / refs.ir_ref
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "spec.ir.yaml").write_text(
            "impl_defaults:\n  toolchain:\n    language: fortran\n    standard: f2008\n"
            "    build_system: make\n  target:\n    backend: openmp\n"
            "dependency:\n"
            '  node_key: "component/top@0.1.0"\n'
            "  direct_deps:\n    - node_key: \"component/mid@0.1.0\"\n"
            "  transitive_deps:\n"
            '    - node_key: "component/base@0.1.0"\n      via: ["component/mid@0.1.0"]\n'
            "  all_nodes:\n"
            '    - node_key: "component/base@0.1.0"\n      topo_level: 0\n'
            '    - node_key: "component/mid@0.1.0"\n      topo_level: 1\n'
            '    - node_key: "component/top@0.1.0"\n      topo_level: 2\n',
            encoding="utf-8")

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
                "  direct_deps:\n    - node_key: \"component/base@0.1.0\"\n"
                "  transitive_deps: []\n"
                "  all_nodes:\n"
                '    - node_key: "component/base@0.1.0"\n      topo_level: 0\n'
                '    - node_key: "component/top@0.1.0"\n      topo_level: 1\n',
                encoding="utf-8")
            c = self._conductor(repo)
            self.assertEqual(c._dependency_closure_nodes(refs), ["component/base@0.1.0"])
            self.assertEqual(c._dependency_closure(refs), ["base"])

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
                "dependency:\n  direct_deps:\n    - node_key: \"component/base@0.1.0\"\n",
                encoding="utf-8")
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

    def test_stage_dependency_sources_raises_on_malformed_ir(self) -> None:
        # direct_deps non-empty but its entries carry no resolvable node_key -> the union
        # closure resolves empty (a compile-contract violation). Fail closed instead of
        # staging a leaf-shaped build.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = self._refs()
            self._write_ir(repo, refs, direct_deps="[{operations: [x]}]")
            obj_dir = repo / "workspace" / "tmp" / "arid_x" / "build"
            with self.assertRaisesRegex(RuntimeError, "closure resolved empty"):
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
                    pass

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
            # no verdict.json under the run node dir -> execute failed before judge
            decision = c.classify_failure(refs, "validate", [])
            self.assertEqual(decision.action, "retry")
            self.assertEqual(decision.target_phase, "generate")
            self.assertEqual(decision.repair_strategy, "restart")

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
            first = c.classify_failure(refs, "validate", [])
            self.assertEqual((first.action, first.target_phase), ("retry", "generate"))
            second = c.classify_failure(refs, "validate", [])
            self.assertEqual((second.action, second.target_phase), ("reopen", "compile"))
            self.assertEqual(second.reason, "validate_execute_fail_ir")
            # After escalating to Compile the counter resets: the Compile reopen
            # regenerates the IR, so the next execute failure (fresh artifacts) gets its
            # own Generate-retry-first cycle rather than immediately re-escalating.
            third = c.classify_failure(refs, "validate", [])
            self.assertEqual((third.action, third.target_phase), ("retry", "generate"))
            fourth = c.classify_failure(refs, "validate", [])
            self.assertEqual((fourth.action, fourth.target_phase), ("reopen", "compile"))


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
