#!/usr/bin/env python3
"""M-D: Z2 pure-function verify reviewer.

Covers the host side of the pure `generate.verify` channel added in
`tools/workflow_conductor.py` (the reviewer loop, verdict validation, the source_meta.json
projection, verdict_meta, the classify_failure verdict route, the `_maybe_warm_resume_verify_meta`
pure early-return) and the `tools/run_workflow.py` gate removal. Mirrors the producer suite
(`test_pure_leaf_producer.py`), whose fixtures it reuses.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.workflow_conductor as wc
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
from tools.tests.test_pure_leaf_producer import (
    _NODE, _SPEC_ID, _write_node, _PureFakeConductor, _valid_bundle, _envelope, _conductor,
)


def _verdict(status: str = "pass", *, severity: str | None = None,
             reason: str | None = None, findings: list | None = None) -> dict:
    if status == "pass":
        return {"verification_status": "pass", "issue_severity": "none",
                "last_fail_reason": None, "findings": []}
    return {"verification_status": "fail", "issue_severity": severity or "major",
            "last_fail_reason": reason or "generated model diverges from the controlled spec",
            "findings": findings or [{"summary": "mass is not conserved"}]}


def _verify_node(repo: Path) -> wc.NodeRefs:
    """A pure node with the producer's codegen_bundle.json already on disk (the reviewer input)."""
    refs = _write_node(repo)
    src = repo / refs.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    (src / "codegen_bundle.json").write_text(json.dumps(_valid_bundle()), encoding="utf-8")
    (repo / refs.spec_path).mkdir(parents=True, exist_ok=True)
    (repo / refs.spec_path / "controlled_spec.md").write_text(
        "# §5.1 interface\nconserves mass.\n", encoding="utf-8")
    return refs


# ======================================================================================
# _build_pure_verify_context
# ======================================================================================
class PureVerifyContextTests(unittest.TestCase):
    def test_context_has_the_four_verify_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            ctx = _conductor(repo)._build_pure_verify_context(refs)
            self.assertEqual(set(ctx),
                             {"controlled_spec_document", "tests_document",
                              "ir_document", "bundle_document"})
            self.assertIn("conserves mass", ctx["controlled_spec_document"])
            self.assertIn("bundle_schema_version", ctx["bundle_document"])


