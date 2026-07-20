#!/usr/bin/env python3
"""Unit tests for tools/lang_backend_fortran (Objective B language backend).

The correctness contract is a round-trip driven by the REAL published interfaces (not a synthetic
fixture — a hand-built struct could pass while the real §5.1 shape breaks; see the fixture-fiction
lesson): loading the real harness structured §5.1 block and rendering/reparsing it through Fortran
must preserve the struct and the exact NORMALIZED stanza lines the current gates compare. The same must hold for
`runner_renderer._HARNESS_V3_INTERFACE`, the third hardcoded copy of the harness signatures the
renderer pin uses. Drift tests confirm the structured form keeps the gate's discriminating power
(a changed intent / rank / type / name changes the normalized index).
"""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from tools.lang_backend_fortran import (
    SignatureParseError,
    load_structured_signatures,
    normalized_stanza_index,
    parse_signatures_from_fortran,
    render_signatures_to_fortran,
    render_symbol_to_fortran,
)
from tools.runner_renderer import _HARNESS_V3_INTERFACE, _HARNESS_V3_PARAMETERS
from tools.validate_pipeline_semantics import _FENCED_BLOCK_RE

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_SPEC = (
    REPO_ROOT
    / "spec/infrastructure/infra/harness/harness_fortran_cpu/controlled_spec.md"
)


def _real_section51_struct() -> dict:
    md = HARNESS_SPEC.read_text(encoding="utf-8")
    section = md.split("### 5.1", 1)[1]
    m = _FENCED_BLOCK_RE.search(section)
    assert m, "harness controlled_spec §5.1 fenced block not found"
    struct, err = load_structured_signatures(m.group(1))
    assert err is None, err
    return struct


def _real_section51_block() -> str:
    return render_signatures_to_fortran(_real_section51_struct())


class RoundTripRealArtifactsTest(unittest.TestCase):
    """parse -> render preserves every symbol's normalized stanza lines on REAL interfaces."""

    def _assert_round_trip(self, block: str) -> dict:
        struct = parse_signatures_from_fortran(block)
        rendered = render_signatures_to_fortran(struct)
        orig = normalized_stanza_index(block)
        rend = normalized_stanza_index(rendered)
        self.assertEqual(set(orig), set(rend), "symbol set changed under round-trip")
        for sym in orig:
            self.assertEqual(
                orig[sym], rend[sym], f"normalized stanza lines drifted for {sym}"
            )
        return struct

    def test_round_trip_harness_controlled_spec_section51(self) -> None:
        published = _real_section51_struct()
        struct = self._assert_round_trip(render_signatures_to_fortran(published))
        # sanity on the parsed shape (the real harness surface)
        self.assertEqual(len(struct["procedures"]), 13)
        self.assertEqual(len(struct["types"]), 5)
        self.assertEqual(
            {mp["name"] for mp in struct["module_parameters"]}, {"dp", "case_id_len"}
        )

    def test_round_trip_runner_renderer_hardcoded_copy(self) -> None:
        # The third copy of the signatures (runner_renderer._HARNESS_V3_INTERFACE) must lower and
        # round-trip identically, so B.3 can single-source the renderer pin through this backend.
        self._assert_round_trip(_HARNESS_V3_INTERFACE)

    def test_struct_is_stable_under_reparse(self) -> None:
        struct = parse_signatures_from_fortran(_real_section51_block())
        reparsed = parse_signatures_from_fortran(render_signatures_to_fortran(struct))
        self.assertEqual(struct, reparsed, "structured form not stable under render->parse")

    def test_language_neutral_vocabulary(self) -> None:
        # The abstract vocabulary must carry no Fortran spelling (`character`, `type(...)`).
        struct = parse_signatures_from_fortran(_real_section51_block())
        specs = [a["spec"] for p in struct["procedures"] for a in p["args"]]
        specs += [
            p["result"]["spec"] for p in struct["procedures"] if p["result"]
        ]
        specs += [c["spec"] for t in struct["types"] for c in t["components"]]
        for spec in specs:
            self.assertIn(spec["type"], {"real", "integer", "logical", "string", "derived"})


