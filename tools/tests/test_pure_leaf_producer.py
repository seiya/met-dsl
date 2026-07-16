#!/usr/bin/env python3
"""M-C: Z2 pure-function CodegenBundle producer.

Covers the host side of the pure `generate.generate` channel added across
`tools/workflow_conductor.py` (the producer loop, bundle validation + assembly preflight, the
bundle-derived Makefile, bundle_meta), `tools/orchestration_runtime.py` (the terminal-payload
carve-out + the cold-repair prompt contract), `tools/validate_pipeline_semantics.py` (the
post_generate bundle re-validation + the sweep output_refs mirror), and `tools/run_workflow.py`
(the M-C `--generate-executor pure` block).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
import tools.validate_pipeline_semantics as vps
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION

_NODE = "problem/shallow_water2d@0.3.0"
_SAFE = wc.node_key_safe(_NODE)
_SPEC_ID = "shallow_water2d"
_HARNESS = "infrastructure/harness_fortran_cpu@0.3.0"
_SPEC_PATH = "spec/problem/ocean/shallow_water2d"


def _valid_bundle() -> dict:
    return {
        "bundle_schema_version": "1.0.0",
        "optimization_unit": {"members": [_NODE]},
        "files": [
            {"logical_path": f"{_SPEC_ID}_model.f90", "role": "model", "language": "fortran",
             "member_node_key": _NODE, "content": f"module {_SPEC_ID}_model\nend module\n",
             "modules": [f"{_SPEC_ID}_model"]},
            {"logical_path": f"{_SPEC_ID}_checks.f90", "role": "checks", "language": "fortran",
             "member_node_key": _NODE, "content": f"module {_SPEC_ID}_checks\nend module\n",
             "modules": [f"{_SPEC_ID}_checks"]},
        ],
        "entrypoints": [
            {"symbol": "sw_update", "kind": "operation", "node_key": _NODE,
             "defined_in": f"{_SPEC_ID}_model.f90", "module": f"{_SPEC_ID}_model"},
            {"symbol": "case_run", "kind": "checks_interface", "node_key": _NODE,
             "defined_in": f"{_SPEC_ID}_checks.f90", "module": f"{_SPEC_ID}_checks"},
        ],
        "target_lowering_plan": {"precision": {"real_kind": "real64"}, "state_residency": "host"},
        "capability_requirements": ["sync_single_case@1"],
    }


def _write_node(repo: Path, *, ir_id="sw_20260715_001", source_id="src_20260715_001",
                state_vars=("h", "u", "v")) -> wc.NodeRefs:
    """Write a minimal M3c IR + dependency-graph sidecar + tests.md for the node."""
    ir_dir = repo / "workspace" / "ir" / _SAFE / ir_id
    ir_dir.mkdir(parents=True, exist_ok=True)
    ir = {
        "meta": {"spec_kind": "problem"},
        "impl_defaults": {
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"},
            "target": {"backend": "cpu"},
        },
        # Canonical shape: state_variables is a list of OBJECTS ({name, shape_expr}), NOT bare
        # strings — the shape real specs emit (a string-list masks the name-extraction path).
        "algorithm": {"state_variables": [{"name": v, "shape_expr": "[nx]"} for v in state_vars]},
        "dependency": {"direct_deps": [{"node_key": _HARNESS}]},
        "case": {"test_case_set": [{"case_id": "c1"}]},
    }
    import yaml
    (ir_dir / "spec.ir.yaml").write_text(yaml.safe_dump(ir), encoding="utf-8")
    sidecar = {
        "all_nodes": [
            {"node_key": _NODE, "topo_level": 1, "direct_deps": [{"node_key": _HARNESS}]},
            {"node_key": _HARNESS, "topo_level": 0, "direct_deps": []},
        ],
    }
    (ir_dir / "dependency_graph.json").write_text(json.dumps(sidecar), encoding="utf-8")
    spec_dir = repo / _SPEC_PATH
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tests.md").write_text("- test: conserves mass\n", encoding="utf-8")
    return wc.NodeRefs(node_key=_NODE, spec_path=_SPEC_PATH, ir_id=ir_id,
                       pipeline_id="sw_20260715_001", source_id=source_id)


class _PureFakeConductor(wc.Conductor):
    """Conductor with the runtime CLI and leaf spawn stubbed, but the pure host-side logic
    (context assembly, bundle validation, graph derivation, artifact writes) real."""

    envelopes: list[str] = []

    def runtime(self, args):  # type: ignore[override]
        sub = args[0]
        self.calls = getattr(self, "calls", [])
        captured: dict = {}
        for flag in ("--agent-run-json", "--request-json", "--run-ids", "--reason"):
            if flag in args and flag != "--run-ids":
                captured[flag] = json.loads(args[args.index(flag) + 1]) \
                    if flag.endswith("-json") else args[args.index(flag) + 1]
        self.calls.append((sub, captured))
        if sub == "record-launch":
            return {"launch_prompt_text": "PROMPT"}
        return {}

    def new_agent_run_id(self):  # type: ignore[override]
        self._n = getattr(self, "_n", 0) + 1
        return f"child-{self._n}"

    def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
        self._spawn = getattr(self, "_spawn", 0)
        env = self.envelopes[min(self._spawn, len(self.envelopes) - 1)]
        self._spawn += 1
        return wc.ProcResult(0, env, "")

    def read_parent_return_token(self, child_arid):  # type: ignore[override]
        return "rtok"

    def _claude_session_resumable(self, arid):  # type: ignore[override]
        return True


def _envelope(bundle_or_text, *, model="claude-opus-4-8", is_error=False) -> str:
    result = bundle_or_text if isinstance(bundle_or_text, str) else json.dumps(bundle_or_text)
    return json.dumps({"result": result, "is_error": is_error, "model": model,
                       "usage": {"output_tokens": 10}, "session_id": "s"})


def _conductor(repo: Path) -> _PureFakeConductor:
    (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
    return _PureFakeConductor(
        repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
        backend="claude", env={}, generate_executor="pure")


# ======================================================================================
# _pure_bundle_violations: clean bundle + each failure category
# ======================================================================================
class PureBundleViolationsTests(unittest.TestCase):
    def _c_refs(self):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        return _conductor(repo), refs

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_clean_bundle_has_no_violations(self) -> None:
        c, refs = self._c_refs()
        self.assertIsNone(c._pure_bundle_violations(refs, _valid_bundle()))

    def test_schema_violation(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        del bad["capability_requirements"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_schema_violation")

    def test_shape_unsupported_multinode(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["optimization_unit"]["members"] = [_NODE, "problem/other@0.1.0"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        # A second member with no files is a schema-invariant failure first; either way it is
        # not accepted. Assert it is rejected with a bundle category.
        self.assertIn(cat, ("bundle_shape_unsupported", "bundle_schema_violation"))

    def test_capability_unsatisfied(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["capability_requirements"] = ["batched_cases@1"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_capability_unsatisfied")

    def test_state_binding_mismatch(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["modules"] = [f"{_SPEC_ID}_checks"]
        bad["state_bindings"] = [{
            "node_key": _NODE, "state_variable": "not_a_state", "storage_symbol": "q_storage",
            "module": f"{_SPEC_ID}_checks", "capture": "checks_getter", "capability": None}]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_state_binding_mismatch")

    def test_state_binding_on_canonical_ir_object_state_var_accepted(self) -> None:
        # Codex P2 (finding 1): with the canonical OBJECT-shaped state_variables, a binding on a
        # REAL declared state var ("h") must be accepted. A comprehension that kept only str
        # entries would leave ir_state_vars empty and wrongly reject this as a mismatch.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["modules"] = [f"{_SPEC_ID}_checks"]
        ok["state_bindings"] = [{
            "node_key": _NODE, "state_variable": "h", "storage_symbol": "q_storage",
            "module": f"{_SPEC_ID}_checks", "capture": "checks_getter", "capability": None}]
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_m3c_name_violation(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][0]["logical_path"] = "wrong_model.f90"
        bad["entrypoints"][0]["defined_in"] = "wrong_model.f90"
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_assembly_collision")

    def test_state_binding_fail_closed_when_ir_declares_no_state(self) -> None:
        # Review fix: an EMPTY declared state set must REJECT any binding (not accept all).
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo, state_vars=())
        c = _conductor(repo)
        bad = _valid_bundle()
        bad["state_bindings"] = [{
            "node_key": _NODE, "state_variable": "h", "storage_symbol": "q_storage",
            "module": f"{_SPEC_ID}_checks", "capture": "checks_getter", "capability": None}]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_state_binding_mismatch")


# ======================================================================================
# Bundle-derived Makefile
# ======================================================================================
class PureMakefileTests(unittest.TestCase):
    def test_makefile_compiles_bundle_files_and_runner_and_deps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = _conductor(repo)
            graph = c._build_pure_bundle_graph(refs, _valid_bundle())
            mk = c._render_pure_makefile_from_graph(refs, graph)
            self.assertIn(f"$(OBJDIR)/{_SPEC_ID}_model.o", mk)
            self.assertIn(f"$(OBJDIR)/{_SPEC_ID}_checks.o", mk)
            self.assertIn(f"$(OBJDIR)/{_SPEC_ID}_runner.o", mk)
            # the staged harness dependency compiles too
            self.assertIn("$(OBJDIR)/harness_fortran_cpu_model.o", mk)
            self.assertIn(f"BIN ?= {_SPEC_ID}_runner", mk)
            self.assertIn("test:", mk)


# ======================================================================================
# _run_pure_generate_substep: happy path + bounded repair + exhaustion
# ======================================================================================
class PureProducerSubstepTests(unittest.TestCase):
    def _run(self, envelopes):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        c = _conductor(repo)
        c.envelopes = envelopes
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        return c, refs, oc

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_happy_path_writes_bundle_artifacts_and_empty_output_refs(self) -> None:
        c, refs, oc = self._run([_envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.output_refs, [])
        base = c.repo_root / refs.source_dir()
        self.assertTrue((base / "codegen_bundle.json").exists())
        self.assertTrue((base / "bundle_meta.json").exists())
        self.assertTrue((base / "src" / f"{_SPEC_ID}_model.f90").exists())
        self.assertTrue((base / "src" / f"{_SPEC_ID}_checks.f90").exists())
        self.assertTrue((base / "src" / "Makefile").exists())
        meta = json.loads((base / "bundle_meta.json").read_text())
        self.assertEqual(meta["result"], "pass")
        self.assertEqual(meta["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
        self.assertEqual(meta["per_attempt"][0]["model"], "claude-opus-4-8")

    def test_finalize_before_write_ordering(self) -> None:
        # The finalize-child call MUST precede the host artifact writes (empty write_roots make
        # an in-window write unauthorized). Pin the ORDERING directly: capture, at the instant
        # finalize_child runs, whether the bundle artifacts already exist on disk — they must
        # NOT (the writes come strictly after). A regression that moved the writes earlier would
        # find them present here and fail.
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        observed: dict[str, bool] = {}

        class _C(_PureFakeConductor):
            def finalize_child(self, child_arid, return_token, reply_text, agent_run_json):  # type: ignore[override]
                base = self.repo_root / refs.source_dir()
                observed["bundle_exists_at_finalize"] = (base / "codegen_bundle.json").exists()
                observed["model_exists_at_finalize"] = (
                    base / "src" / f"{_SPEC_ID}_model.f90").exists()
                return super().finalize_child(child_arid, return_token, reply_text, agent_run_json)

        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               backend="claude", env={}, generate_executor="pure")
        c.envelopes = [_envelope(_valid_bundle())]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "pass")
        self.assertFalse(observed["bundle_exists_at_finalize"])
        self.assertFalse(observed["model_exists_at_finalize"])
        # ...and they DO exist after the substep returns (the writes ran, just later).
        self.assertTrue((c.repo_root / refs.source_dir() / "codegen_bundle.json").exists())

    def test_bounded_repair_recovers_on_second_turn(self) -> None:
        bad = _valid_bundle()
        del bad["capability_requirements"]  # schema violation -> repair
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)

    def test_pass_after_repair_tombstones_orphan_attempts(self) -> None:
        # Review fix (HIGH): a repaired pass must tombstone the earlier (finalized, un-vouched)
        # attempt arids, or the completion gate rejects the otherwise-passing run.
        bad = _valid_bundle()
        del bad["capability_requirements"]
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertTrue(any(sub == "add-superseded-runs" for sub, _ in c.calls))

    def test_exhausted_repair_records_bundle_meta_fail(self) -> None:
        bad = _valid_bundle()
        del bad["capability_requirements"]
        # MAX_BUNDLE_REPAIR_TURNS=2 -> 3 attempts total, all bad.
        c, refs, oc = self._run([_envelope(bad)])
        self.assertEqual(oc.status, "fail")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["result"], "fail")
        self.assertEqual(meta["failure_category"], "bundle_schema_violation")
        self.assertTrue(meta.get("failure_excerpt"))

    def test_unparseable_reply_categorized(self) -> None:
        c, refs, oc = self._run([_envelope("not json at all", )])
        self.assertEqual(oc.status, "fail")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["failure_category"], "pure_response_unparseable")

    def test_host_write_failure_after_finalize_recovers(self) -> None:
        # Codex P2 (finding 2): a host-side write that fails AFTER finalize_child recorded the
        # attempt as a passing terminal row must NOT leave an un-vouched orphan. The substep must
        # instead route fail_closed (non-zero leaf_returncode + a pure_host_write_failed tag) so
        # run_phase's transport branch tombstones the orphan and the operator resumes.
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        finalized: dict[str, bool] = {}

        class _C(_PureFakeConductor):
            def finalize_child(self, child_arid, return_token, reply_text, agent_run_json):  # type: ignore[override]
                finalized["did"] = True
                return super().finalize_child(child_arid, return_token, reply_text, agent_run_json)

            def _write_pure_bundle_artifacts(self, refs, doc, graph):  # type: ignore[override]
                raise OSError(28, "No space left on device")

        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               backend="claude", env={}, generate_executor="pure")
        c.envelopes = [_envelope(_valid_bundle())]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertTrue(finalized.get("did"))            # the window WAS closed (finalize ran)
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)       # forces run_phase's fail_closed branch
        self.assertEqual(oc.infra_error[0], "pure_host_write_failed")
        # No passing bundle_meta was left behind claiming success.
        base = c.repo_root / refs.source_dir()
        if (base / "bundle_meta.json").exists():
            self.assertNotEqual(json.loads((base / "bundle_meta.json").read_text())["result"],
                                "pass")

    def test_unencodable_valid_bundle_is_schema_violation_not_transport(self) -> None:
        # M-D2 (D1 mirror of the verify reviewer): a bundle that passes every content layer but
        # carries a lone surrogate in a files[].content is not UTF-8 persistable. It must be caught
        # BEFORE accept as a schema violation (repairable, routable via the bundle table) — NOT
        # accepted and then mis-routed through the pass branch's host-write/transport recovery.
        bad = _valid_bundle()
        bad["files"][0]["content"] += "\ud800"
        c, refs, oc = self._run([_envelope(bad)])  # persistently unencodable -> exhaustion
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 0)          # routable, NOT a transport fail_closed
        base = c.repo_root / refs.source_dir()
        # Never accepted => no bundle artifacts authored.
        self.assertFalse((base / "codegen_bundle.json").exists())
        meta = json.loads((base / "bundle_meta.json").read_text())
        self.assertEqual(meta["failure_category"], "bundle_schema_violation")

    def test_bundle_meta_write_failure_on_exhaustion_recovers(self) -> None:
        # M-D2 (D2 mirror of the verify reviewer): the exhaustion-path bundle_meta write must be
        # guarded like the pass path — a host-write failure (ENOSPC, or a non-encodable excerpt)
        # recovers as a fail_closed transport outcome, never an uncaught exception escaping
        # run_substep and crashing the conductor.
        class _C(_PureFakeConductor):
            def _write_bundle_meta(self, *a, **k):  # type: ignore[override]
                raise OSError(28, "No space left on device")
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
               backend="claude", env={}, generate_executor="pure")
        bad = _valid_bundle()
        del bad["capability_requirements"]  # persistently schema-invalid -> exhaustion
        c.envelopes = [_envelope(bad)]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)
        self.assertEqual(oc.infra_error[0], "pure_host_write_failed")


# ======================================================================================
# M-D2: producer cold-fallback / capture-time surrogate safety (verify-side mirror)
# ======================================================================================
class PureProducerColdFallbackSurrogateTests(unittest.TestCase):
    def test_cold_fallback_repair_with_surrogate_does_not_crash(self) -> None:
        # M-D2 (G1 mirror): a bundle carrying an unpaired surrogate goes to the
        # bundle_schema_violation repair path; on a COLD fallback (session not resumable) its
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
        bad = _valid_bundle()
        bad["files"][0]["content"] += "\ud800"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
            c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                   backend="claude", env={}, generate_executor="pure")
            c.envelopes = [_envelope(bad)]
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())  # must not raise
            self.assertEqual(oc.status, "fail")

    def test_surrogate_in_findings_does_not_crash_repair_or_meta(self) -> None:
        # M-D2 (G3 mirror): `last_excerpt` (findings) flows into both the repair turn's
        # repair_findings AND bundle_meta's failure_excerpt, both persisted as UTF-8. Capture-time
        # normalization must keep every downstream write from raising even if a violation message
        # ever carried a lone surrogate. Force it by making the validator emit one.
        class _C(_PureFakeConductor):
            def _pure_bundle_violations(self, refs, doc):  # type: ignore[override]
                return ("bundle_schema_violation", "bad field value \ud800 here")
            def record_launch(self, child_arid, request):  # type: ignore[override]
                json.dumps(request, ensure_ascii=False).encode("utf-8")  # emulate prompt persist
                return {"launch_prompt_text": "PROMPT"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
            c = _C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                   backend="claude", env={}, generate_executor="pure")
            c.envelopes = [_envelope(_valid_bundle())]  # valid doc; validator forces the violation
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())  # must not raise
            self.assertEqual(oc.status, "fail")
            # bundle_meta persisted cleanly (excerpt normalized), and its write did not fail_close.
            meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
            self.assertEqual(meta["failure_category"], "bundle_schema_violation")
            self.assertEqual(oc.leaf_returncode, 0)   # NOT a host-write fail_close


# ======================================================================================
# build_launch_request pure variant
# ======================================================================================
class PureLaunchRequestTests(unittest.TestCase):
    def _refs(self, repo):
        return _write_node(repo)

    def test_pure_producer_request_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            refs = self._refs(Path(tmp))
            req = wc.build_launch_request(
                refs, step="generate", substep="generate", orchestration_id="o",
                orchestration_agent_run_id="orch", child_agent_run_id="c",
                agent_model="opus", workflow_mode="dev",
                makefile_host_authored=True, runner_host_authored=True,
                pure_leaf=True, pure_context={"harness_capabilities": "x", "target_profile": "y",
                                              "ir_document": "z", "tests_document": "t"})
            self.assertEqual(req["leaf_mode"], "pure")
            self.assertEqual(req["prompt_contract_version"], PURE_PROMPT_CONTRACT_VERSION)
            self.assertEqual(req["allowed_output_paths"], [])
            self.assertEqual(req["skill_name"], "")
            self.assertEqual(req["skill_ref"], "")
            self.assertEqual(req["skill_must_read_refs"], "")
            self.assertIn("pure_context", req)


# ======================================================================================
# Terminal-payload carve-out (M-C precondition 修正2)
# ======================================================================================
class PureTerminalCarveOutTests(unittest.TestCase):
    def _setup(self, tmp):
        repo = Path(tmp)
        arid = "child-pure-1"
        launches = repo / "workspace" / "orchestrations" / "o" / "launches"
        launches.mkdir(parents=True, exist_ok=True)
        (launches / f"{arid}.request.json").write_text(
            json.dumps({"leaf_mode": "pure", "step": "generate", "substep": "generate"}),
            encoding="utf-8")
        return repo, arid

    def test_pure_pass_row_requires_empty_output_refs(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo, arid = self._setup(tmp)
            with patch.object(ort, "_validate_actual_write_paths", return_value=None):
                # empty output_refs accepted
                ort._validate_terminal_run_payload(
                    repo, "o", {"agent_role": "substep", "status": "pass",
                                "agent_run_id": arid, "output_refs": []})
                # non-empty rejected (forged provenance)
                with self.assertRaises(ValueError):
                    ort._validate_terminal_run_payload(
                        repo, "o", {"agent_role": "substep", "status": "pass",
                                    "agent_run_id": arid, "output_refs": ["x/y.f90"]})
                # Codex P2 (finding 4): a MISSING field is not "empty" — a tampered pass row that
                # drops output_refs must be rejected, not read as [] and waved through.
                with self.assertRaises(ValueError):
                    ort._validate_terminal_run_payload(
                        repo, "o", {"agent_role": "substep", "status": "pass",
                                    "agent_run_id": arid})


# ======================================================================================
# Cold-repair prompt contract (M-C 修正4)
# ======================================================================================
class PureColdRepairPromptTests(unittest.TestCase):
    def _req(self, **overrides):
        req = {
            "leaf_mode": "pure", "step": "generate", "substep": "generate",
            "node_key": _NODE, "orchestration_id": "o", "agent_run_id": "c",
            "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
            "repair_findings": "capability_requirements missing",
            "pure_context": {"harness_capabilities": "hc", "target_profile": "tp",
                             "ir_document": "ir", "tests_document": "tt"},
            "prior_document": '{"bundle_schema_version": "1.0.0"}',
        }
        req.update(overrides)
        return req

    def test_cold_repair_includes_output_contract_and_prior_document(self) -> None:
        text = ort._render_pure_repair_prompt(self._req())
        self.assertIn("Output contract", text)
        self.assertIn("prior document under repair", text)
        self.assertIn("bundle_schema_version", text)  # the prior document body

    def test_warm_repair_omits_output_contract_and_prior_document(self) -> None:
        text = ort._render_pure_repair_prompt(self._req(warm_resume=True))
        self.assertNotIn("Output contract", text)
        self.assertNotIn("prior document under repair", text)

    def test_cold_repair_includes_dependency_facts(self) -> None:
        # Codex P2: a cold-fallback repair must re-inline the host-resolved dependency facts (the
        # initial launch injects them), else a component-dependent node could re-author code
        # without its dependency APIs. `resolved_dependencies` is threaded on every repair turn.
        req = self._req(resolved_dependencies=[
            {"node_key": "component/foo@1.0.0", "pipeline_ref": "workspace/x",
             "run_id": "r1", "aggregate_verdict_ref": "workspace/v"}])
        text = ort._render_pure_repair_prompt(req)
        self.assertIn("Dependency facts", text)
        self.assertIn("component/foo@1.0.0", text)

    def test_warm_repair_omits_dependency_facts(self) -> None:
        # A warm resume already holds the dependency facts from the initial turn — omit them.
        req = self._req(warm_resume=True, resolved_dependencies=[
            {"node_key": "component/foo@1.0.0", "pipeline_ref": "workspace/x",
             "run_id": "r1", "aggregate_verdict_ref": "workspace/v"}])
        text = ort._render_pure_repair_prompt(req)
        self.assertNotIn("Dependency facts", text)

    def test_output_contract_lift_is_the_whole_paragraph(self) -> None:
        # Guards against silent truncation: if a template edit splits the "Output contract"
        # paragraph with a blank line, `split("\n\n")` would drop everything after it and the
        # cold repair would ship a schema-thin contract. Pin both the heading (start) and the
        # paragraph's final clause (end) so either kind of drift fails the suite rather than
        # silently degrading a cold repair turn.
        text = ort._pure_output_contract_text(self._req())
        self.assertTrue(text.startswith("Output contract"))
        self.assertIn("diagnose it", text)  # the closing clause of the generate.generate contract


# ======================================================================================
# post_generate bundle re-validation
# ======================================================================================
class PurePostGenerateBundleTests(unittest.TestCase):
    def _gen_dir(self, tmp):
        repo = Path(tmp)
        # The tamper gate now re-runs the FULL acceptance contract, so it reads the IR + sidecar
        # for capability negotiation / state vars / assembly graph — write them (returns ir_ref).
        refs = _write_node(repo)
        gen = repo / "src" / _SPEC_ID
        (gen / "src").mkdir(parents=True, exist_ok=True)
        bundle = _valid_bundle()
        (gen / "codegen_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        for entry in bundle["files"]:
            (gen / "src" / entry["logical_path"]).write_text(entry["content"], encoding="utf-8")
        (gen / "src" / f"{_SPEC_ID}_runner.f90").write_text("program p\nend program\n",
                                                            encoding="utf-8")
        return repo, gen, refs.ir_ref

    def test_clean_bundle_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertEqual(v, [])

    def test_absent_bundle_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            gen = repo / "src" / _SPEC_ID
            gen.mkdir(parents=True)
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, None, v)
            self.assertEqual(v, [])

    def test_tampered_content_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            (gen / "src" / f"{_SPEC_ID}_model.f90").write_text("tampered\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("tamper" in s for s in v), v)

    def test_undeclared_f90_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            (gen / "src" / "sneaky.f90").write_text("module sneaky\nend module\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("undeclared" in s for s in v), v)

    def test_undeclared_uppercase_F90_flagged(self) -> None:
        # Codex P2 (finding 2): an uppercase .F90 suffix must be caught too — a case-sensitive
        # rglob("*.f90") would miss it and let the undeclared source bypass the provenance check.
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            (gen / "src" / "sneaky.F90").write_text("module sneaky\nend module\n", encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("undeclared" in s for s in v), v)

    def test_capability_tamper_flagged(self) -> None:
        # Codex P2 (finding 1): a schema-VALID but unsupported capability swap leaves every source
        # byte unchanged; the gate must still reject it via the reused capability negotiation.
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            bundle = _valid_bundle()
            bundle["capability_requirements"] = ["batched_cases@1"]  # schema-valid, unsupported
            (gen / "codegen_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("bundle_capability_unsatisfied" in s for s in v), v)


# ======================================================================================
# run_workflow --generate-executor pure block (M-C inert)
# ======================================================================================
class GenerateExecutorFlagTests(unittest.TestCase):
    def test_pure_selection_accepted_after_md(self) -> None:
        # M-D unlocks `--generate-executor pure`: the gate no longer errors, so a pure selection
        # passes the executor block and proceeds to normal startup resolution (here failing on the
        # bogus spec, NOT on `generate_executor_pure_unavailable`).
        import io
        from contextlib import redirect_stdout
        import tools.run_workflow as rw
        buf = io.StringIO()
        prev = os.environ.pop("METDSL_GENERATE_EXECUTOR", None)
        try:
            with redirect_stdout(buf):
                rc = rw.main(["spec/nonexistent_xyz", "generate", "--generate-executor", "pure"])
            self.assertNotIn("generate_executor_pure_unavailable", buf.getvalue())
            self.assertIn("invalid_startup_input", buf.getvalue())
            self.assertEqual(rc, 2)
        finally:
            # main() sets os.environ["METDSL_GENERATE_EXECUTOR"] to the resolved value, so restore
            # the ORIGINAL state fully — including deleting it when it was absent before — or the
            # leaked "pure" pollutes every later test's ambient env.
            if prev is not None:
                os.environ["METDSL_GENERATE_EXECUTOR"] = prev
            else:
                os.environ.pop("METDSL_GENERATE_EXECUTOR", None)

    def test_invalid_env_executor_rejected(self) -> None:
        # Codex P2 (finding 3): a typo in METDSL_GENERATE_EXECUTOR bypasses argparse's `choices`;
        # the resolved value must be validated so it cannot silently fall through to legacy.
        import io
        from contextlib import redirect_stdout
        import tools.run_workflow as rw
        buf = io.StringIO()
        prev = os.environ.get("METDSL_GENERATE_EXECUTOR")
        os.environ["METDSL_GENERATE_EXECUTOR"] = "pur"
        try:
            with redirect_stdout(buf):
                rc = rw.main(["spec/x", "generate"])
            self.assertEqual(rc, 2)
            self.assertIn("generate_executor_invalid", buf.getvalue())
        finally:
            if prev is not None:
                os.environ["METDSL_GENERATE_EXECUTOR"] = prev
            else:
                os.environ.pop("METDSL_GENERATE_EXECUTOR", None)


if __name__ == "__main__":
    unittest.main()
