#!/usr/bin/env python3
"""M-B: Z2 pure-function-leaf launch wiring (inert — no caller passes `leaf_mode=pure` yet).

Covers the pure branches added across `tools/orchestration_runtime.py` and
`tools/validate_pipeline_semantics.py`: prepared-payload skill emptying, the pure request
validator, the pure launch/repair renderers and their markers, the gate-allowlist fence
carve-out, the record-launch write-authorization skip (with the read-only profile / denied-all
read manifest / `pure_readonly` capability), the empty-write_roots fail-closed guard, and the
pipeline-semantics launch-record sweep's pure checks.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Trust the persisted dependency-readiness booleans _mark_dependencies_ready injects, so a
# record_launch test does not need a real deps.yaml on disk (mirrors test_orchestration_runtime).
os.environ.setdefault("METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.orchestration_runtime as ort
import tools.validate_pipeline_semantics as vps
from tools.orchestration_runtime import (
    build_access_policy_payload,
    build_capability_document,
    init_orchestration,
    record_launch,
    write_preflight,
)
from tools.pure_leaf import (
    PURE_DOC_FENCE_BEGIN,
    PURE_DOC_FENCE_END,
    PURE_PROMPT_CONTRACT_VERSION,
    PURE_PROMPT_SENTINEL,
)

_NODE = "problem/shallow_water2d@0.3.0"
_NODE_SAFE = "problem__shallow_water2d__0.3.0"
_IR_REF = f"workspace/ir/{_NODE_SAFE}/shallow-water2d_20260415_001"
_PIPE_REF = f"workspace/pipelines/{_NODE_SAFE}/shallow-water2d_20260415_001"
_DEP_REF = f"{_IR_REF}/spec.ir.yaml"


def _pure_generate_context() -> dict[str, str]:
    return {
        "harness_capabilities": '{"operations": []}',
        "target_profile": "language=fortran build_system=make",
        "ir_document": "algorithm:\n  state_variables: [h]\n",
        "tests_document": "- test: conserves mass",
        "runner_document": ("program sw_runner\n  use sw_checks, only: &\n    case_run\n"
                            "end program\n"),
    }


def _pure_verify_context() -> dict[str, str]:
    return {
        "controlled_spec_document": "the model conserves mass",
        "tests_document": "- test: conserves mass",
        "ir_document": "algorithm:\n  state_variables: [h]\n",
        "bundle_document": '{"files": []}',
    }


def _pure_request(substep: str = "generate", **overrides) -> dict[str, object]:
    ctx = _pure_generate_context() if substep == "generate" else _pure_verify_context()
    req: dict[str, object] = {
        "leaf_mode": "pure",
        "agent_model": "opus",
        "agent_role": "substep",
        "node_key": _NODE,
        "step": "generate",
        "substep": substep,
        "orchestration_id": "orch_001",
        "agent_run_id": "ar_pure_child_001",
        "parent_agent_run_id": "orch_run_001",
        "ir_ref": _IR_REF,
        "pipeline_ref": _PIPE_REF,
        "dependency_ref": _DEP_REF,
        "source_id": "src_20260415_001",
        "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
        "allowed_output_paths": [],
        "pure_context": ctx,
    }
    req.update(overrides)
    return req


def _mark_dependencies_ready(repo_root: Path, orchestration_id: str = "orch_001") -> None:
    meta_path = (
        repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
    )
    if not meta_path.is_file():
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["dependency_readiness"] = {
        "direct_dependency_compile_readiness": True,
        "direct_dependency_execution_readiness": True,
        "detail": {
            "ir_ref_verified": True,
            "pipeline_ref_verified": True,
            "aggregate_verdict_verified": True,
        },
        "dep_set_fingerprint": ort._dependency_set_fingerprint(repo_root, meta.get("spec_ref")),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _spawn_response(session_id: str) -> dict[str, object]:
    return {"agent_session_id": session_id, "accepted": True, "launch_reply": f"ok {session_id}"}


def _preflight(repo_root: Path) -> None:
    write_preflight(
        repo_root=repo_root,
        orchestration_id="orch_001",
        payload={
            "status": "pass",
            "sandbox_runtime": "bwrap",
            "sandbox_enforced": True,
            "can_launch_step_agents": True,
            "can_launch_substep_agents": True,
            "feature_states": {"multi_agent": True, "hooks": True},
            "checks": [{"name": "multi_agent_enabled", "pass": True}],
        },
    )


# ======================================================================================
# B1 / B2: prepared-payload emptying + request validation
# ======================================================================================
class PurePayloadValidationTests(unittest.TestCase):
    def test_prepare_payload_pure_empties_skill_fields(self) -> None:
        prepared = ort.prepare_launch_request_payload(_pure_request())
        self.assertEqual(prepared["skill_name"], "")
        self.assertEqual(prepared["skill_ref"], "")
        self.assertEqual(prepared["skill_must_read_refs"], "")

    def test_validate_payload_accepts_pure_generate_generate(self) -> None:
        ort._validate_launch_request_payload(ort.prepare_launch_request_payload(_pure_request("generate")))

    def test_validate_payload_accepts_pure_generate_verify(self) -> None:
        ort._validate_launch_request_payload(ort.prepare_launch_request_payload(_pure_request("verify")))

    def test_validate_payload_rejects_pure_outside_generate(self) -> None:
        for step, substep in (("compile", "generate"), ("validate", "judge")):
            bad = _pure_request()
            bad["step"] = step
            bad["substep"] = substep
            with self.assertRaises(ValueError):
                ort._validate_pure_launch_request_payload(bad)

    def test_validate_payload_rejects_pure_with_deterministic(self) -> None:
        bad = _pure_request(deterministic=True)
        with self.assertRaises(ValueError):
            ort._validate_pure_launch_request_payload(bad)

    def test_validate_payload_rejects_unknown_leaf_mode(self) -> None:
        bad = _pure_request(leaf_mode="agentic")
        with self.assertRaises(ValueError):
            ort._validate_pure_launch_request_payload(bad)

    def test_validate_payload_pure_requires_exact_contract_version(self) -> None:
        # Any version other than the CURRENT constant is rejected — including the immediately
        # preceding one. (The stand-in must not be a version that a later bump makes valid; keep
        # it a value the contract will never take.)
        for version in ("pure-OBSOLETE", "pure-1", "pure-3", "", None):
            bad = _pure_request(prompt_contract_version=version)
            with self.assertRaises(ValueError):
                ort._validate_pure_launch_request_payload(bad)

    def test_validate_payload_pure_requires_context_keys(self) -> None:
        for substep, keys in (
            ("generate", _pure_generate_context()),
            ("verify", _pure_verify_context()),
        ):
            for missing in keys:
                ctx = dict(keys)
                del ctx[missing]
                bad = _pure_request(substep)
                bad["pure_context"] = ctx
                with self.assertRaises(ValueError):
                    ort._validate_pure_launch_request_payload(bad)

    def test_validate_payload_pure_warm_repair_may_omit_context(self) -> None:
        req = _pure_request(warm_resume=True, repair_strategy="reuse",
                            repair_findings="fix status", repair_target_agent_run_id="ar_prev")
        del req["pure_context"]
        ort._validate_pure_launch_request_payload(req)

    def test_validate_payload_pure_restart_repair_still_requires_context(self) -> None:
        # The context-omission exemption applies ONLY to a genuine warm REUSE repair. A restart
        # (or any non-reuse) with warm_resume + findings has no resumed session and must still
        # carry pure_context.
        for strategy in ("restart", "none"):
            req = _pure_request(warm_resume=True, repair_strategy=strategy,
                                repair_findings="fix status",
                                repair_target_agent_run_id="ar_prev")
            del req["pure_context"]
            with self.assertRaises(ValueError):
                ort._validate_pure_launch_request_payload(req)

    def test_validate_payload_rejects_explicit_null_leaf_mode(self) -> None:
        # A PRESENT leaf_mode other than "pure" — including an explicit JSON null — must be
        # rejected by the full validator, NOT silently treated as an agentic (write-capable)
        # launch. Key presence, not `is not None`, is the gate.
        for bad_value in (None, "", "agentic", "PURE_TYPO"):
            req = _pure_request("generate")
            req["leaf_mode"] = bad_value
            with self.assertRaises(ValueError):
                ort._validate_launch_request_payload(ort.prepare_launch_request_payload(dict(req)))

    def test_validate_payload_pure_rejects_nonempty_output_paths(self) -> None:
        bad = _pure_request(allowed_output_paths=["workspace/pipelines/x/source/s/model.f90"])
        with self.assertRaises(ValueError):
            ort._validate_pure_launch_request_payload(bad)

    def test_validate_payload_pure_verify_skips_skill_requirements(self) -> None:
        # A pure verify carries empty skill fields; the agentic verify skill-requirement block
        # must NOT reject it (this is the check that currently rejects pure verify).
        prepared = ort.prepare_launch_request_payload(_pure_request("verify"))
        self.assertEqual(prepared["skill_name"], "")
        ort._validate_launch_request_payload(prepared)


# ======================================================================================
# B3 / B4 / B5 / B6 / B8: renderers, markers, fence carve-out
# ======================================================================================
class PureRenderTests(unittest.TestCase):
    def test_render_pure_prompt_full_skeleton(self) -> None:
        prepared = ort.prepare_launch_request_payload(_pure_request("generate"))
        prompt = prepared["launch_prompt_full"]
        self.assertTrue(prompt.startswith(PURE_PROMPT_SENTINEL))
        for token in (
            "Target node_key:", "Target step:", "Target substep:",
            "orchestration_id:", "agent_run_id:",
            f"prompt_contract_version: {PURE_PROMPT_CONTRACT_VERSION}",
        ):
            self.assertIn(token, prompt)
        # No `<placeholder>` token survives substitution.
        self.assertNotIn("<tests_document>", prompt)
        self.assertNotIn("<ir_document>", prompt)
        self.assertNotIn("<runner_document>", prompt)
        # The identity block is the tail (variable ids last).
        self.assertGreater(prompt.index("Target node_key:"), prompt.index("Tests"))

    def test_pure_launch_prompt_carries_the_runner_and_its_checks_abi(self) -> None:
        # Z2 defect D: the tool-less leaf cannot read CHECKS_MODULE_CONTRACT.md or the runner
        # from disk, so the ABI reaches it ONLY here. Pin the heading AND the runner body's
        # `use ..._checks, only:` line — a heading alone would still pass with an empty runner.
        prepared = ort.prepare_launch_request_payload(_pure_request("generate"))
        prompt = prepared["launch_prompt_full"]
        self.assertIn("Host-rendered runner", prompt)
        self.assertIn("use sw_checks, only:", prompt)
        self.assertIn("case_run", prompt)

    def test_prompt_states_the_static_prohibitions_the_leaf_cannot_otherwise_know(self) -> None:
        # `_validate_checks_source_files` rejects three things the acceptance gate does NOT
        # pre-empt, so each is a phase reopen — the failure mode this whole change exists to
        # remove. A tool-less leaf can only learn them here. The harness ban is the sharpest:
        # the injected runner IS a `use harness_fortran_cpu_model` block the leaf must not copy.
        tpl = ort._load_launch_prompt_templates()["pure generate.generate"]
        for token in ("use harness_", "open(", "verdict.json", "aggregate_verdict.json",
                      "summary.json", "trial_meta.json"):
            self.assertIn(token, tpl, f"prompt must name the {token!r} prohibition")

    def test_placeholder_drop_uses_the_renderer_definition_of_a_slot(self) -> None:
        # One fact, one authority. A second pattern that disagreed would let a real slot survive
        # the cold-repair lift and ship as a literal token — the leak the drop exists to prevent.
        self.assertIs(ort._PURE_PLACEHOLDER_ONLY_RE, ort._PURE_PLACEHOLDER_RE)
        for slot in ("<runner_document>", "<runner_document2>", "<Exemplar>"):
            self.assertTrue(ort._PURE_PLACEHOLDER_ONLY_RE.fullmatch(slot), slot)

    def test_prompt_forbidden_filenames_match_the_gate_exactly(self) -> None:
        # Pin the two together: a name added to the gate's tuple and not to the prompt is a rule
        # the producer is punished for breaking and never told about.
        from tools.validate_pipeline_semantics import FORBIDDEN_RUNNER_OUTPUTS
        tpl = ort._load_launch_prompt_templates()["pure generate.generate"]
        for name in FORBIDDEN_RUNNER_OUTPUTS:
            self.assertIn(name, tpl, f"the prompt must name {name!r}, which the gate rejects")

    def test_cold_repair_reinlines_the_runner_document(self) -> None:
        # A cold fallback re-authors the bundle with no prior turn, so the ABI must come back
        # with the rest of the context (auto-inlined from pure_context).
        req = ort.prepare_launch_request_payload(_pure_request(
            "generate", repair_findings="fix the checks ABI", repair_strategy="reuse"))
        text = ort._render_pure_repair_prompt(req)
        self.assertIn("**runner_document:**", text)
        self.assertIn("use sw_checks, only:", text)

    def test_render_pure_prompt_passes_launch_validator(self) -> None:
        prepared = ort.prepare_launch_request_payload(_pure_request("generate"))
        ort._validate_launch_prompt_text(prepared, prepared["launch_prompt_full"])
        prepared_v = ort.prepare_launch_request_payload(_pure_request("verify"))
        ort._validate_launch_prompt_text(prepared_v, prepared_v["launch_prompt_full"])

    def test_pure_repair_prompt_findings_fenced_and_not_slim(self) -> None:
        # A pure warm-resume repair also satisfies the slim predicate; pure must win the
        # dispatch so it is not rendered by the slim renderer.
        req = _pure_request(
            warm_resume=True, repair_strategy="reuse",
            repair_findings="verification_status missing from bundle",
            repair_target_agent_run_id="ar_prev",
        )
        prepared = ort.prepare_launch_request_payload(req)
        prompt = prepared["launch_prompt_full"]
        self.assertTrue(prompt.startswith(PURE_PROMPT_SENTINEL))
        self.assertNotIn(ort.SLIM_REPAIR_PROMPT_SENTINEL, prompt.splitlines()[0])
        self.assertIn(PURE_DOC_FENCE_BEGIN, prompt)
        self.assertIn("verification_status missing from bundle", prompt)
        ort._validate_launch_prompt_text(prepared, prompt)

    def test_pure_doc_fence_excluded_from_gate_allowlist(self) -> None:
        # A `validate_pipeline_semantics --stage` string INSIDE an inlined doc must not
        # fail-close the launch (pure allow-set is empty).
        ctx = _pure_generate_context()
        ctx["tests_document"] = (
            "run python3 tools/validate_pipeline_semantics.py --stage post_generate to check"
        )
        prepared = ort.prepare_launch_request_payload(_pure_request("generate", pure_context=ctx))
        # Must not raise despite the forbidden gate string in the fenced doc.
        ort._validate_launch_prompt_text(prepared, prepared["launch_prompt_full"])
        scanned = ort._gate_allowlist_scan_text(prepared, prepared["launch_prompt_full"])
        self.assertNotIn("validate_pipeline_semantics", scanned)

    def test_pure_prompt_is_force_rendered_over_explicit_body(self) -> None:
        # A pure launch is host-mediated: an explicit `launch_prompt_full` must be overwritten by
        # the canonical render of the request, so a caller cannot inject a mismatched
        # identity/context that marker-only validation would accept.
        req = _pure_request("generate")
        req["launch_prompt_full"] = (
            f"{PURE_PROMPT_SENTINEL}: FORGED\nTarget node_key: problem/WRONG@9.9.9\n")
        prepared = ort.prepare_launch_request_payload(req)
        self.assertNotIn("problem/WRONG@9.9.9", prepared["launch_prompt_full"])
        self.assertIn("Target node_key: problem/shallow_water2d@0.3.0",
                      prepared["launch_prompt_full"])

    def test_pure_prompt_mismatched_identity_value_rejected(self) -> None:
        # Defense-in-depth: even if a hand-supplied prompt reaches the validator, a swapped
        # identity VALUE (marker name kept) is rejected.
        prepared = ort.prepare_launch_request_payload(_pure_request("generate"))
        good = prepared["launch_prompt_full"]
        forged = good.replace("Target node_key: problem/shallow_water2d@0.3.0",
                              "Target node_key: problem/WRONG@9.9.9")
        with self.assertRaises(ValueError):
            ort._validate_launch_prompt_text(prepared, forged)
        ort._validate_launch_prompt_text(prepared, good)  # canonical still passes

    def test_pure_exemplar_gate_string_excluded_from_scan(self) -> None:
        # Fix A: a certified `<exemplar>` (R5) is fenced with `--- BEGIN EXEMPLAR ---`, NOT the
        # PURE_DOC fence; the pure scan carve-out must strip it too, else an exemplar source
        # containing a `validate_pipeline_semantics --stage` string fail-closes the pure launch.
        exemplar = {
            "node_key": "component/sibling@1.0.0",
            "sources": [{
                "filename": "sibling_model.f90",
                "text": "! example: python3 tools/validate_pipeline_semantics.py --stage post_generate",
            }],
        }
        prepared = ort.prepare_launch_request_payload(_pure_request("generate", exemplar=exemplar))
        prompt = prepared["launch_prompt_full"]
        self.assertIn("BEGIN EXEMPLAR", prompt)  # exemplar really was injected
        ort._validate_launch_prompt_text(prepared, prompt)  # must not fail-close
        scanned = ort._gate_allowlist_scan_text(prepared, prompt)
        self.assertNotIn("validate_pipeline_semantics", scanned)

    def test_pure_launch_prompt_carries_authoring_rules_tokens(self) -> None:
        # Defect C (billed E2E, 2026-07-16): the pure template stated NO authoring rules, so the
        # producer met the deterministic gates blind and oscillated between the two wrong
        # `implicit none` forms until its retry budget ran out. Pin the load-bearing literals of
        # each rule group — a rewrite that drops one fails here rather than in a billed run.
        prepared = ort.prepare_launch_request_payload(_pure_request("generate"))
        prompt = prepared["launch_prompt_full"]
        for token in (
            "! allow(C003)",          # the C003 <-> f2008 escape, verbatim
            "-std=f2008",             # ... and why the F2018 spec-list is not the fix
            "use, intrinsic ::",      # fortitude C122
            "public :: <spec_id>__<op>",  # fortitude C131 + its Generate.static counterpart
            "case default",           # fortitude C011
            "associate (unused_<name> => <name>)",  # the unused-dummy bind form
            "intent(out)",            # Generate.static dataflow
            "INERT",                  # the inert dependency-call rule
        ):
            self.assertIn(token, prompt)
        # `<name>` is not a substitution key, so the single-pass renderer must leave the
        # `associate` form intact — the assertion above is also this pin.
        # Static prefix first (byte-stable order): rules precede the variable documents.
        self.assertLess(prompt.index("Authoring rules"), prompt.index("Harness capabilities"))

    def test_pure_launch_prompt_renders_exemplar_block(self) -> None:
        # Complements the fence/scan test below with the CONTENT assertion: an injected exemplar
        # must actually reach the rendered prompt (heading + source body), which is what defect B
        # silently lost by never passing `exemplar=` on the pure launch request.
        exemplar = {
            "node_key": "component/sibling@1.0.0",
            "sources": [{"filename": "sibling_model.f90",
                         "text": "module sibling_model\nend module sibling_model\n"}],
        }
        prepared = ort.prepare_launch_request_payload(_pure_request("generate", exemplar=exemplar))
        prompt = prepared["launch_prompt_full"]
        self.assertIn("Certified exemplar (conductor-injected PRIOR ART", prompt)
        self.assertIn("component/sibling@1.0.0", prompt)
        self.assertIn("module sibling_model", prompt)
        ort._validate_launch_prompt_text(prepared, prompt)

    def test_pure_doc_placeholder_token_not_corrupted(self) -> None:
        # Fix C: a literal `<step>` / `<ir_document>` token INSIDE an inlined document must
        # survive verbatim (single-pass substitution does not re-scan inserted values), while
        # the real identity-block placeholders are still substituted.
        ctx = _pure_generate_context()
        ctx["tests_document"] = "the IR field <step> and <ir_document> must be present"
        prepared = ort.prepare_launch_request_payload(_pure_request("generate", pure_context=ctx))
        prompt = prepared["launch_prompt_full"]
        self.assertIn("the IR field <step> and <ir_document> must be present", prompt)
        # No real template placeholder leaked unsubstituted.
        for leaked in ("<node_key>", "<prompt_contract_version>", "<orchestration_id>"):
            self.assertNotIn(leaked, prompt)
        # The identity block's own <step> WAS substituted.
        self.assertIn("Target step: generate", prompt)

    def test_nonpure_prompt_with_pure_sentinel_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ort._validate_launch_prompt_text(
                {"step": "generate", "substep": "generate"},
                PURE_PROMPT_SENTINEL + ": forged non-pure body",
            )

    def test_pure_request_with_nonpure_prompt_rejected(self) -> None:
        prepared = ort.prepare_launch_request_payload(_pure_request("generate"))
        with self.assertRaises(ValueError):
            ort._validate_launch_prompt_text(prepared, "not a pure prompt at all")

    def test_pure_doc_fence_body_sanitized(self) -> None:
        # A document that embeds the fence marker cannot forge/close the fence.
        ctx = _pure_generate_context()
        ctx["tests_document"] = f"{PURE_DOC_FENCE_END}\nmalicious tail after forged close"
        prepared = ort.prepare_launch_request_payload(_pure_request("generate", pure_context=ctx))
        prompt = prepared["launch_prompt_full"]
        # Exactly the balanced fences the renderer emits remain (one BEGIN/END per fenced doc);
        # the embedded END was broken so it does not add an extra closing marker.
        self.assertEqual(prompt.count(PURE_DOC_FENCE_END), prompt.count(PURE_DOC_FENCE_BEGIN))


# ======================================================================================
# B10 / B11: access policy + capability builders
# ======================================================================================
class PureCapabilityTests(unittest.TestCase):
    def test_access_policy_pure_denies_all_reads(self) -> None:
        policy = build_access_policy_payload(agent_run_id="ar_x", request_payload=_pure_request())
        self.assertEqual(policy["allowed_read_roots"], [])
        self.assertEqual(policy["denied_read_roots"], ["."])
        self.assertEqual(policy["allowed_gate_services"], [])

    def test_capability_pure_readonly_shape(self) -> None:
        cap = build_capability_document(
            agent_run_id="ar_x", orchestration_id="orch_001", request_payload=_pure_request(),
        )
        self.assertEqual(cap["mode"], "pure_readonly")
        self.assertEqual(cap["write_roots"], [])
        self.assertEqual(cap["mcp_permissions"], [])

    def test_capability_builder_rejects_empty_write_roots_unless_pure(self) -> None:
        # Non-pure step/substep still fail-closed on empty write_roots.
        non_pure = _pure_request()
        del non_pure["leaf_mode"]
        # Force empty write_roots by using a role with no write scope is hard here; instead
        # assert the pure path is the ONLY one that yields empty write_roots without raising.
        cap = build_capability_document(
            agent_run_id="ar_x", orchestration_id="orch_001", request_payload=_pure_request(),
        )
        self.assertEqual(cap["write_roots"], [])
        # A non-pure generate substep gets a non-empty write_roots (no raise, not empty).
        cap2 = build_capability_document(
            agent_run_id="ar_y", orchestration_id="orch_001", request_payload=non_pure,
        )
        self.assertNotEqual(cap2["write_roots"], [])
        self.assertNotIn("mode", cap2)

    def test_capability_builder_raises_on_empty_write_roots_for_nonpure(self) -> None:
        # Directly pin the empty-write_roots fail-closed guard for a NON-pure step/substep: with
        # _write_roots_for_launch forced empty, build_capability_document must raise
        # capability_invalid_empty_write_roots. (Without this the whole guard could be deleted
        # and every other test would stay green — only the `not pure` clause is otherwise
        # covered.)
        non_pure = _pure_request()
        del non_pure["leaf_mode"]
        with patch.object(ort, "_write_roots_for_launch", return_value=[]):
            with self.assertRaises(ValueError) as ctx:
                build_capability_document(
                    agent_run_id="ar_z", orchestration_id="orch_001", request_payload=non_pure,
                )
        self.assertIn("capability_invalid_empty_write_roots", str(ctx.exception))
        # The pure path with the SAME forced-empty helper still succeeds (it never calls the
        # helper — write_roots is [] by construction) and is exempt from the guard.
        with patch.object(ort, "_write_roots_for_launch", return_value=[]):
            cap = build_capability_document(
                agent_run_id="ar_z2", orchestration_id="orch_001", request_payload=_pure_request(),
            )
        self.assertEqual(cap["write_roots"], [])
        self.assertEqual(cap["mode"], "pure_readonly")


# ======================================================================================
# B9 + plan-fix-1: record_launch writes-and-skips, baseline, empty-write_roots fail-closed
# ======================================================================================
class PureRecordLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            k: os.environ.get(k)
            for k in ("METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT",
                      "METDSL_ORCHESTRATION_ASSUME_BWRAP", "METDSL_HOME")
        }
        os.environ["METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT"] = "0"
        os.environ["METDSL_ORCHESTRATION_ASSUME_BWRAP"] = "1"
        os.environ["METDSL_HOME"] = "/tmp/pure-leaf-test-home"

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _launch(self, repo_root: Path, substep: str = "generate") -> dict[str, object]:
        init_orchestration(repo_root=repo_root, orchestration_id="orch_001")
        _mark_dependencies_ready(repo_root)
        _preflight(repo_root)
        req = _pure_request(substep)
        prompt = ort.render_launch_prompt_text(ort.prepare_launch_request_payload(dict(req)))
        req["launch_prompt_full"] = prompt
        return record_launch(
            repo_root=repo_root,
            orchestration_id="orch_001",
            parent_agent_run_id="orch_run_001",
            child_agent_run_id="ar_pure_child_001",
            request_payload=req,
            response_payload=_spawn_response("sess_pure_001"),
        )

    def test_record_launch_pure_writes_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._launch(repo_root)
            base = repo_root / "workspace/orchestrations/orch_001"
            arid = "ar_pure_child_001"
            # WRITES: capability (pure_readonly / empty write_roots), denied-all read manifest,
            # read-only sandbox profile.
            cap = json.loads((base / "capabilities" / f"{arid}.json").read_text())
            self.assertEqual(cap["mode"], "pure_readonly")
            self.assertEqual(cap["write_roots"], [])
            rman = json.loads((base / "read_manifests" / f"{arid}.json").read_text())
            self.assertEqual(rman["allowed_read_roots"], [])
            self.assertTrue(rman["denied_read_roots"])
            profile = json.loads((base / "sandbox_profiles" / f"{arid}.json").read_text())
            self.assertTrue(profile.get("readonly"))
            self.assertEqual(profile.get("write_roots"), [])
            # SKIPS: output manifest is never written.
            self.assertFalse((base / "output_manifests" / f"{arid}.json").exists())

    def test_record_launch_pure_still_writes_baseline_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._launch(repo_root)
            base = repo_root / "workspace/orchestrations/orch_001"
            # FS-diff baseline + session-run-index are unconditional.
            baseline = ort._load_run_write_baseline(repo_root, "orch_001")
            self.assertIsInstance(baseline, dict)
            index_path = base / "session_run_index.json"
            self.assertTrue(index_path.is_file())
            self.assertIn("ar_pure_child_001", index_path.read_text())

    def test_pure_child_window_write_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            self._launch(repo_root)
            # Simulate a write from inside the pure child window: create a repo file after the
            # baseline was taken. With write_roots=[] the containment rule must flag it.
            forged = repo_root / "workspace" / "pipelines" / _NODE_SAFE / "forged.txt"
            forged.parent.mkdir(parents=True, exist_ok=True)
            forged.write_text("leaf tried to write", encoding="utf-8")
            with self.assertRaises(ValueError):
                ort._validate_actual_write_paths(
                    repo_root,
                    "orch_001",
                    {
                        "agent_role": "substep",
                        "agent_run_id": "ar_pure_child_001",
                        "status": "pass",
                    },
                )


# ======================================================================================
# D: validate_pipeline_semantics pure detectors + parity
# ======================================================================================
class PureValidatePipelineTests(unittest.TestCase):
    def test_pure_detectors(self) -> None:
        self.assertTrue(vps._is_pure_launch_prompt_text(PURE_PROMPT_SENTINEL + ": x"))
        self.assertFalse(vps._is_pure_launch_prompt_text("something else"))
        self.assertTrue(vps._launch_request_is_pure({"leaf_mode": "pure"}))
        self.assertFalse(vps._launch_request_is_pure({"leaf_mode": "agentic"}))

    def test_pure_predicate_shared_single_source(self) -> None:
        # Both modules delegate to pure_leaf.is_pure_request (single detection source), so they
        # cannot disagree about what "pure" is.
        from tools.pure_leaf import is_pure_request, PURE_LEAF_MODE, PURE_CAPABILITY_MODE
        for payload in ({"leaf_mode": "pure"}, {"leaf_mode": "  PURE "}, {"leaf_mode": "agentic"},
                        {}, {"leaf_mode": None}):
            self.assertEqual(ort._is_pure_launch_request(payload), is_pure_request(payload))
            self.assertEqual(vps._launch_request_is_pure(payload), is_pure_request(payload))
        self.assertEqual(PURE_LEAF_MODE, "pure")
        self.assertEqual(PURE_CAPABILITY_MODE, "pure_readonly")

    def test_pure_and_slim_are_mutually_exclusive(self) -> None:
        # A pure warm-resume repair satisfies the slim shape; both slim predicates must exclude
        # it so the render/marker dispatch order is defensive, not load-bearing.
        pure_repair = _pure_request(
            warm_resume=True, repair_strategy="reuse", repair_findings="fix",
            repair_target_agent_run_id="ar_prev")
        self.assertFalse(ort._is_slim_repair_request(pure_repair))
        self.assertFalse(vps._launch_request_is_slim_repair(pure_repair))
        # A genuine (non-pure) slim repair is still slim.
        slim = {"warm_resume": True, "repair_strategy": "reuse", "repair_findings": "fix"}
        self.assertTrue(ort._is_slim_repair_request(slim))
        self.assertTrue(vps._launch_request_is_slim_repair(slim))

    def test_pure_marker_set_matches_orchestration_runtime(self) -> None:
        prepared = ort.prepare_launch_request_payload(_pure_request("generate"))
        ort_markers = set(ort._required_launch_prompt_markers(prepared))
        vps_markers = set(vps._required_launch_prompt_markers_for_role("substep", pure=True))
        self.assertEqual(ort_markers, vps_markers)

    def test_sentinel_parity_across_modules_and_templates(self) -> None:
        self.assertEqual(ort.PURE_PROMPT_SENTINEL, PURE_PROMPT_SENTINEL)
        self.assertEqual(vps.PURE_PROMPT_SENTINEL, PURE_PROMPT_SENTINEL)
        tpl_dir = Path(__file__).resolve().parent.parent / "prompt_templates"
        for fname in ("pure_generate_generate.txt", "pure_generate_verify.txt",
                      "pure_bundle_repair.txt"):
            line0 = (tpl_dir / fname).read_text(encoding="utf-8").splitlines()[0]
            self.assertTrue(line0.startswith(PURE_PROMPT_SENTINEL), (fname, line0))


if __name__ == "__main__":
    unittest.main()