class DriftDiscriminationTest(unittest.TestCase):
    """A semantic drift in the struct changes the rendered normalized index (gate keeps its teeth)."""

    def setUp(self) -> None:
        self.struct = parse_signatures_from_fortran(_real_section51_block())
        self.base = normalized_stanza_index(render_signatures_to_fortran(self.struct))

    def _index_after(self, mutate) -> dict:
        s = copy.deepcopy(self.struct)
        mutate(s)
        return normalized_stanza_index(render_signatures_to_fortran(s))

    def test_intent_change_is_detected(self) -> None:
        def m(s):
            proc = next(p for p in s["procedures"] if p["name"].endswith("parse_cases"))
            proc["args"][0]["intent"] = "inout"  # was in
        self.assertNotEqual(self.base, self._index_after(m))

    def test_rank_change_is_detected(self) -> None:
        def m(s):
            proc = next(p for p in s["procedures"] if p["name"].endswith("emit_array_r1"))
            proc["args"][0]["rank"] = 2  # was 1
        self.assertNotEqual(self.base, self._index_after(m))

    def test_type_change_is_detected(self) -> None:
        def m(s):
            proc = next(p for p in s["procedures"] if p["name"].endswith("emit_int"))
            proc["args"][0]["spec"]["type"] = "real"  # was integer
        self.assertNotEqual(self.base, self._index_after(m))

    def test_component_reorder_is_detected_for_types(self) -> None:
        # A derived type's component LAYOUT is part of the §5 compatibility contract; a reorder
        # changes the ordered rendering (the type gate compares ordered lists, not sets).
        def m(s):
            t = next(t for t in s["types"] if t["name"].endswith("h_named"))
            t["components"].reverse()
        rendered = render_signatures_to_fortran(self._mutated(m))
        # The normalized *set* is order-insensitive, so assert on the ordered rendered lines.
        base_lines = _type_lines(render_signatures_to_fortran(self.struct), "h_named")
        drift_lines = _type_lines(rendered, "h_named")
        self.assertNotEqual(base_lines, drift_lines)

    def _mutated(self, mutate) -> dict:
        s = copy.deepcopy(self.struct)
        mutate(s)
        return s


class FailClosedTest(unittest.TestCase):
    def test_unparseable_type_spec_raises(self) -> None:
        with self.assertRaises(SignatureParseError):
            parse_signatures_from_fortran(
                "subroutine foo(x)\n  frobnicate :: x\nend subroutine foo\n"
            )


class FortranStanzaParserTests(unittest.TestCase):
    """Generated .f90 still uses the stanza splitter; retain its legacy fail-closed coverage."""

    def test_unterminated_stanza_errors(self) -> None:
        with self.assertRaisesRegex(SignatureParseError, "unterminated"):
            parse_signatures_from_fortran(
                "subroutine hx__foo(a)\n  integer, intent(in) :: a\n")

    def test_duplicate_stanza_errors(self) -> None:
        dup = (
            "function hx__foo(a) result(s)\n  integer, intent(in) :: a\n"
            "  character(len=:), allocatable :: s\nend function hx__foo\n"
            "function hx__foo(a, b) result(s)\n  integer, intent(in) :: a\n"
            "  integer, intent(in) :: b\n  character(len=:), allocatable :: s\n"
            "end function hx__foo\n")
        with self.assertRaisesRegex(SignatureParseError, "duplicate"):
            parse_signatures_from_fortran(dup)

    def test_bare_end_does_not_swallow_following_procedure(self) -> None:
        from tools.validate_pipeline_semantics import _parse_interface_stanzas

        block = (
            "function hx__a(x) result(s)\n  real, intent(in) :: x\n  real :: s\nend\n"
            "function hx__b(y) result(s)\n  real, intent(in) :: y\n  real :: s\n"
            "end function hx__b\n")
        ops, _types, _errors = _parse_interface_stanzas(block)
        self.assertEqual(set(ops), {"hx__a", "hx__b"})

    def test_type_missing_end_type_is_unterminated(self) -> None:
        block = (
            "type :: hx__a\n  integer :: x\n"
            "type :: hx__b\n  integer :: y\nend type hx__b\n")
        with self.assertRaisesRegex(SignatureParseError, "unterminated.*hx__a"):
            parse_signatures_from_fortran(block)

    def test_no_space_end_keyword_closes_stanza(self) -> None:
        block = (
            "function hx__a(x) result(s)\n  real, intent(in) :: x\n"
            "  real :: s\nendfunction hx__a\n"
            "function hx__b(y) result(s)\n  real, intent(in) :: y\n"
            "  real :: s\nend function hx__b\n")
        struct = parse_signatures_from_fortran(block)
        self.assertEqual({p["name"] for p in struct["procedures"]}, {"hx__a", "hx__b"})


