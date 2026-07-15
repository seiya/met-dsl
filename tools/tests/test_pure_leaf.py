"""Unit tests for the Z2 pure-leaf transport (`tools/pure_leaf.py`) and the
`Conductor.leaf_command` pure branch (M-A).

The transport is inert at this milestone (no caller passes `pure=True`), so these tests
are the whole of its verification. They follow the Z0 mutation-flow discipline: the parser
is fed a null `result`, an absent `usage`, `is_error`, non-JSON, and empty stdout to prove a
present `null` is never read as an absent key (`_MISSING`); the extractor is fed fenced /
unfenced / truncated bodies; the verdict validator is fed each joint-invariant violation and
an uppercase `"PASS"` to prove the status enum is exact, not case-folded; and the flag set is
pinned by a golden.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools import pure_leaf as pl
from tools import workflow_conductor as wc


def _envelope(**overrides) -> str:
    """A well-formed result envelope with `overrides` applied; keys mapped to the sentinel
    `DROP` are omitted entirely (so absence, not a null, is tested)."""
    base = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "{}",
        "session_id": "sess-1",
        "model": "claude-opus-4-8",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    base.update(overrides)
    payload = {k: v for k, v in base.items() if v is not DROP}
    return json.dumps(payload)


DROP = object()


class PureLeafFlagsTest(unittest.TestCase):
    def test_flag_set_golden(self):
        self.assertEqual(
            pl.pure_leaf_flags(),
            ["--safe-mode", "--system-prompt", pl.PURE_SYSTEM_PROMPT, "--tools", "",
             "--strict-mcp-config", "--disable-slash-commands", "--output-format", "json"])

    def test_context_closing_flags_present(self):
        # --safe-mode disables CLAUDE.md + the repo's UserPromptSubmit hook; --system-prompt
        # replaces the default system prompt (dropping its per-machine dynamic sections).
        # A regression that drops either re-opens ambient / host-varying input.
        flags = pl.pure_leaf_flags()
        self.assertIn("--safe-mode", flags)
        self.assertEqual(flags[flags.index("--system-prompt") + 1], pl.PURE_SYSTEM_PROMPT)

    def test_flags_are_a_fresh_list(self):
        a = pl.pure_leaf_flags()
        a.append("--mutated")
        self.assertNotIn("--mutated", pl.pure_leaf_flags())


class ParseResultEnvelopeTest(unittest.TestCase):
    def test_well_formed(self):
        env = pl.parse_result_envelope(_envelope())
        self.assertTrue(env.parsed)
        self.assertIsNone(env.parse_error)
        self.assertEqual(env.result, "{}")
        self.assertIs(env.is_error, False)
        self.assertEqual(env.model, "claude-opus-4-8")
        self.assertEqual(env.usage, {"input_tokens": 10, "output_tokens": 20})

    def test_result_null_is_present_not_missing(self):
        # "result": null is a PRESENT null (None), never conflated with an absent key.
        env = pl.parse_result_envelope(_envelope(result=None))
        self.assertTrue(env.parsed)
        self.assertIsNone(env.result)
        self.assertIsNot(env.result, pl._MISSING)
        # And the extractor treats that null as no document.
        doc, category = pl.extract_json_document(env.result)
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_absent_usage_is_missing(self):
        env = pl.parse_result_envelope(_envelope(usage=DROP))
        self.assertIs(env.usage, pl._MISSING)

    def test_is_error_true_surfaced(self):
        env = pl.parse_result_envelope(_envelope(is_error=True))
        self.assertIs(env.is_error, True)

    def test_model_from_model_usage_fallback(self):
        env = pl.parse_result_envelope(
            _envelope(model=DROP, modelUsage={"claude-sonnet-5": {"output_tokens": 1}}))
        self.assertEqual(env.model, "claude-sonnet-5")

    def test_model_missing_when_ambiguous(self):
        env = pl.parse_result_envelope(
            _envelope(model=DROP, modelUsage={"a": {}, "b": {}}))
        self.assertIs(env.model, pl._MISSING)

    def test_model_empty_top_level_falls_back_to_model_usage(self):
        env = pl.parse_result_envelope(
            _envelope(model="  ", modelUsage={"claude-x": {}}))
        self.assertEqual(env.model, "claude-x")

    def test_deeply_nested_stdout_does_not_raise(self):
        # RecursionError (not a JSONDecodeError) must not escape the "never raises" contract.
        env = pl.parse_result_envelope("[" * 200000)
        self.assertFalse(env.parsed)
        self.assertIs(env.result, pl._MISSING)

    def test_non_json_stdout(self):
        env = pl.parse_result_envelope("not json at all")
        self.assertFalse(env.parsed)
        self.assertIsNotNone(env.parse_error)
        self.assertIs(env.result, pl._MISSING)

    def test_empty_stdout(self):
        env = pl.parse_result_envelope("")
        self.assertFalse(env.parsed)
        self.assertIs(env.result, pl._MISSING)
        self.assertIs(env.usage, pl._MISSING)

    def test_json_array_is_not_an_object(self):
        env = pl.parse_result_envelope("[1, 2, 3]")
        self.assertFalse(env.parsed)
        self.assertIs(env.result, pl._MISSING)

    def test_never_raises_on_hostile_input(self):
        for bad in (None, 42, {"a": 1}, b"bytes"):
            env = pl.parse_result_envelope(bad)  # type: ignore[arg-type]
            self.assertFalse(env.parsed)


class ExtractJsonDocumentTest(unittest.TestCase):
    def test_bare_json_object(self):
        doc, category = pl.extract_json_document('{"a": 1}')
        self.assertIsNone(category)
        self.assertEqual(doc, {"a": 1})

    def test_single_json_fence(self):
        # A reply that is a single fenced block with only whitespace outside is accepted.
        text = "```json\n{\"a\": 1}\n```\n"
        doc, category = pl.extract_json_document(text)
        self.assertIsNone(category)
        self.assertEqual(doc, {"a": 1})

    def test_prose_outside_fence_rejected(self):
        # Any non-whitespace outside the sole fence (a prose preamble here) is ambiguous.
        text = "Here is the bundle:\n```json\n{\"a\": 1}\n```\n"
        doc, category = pl.extract_json_document(text)
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_plain_fence_without_language_tag(self):
        doc, category = pl.extract_json_document("```\n{\"a\": 1}\n```")
        self.assertIsNone(category)
        self.assertEqual(doc, {"a": 1})

    def test_fenced_document_with_backticks_in_string(self):
        # A ``` sequence inside a JSON string value (a bundle source file, a verdict message)
        # must not be counted as a fence marker — the outer fence is parsed structurally.
        text = '```json\n{"content": "x ``` y"}\n```'
        doc, category = pl.extract_json_document(text)
        self.assertIsNone(category)
        self.assertEqual(doc, {"content": "x ``` y"})

    def test_fence_opened_never_closed_is_truncated(self):
        doc, category = pl.extract_json_document("```json\n{\"a\": 1")
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_TRUNCATED)

    def test_fence_tag_only_no_newline_is_truncated(self):
        doc, category = pl.extract_json_document("```json")
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_TRUNCATED)

    def test_unclosed_fence_is_truncated(self):
        doc, category = pl.extract_json_document("```json\n{\"a\": 1, \"b\": ")
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_TRUNCATED)

    def test_unbalanced_braces_is_truncated(self):
        doc, category = pl.extract_json_document('{"a": 1, "b": {"c": 2')
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_TRUNCATED)

    def test_garbage_is_unparseable(self):
        doc, category = pl.extract_json_document("this is not json }{")
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_two_fences_is_unparseable(self):
        text = "```json\n{\"a\":1}\n```\nand\n```json\n{\"b\":2}\n```"
        doc, category = pl.extract_json_document(text)
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_valid_document_with_backticks_in_string_is_parsed(self):
        # Parse-first: a clean JSON doc whose string value merely mentions a ``` fence must
        # not be rejected by fence bookkeeping. An odd fence count inside a string and an even
        # (spurious-block) one both used to be miscategorized.
        for value in ("```", "the model emitted ```json``` instead of bare JSON"):
            doc, category = pl.extract_json_document(json.dumps({"last_fail_reason": value}))
            self.assertIsNone(category)
            self.assertEqual(doc, {"last_fail_reason": value})

    def test_deeply_nested_result_does_not_raise(self):
        doc, category = pl.extract_json_document("[" * 200000)
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_TRUNCATED)

    def test_duplicate_top_level_key_rejected(self):
        # A duplicate key must not silently collapse (last-wins) — a fail->pass duplicate
        # could suppress a failure at the model-document trust boundary.
        doc, category = pl.extract_json_document(
            '{"verification_status": "fail", "verification_status": "pass"}')
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_duplicate_nested_key_rejected(self):
        doc, category = pl.extract_json_document('{"a": {"x": 1, "x": 2}}')
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_bare_document_before_fence_rejected(self):
        # A bare document + a fenced document is TWO documents; selecting the fenced one would
        # silently drop the bare fail (multi-document trust hole).
        text = ('{"verification_status": "fail"}\n'
                '```json\n{"verification_status": "pass"}\n```')
        doc, category = pl.extract_json_document(text)
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_document_after_fence_rejected(self):
        text = ('```json\n{"verification_status": "pass"}\n```\n'
                '{"verification_status": "fail"}')
        doc, category = pl.extract_json_document(text)
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_scalar_document_outside_fence_rejected(self):
        # A bare scalar (not just object/array) outside the fence is still a competing
        # document and must not be silently dropped.
        text = '"fail"\n```json\n{"verification_status": "pass"}\n```'
        doc, category = pl.extract_json_document(text)
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_non_finite_constants_rejected(self):
        for body in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            doc, category = pl.extract_json_document(body)
            self.assertIsNone(doc, body)
            self.assertEqual(category, pl.RESPONSE_UNPARSEABLE, body)

    def test_non_finite_overflow_float_rejected(self):
        doc, category = pl.extract_json_document('{"x": 1e999}')
        self.assertIsNone(doc)
        self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)

    def test_finite_floats_accepted(self):
        doc, category = pl.extract_json_document('{"x": 1.5, "y": -2.0e3}')
        self.assertIsNone(category)
        self.assertEqual(doc, {"x": 1.5, "y": -2000.0})

    def test_missing_and_null_and_empty(self):
        for value in (pl._MISSING, None, "", "   ", 123):
            doc, category = pl.extract_json_document(value)
            self.assertIsNone(doc)
            self.assertEqual(category, pl.RESPONSE_UNPARSEABLE)


def _verdict(**overrides) -> dict:
    base = {
        "verification_status": "pass",
        "issue_severity": "none",
        "last_fail_reason": None,
        "findings": [],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not DROP}


class VerifyVerdictTest(unittest.TestCase):
    def test_valid_pass(self):
        self.assertEqual(pl.verify_verdict_violations(_verdict()), [])

    def test_valid_fail(self):
        v = _verdict(verification_status="fail", issue_severity="minor",
                     last_fail_reason="io contract mismatch",
                     findings=[{"summary": "wrong intent shape"}])
        self.assertEqual(pl.verify_verdict_violations(v), [])

    def test_uppercase_status_rejected_not_folded(self):
        # The status enum is EXACT: "PASS" is a violation, never folded into a pass.
        violations = pl.verify_verdict_violations(_verdict(verification_status="PASS"))
        self.assertTrue(any("verification_status must be one of" in c for c in violations))

    def test_unknown_severity_rejected(self):
        violations = pl.verify_verdict_violations(_verdict(issue_severity="blocker"))
        self.assertTrue(any("issue_severity must be one of" in c for c in violations))

    def test_missing_key(self):
        violations = pl.verify_verdict_violations(_verdict(findings=DROP))
        self.assertIn("findings is required", violations)

    def test_unknown_key_closed(self):
        v = _verdict()
        v["extra"] = 1
        violations = pl.verify_verdict_violations(v)
        self.assertTrue(any("unknown key" in c for c in violations))

    def test_last_fail_reason_object_rejected(self):
        violations = pl.verify_verdict_violations(
            _verdict(verification_status="fail", issue_severity="major",
                     last_fail_reason={"detail": "x"}, findings=[{"summary": "s"}]))
        self.assertIn("last_fail_reason must be a string or null", violations)

    # --- joint invariants ---
    def test_pass_with_severity_violation(self):
        violations = pl.verify_verdict_violations(_verdict(issue_severity="minor"))
        self.assertTrue(any("requires issue_severity 'none'" in c for c in violations))

    def test_pass_with_findings_violation(self):
        violations = pl.verify_verdict_violations(
            _verdict(findings=[{"summary": "s"}]))
        # pass + non-none-forcing findings: severity stays none so the findings-empty
        # invariant is what fires.
        self.assertTrue(any("empty findings array" in c for c in violations))

    def test_pass_with_reason_violation(self):
        violations = pl.verify_verdict_violations(_verdict(last_fail_reason="oops"))
        self.assertTrue(any("last_fail_reason null" in c for c in violations))

    def test_fail_requires_severity(self):
        violations = pl.verify_verdict_violations(
            _verdict(verification_status="fail", last_fail_reason="x",
                     findings=[{"summary": "s"}]))
        self.assertTrue(any("non-'none' issue_severity" in c for c in violations))

    def test_fail_requires_findings_and_reason(self):
        violations = pl.verify_verdict_violations(
            _verdict(verification_status="fail", issue_severity="major"))
        self.assertTrue(any("at least one finding" in c for c in violations))
        self.assertTrue(any("non-empty last_fail_reason" in c for c in violations))

    def test_finding_without_summary(self):
        violations = pl.verify_verdict_violations(
            _verdict(verification_status="fail", issue_severity="major",
                     last_fail_reason="x", findings=[{"note": "no summary"}]))
        self.assertTrue(any("findings[0].summary" in c for c in violations))

    def test_findings_not_an_array(self):
        violations = pl.verify_verdict_violations(
            _verdict(verification_status="fail", issue_severity="major",
                     last_fail_reason="x", findings="oops"))
        self.assertIn("findings must be an array", violations)

    def test_finding_element_not_an_object(self):
        violations = pl.verify_verdict_violations(
            _verdict(verification_status="fail", issue_severity="major",
                     last_fail_reason="x", findings=["a string finding"]))
        self.assertIn("findings[0] must be an object", violations)

    def test_non_dict_verdict(self):
        self.assertEqual(pl.verify_verdict_violations([1, 2]),
                         ["verdict must be a JSON object"])


class VerdictVocabParityTest(unittest.TestCase):
    """Guard the verdict enums against drift from the conductor's severity router — the two
    must agree or the model could author a severity the router mishandles (or vice versa)."""

    def test_status_vocab_pinned(self):
        self.assertEqual(pl.VERDICT_STATUSES, ("pass", "fail"))

    def test_severity_vocab_pinned(self):
        self.assertEqual(pl.VERDICT_SEVERITIES, ("none", "minor", "major", "critical"))

    def test_every_severity_routes_consistently(self):
        # 'none' advances; every non-'none' verdict severity must route to a repair action
        # (never 'advance') in classify_verify_severity.
        for sev in pl.VERDICT_SEVERITIES:
            decision = wc.classify_verify_severity(sev, "prod")
            if sev == "none":
                self.assertEqual(decision.action, "advance")
            else:
                self.assertNotEqual(
                    decision.action, "advance",
                    f"severity {sev!r} must route to a repair, not advance")


class LeafCommandPureBranchTest(unittest.TestCase):
    def _conductor(self, backend: str) -> wc.Conductor:
        return wc.Conductor(
            repo_root=Path("/tmp/repo"), orchestration_id="o",
            orchestration_agent_run_id="ORCH", backend=backend, env={})

    def test_pure_claude_command_includes_flags(self):
        argv = self._conductor("claude").leaf_command(
            "PROMPT", session_id="arid-1", pure=True)
        self.assertEqual(argv[0], "claude")
        self.assertIn("--session-id", argv)
        for flag in pl.pure_leaf_flags():
            if flag:  # the empty "" value of --tools is order-checked below
                self.assertIn(flag, argv)
        # the -p body is last, the prompt after it
        self.assertEqual(argv[-2:], ["-p", "PROMPT"])
        # --tools "" is a value pair
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_pure_warm_resume_prefixes_resume_flags(self):
        argv = self._conductor("claude").leaf_command(
            "PROMPT", session_id="arid-2", resume_session_id="arid-1", pure=True)
        self.assertEqual(argv[argv.index("--resume") + 1], "arid-1")
        self.assertIn("--fork-session", argv)
        self.assertIn("--output-format", argv)

    def test_non_pure_claude_has_no_pure_flags(self):
        argv = self._conductor("claude").leaf_command("PROMPT", session_id="a")
        self.assertNotIn("--strict-mcp-config", argv)
        self.assertNotIn("--disable-slash-commands", argv)

    def test_codex_pure_fails_closed(self):
        with self.assertRaises(ValueError) as ctx:
            self._conductor("codex").leaf_command("PROMPT", pure=True)
        self.assertIn("claude-only", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
