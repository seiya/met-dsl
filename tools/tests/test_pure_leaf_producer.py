#!/usr/bin/env python3
"""M-C: Z2 pure-function CodegenBundle producer.

Covers the host side of the pure `generate.generate` channel added across
`tools/workflow_conductor.py` (the producer loop, bundle validation + assembly preflight, the
bundle-derived Makefile, bundle_meta), `tools/orchestration_runtime.py` (the terminal-payload
carve-out + the cold-repair prompt contract), `tools/validate_pipeline_semantics.py` (the
post_generate bundle re-validation + the sweep output_refs mirror), and `tools/run_workflow.py`
(the generate-executor surface — since M-F the executor is hardcoded pure).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK", "1")

import tools.codegen_bundle as cb
import tools.orchestration_runtime as ort
import tools.workflow_conductor as wc
import tools.validate_pipeline_semantics as vps
from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION

_NODE = "problem/shallow_water2d@0.3.0"
_SAFE = wc.node_key_safe(_NODE)
_SPEC_ID = "shallow_water2d"
_HARNESS = "infrastructure/harness_fortran_cpu@0.5.0"
_HARNESS_SPEC_ID = "harness_fortran_cpu"
_SPEC_PATH = "spec/problem/ocean/shallow_water2d"

def _runner_text() -> str:
    from tools.runner_renderer import render_runner
    return render_runner(_node_ir(), _SPEC_ID, _HARNESS_SPEC_ID)


def cb_runner_imports(runner_text: str, spec_id: str) -> tuple[str, ...]:
    """The names a rendered runner actually imports — a TEST-only read of the runner, kept here to
    assert that the required ABI is deliberately WIDER than it (production must not key off this;
    `Generate.static` requires all ten regardless)."""
    out: list[str] = []
    body, seen = "", False
    for raw in runner_text.splitlines():
        line = raw.split("!", 1)[0].strip()
        if not seen:
            m = re.match(rf"(?i)^use\s+{re.escape(spec_id)}_checks\s*,\s*only\s*:\s*(.*)$", line)
            if not m:
                continue
            seen, line = True, m.group(1).strip()
        else:
            line = line.lstrip("&").strip()
        cont = line.endswith("&")
        body += line[:-1] if cont else line
        if not cont:
            break
    for tok in body.split(","):
        tok = tok.strip()
        if re.fullmatch(r"[A-Za-z]\w*", tok):
            out.append(tok)
    return tuple(out)


def _checks_symbols() -> tuple[str, ...]:
    """The fixed checks ABI — required in FULL of every M3c node, not the subset this node's
    runner imports (`Generate.static` checks all ten). Imported from the renderer, the ABI's
    authority, rather than hand-typed here."""
    from tools.runner_renderer import CHECKS_PUBLIC_NAMES
    return CHECKS_PUBLIC_NAMES


def _checks_content(*, omit: str = "", as_function: str = "", unexported: str = "") -> str:
    """A checks module publishing the fixed ABI, in the certified idiom (a bare `private` default
    plus an explicit `public ::` list — authoring rule 1). `omit` drops a name entirely,
    `as_function` defines one as a FUNCTION (the two shapes the sw2d P-arm emitted), and
    `unexported` defines one but leaves it off the export list.

    Since the per-id checks ABI (pure-8), the module authors no check id — the runner supplies each
    id as a literal actual — so there is no id-literal-presence layer to satisfy here."""
    syms = [s for s in _checks_symbols() if s != omit]
    body = [f"module {_SPEC_ID}_checks", "  private"]
    body += [f"  public :: {s}" for s in syms if s != unexported]
    body.append("contains")
    for sym in syms:
        if sym == as_function:
            body += [f"  function {sym}() result(r)", f"  end function {sym}"]
        else:
            body += [f"  subroutine {sym}()", f"  end subroutine {sym}"]
    body.append("end module")
    return "\n".join(body) + "\n"


def _valid_bundle() -> dict:
    return {
        "bundle_schema_version": "1.0.0",
        "optimization_unit": {"members": [_NODE]},
        "files": [
            {"logical_path": f"{_SPEC_ID}_model.f90", "role": "model", "language": "fortran",
             "member_node_key": _NODE, "content": f"module {_SPEC_ID}_model\nend module\n",
             "modules": [f"{_SPEC_ID}_model"]},
            {"logical_path": f"{_SPEC_ID}_checks.f90", "role": "checks", "language": "fortran",
             "member_node_key": _NODE, "content": _checks_content(),
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


def _node_ir(state_vars=("h", "u", "v")) -> dict:
    """A minimal M3c problem IR that `render_runner` accepts — so the fixture's runner can be
    produced by the real renderer instead of hand-typed (a hand-typed runner would let the ABI
    layer pass against a shape the renderer never emits)."""
    return {
        "meta": {"spec_id": _SPEC_ID, "spec_kind": "problem"},
        "impl_defaults": {
            "toolchain": {"language": "fortran", "standard": "f2008", "build_system": "make"},
            "target": {"backend": "cpu"},
        },
        # Canonical shape: state_variables is a list of OBJECTS ({name, shape_expr}), NOT bare
        # strings — the shape real specs emit (a string-list masks the name-extraction path).
        "algorithm": {"state_variables": [{"name": v, "shape_expr": "[nx]"} for v in state_vars]},
        "dependency": {"direct_deps": [{"node_key": _HARNESS}]},
        "case": {"test_case_set": [{"case_id": "c1"}]},
        "io_contract": {
            "raw_requirements": {"required_evidence": [
                {"artifact": "state_snapshots", "schema": {
                    "variables": [{"name": "h", "shape_expr": "[4, 4]"}],
                    "time_variable": "t"}},
            ]},
            "test_evidence_requirements": [{"test_id": "c1", "required_raw_variables": ["h"]}],
            "diagnostics_contract": {
                "checks": [{"id": "mass"}],
                "metrics": ["error.l2"],
                "verdict": {"required": True, "fields": ["overall", "failed_checks"]},
            },
            "test_predicates": [
                {"test_id": "c1", "expected_outcome": "pass", "target_cases": ["c1"]}],
        },
    }


def _write_node(repo: Path, *, ir_id="sw_20260715_001", source_id="src_20260715_001",
                state_vars=("h", "u", "v"), stage_runner=True) -> wc.NodeRefs:
    """Write a minimal M3c IR + dependency-graph sidecar + tests.md for the node, and stage the
    host-rendered runner the way `run_phase` does before any generate substep runs."""
    ir_dir = repo / "workspace" / "ir" / _SAFE / ir_id
    ir_dir.mkdir(parents=True, exist_ok=True)
    ir = _node_ir(state_vars)
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
    (spec_dir / "controlled_spec.md").write_text(
        "## 5 Algorithm\nhydrostatic reconstruction: h_star = max(0, eta - z_b)\n",
        encoding="utf-8")
    refs = wc.NodeRefs(node_key=_NODE, spec_path=_SPEC_PATH, ir_id=ir_id,
                       pipeline_id="sw_20260715_001", source_id=source_id)
    if stage_runner:
        src_dir = repo / refs.source_dir() / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / f"{_SPEC_ID}_runner.f90").write_text(_runner_text(), encoding="utf-8")
    return refs


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
        backend="claude", env={})


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

    def test_m3c_name_match_is_case_sensitive_like_the_filesystem(self) -> None:
        # `logical_path` becomes a FILENAME, and Generate.static opens `<spec_id>_checks.f90`
        # verbatim on a case-sensitive filesystem. Casefolding accepted `Shallow_Water2d_Checks.f90`
        # — which lints and compiles fine (Fortran resolves `use` by module name, never by
        # filename) — and then Generate.static rejected it on the name: accepted here, rejected
        # there, i.e. a phase reopen, which is what this layer exists to prevent.
        c, refs = self._c_refs()
        for role, mixed in (("checks", "Shallow_Water2d_Checks.f90"),
                            ("model", "Shallow_Water2d_Model.f90")):
            bad = _valid_bundle()
            entry = next(f for f in bad["files"] if f["role"] == role)
            was = entry["logical_path"]
            entry["logical_path"] = mixed
            for e in bad["entrypoints"]:
                if e["defined_in"] == was:
                    e["defined_in"] = mixed
            self.assertEqual(cb.validate_bundle(bad), [], "the shape must be schema-legal")
            cat, _ = c._pure_bundle_violations(refs, bad)
            self.assertEqual(cat, "bundle_assembly_collision", role)

    def test_m3c_module_name_match_stays_case_insensitive(self) -> None:
        # The mirror image: a Fortran identifier IS case-insensitive, so a module declared
        # `Shallow_Water2D_Checks` resolves for the runner's `use` and must be accepted.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["modules"] = [f"{_SPEC_ID}_CHECKS"]
        self.assertIsNone(cb.m3c_literal_name_violation(ok, _SPEC_ID))

    def test_m3c_name_violation(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][0]["logical_path"] = "wrong_model.f90"
        bad["entrypoints"][0]["defined_in"] = "wrong_model.f90"
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_assembly_collision")

    def test_accepted_bundle_also_passes_the_real_generate_static_gate(self) -> None:
        # Codex P1, the regression that matters: this layer accepting a bundle that
        # `Generate.static` then rejects reopens the phase — the failure it exists to prevent.
        # Drive the REAL gate, not a restatement of it: the ABI is fixed at ten for every node,
        # and requiring only the subset THIS node's runner imports (6 of 10 here) was accepted
        # here and rejected there.
        import tempfile as _tf
        c, refs = self._c_refs()
        bundle = _valid_bundle()
        self.assertIsNone(c._pure_bundle_violations(refs, bundle))
        checks = next(f for f in bundle["files"] if f["role"] == "checks")
        with _tf.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / checks["logical_path"]).write_text(checks["content"], encoding="utf-8")
            execution = SimpleNamespace(node_key=_NODE)
            v: list[str] = []
            vps._validate_checks_source_files(execution, src, [], v)
            self.assertEqual([s for s in v if "publish the fixed ABI" in s], [], v)

    def test_runner_imported_subset_is_not_the_required_set(self) -> None:
        # Pins the direction of the Codex P1 fix: the runner here imports 6 of the 10, and a
        # bundle publishing only those 6 must be REJECTED (Generate.static wants all ten).
        c, refs = self._c_refs()
        from tools.runner_renderer import CHECKS_PUBLIC_NAMES
        imported = cb_runner_imports(_runner_text(), _SPEC_ID)
        self.assertTrue(set(imported) < set(CHECKS_PUBLIC_NAMES), imported)
        bad = _valid_bundle()
        syms = list(imported)
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n"
            + "".join(f"  public :: {s}\n" for s in syms)
            + "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine {s}\n" for s in syms)
            + "end module\n")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        for absent in set(CHECKS_PUBLIC_NAMES) - set(imported):
            self.assertIn(absent, findings)

    def test_checks_abi_missing_symbol(self) -> None:
        # Z2 defect D, shape 1 (sw2d src_003): the checks module omits names the runner imports
        # -> Generate.syntax "Symbol 'x' not found in module". Now a bounded in-loop repair.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(omit="checks_compute")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("checks_compute", findings)

    def test_checks_abi_quoted_type_function_form_rejected(self) -> None:
        # Codex review: a legal type-spec can hold a quote in its parens —
        # `character(kind=kind('a')) function metric_compute()`. Excluding quotes from the
        # proc-header prefix made that header unmatchable, so the function was published-but-not-
        # defined and the gate ACCEPTED it; the runner then `call`s it and Generate.syntax fails —
        # the phase reopen this gate exists to prevent. Matching a string-masked line catches it.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        syms = _checks_symbols()
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n"
            + "".join(f"  public :: {s}\n" for s in syms) + "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine {s}\n"
                      for s in syms if s != "metric_compute")
            + "  character(kind=kind('a')) function metric_compute()\n"
            "    metric_compute = 'x'\n  end function metric_compute\n"
            "end module\n")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("metric_compute", findings)
        self.assertIn("FUNCTION", findings)

    def test_checks_abi_function_form_rejected(self) -> None:
        # Z2 defect D, shape 2 (sw2d src_004): the name EXISTS but is authored as a FUNCTION,
        # while the runner `call`s it -> "has a type, which is not consistent with the CALL".
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(as_function="metric_compute")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("metric_compute", findings)
        self.assertIn("FUNCTION", findings)

    def test_checks_abi_accepts_names_not_defined_locally(self) -> None:
        # Review finding: a published ABI name need not be defined in this module's text to be
        # callable — `use`-association, a generic `interface`, and a submodule all compile, LINK
        # and `call` fine (verified with gfortran). Demanding a local `subroutine` header rejected
        # those, with findings telling the producer to write what it had already written: a repair
        # loop with no exit, i.e. this defect's own failure mode on a LEGAL bundle. Only positive
        # evidence of the wrong kind (a local FUNCTION) may reject.
        c, refs = self._c_refs()
        syms = _checks_symbols()
        pubs = "".join(f"  public :: {s}\n" for s in syms)
        defs = "".join(f"  subroutine {s}()\n  end subroutine {s}\n"
                       for s in syms if s != "case_setup")
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks_impl\n  public\ncontains\n"
            "  subroutine case_setup()\n  end subroutine case_setup\n"
            f"end module {_SPEC_ID}_checks_impl\n"
            f"module {_SPEC_ID}_checks\n"
            f"  use {_SPEC_ID}_checks_impl, only: case_setup\n  private\n{pubs}contains\n"
            + defs
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_defined_but_unexported_rejected(self) -> None:
        # Review finding: the certified idiom is a bare `private` + explicit `public ::` list, so
        # a name defined but left off that list is INVISIBLE to the runner and fails
        # Generate.syntax with the same "Symbol not found in module" as an omitted one. Checking
        # definition alone would fail open on half of the shape this layer exists to catch.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(unexported="checks_compute")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("checks_compute", findings)
        self.assertIn("not published", findings)

    def test_checks_abi_explicit_private_name_rejected(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content().replace(
            "  private\n", "  private :: get_time\n", 1)
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("get_time", findings)

    def test_checks_abi_accepts_default_public_module(self) -> None:
        # No bare `private` => Fortran's own default is public => nothing to export explicitly.
        # The export check must not invent a requirement the language does not impose.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_accepts_leading_ampersand_continuation(self) -> None:
        # Free-form Fortran allows an OPTIONAL leading `&` on a continuation line (confirmed
        # legal with `gfortran -fsyntax-only -std=f2008`). Fused onto the next name it would be
        # dropped by the identifier filter and reject a module that DID export it — and the
        # findings text would tell the leaf to do what it had already done, so the repair loop
        # could only thrash. Latent today (every certified module wraps with a trailing `&`
        # only), but the checks module is leaf-authored and continuation style varies.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        syms = _checks_symbols()
        wrapped = "  public :: " + ", &\n       &  ".join(syms) + "\n"
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n" + wrapped
            + "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in syms)
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_derived_type_private_is_not_the_module_default(self) -> None:
        # False-positive guard on a fail-closed gate: a bare `private` inside a derived type sets
        # that TYPE's component accessibility. Reading it as the module default would demand a
        # `public ::` list from a legal default-public module and reject it.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            "  type :: bucket\n    private\n    integer :: n\n  end type bucket\n"
            "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_declaration_attribute_is_not_an_accessibility_statement(self) -> None:
        # `integer, private :: n` is an attribute on a declaration, not a module default, and
        # `private :: n` on an unrelated helper must not touch the ABI names.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            "  integer, private :: n\n  private :: helper\n"
            "contains\n"
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_accepts_wrapped_export_list(self) -> None:
        # The certified idiom wraps its `public ::` list with `&` continuations.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        syms = _checks_symbols()
        wrapped = "  public :: " + ", &\n    ".join(syms) + "\n"
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n  private\n" + wrapped
            + "".join(f"  subroutine {s}()\n  end subroutine\n" for s in syms)
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_end_subroutine_and_comments_do_not_satisfy(self) -> None:
        # Fail-open guard: every real module carries `end subroutine <name>` for each subroutine,
        # so an unanchored `subroutine <name>` search would accept the FUNCTION form this layer
        # exists to reject. A mention in a comment must not satisfy it either.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            + "".join(f"subroutine {s}()\nend subroutine {s}\n"
                      for s in _checks_symbols() if s != "metric_compute")
            + "! the runner also calls subroutine metric_compute\n"
            + "function metric_compute() result(r)\nend subroutine metric_compute\n"
            + "end module\n")
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("metric_compute", findings)

    def test_checks_abi_accepts_prefixed_subroutine_forms(self) -> None:
        # ...while the anchoring must not reject a legal `pure subroutine foo()` definition.
        c, refs = self._c_refs()
        ok = _valid_bundle()
        ok["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\n"
            + "".join(f"  pure subroutine {s}()\n  end subroutine {s}\n"
                      for s in _checks_symbols())
            + "end module\n")
        self.assertIsNone(c._pure_bundle_violations(refs, ok))

    def test_checks_abi_ignores_a_decoy_sibling_checks_module(self) -> None:
        # Codex review: a bundle may legally carry more than one checks-role file. Searching them
        # all lets a sibling module's procedures vouch for names the runner — which imports
        # <spec_id>_checks alone — can never see: accepted here, then dead at Generate.syntax.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = f"module {_SPEC_ID}_checks\nend module\n"
        bad["files"].append({
            "logical_path": "decoy_checks.f90", "role": "checks", "language": "fortran",
            "member_node_key": _NODE,
            "content": _checks_content().replace(f"{_SPEC_ID}_checks", "decoy_checks"),
            "modules": ["decoy_checks"]})
        self.assertEqual(cb.validate_bundle(bad), [])  # the shape really is schema-legal
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")

    def test_checks_abi_ignores_a_decoy_second_module_in_the_same_file(self) -> None:
        # Same class, one level down: file scoping alone would still be fooled, so the check is
        # scoped to the MODULE the runner imports.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = (
            f"module {_SPEC_ID}_checks\nend module\n"
            + _checks_content().replace(f"{_SPEC_ID}_checks", "decoy_checks"))
        bad["files"][1]["modules"] = [f"{_SPEC_ID}_checks", "decoy_checks"]
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")

    def test_checks_abi_extra_checks_file_does_not_mask_a_real_violation(self) -> None:
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(omit="checks_compute")
        bad["files"].append({
            "logical_path": "helper_checks.f90", "role": "checks", "language": "fortran",
            "member_node_key": _NODE,
            "content": _checks_content().replace(f"{_SPEC_ID}_checks", "helper_checks"),
            "modules": ["helper_checks"]})
        cat, findings = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")
        self.assertIn("checks_compute", findings)

    def test_checks_abi_fail_closed_when_declared_module_is_not_defined(self) -> None:
        # `modules[]` is leaf-declared; if the content defines no such module the runner's import
        # cannot resolve, so an unverifiable ABI must not be an accepted one.
        c, refs = self._c_refs()
        bad = _valid_bundle()
        bad["files"][1]["content"] = "! no module here\n"
        cat, _ = c._pure_bundle_violations(refs, bad)
        self.assertEqual(cat, "bundle_checks_abi_violation")

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


class PureContextRunnerInjectionTests(unittest.TestCase):
    """Z2 defect D: the tool-less leaf can reach the checks ABI ONLY through the injected
    runner, so the injection itself is the fix and is pinned here."""

    def test_runner_document_is_the_staged_file_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            ctx = _conductor(repo)._build_pure_context(refs)
            staged = (repo / refs.source_dir() / "src"
                      / f"{_SPEC_ID}_runner.f90").read_text(encoding="utf-8")
            self.assertEqual(ctx["runner_document"], staged)
            # The ABI the leaf must author against is actually reachable in what it is shown.
            self.assertIn(f"use {_SPEC_ID}_checks, only:", ctx["runner_document"])

    def test_controlled_spec_is_not_in_producer_context(self) -> None:
        # pure-10: the pure-5 interim carve-out was removed — the producer is spec-blind again
        # (phase_02 §2-1), so its context carries NO controlled_spec_document. The verify reviewer
        # still reads controlled_spec.md by design (test_pure_leaf_verify.py).
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            ctx = _conductor(repo)._build_pure_context(refs)
            self.assertNotIn("controlled_spec_document", ctx)

    def test_non_utf8_runner_raises_the_named_contract_not_a_bare_decode_error(self) -> None:
        # UnicodeDecodeError is a ValueError, not an OSError: catching OSError alone let it escape
        # unnamed. The caller recovers any exception, so this is about the diagnosis an operator
        # reads, not about containment.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            (repo / refs.source_dir() / "src" / f"{_SPEC_ID}_runner.f90").write_bytes(
                b"program p\n  use x_checks, only: \xff\xfe bar\nend program\n")
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_context(refs)
            self.assertIn("pure_runner_document_missing", str(cm.exception))

    def test_missing_runner_raises_rather_than_shipping_a_blank_abi(self) -> None:
        # NOT the swallow-to-"" idiom ir/tests use: an empty string satisfies the renderer's
        # presence check and would ship a prompt whose ABI section is blank — the very defect.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo, stage_runner=False)
            with self.assertRaises(RuntimeError) as cm:
                _conductor(repo)._build_pure_context(refs)
            self.assertIn("pure_runner_document_missing", str(cm.exception))


class PureHarnessManifestNarrowingTests(unittest.TestCase):
    """The manifest a pure leaf is SHOWN must be the one it is JUDGED against (Codex review).

    `_build_pure_context` used to inline the FULL `HARNESS_CAPABILITY_MANIFESTS` table while
    `_pure_bundle_violations` negotiated only against the node's own infra dependency. With one
    registered harness the two coincide, so the defect is latent; these tests register a SECOND
    harness to make it live, which is the only way to reach the branch.
    """

    def setUp(self) -> None:
        self._saved = dict(cb.HARNESS_CAPABILITY_MANIFESTS)
        # A second harness providing what the node's own harness does not.
        cb.HARNESS_CAPABILITY_MANIFESTS["infrastructure/harness_gpu_next@0.1.0"] = frozenset(
            {"async_device_resident@1", "state_registration@1"})
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.refs = _write_node(self.repo, state_vars=("h",))
        self.c = _conductor(self.repo)

    def tearDown(self) -> None:
        cb.HARNESS_CAPABILITY_MANIFESTS.clear()
        cb.HARNESS_CAPABILITY_MANIFESTS.update(self._saved)
        self._tmp.cleanup()

    def test_context_shows_only_the_nodes_own_harness(self) -> None:
        shown = json.loads(self.c._build_pure_context(self.refs)["harness_capabilities"])
        self.assertEqual([m["node_key"] for m in shown["manifests"]], [_HARNESS])

    def test_context_hides_another_harnesss_capabilities(self) -> None:
        # The live consequence: the prompt licenses `harness_registration` when the manifest it is
        # shown lists `state_registration@N`. Leaking another harness's token would license a
        # requirement `_pure_bundle_violations` then rejects as `bundle_capability_unsatisfied` —
        # a repair burn on a bundle the leaf could not know was unsatisfiable.
        shown = json.loads(self.c._build_pure_context(self.refs)["harness_capabilities"])
        provided = {t for m in shown["manifests"] for t in m["provides"]}
        self.assertNotIn("state_registration@1", provided)
        self.assertNotIn("async_device_resident@1", provided)
        self.assertEqual(provided, {"sync_single_case@1"})

    def test_context_and_gate_resolve_the_same_harness(self) -> None:
        # The invariant the fix rests on: one resolution, so the two cannot drift.
        ir = {"dependency": {"direct_deps": [{"node_key": _HARNESS}]}}
        self.assertEqual(self.c._pure_harness_node_key(ir), _HARNESS)
        shown = json.loads(self.c._build_pure_context(self.refs)["harness_capabilities"])
        from_ir = self.c._pure_harness_node_key(
            wc._read_yaml(self.repo / self.refs.ir_ref / "spec.ir.yaml") or {})
        self.assertEqual([m["node_key"] for m in shown["manifests"]], [from_ir])

    def test_narrowing_is_fail_closed_for_an_unresolvable_harness(self) -> None:
        # None / unregistered => EMPTY manifests, mirroring `harness_provided_capabilities`'s
        # fail-closed None. Never "show the whole table because we could not pick one".
        for key in (None, "infrastructure/not_registered@9.9.9"):
            self.assertEqual(
                cb.harness_capability_manifest_document_for(key)["manifests"], [],
                f"narrowing must be empty for {key!r}")

    def test_zero_or_multiple_infra_deps_resolve_to_none(self) -> None:
        self.assertIsNone(self.c._pure_harness_node_key({"dependency": {"direct_deps": []}}))
        self.assertIsNone(self.c._pure_harness_node_key({"dependency": {"direct_deps": [
            {"node_key": _HARNESS}, {"node_key": "infrastructure/harness_gpu_next@0.1.0"}]}}))

    def test_full_document_still_carries_every_manifest(self) -> None:
        # The unnarrowed document is the canonical Z6 shape; narrowing is the leaf's projection
        # of it, not a replacement.
        full = [m["node_key"] for m in cb.harness_capability_manifest_document()["manifests"]]
        self.assertIn(_HARNESS, full)
        self.assertIn("infrastructure/harness_gpu_next@0.1.0", full)


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

    def test_pure_pass_finalize_payload_satisfies_the_real_summary_validator(self) -> None:
        """The finalize payload a passing pure leaf produces must survive the REAL
        runtime validators.

        REGRESSION (billed E2E, 2026-07-16): every passing pure leaf was rejected by
        `finalize-child` with "agent.summary.txt must include summary or failure
        reason" — the pure executor could never complete a real run. A pure row carries
        an EMPTY `output_refs` by contract, so `_validate_agent_summary_text` falls to
        its "a terminal row with no output_refs must explain itself" branch; the
        conductor passed `result_summary=None` on pass, leaving nothing to satisfy it.

        The whole suite missed it because these tests stub `runtime()`. So drive the
        conductor's ACTUAL captured payload through the REAL validators rather than
        re-asserting the stub. Same anti-mock-green shape as the meta writer↔reader
        contract test.
        """
        from tools.orchestration_runtime import (
            _extract_agent_summary_text, _validate_agent_summary_text,
        )

        c, refs, oc = self._run([_envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        payloads = [cap["--agent-run-json"] for sub, cap in c.calls
                    if sub == "finalize-child" and "--agent-run-json" in cap]
        self.assertTrue(payloads, "finalize-child must have been called with a payload")
        for payload in payloads:
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["output_refs"], [])  # the pure contract
            # The real generator + the real validator — no stub in this path.
            _validate_agent_summary_text(payload, _extract_agent_summary_text(payload))

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
               backend="claude", env={})
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

    def test_per_attempt_records_failure_of_a_superseded_attempt_only(self) -> None:
        # Item 6 observability: the failed (superseded) attempt carries its failure_category and a
        # bounded failure_excerpt; the passing attempt that supersedes it does not.
        bad = _valid_bundle()
        del bad["capability_requirements"]  # schema violation -> repair
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        pa = meta["per_attempt"]
        self.assertEqual(len(pa), 2)
        self.assertEqual(pa[0]["failure_category"], "bundle_schema_violation")
        self.assertTrue(pa[0]["failure_excerpt"])
        self.assertLessEqual(len(pa[0]["failure_excerpt"]), 400)
        self.assertNotIn("failure_category", pa[1])
        self.assertNotIn("failure_excerpt", pa[1])

    def test_checks_abi_violation_is_repaired_in_loop(self) -> None:
        # The whole point of the layer: what cost the sw2d P-arm its retry budget (a phase reopen
        # per guess) is now ONE bounded in-conversation repair.
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(as_function="metric_compute")
        c, refs, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)

    def test_exhausted_checks_abi_repair_routes_to_generate_reuse(self) -> None:
        bad = _valid_bundle()
        bad["files"][1]["content"] = _checks_content(omit="case_run")
        c, refs, oc = self._run([_envelope(bad)])
        self.assertEqual(oc.status, "fail")
        meta = json.loads((c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
        self.assertEqual(meta["failure_category"], "bundle_checks_abi_violation")
        # The terminal category must carry an outer route, or the phase fails closed instead of
        # reopening its own producer.
        self.assertEqual(
            wc.GENERATE_BUNDLE_FAILURE_ROUTING["bundle_checks_abi_violation"],
            ("generate", "reuse"))

    def test_missing_runner_fails_closed_before_any_leaf_spawn(self) -> None:
        # A host artifact the conductor itself renders is not something a generate retry can fix:
        # fail_closed (operator --resume), and no leaf is spawned to burn tokens on it.
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo, stage_runner=False)
        c = _conductor(repo)
        c.envelopes = [_envelope(_valid_bundle())]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "fail")
        self.assertEqual(oc.leaf_returncode, 1)
        self.assertEqual(oc.infra_error[0], "pure_context_assembly_failed")
        self.assertEqual(getattr(c, "_spawn", 0), 0)

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
               backend="claude", env={})
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
               backend="claude", env={})
        bad = _valid_bundle()
        del bad["capability_requirements"]  # persistently schema-invalid -> exhaustion
        c.envelopes = [_envelope(bad)]
        oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
        self.assertEqual(oc.status, "fail")
        self.assertNotEqual(oc.leaf_returncode, 0)
        self.assertEqual(oc.infra_error[0], "pure_host_write_failed")


# ======================================================================================
# --wait-usage-reset in the pure producer loop
# ======================================================================================
class PureUsageLimitWaitTest(unittest.TestCase):
    """--wait-usage-reset (opt-in) in the pure producer: a transport death (rc!=0) that carries a
    machine-form usage-limit reset epoch is waited out IN PLACE and the SAME turn re-launched,
    instead of falling to the terminal fail branch for a next-day --resume. The wait is NOT a repair
    turn — it must not consume the bundle-repair budget and must not pollute the repair carriers
    (last_excerpt / resume_session_id). Default OFF preserves the current terminal behavior."""

    class _C(_PureFakeConductor):
        def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
            self._spawn = getattr(self, "_spawn", 0)
            proc = self.procs[min(self._spawn, len(self.procs) - 1)]
            self._spawn += 1
            return proc

        def _sleep_backoff(self, seconds):  # type: ignore[override]
            self.slept.append(seconds)

    def _conductor(self, repo: Path, procs: list, **kw) -> "_C":
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = self._C(repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
                    backend="claude", env={}, **kw)
        c.procs = procs
        c.slept = []
        return c

    def test_transport_usage_limit_waits_then_passes(self) -> None:
        now = 1_752_200_000.0
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(
                repo,
                [wc.ProcResult(1, "", f"Claude AI usage limit reached|{int(now) + 300}"),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                wait_usage_reset=True)
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "pass")
            self.assertEqual(c._spawn, 2)          # dead attempt + the recovered launch
            # `attempts` is the LAUNCH count (== len(per_attempt)); the repair BUDGET is untouched
            # (a wait is not a repair turn), but the wait's launch is still counted honestly.
            self.assertEqual(oc.attempts, 2)
            self.assertEqual(c.slept, [420.0])     # 300s to the reset + 120s margin
            base = c.repo_root / refs.source_dir()
            self.assertTrue((base / "codegen_bundle.json").exists())
            meta = json.loads((base / "bundle_meta.json").read_text())
            self.assertEqual(meta["result"], "pass")
            self.assertEqual(meta["attempts"], 2)          # launch count == len(per_attempt)
            # both launches are visible as per_attempt rows; the dead one is labeled pure_transport
            self.assertEqual(len(meta["per_attempt"]), 2)
            self.assertEqual(meta["per_attempt"][0]["failure_category"], "pure_transport")
            # the dead usage attempt is tombstoned under the wait's own prefix
            reasons = [cap["--reason"] for s, cap in c.calls if s == "add-superseded-runs"]
            self.assertTrue(any("leaf_usage_limit_wait_orphan" in r for r in reasons))

    def test_transport_wait_does_not_pollute_the_repair_turn(self) -> None:
        """A transport wait must leave the repair carriers clean: the launch AFTER the wait is a
        fresh COLD attempt (no prior_document, no repair_findings carrying the transport summary),
        and the following content violation repairs normally. Sequence: transport(usage) -> wait ->
        cold launch that content-fails -> warm repair -> pass."""
        now = 1_752_200_000.0
        bad = _valid_bundle()
        del bad["capability_requirements"]     # a content (schema) violation
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(
                repo,
                [wc.ProcResult(1, "", f"usage limit reached|{int(now) + 100}"),
                 wc.ProcResult(0, _envelope(bad), ""),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")],
                wait_usage_reset=True)
            captured: list[dict] = []
            orig = c.record_launch

            def _rec(child_arid, request):  # capture the per-launch request shape
                captured.append(request)
                return orig(child_arid, request)

            c.record_launch = _rec  # type: ignore[assignment]
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "pass")
            self.assertEqual(c._spawn, 3)
            # 3 launches: transport(waited) + cold content-fail + warm repair pass. `attempts` is the
            # launch count; the repair budget saw only 1 turn (the wait did not consume it).
            self.assertEqual(oc.attempts, 3)
            self.assertEqual(c.slept, [220.0])     # 100s + 120s margin
            # the post-wait launch (index 1) is a COLD retry: no prior_document carried from the
            # transport death.
            self.assertNotIn("prior_document", captured[1])
            # the repair turn (index 2) carries the CONTENT failure's findings, never the transport
            # summary ("Connection closed"/"usage limit").
            repair_req = captured[2]
            findings = str(repair_req.get("repair", {}).get("repair_findings", ""))
            self.assertNotIn("usage limit", findings.lower())

    def test_transport_usage_limit_is_terminal_when_flag_off(self) -> None:
        now = 1_752_200_000.0
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(   # wait_usage_reset defaults OFF
                repo,
                [wc.ProcResult(1, "", f"usage limit reached|{int(now) + 300}"),
                 wc.ProcResult(0, _envelope(_valid_bundle()), "")])
            with mock.patch.object(wc.time, "time", return_value=now):
                oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.leaf_returncode, 1)   # run_phase's transport fail_closed branch
            self.assertEqual(c._spawn, 1)             # no second launch
            self.assertEqual(c.slept, [])
            # Regression pin (byte-identity): the terminal bundle_meta must describe the transport
            # DEATH that terminated the substep — the bookkeeping guard that protects the repair
            # carriers must NOT leak an empty/stale failure_excerpt into the meta.
            meta = json.loads(
                (c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
            self.assertEqual(meta["result"], "fail")
            self.assertEqual(meta["failure_category"], "pure_transport")
            self.assertIn("usage limit", (meta.get("failure_excerpt") or "").lower())
            self.assertEqual(meta["attempts"], 1)     # one launch, one per_attempt row

    def test_transport_terminal_after_a_content_repair_records_the_transport_excerpt(self) -> None:
        # Finding-1 twin: attempt0 content-fails (repair carrier = schema text), attempt1 dies of a
        # transport usage limit with the flag OFF -> terminal. The meta must pair
        # failure_category=pure_transport with the TRANSPORT excerpt, never the stale schema carrier.
        bad = _valid_bundle()
        del bad["capability_requirements"]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            refs = _write_node(repo)
            c = self._conductor(   # flag OFF
                repo,
                [wc.ProcResult(0, _envelope(bad), ""),
                 wc.ProcResult(1, "", "Claude AI usage limit reached")])
            oc = c._run_pure_generate_substep(refs, "generate", "generate", None, ())
            self.assertEqual(oc.status, "fail")
            self.assertEqual(oc.leaf_returncode, 1)
            meta = json.loads(
                (c.repo_root / refs.source_dir() / "bundle_meta.json").read_text())
            self.assertEqual(meta["failure_category"], "pure_transport")
            excerpt = (meta.get("failure_excerpt") or "").lower()
            self.assertIn("usage limit", excerpt)
            self.assertNotIn("capability_requirements", excerpt)  # not the stale schema carrier
            self.assertEqual(meta["attempts"], 2)     # two launches


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
                   backend="claude", env={})
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
                   backend="claude", env={})
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
                                              "ir_document": "z", "tests_document": "t",
                                              "runner_document": "program r\nend program\n"})
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
# Exemplar attachment on the pure launch (defect B)
# ======================================================================================
class _RenderingFakeConductor(_PureFakeConductor):
    """`_PureFakeConductor` whose record-launch runs the REAL prompt pipeline.

    The base stub returns a literal "PROMPT", so every prompt-content regression is invisible to
    it — exactly how defect B (`exemplar=` never passed to `build_launch_request`) survived a
    green suite. Here record-launch drives the real `prepare_launch_request_payload` +
    `_validate_launch_prompt_text` over the conductor's ACTUAL request, so a request the
    validators would reject fails the test too. Same anti-mock-green shape as the
    finalize-payload test above.
    """

    exemplar_value: dict | None = None

    def _resolve_exemplar(self, refs):  # type: ignore[override]
        # Canned: the real selector needs a certified sibling on disk, which this fixture has no
        # reason to build — the wiring under test is whether the resolved value reaches the render.
        return self.exemplar_value

    def runtime(self, args):  # type: ignore[override]
        out = super().runtime(args)
        if args[0] != "record-launch":
            return out
        request = json.loads(args[args.index("--request-json") + 1])
        self.requests = getattr(self, "requests", [])
        self.requests.append(request)
        prepared = ort.prepare_launch_request_payload(dict(request))
        prompt = prepared["launch_prompt_full"]
        ort._validate_launch_prompt_text(prepared, prompt)
        return {"launch_prompt_text": prompt}

    def spawn_leaf(self, prompt_text, child_env, **kwargs):  # type: ignore[override]
        self.prompts = getattr(self, "prompts", [])
        self.prompts.append(prompt_text)
        return super().spawn_leaf(prompt_text, child_env, **kwargs)


class PureProducerExemplarTests(unittest.TestCase):
    """R5 exemplar injection on the pure `generate.generate` launch.

    REGRESSION (billed E2E, 2026-07-16 — defect B): every other strand of the pure exemplar
    wiring was in place (the template's `<exemplar>` slot, `_build_exemplar`, the
    `build_launch_request` attach condition, the scan carve-out) but `_run_pure_generate_substep`
    never passed `exemplar=`, so the pure producer authored with strictly less information than
    the legacy leaf it was being A/B'd against.
    """

    _EXEMPLAR = {
        "node_key": "component/sibling@1.0.0",
        "sources": [{"filename": "sibling_model.f90",
                     "text": "module sibling_model\n  ! prior art body\nend module sibling_model\n"}],
    }

    def _run(self, envelopes, repair=None):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name)
        refs = _write_node(repo)
        (repo / "workspace" / "orchestrations" / "o").mkdir(parents=True, exist_ok=True)
        c = _RenderingFakeConductor(
            repo_root=repo, orchestration_id="o", orchestration_agent_run_id="orch",
            backend="claude", env={})
        c.exemplar_value = self._EXEMPLAR
        c.envelopes = envelopes
        oc = c._run_pure_generate_substep(refs, "generate", "generate", repair, ())
        return c, oc

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_cold_pure_launch_renders_exemplar_in_prompt(self) -> None:
        c, oc = self._run([_envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(c.requests[0]["exemplar"], self._EXEMPLAR)
        # ...and it survives the real render + the real launch validator (no fail-close).
        self.assertIn("Certified exemplar (conductor-injected PRIOR ART", c.prompts[0])
        self.assertIn("! prior art body", c.prompts[0])

    def test_warm_repair_attempt_does_not_attach_exemplar(self) -> None:
        # The repair template has no `<exemplar>` slot, so attaching it to a repair turn would
        # ship payload bytes nothing renders.
        bad = _valid_bundle()
        del bad["capability_requirements"]
        c, oc = self._run([_envelope(bad), _envelope(_valid_bundle())])
        self.assertEqual(oc.status, "pass")
        self.assertEqual(oc.attempts, 2)
        self.assertNotIn("exemplar", c.requests[1])
        self.assertNotIn("Certified exemplar", c.prompts[1])

    def test_outer_reopen_without_findings_renders_launch_prompt_with_exemplar(self) -> None:
        """The ONE case where the attach predicate differs from the legacy `not warm_resume`.

        An outer reopen seeded with NO findings excerpt is warm (a session to resume) yet still
        renders the full LAUNCH template — so it wants the exemplar, and the legacy condition
        would have wrongly withheld it. This drives the conductor's predicate and the renderer's
        dispatch against each other through the real renderer on the exact case where they could
        disagree; every other test covers a case where the two happen to agree.
        """
        c, oc = self._run(
            [_envelope(_valid_bundle())],
            repair={"repair_strategy": "reuse", "repair_target_agent_run_id": "prior-arid"})
        self.assertEqual(oc.status, "pass")
        # The launch template rendered (not the repair template) ...
        self.assertIn("Authoring rules", c.prompts[0])
        # ... and the exemplar rode along with it.
        self.assertEqual(c.requests[0]["exemplar"], self._EXEMPLAR)
        self.assertIn("Certified exemplar (conductor-injected PRIOR ART", c.prompts[0])


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
                             "ir_document": "ir", "tests_document": "tt",
                             "runner_document": "program r\nend program\n"},
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

    def test_cold_repair_includes_authoring_rules_and_output_contract(self) -> None:
        # A cold fallback re-authors the whole bundle with no prior turn, so it needs the
        # deterministic-gate rules for the same reason it needs the schema (M-C cold-repair
        # contract, extended for defect C).
        text = ort._render_pure_repair_prompt(self._req())
        self.assertIn("Output contract", text)
        self.assertIn("Authoring rules", text)
        self.assertIn("! allow(C003)", text)

    def test_cold_repair_lifts_every_static_rule_paragraph(self) -> None:
        # The ABI paragraph was added to the launch template and silently not lifted, so a cold
        # repair handed the producer the runner source with no statement that it must publish all
        # ten as subroutines — Z2 defect D reproduced in the RECOVERY path, which is reached
        # exactly when recovery is happening. Assert against the template's own paragraphs, not a
        # literal list, so a third static paragraph cannot be silently omitted the same way.
        text = ort._render_pure_repair_prompt(self._req())
        template = ort._load_launch_prompt_templates()["pure generate.generate"]
        for prefix in ort.PURE_REPAIR_STATIC_PARAGRAPH_PREFIXES:
            start = template.index(prefix)
            end = template.index("\n\n", start)
            for line in (ln.strip() for ln in template[start:end].splitlines()):
                if line and not line.startswith("<"):  # the doc slot itself is filled elsewhere
                    self.assertIn(line, text, f"cold repair dropped: {line[:60]}")

    def test_placeholder_drop_keeps_rule_text_that_mentions_placeholders(self) -> None:
        # The drop is `fullmatch` on the STRIPPED line, so only a line that is nothing but a
        # document slot goes. Rule text quoting a `<...>` metavariable — `use <module>, only:
        # <names>`, the `associate (unused_<name> => <name>)` binding — must survive; dropping it
        # would silently delete a rule from every cold repair.
        text = ort._render_pure_repair_prompt(self._req())
        self.assertIn("use <module>, only: <names>", text)
        self.assertIn("associate (unused_<name> => <name>); end associate", text)

    def test_cold_repair_leaks_no_document_placeholder(self) -> None:
        # A lifted paragraph ends with the `<doc>` slot its launch template fills; nothing
        # substitutes it here, so it would ship as a literal token.
        text = ort._render_pure_repair_prompt(self._req())
        for slot in ("<runner_document>", "<ir_document>", "<tests_document>", "<exemplar>"):
            self.assertNotIn(slot, text)

    def test_warm_repair_omits_authoring_rules(self) -> None:
        # The resumed session already holds the launch prompt's static prefix.
        text = ort._render_pure_repair_prompt(self._req(warm_resume=True))
        self.assertNotIn("Authoring rules", text)
        self.assertNotIn("! allow(C003)", text)

    def test_authoring_rules_lift_is_the_whole_paragraph(self) -> None:
        # An interior blank line introduced by a template edit would make `split("\n\n")` drop
        # everything after it, shipping a cold repair with only the first rule groups. Assert
        # STRUCTURALLY (not just heading + current closing clause): everything the template holds
        # between the heading and the next section must survive the lift. A literal-anchored pin
        # catches truncation of today's text but NOT an append — a rule (6) added after a blank
        # line would vanish from the cold repair with the test still green.
        text = ort._pure_authoring_rules_text(self._req())
        self.assertTrue(text.startswith("Authoring rules"))
        template = ort._load_launch_prompt_templates()["pure generate.generate"]
        start = template.index("Authoring rules")
        end = template.index("**Harness capabilities")  # the next section of the static prefix
        for line in (ln.strip() for ln in template[start:end].splitlines()):
            if line:
                self.assertIn(line, text)

    def test_authoring_rules_lift_empty_for_verify(self) -> None:
        # The verify template has no authoring-rules paragraph; the lift returns '' and the
        # renderer must leave no unfilled `<authoring_rules>` token behind.
        req = self._req(substep="verify")
        self.assertEqual(ort._pure_authoring_rules_text(req), "")
        self.assertNotIn("<authoring_rules>", ort._render_pure_repair_prompt(req))

    def test_style_lint_distills_c072_character_dummy_widths(self) -> None:
        # Fail #1 (orch_9a5fe93e): the pure leaf authored `character(len=*), intent(out)` and
        # burned a retry on fortitude C072. The doc-blind producer learns the rule only if the
        # launch template states it. Under the per-id ABI (pure-8) the sole `intent(out)` character
        # dummy is `status`, whose width MUST match the one authority the runner renders against
        # (runner_renderer.CHECK_STATUS_WIDTH), not a hand-copied number that could drift.
        from tools.runner_renderer import CHECK_STATUS_WIDTH
        template = ort._load_launch_prompt_templates()["pure generate.generate"]
        start = template.index("(1) Style lint")
        end = template.index("\n(2)", start)
        style = template[start:end]
        self.assertIn("C072", style)
        self.assertIn("checks_compute(case_id, check_id, status)", style)
        self.assertIn(f"character(len={CHECK_STATUS_WIDTH})", style)


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
        # The real rendered runner, not a `program p` stub: it is the checks-ABI layer's
        # authority, so a stub would make the gate unverifiable (fail-closed) here.
        (gen / "src" / f"{_SPEC_ID}_runner.f90").write_text(_runner_text(), encoding="utf-8")
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

    def test_checks_abi_tamper_survives_byte_compare_but_is_caught(self) -> None:
        # A CONSISTENT tamper (bundle content and the staged .f90 edited together) defeats the
        # byte-compare mirror, so only the re-run acceptance contract can catch it. Drops a
        # subroutine the runner calls.
        with tempfile.TemporaryDirectory() as tmp:
            repo, gen, ir_ref = self._gen_dir(tmp)
            bundle = json.loads((gen / "codegen_bundle.json").read_text())
            bundle["files"][1]["content"] = _checks_content(omit="checks_compute")
            (gen / "codegen_bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
            (gen / "src" / f"{_SPEC_ID}_checks.f90").write_text(
                bundle["files"][1]["content"], encoding="utf-8")
            v: list[str] = []
            vps._validate_post_generate_bundle(repo, gen, _NODE, ir_ref, v)
            self.assertTrue(any("bundle_checks_abi_violation" in s for s in v), v)

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
# run_workflow generate-executor surface (M-F: flag + env removed, pure is the only executor)
# ======================================================================================
class GenerateExecutorFlagTests(unittest.TestCase):
    def test_cold_run_has_no_executor_gate(self) -> None:
        # M-F: a cold run has NO executor gate — the executor is hardcoded pure. The run proceeds
        # straight to normal startup resolution (failing here on the bogus spec, not on any
        # executor block), and does NOT emit the retired generate_executor_invalid reason nor stamp
        # the removed METDSL_GENERATE_EXECUTOR env.
        import io
        from contextlib import redirect_stdout
        import tools.run_workflow as rw
        buf = io.StringIO()
        prev = os.environ.pop("METDSL_GENERATE_EXECUTOR", None)
        try:
            with redirect_stdout(buf):
                rc = rw.main(["spec/nonexistent_xyz", "generate"])
            self.assertEqual(rc, 2)
            self.assertIn("invalid_startup_input", buf.getvalue())
            self.assertNotIn("generate_executor_invalid", buf.getvalue())
            self.assertNotIn("METDSL_GENERATE_EXECUTOR", os.environ)
        finally:
            if prev is not None:
                os.environ["METDSL_GENERATE_EXECUTOR"] = prev

    def test_ambient_env_executor_ignored(self) -> None:
        # M-F: METDSL_GENERATE_EXECUTOR is fully inert. A stale ambient value (even an old typo)
        # is neither read nor validated — no generate_executor_invalid, and the run proceeds to
        # normal startup resolution.
        import io
        from contextlib import redirect_stdout
        import tools.run_workflow as rw
        buf = io.StringIO()
        prev = os.environ.get("METDSL_GENERATE_EXECUTOR")
        os.environ["METDSL_GENERATE_EXECUTOR"] = "pur"
        try:
            with redirect_stdout(buf):
                rc = rw.main(["spec/nonexistent_xyz", "generate"])
            self.assertEqual(rc, 2)
            self.assertIn("invalid_startup_input", buf.getvalue())
            self.assertNotIn("generate_executor_invalid", buf.getvalue())
        finally:
            if prev is not None:
                os.environ["METDSL_GENERATE_EXECUTOR"] = prev
            else:
                os.environ.pop("METDSL_GENERATE_EXECUTOR", None)



if __name__ == "__main__":
    unittest.main()