def _type_lines(block: str, suffix: str) -> list[str]:
    from tools.validate_pipeline_semantics import _parse_interface_stanzas, _normalize_fortran_line

    _ops, types, _errs = _parse_interface_stanzas(block)
    name = next(n for n in types if n.endswith(suffix))
    return [_normalize_fortran_line(ln) for ln in types[name] if _normalize_fortran_line(ln)]


class MalformedStructFailClosedTest(unittest.TestCase):
    """A leaf-fabricated malformed signature struct must raise SignatureParseError (clean fail-closed
    the gate turns into a repairable violation), NEVER an uncaught KeyError/TypeError/AttributeError
    that crashes the gate with a Python traceback."""

    def _proc(self) -> dict:
        # a minimal VALID function to mutate into each malformed shape
        return {
            "kind": "function", "name": "hx__f",
            "args": [{"name": "x", "rank": 0, "intent": "in",
                      "spec": {"type": "real", "kind": "dp"}}],
            "result": {"name": "s", "rank": 0,
                       "spec": {"type": "string", "len": ":", "alloc": True}},
        }

    def test_valid_baseline_renders(self) -> None:
        render_symbol_to_fortran(self._proc())  # must not raise

    def test_function_null_result_raises(self) -> None:
        p = self._proc(); p["result"] = None
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_derived_spec_missing_name_raises(self) -> None:
        p = self._proc(); p["args"][0]["spec"] = {"type": "derived"}
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_string_spec_missing_len_raises(self) -> None:  # closes F2 fail-open (no silent len=*)
        p = self._proc(); p["args"][0]["spec"] = {"type": "string"}
        with self.assertRaisesRegex(SignatureParseError, "len"):
            render_symbol_to_fortran(p)

    def test_spec_not_a_mapping_raises(self) -> None:
        p = self._proc(); p["args"][0]["spec"] = "real"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_rank_wrong_type_raises(self) -> None:
        p = self._proc(); p["args"][0]["rank"] = "1"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_unknown_entity_key_raises(self) -> None:  # closes F4 (typo silently defaulting)
        p = self._proc(); p["args"][0]["rankk"] = 1
        with self.assertRaisesRegex(SignatureParseError, "unknown key"):
            render_symbol_to_fortran(p)

    def test_bad_intent_value_raises(self) -> None:
        p = self._proc(); p["args"][0]["intent"] = "sideways"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_intent_on_result_raises(self) -> None:
        p = self._proc(); p["result"]["intent"] = "out"
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_subroutine_with_result_raises(self) -> None:
        p = self._proc(); p["kind"] = "subroutine"  # keeps a `result` -> illegal
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(p)

    def test_module_parameter_missing_value_raises(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp"}], "types": [], "procedures": []})

    def test_whole_struct_non_mapping_procedure_raises(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"procedures": ["not a mapping"], "types": [], "module_parameters": []})