# ======================================================================================
# _run_pure_verify_substep: happy path, fail verdict, repair, exhaustion, transport
# ======================================================================================
class PureVerifySubstepTests(unittest.TestCase):
    def _run(self, envelopes, *, cls=_PureFakeConductor):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = cls(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                backend="claude", env={})
        c.envelopes = envelopes
        oc = c._run_pure_verify_substep(refs, "generate", "verify", ())
        return c, refs, oc

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_pure_pass_finalize_payload_satisfies_the_real_summary_validator(self) -> None:
        """Mirror of the producer's regression pin (billed E2E, 2026-07-16).

        A pure row's `output_refs` is empty by contract, so `_validate_agent_summary_text`
        requires `result_summary` to explain the row. The reviewer, like the producer,
        passed None on pass — which `finalize-child` rejects. These tests stub
        `runtime()`, so the only way to catch it is to run the conductor's ACTUAL
        captured payload through the REAL validators.
        """
        from tools.orchestration_runtime import (
            _extract_agent_summary_text, _validate_agent_summary_text,
        )

        c, refs, oc = self._run([_envelope(_verdict("pass"))])
        self.assertEqual(oc.status, "pass")
        payloads = [cap["--agent-run-json"] for sub, cap in c.calls
                    if sub == "finalize-child" and "--agent-run-json" in cap]
        self.assertTrue(payloads, "finalize-child must have been called with a payload")
        for payload in payloads:
            self.assertEqual(payload["output_refs"], [])  # the pure contract
            _validate_agent_summary_text(payload, _extract_agent_summary_text(payload))

    def test_pass_verdict_writes_source_meta_and_empty_output_refs(self) -> None:
        c, refs, oc = self._run([_envelope(_verdict("pass"))])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.output_refs, [])
        base = c.repo_root / refs.source_dir()
        meta = json.loads((base / "source_meta.json").read_text())
        self.assertEqual(meta["verification_status"], "pass")
        self.assertEqual(meta["issue_severity"], "none")
        self.assertIsNone(meta["last_fail_reason"])
        # meta_contracts-required keys are all present with correct types.
        for k in ("attempt_count", "verification_status", "last_fail_reason",
                  "debug_mode", "context_isolated"):
            self.assertIn(k, meta)
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["result"], "pass")
        self.assertIsNone(vmeta["failure_category"])
        self.assertEqual(vmeta["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
        self.assertEqual(vmeta["per_attempt"][0]["model"], "claude-opus-4-8")

    def test_fail_verdict_is_substep_fail_with_projected_source_meta(self) -> None:
        c, refs, oc = self._run([_envelope(_verdict("fail", severity="minor",
                                                    reason="flux sign is wrong"))])
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 0)      # a content fail, NOT transport
        self.assertIsNone(oc.infra_error)
        base = c.repo_root / refs.source_dir()
        meta = json.loads((base / "source_meta.json").read_text())
        self.assertEqual(meta["verification_status"], "fail")
        self.assertEqual(meta["issue_severity"], "minor")
        self.assertEqual(meta["last_fail_reason"], "flux sign is wrong")
        # A valid fail verdict is NOT a routed category — verdict_meta stays result=pass.
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["result"], "pass")
        self.assertIsNone(vmeta["failure_category"])

    def test_bounded_repair_recovers_on_second_turn(self) -> None:
        bad = {"verification_status": "pass"}  # schema violation (missing keys)
        c, refs, oc = self._run([_envelope(bad), _envelope(_verdict("pass"))])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)
        self.assertTrue((c.repo_root / refs.source_dir() / "source_meta.json").exists())

    def test_exhausted_schema_repair_no_source_meta_proof_of_work(self) -> None:
        bad = {"verification_status": "pass"}  # persistently schema-invalid
        c, refs, oc = self._run([_envelope(bad)])  # same bad envelope every turn
        self.assertEqual(oc.status, "fail")
        base = c.repo_root / refs.source_dir()
        # Proof-of-work: no schema-valid verdict => source_meta.json is NOT written.
        self.assertFalse((base / "source_meta.json").exists())
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["result"], "fail")
        self.assertEqual(vmeta["failure_category"], "verdict_schema_violation")
        self.assertTrue(vmeta.get("failure_excerpt"))

    def test_unparseable_reply_categorized(self) -> None:
        c, refs, oc = self._run([_envelope("not a verdict at all")])
        self.assertEqual(oc.status, "fail")
        vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["failure_category"], "pure_response_unparseable")

    def test_transport_error_routes_fail_closed(self) -> None:
        class _C(_PureFakeConductor):
            def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
                return wc.ProcResult(1, "", "usage limit reached")
        c, refs, oc = self._run([_envelope(_verdict("pass"))], cls=_C)
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)   # forces run_phase's fail_closed branch
        # A transport failure has no fixable verdict — not repaired (one attempt only).
        self.assertEqual(oc.attempts, 1)
        self.assertFalse((c.repo_root / refs.source_dir() / "source_meta.json").exists())

    def test_wait_usage_reset_recovers_a_transport_usage_limit(self) -> None:
        """--wait-usage-reset (opt-in) mirrors the producer: a reviewer transport death carrying a
        machine-form usage-limit epoch is waited out in place and re-launched, rather than
        fail-closing. The wait is not a repair turn (attempts stays the reviewer's repair count)."""
        now = 1_752_200_000.0

        class _C(_PureFakeConductor):
            def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
                self._spawn = getattr(self, "_spawn", 0)
                proc = self.procs[min(self._spawn, len(self.procs) - 1)]
                self._spawn += 1
                return proc

            def _sleep_backoff(self, seconds):  # type: ignore[override]
                self.slept.append(seconds)

        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               backend="claude", env={}, wait_usage_reset=True)
        c.procs = [wc.ProcResult(1, "", f"usage limit reached|{int(now) + 300}"),
                   wc.ProcResult(0, _envelope(_verdict("pass")), "")]
        c.slept = []
        with mock.patch.object(wc.time, "time", return_value=now):
            oc = c._run_pure_verify_substep(refs, "generate", "verify", ())
        self.assertEqual(oc.status, "pass")
        self.assertEqual(c._spawn, 2)
        self.assertEqual(oc.attempts, 2)             # launch count (the wait launch is counted)
        self.assertEqual(c.slept, [420.0])           # 300s + 120s margin
        self.assertTrue((c.repo_root / refs.source_dir() / "source_meta.json").exists())
        reasons = [cap["--reason"] for s, cap in c.calls if s == "add-superseded-runs"]
        self.assertTrue(any("leaf_usage_limit_wait_orphan" in r for r in reasons))

    def test_unencodable_valid_verdict_is_schema_violation_not_transport(self) -> None:
        # Codex review (defect 1): a schema-SOUND verdict whose last_fail_reason holds a lone
        # surrogate is not UTF-8 persistable. It must be caught as a schema violation (repairable,
        # routable via the verdict table) — NOT accepted and then mis-routed as a host-write /
        # transport failure. A schema-valid verdict must always reach its routing.
        bad = {"verification_status": "fail", "issue_severity": "major",
               "last_fail_reason": "\ud800", "findings": [{"summary": "x"}]}
        c, refs, oc = self._run([_envelope(bad)])  # persistently unencodable -> exhaustion
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 0)          # routable, NOT a transport fail_closed
        base = c.repo_root / refs.source_dir()
        self.assertFalse((base / "source_meta.json").exists())
        vmeta = json.loads((base / "verdict_meta.json").read_text())
        self.assertEqual(vmeta["failure_category"], "verdict_schema_violation")

    def test_verdict_meta_write_failure_on_exhaustion_recovers(self) -> None:
        # Codex review (defect 2): the exhaustion-path verdict_meta write must be guarded like the
        # accepted path — a host-write failure recovers as a fail_closed transport outcome, never
        # an uncaught exception escaping run_substep.
        class _C(_PureFakeConductor):
            def _write_verdict_meta(self, *a, **k):  # type: ignore[override]
                raise OSError(28, "No space left on device")
        bad = {"verification_status": "pass"}  # persistently schema-invalid -> exhaustion
        c, refs, oc = self._run([_envelope(bad)], cls=_C)
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)
        self.assertEqual(oc.infra_error[0], "pure_verify_host_write_failed")

    def test_finalize_before_source_meta_write_ordering(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        observed: dict[str, bool] = {}

        class _C(_PureFakeConductor):
            def finalize_child(self, child_arid, return_token, reply_text, agent_run_json):  # type: ignore[override]
                base = self.repo_root / refs.source_dir()
                observed["meta_exists_at_finalize"] = (base / "source_meta.json").exists()
                return super().finalize_child(child_arid, return_token, reply_text, agent_run_json)

        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               backend="claude", env={})
        c.envelopes = [_envelope(_verdict("pass"))]
        oc = c._run_pure_verify_substep(refs, "generate", "verify", ())
        self.assertEqual(oc.status, "pass")
        # The window was closed BEFORE source_meta.json was authored (empty write_roots).
        self.assertFalse(observed["meta_exists_at_finalize"])
        self.assertTrue((c.repo_root / refs.source_dir() / "source_meta.json").exists())

    def test_host_write_failure_after_finalize_recovers(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _verify_node(repo)
        finalized: dict[str, bool] = {}

        class _C(_PureFakeConductor):
            def finalize_child(self, child_arid, return_token, reply_text, agent_run_json):  # type: ignore[override]
                finalized["did"] = True
                return super().finalize_child(child_arid, return_token, reply_text, agent_run_json)

            def _write_verify_source_meta(self, refs, verdict, *, attempts):  # type: ignore[override]
                raise OSError(28, "No space left on device")

        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               backend="claude", env={})
        c.envelopes = [_envelope(_verdict("pass"))]
        oc = c._run_pure_verify_substep(refs, "generate", "verify", ())
        self.assertTrue(finalized.get("did"))
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)
        self.assertEqual(oc.infra_error[0], "pure_verify_host_write_failed")

    def test_repair_resumes_only_reviewers_own_session(self) -> None:
        # Persona separation: on the repair turn the resumed session is the reviewer's OWN prior
        # attempt arid (a "child-N" minted in this loop), never an external/producer arid.
        resumes: list = []

        class _C(_PureFakeConductor):
            def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
                resumes.append(kwargs.get("resume_session_id"))
                return super().spawn_leaf(prompt_text, child_env, **kwargs)
        bad = {"verification_status": "pass"}
        c, refs, oc = self._run([_envelope(bad), _envelope(_verdict("pass"))], cls=_C)
        self.assertEqual(oc.status, "pass")
        self.assertIsNone(resumes[0])                      # first turn is a cold launch
        self.assertEqual(resumes[1], "child-1")            # repair resumes the reviewer's arid
        self.assertTrue(all(r is None or str(r).startswith("child-") for r in resumes))


