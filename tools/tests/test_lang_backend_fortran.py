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


if __name__ == "__main__":
    unittest.main()