class ExplicitDimsTest(unittest.TestCase):
    """A signature can express a fixed dimension bound (e.g. coef(3)), not only assumed-shape (:)."""

    def test_fixed_dim_round_trips(self) -> None:
        block = ("subroutine hx__g(coef)\n"
                 "  real(dp), intent(in) :: coef(3)\n"
                 "end subroutine hx__g\n")
        struct = parse_signatures_from_fortran(block)
        self.assertEqual(struct["procedures"][0]["args"][0]["dims"], ["3"])
        rendered = render_signatures_to_fortran(struct)
        self.assertEqual(normalized_stanza_index(block), normalized_stanza_index(rendered))

    def test_assumed_shape_carries_no_dims_key(self) -> None:
        block = "subroutine hx__h(a)\n  real(dp), intent(in) :: a(:,:)\nend subroutine hx__h\n"
        arg = parse_signatures_from_fortran(block)["procedures"][0]["args"][0]
        self.assertNotIn("dims", arg)
        self.assertEqual(arg["rank"], 2)

    def test_dims_rank_disagreement_fails_closed(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran({"module_parameters": [], "types": [], "procedures": [
                {"kind": "subroutine", "name": "hx__g", "args": [
                    {"name": "c", "rank": 2, "dims": ["3"],
                     "spec": {"type": "real", "kind": "dp"}}]}]})


class Round2HardeningTest(unittest.TestCase):
    """Second-pass review fixes: bounded rank, empty-type symmetry, identifier/token injection guard,
    boolean parameter value."""

    def _arg(self, **over) -> dict:
        ent = {"name": "x", "rank": 0, "intent": "in", "spec": {"type": "real", "kind": "dp"}}
        ent.update(over)
        return {"kind": "subroutine", "name": "hx__f", "args": [ent]}

    def test_out_of_range_rank_fails_closed_not_oom(self) -> None:
        # An unbounded rank would amplify one int into a multi-GB string; it must fail closed fast.
        with self.assertRaisesRegex(SignatureParseError, "rank"):
            render_symbol_to_fortran(self._arg(rank=500_000_000))
        with self.assertRaisesRegex(SignatureParseError, "rank"):
            render_symbol_to_fortran(self._arg(rank=50))

    def test_empty_derived_type_round_trips(self) -> None:
        # parse and validate must agree: an empty (opaque tag) type is Fortran-legal and was
        # accepted by the pre-B Fortran-fence gate, so it must not false-reject now.
        block = "type :: hx__opaque\nend type hx__opaque\n"
        struct = parse_signatures_from_fortran(block)
        self.assertEqual(struct["types"][0]["components"], [])
        rendered = render_signatures_to_fortran(struct)  # must not raise
        self.assertEqual(normalized_stanza_index(block), normalized_stanza_index(rendered))

    def test_name_with_structural_chars_rejected(self) -> None:
        # A name carrying `end subroutine` / a newline could split into a second stanza.
        with self.assertRaisesRegex(SignatureParseError, "identifier"):
            render_symbol_to_fortran(
                {"kind": "subroutine", "name": "hx__f\nend subroutine hx__f\nsubroutine hx__evil",
                 "args": []})

    def test_dims_token_injection_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(self._arg(rank=1, dims=["3) :: evil ! "]))

    def test_string_len_injection_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(self._arg(spec={"type": "string", "len": "4) :: evil"}))

    def test_boolean_parameter_value_rejected(self) -> None:
        with self.assertRaisesRegex(SignatureParseError, "boolean"):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp", "value": True}],
                 "types": [], "procedures": []})

    def test_integer_parameter_value_accepted(self) -> None:
        render_signatures_to_fortran(
            {"module_parameters": [{"name": "case_id_len", "value": 64}],
             "types": [], "procedures": []})  # must not raise

    def test_parenthesized_kind_parameter_value_accepted(self) -> None:
        # A module-parameter value renders OUTSIDE parens, so the portable-kind idiom must not
        # false-reject (and it round-trips from parse).
        block = "integer, parameter :: dp = selected_real_kind(15, 307)\n"
        struct = parse_signatures_from_fortran(block)
        self.assertEqual(struct["module_parameters"][0]["value"], "selected_real_kind(15, 307)")
        render_signatures_to_fortran(struct)  # must not raise

    def test_parameter_value_with_double_colon_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp", "value": "real64 :: evil"}],
                 "types": [], "procedures": []})

    def test_mixed_type_unknown_keys_fails_closed_not_crash(self) -> None:
        # A YAML mapping mixing an int key with string keys must not crash `sorted(...)` on the
        # unknown-key path; it must fail closed.
        with self.assertRaisesRegex(SignatureParseError, "unknown key"):
            render_symbol_to_fortran(
                {"kind": "subroutine", "name": "hx__f",
                 "args": [{"name": "x", 1: "z", "q": "z", "spec": {"type": "real", "kind": "dp"}}]})

    def test_dims_comma_injection_rejected(self) -> None:
        # `dims: ['3,4']` is one entry (rank passes) but would render the rank-2 `(3,4)`.
        with self.assertRaises(SignatureParseError):
            render_symbol_to_fortran(
                {"kind": "subroutine", "name": "hx__f",
                 "args": [{"name": "a", "rank": 1, "dims": ["3,4"],
                           "spec": {"type": "real", "kind": "dp"}}]})

    def test_parameter_value_semicolon_rejected(self) -> None:
        with self.assertRaises(SignatureParseError):
            render_signatures_to_fortran(
                {"module_parameters": [{"name": "dp", "value": "real64; integer evil"}],
                 "types": [], "procedures": []})

    def test_present_null_top_key_fails_closed(self) -> None:
        # `module_parameters: null` (present but null) must fail closed — silently emptying it
        # would drop the dp/case_id_len value pins and let a drifted parameter pass.
        struct, err = load_structured_signatures(
            "module_parameters: null\ntypes: []\nprocedures: []\n")
        self.assertIsNotNone(err)
        self.assertIn("must be a list", err)

    def test_absent_top_key_is_empty(self) -> None:
        # An ABSENT key legitimately means that category is empty (a pruned §5.1 may omit types).
        struct, err = load_structured_signatures(
            "procedures:\n- {kind: subroutine, name: hx__f, args: []}\n")
        self.assertIsNone(err)
        self.assertEqual(struct["types"], [])
        self.assertEqual(struct["module_parameters"], [])

    def test_implicit_result_function_round_trips(self) -> None:
        # `function f(x)` (no result clause) has the function NAME as its result variable; rendering
        # `result(f)` would be invalid Fortran (result name must differ from the function name).
        block = ("function hx__f(x)\n"
                 "  real(dp), intent(in) :: x\n"
                 "  real(dp) :: hx__f\n"
                 "end function hx__f\n")
        struct = parse_signatures_from_fortran(block)
        rendered = render_signatures_to_fortran(struct)
        self.assertNotIn("result(", rendered)
        self.assertEqual(normalized_stanza_index(block), normalized_stanza_index(rendered))

    def test_unsupported_declaration_attribute_rejected(self) -> None:
        # `dimension(:)` / `optional` / `pointer` / `value` are not modeled; silently dropping them
        # would change the ABI (a `dimension(:)` arg parsed as a scalar).
        for decl in ("real, dimension(:), intent(in) :: x", "real, optional, intent(in) :: x",
                     "real, pointer :: x", "real, value :: x"):
            with self.assertRaisesRegex(SignatureParseError, "unsupported declaration attribute"):
                parse_signatures_from_fortran(
                    f"subroutine hx__g(x)\n  {decl}\nend subroutine hx__g\n")

    def test_unhashable_type_value_fails_closed_not_crash(self) -> None:
        # `type: []` / `type: {}` is unhashable; a raw `not in frozenset` would TypeError and escape
        # the callers' `except SignatureParseError`, crashing the gate instead of failing closed.
        for bad_type in ([], {}, 3):
            with self.assertRaisesRegex(SignatureParseError, "spec.type"):
                render_symbol_to_fortran(
                    {"kind": "subroutine", "name": "hx__f",
                     "args": [{"name": "x", "spec": {"type": bad_type}}]})

    def test_unhashable_intent_value_fails_closed_not_crash(self) -> None:
        for bad_intent in ([], {}):
            with self.assertRaisesRegex(SignatureParseError, "intent"):
                render_symbol_to_fortran(
                    {"kind": "subroutine", "name": "hx__f",
                     "args": [{"name": "x", "intent": bad_intent,
                               "spec": {"type": "real", "kind": "dp"}}]})

    def test_inapplicable_type_field_fails_closed(self) -> None:
        # A field the renderer drops for this type would let §5.1 and the IR differ yet render equal.
        cases = [
            {"type": "real", "len": "case_id_len"},   # len ignored on real
            {"type": "real", "name": "foo"},          # name ignored on real
            {"type": "string", "len": ":", "kind": "dp"},   # kind ignored on string
            {"type": "string", "len": ":", "name": "foo"},  # name ignored on string
            {"type": "derived", "name": "hx__t", "len": ":"},  # len ignored on derived
        ]
        for spec in cases:
            with self.assertRaisesRegex(SignatureParseError, "not applicable"):
                render_symbol_to_fortran(
                    {"kind": "subroutine", "name": "hx__f",
                     "args": [{"name": "x", "spec": spec}]})

    def test_full_form_spec_with_none_inapplicable_fields_accepted(self) -> None:
        # The full-form struct parse_signatures_from_fortran emits carries kind/len/name=None for
        # inapplicable fields; None must NOT trip the inapplicable-field guard.
        render_symbol_to_fortran(
            {"kind": "subroutine", "name": "hx__f", "args": [
                {"name": "x", "rank": 0, "intent": "in",
                 "spec": {"type": "real", "kind": "dp", "len": None, "name": None, "alloc": False}}]})


if __name__ == "__main__":
    unittest.main()