# ======================================================================================
# classify_failure verdict routing (M-D)
# ======================================================================================
class PureVerifyRoutingTests(unittest.TestCase):
    def _outcomes(self, status: str) -> list:
        # SUBSTEPS["generate"] == ("generate","gate","verify"); a verify failure is index 2.
        oc_pass = wc.SubstepOutcome("a", "pass", [])
        oc_last = wc.SubstepOutcome("v", status, [])
        return [oc_pass, oc_pass, oc_last]

    def test_schema_exhaustion_routes_cold_generate_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _conductor(repo)
            c._write_verdict_meta(refs, result="fail",
                                  failure_category="verdict_schema_violation",
                                  failure_excerpt="issue_severity must be one of ...",
                                  attempts=3, per_attempt=[])
            dec = c.classify_failure(refs, "generate", self._outcomes("fail"))
            self.assertEqual(dec.action, "retry")
            self.assertEqual(dec.target_phase, "generate")
            self.assertEqual(dec.repair_strategy, "restart")
            self.assertEqual(dec.reason, "generate_verdict_verdict_schema_violation")

    def test_valid_fail_verdict_falls_through_to_severity_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _conductor(repo)
            # A valid fail verdict wrote source_meta (minor) + a category-less verdict_meta.
            c._write_verify_source_meta(refs, _verdict("fail", severity="minor",
                                                       reason="flux sign"), attempts=1)
            c._write_verdict_meta(refs, result="pass", failure_category=None,
                                  failure_excerpt=None, attempts=1, per_attempt=[])
            dec = c.classify_failure(refs, "generate", self._outcomes("fail"))
            # Fell through to the verify-severity gate (NOT a verdict route).
            self.assertFalse((dec.reason or "").startswith("generate_verdict_"))
            self.assertEqual(dec.reason, "verify_minor")
            self.assertEqual(dec.repair_strategy, "reuse")


# ======================================================================================
# Routing-category drift guard + cold-fallback surrogate safety (parallel_review)
# ======================================================================================
class PureVerifyRoutingDriftTests(unittest.TestCase):
    def test_routing_categories_match_pure_leaf_constants(self) -> None:
        # parallel_review (glm): the routing table must key on the SAME category strings the loop
        # assigns from pure_leaf, or a constant rename would silently mis-route. Pin the identity.
        from tools.pure_leaf import RESPONSE_UNPARSEABLE, RESPONSE_TRUNCATED
        self.assertIn(RESPONSE_UNPARSEABLE, wc.GENERATE_VERDICT_FAILURE_ROUTING)
        self.assertIn(RESPONSE_TRUNCATED, wc.GENERATE_VERDICT_FAILURE_ROUTING)
        self.assertIn(wc.GENERATE_VERDICT_SCHEMA_VIOLATION, wc.GENERATE_VERDICT_FAILURE_ROUTING)


class PureVerifyColdFallbackSurrogateTests(unittest.TestCase):
    def test_cold_fallback_repair_with_surrogate_does_not_crash(self) -> None:
        # parallel_review (glm §5.3): a verdict carrying an unpaired surrogate goes to the
        # verdict_schema_violation repair path; on a COLD fallback (session not resumable) its
        # prior_document is echoed into the repair prompt that record_launch writes as UTF-8. It
        # must be normalized so the write does not raise UnicodeEncodeError mid-repair.
        class _C(_PureFakeConductor):
            def _claude_session_resumable(self, arid):  # type: ignore[override]
                return False  # force the cold-fallback repair branch on every turn
            def record_launch(self, child_arid, request):  # type: ignore[override]
                # Emulate the real record_launch's UTF-8 prompt persistence to surface any
                # non-encodable prior_document as the real path would.
                json.dumps(request, ensure_ascii=False).encode("utf-8")
                return {"launch_prompt_text": "PROMPT"}
        bad = {"verification_status": "fail", "issue_severity": "major",
               "last_fail_reason": "\ud800", "findings": [{"summary": "x"}]}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
            c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                   backend="claude", env={})
            c.envelopes = [_envelope(bad)]
            oc = c._run_pure_verify_substep(refs, "generate", "verify", ())  # must not raise
            self.assertEqual(oc.status, "fail")

    def test_surrogate_in_findings_does_not_crash_repair_or_meta(self) -> None:
        # parallel_review round 2 (kimi §2.A): `last_excerpt` (findings) flows into both the repair
        # turn's `repair_findings` and verdict_meta's failure_excerpt, both persisted as UTF-8. Even
        # if a violation message ever carried a lone surrogate, capture-time normalization must keep
        # every downstream write from raising. Force it by making the validator emit one.
        from unittest import mock
        import tools.pure_leaf as pl

        class _C(_PureFakeConductor):
            def record_launch(self, child_arid, request):  # type: ignore[override]
                json.dumps(request, ensure_ascii=False).encode("utf-8")  # emulate prompt persist
                return {"launch_prompt_text": "PROMPT"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
            c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                   backend="claude", env={})
            c.envelopes = [_envelope({"verification_status": "pass"})]  # schema-invalid -> repair
            with mock.patch.object(pl, "verify_verdict_violations",
                                   return_value=["bad field value \ud800 here"]):
                oc = c._run_pure_verify_substep(refs, "generate", "verify", ())  # must not raise
            self.assertEqual(oc.status, "fail")
            # verdict_meta persisted cleanly (excerpt normalized), and its write did not fail_close.
            vmeta = json.loads((c.repo_root / refs.source_dir() / "verdict_meta.json").read_text())
            self.assertEqual(vmeta["failure_category"], "verdict_schema_violation")
            self.assertEqual(oc.leaf_returncode, 0)   # NOT a host-write fail_close


# ======================================================================================
# _maybe_warm_resume_verify_meta early-return for pure
# ======================================================================================
class PureVerifyMetaWarmResumeTests(unittest.TestCase):
    def test_pure_verify_skips_legacy_meta_warm_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _verify_node(repo)
            c = _conductor(repo)
            # A failed verify outcome list (verify is the last generate substep, index 2).
            outcomes = [wc.SubstepOutcome("a", "pass", [])] * 2 + [
                wc.SubstepOutcome("v", "fail", [])]
            # Would otherwise inspect/repair the meta; for pure it must return unchanged, untouched.
            called = {"stage_meta": False}
            orig = c._stage_meta_contract_findings

            def _spy(*a, **k):
                called["stage_meta"] = True
                return orig(*a, **k)
            c._stage_meta_contract_findings = _spy  # type: ignore[assignment]
            out = c._maybe_warm_resume_verify_meta(refs, "generate", list(outcomes), ())
            self.assertEqual([o.agent_run_id for o in out], [o.agent_run_id for o in outcomes])
            self.assertFalse(called["stage_meta"])   # the legacy loop body never ran


# ======================================================================================
# build_launch_request pure verify variant
# ======================================================================================
class PureVerifyLaunchRequestTests(unittest.TestCase):
    def test_pure_verify_request_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            refs = _verify_node(Path(tmp))
            req = wc.build_launch_request(
                refs, step="generate", substep="verify", orchestration_id="o",
                orchestration_agent_run_id="orch", child_agent_run_id="c",
                agent_model="opus", workflow_mode="dev",
                makefile_host_authored=True, runner_host_authored=True,
                pure_leaf=True,
                pure_context={"controlled_spec_document": "cs", "tests_document": "t",
                              "ir_document": "ir", "bundle_document": "b"})
            self.assertEqual(req["leaf_mode"], "pure")
            self.assertEqual(req["substep"], "verify")
            self.assertEqual(req["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
            self.assertEqual(req["allowed_output_paths"], [])
            self.assertEqual(req["skill_name"], "")
            self.assertIn("pure_context", req)


# ======================================================================================
# verify output-contract lift (cold-repair reuse of the verify template's schema)
# ======================================================================================
class PureVerifyOutputContractTests(unittest.TestCase):
    def test_verify_output_contract_paragraph_lifts_whole(self) -> None:
        import tools.orchestration_runtime as ort
        req = {"leaf_mode": "pure", "step": "generate", "substep": "verify",
               "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION}
        text = ort._pure_output_contract_text(req)
        self.assertTrue(text.startswith("Output contract"))
        self.assertIn("verify verdict", text)
        # The verify contract's closing clause survives (no blank-line truncation).
        self.assertIn("more than one document", text)


# ======================================================================================
# Full pure generate phase through the real step_result validator (subagent review round 1)
# ======================================================================================
class PureStepResultValidationTests(unittest.TestCase):
    """A passing pure generate phase must clear `_validate_step_result_payload`.

    The pure producer AND the pure verify reviewer both finalize with output_refs==[] (the host
    authors model/checks/source_meta after the child windows close). Without the pure carve-out
    the per-step-result validator raises — `substep must publish non-empty output_refs` and
    `required_outputs must be satisfied by substep output_refs` — so a pure-leaf node
    could never certify. This exercises the real validator end-to-end (the unit loops stub
    the runtime, so they miss this)."""

    def _setup(self, tmp: str):
        import tools.orchestration_runtime as ort
        repo = Path(tmp)
        refs = _verify_node(repo)
        oid = "o"
        orch = repo / "workspace" / "orchestrations" / oid
        (orch / "launches").mkdir(parents=True, exist_ok=True)
        gen_arid, ver_arid = "gen-pure-1", "ver-pure-1"
        # agent_runs.jsonl: both substeps pure, pass, empty output_refs.
        rows = [
            {"agent_run_id": gen_arid, "agent_role": "substep", "node_key": _NODE,
             "step": "generate", "substep": "generate", "status": "pass", "output_refs": []},
            {"agent_run_id": ver_arid, "agent_role": "substep", "node_key": _NODE,
             "step": "generate", "substep": "verify", "status": "pass", "output_refs": []},
        ]
        (orch / "agent_runs.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        for arid, substep in ((gen_arid, "generate"), (ver_arid, "verify")):
            (orch / "launches" / f"{arid}.request.json").write_text(
                json.dumps({"leaf_mode": "pure", "step": "generate", "substep": substep}),
                encoding="utf-8")
        # Host-authored deliverables on disk (what the pure producer + verify write).
        required = wc.phase_required_outputs(refs, "generate", makefile_required=False,
                                             runner_host_authored=True)
        for ref in required:
            p = repo / ref
            p.parent.mkdir(parents=True, exist_ok=True)
            if ref.endswith("source_meta.json"):
                p.write_text(json.dumps({
                    "source_id": refs.source_id, "node_key": _NODE, "attempt_count": 1,
                    "verification_status": "pass", "issue_severity": "none",
                    "last_fail_reason": None, "debug_mode": True, "context_isolated": True,
                }), encoding="utf-8")
            else:
                p.write_text("module m\nend module\n", encoding="utf-8")
        payload = {
            "status": "pass", "validation_stage": "post_generate",
            "substep_agent_run_ids": [gen_arid, ver_arid], "failed_substeps": [],
            "retry_decisions": None, "required_outputs": required,
        }
        return ort, repo, oid, refs, payload, required

    def test_passing_pure_generate_step_result_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ort, repo, oid, refs, payload, _ = self._setup(tmp)
            # Must NOT raise — the pure carve-out vouches host-authored deliverables by existence.
            ort._validate_step_result_payload(
                repo, oid, node_key=_NODE, step="generate", agent_run_id="orch", payload=payload)

    def test_missing_host_authored_deliverable_still_fails(self) -> None:
        # Anti-mock-green: the carve-out vouches by ON-DISK existence, so a declared-but-unauthored
        # deliverable must still fail (not be waved through).
        with tempfile.TemporaryDirectory() as tmp:
            ort, repo, oid, refs, payload, required = self._setup(tmp)
            model_ref = next(r for r in required if r.endswith("_model.f90"))
            (repo / model_ref).unlink()
            with self.assertRaises(ValueError):
                ort._validate_step_result_payload(
                    repo, oid, node_key=_NODE, step="generate", agent_run_id="orch", payload=payload)


if __name__ == "__main__":
    unittest.main()
