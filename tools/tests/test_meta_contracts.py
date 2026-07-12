#!/usr/bin/env python3
"""Unit tests for tools/meta_contracts.stage_meta_type_violations.

The checker is the single canonical definition of the stage-meta VALUE-TYPE contract,
shared by the runtime write gate (`_validate_step_meta_payload`) and both validator
sweeps (`_validate_source_meta_json_files` / `_validate_ir_meta_json`). It returns bare
clauses; the byte-exact clause text is part of the contract (callers prefix it with their
own path idiom, and existing tests substring-match the clauses).
"""

from __future__ import annotations

import unittest

from tools.meta_contracts import (
    missing_required_meta_keys,
    stage_meta_type_violations,
)


def _conformant() -> dict:
    return {
        "attempt_count": 1,
        "verification_status": "pass",
        "last_fail_reason": None,
        "debug_mode": False,
        "context_isolated": True,
    }


class StageMetaTypeViolationsTests(unittest.TestCase):
    def test_conformant_meta_has_no_violations(self) -> None:
        for step in ("generate", "compile"):
            with self.subTest(step=step):
                self.assertEqual(stage_meta_type_violations(_conformant(), step_token=step), [])

    def test_fail_status_with_string_reason_is_conformant(self) -> None:
        meta = _conformant()
        meta["verification_status"] = "fail"
        meta["last_fail_reason"] = "model.f90: missing z_b associate directive"
        self.assertEqual(stage_meta_type_violations(meta, step_token="generate"), [])

    def test_incident_shaped_dict_last_fail_reason_is_the_only_violation(self) -> None:
        """The E2E #4 regression: a verify leaf authoring a STRUCTURED incident record in
        last_fail_reason (violated convention / target artifact / reason) instead of one
        plain string. Everything else in the meta is well-formed."""
        meta = _conformant()
        meta["verification_status"] = "fail"
        meta["last_fail_reason"] = {
            "violated_convention": "inert_dependency_call",
            "target_artifact": "src/model.f90",
            "reason": "binding probe invented",
        }
        self.assertEqual(
            stage_meta_type_violations(meta, step_token="generate"),
            ["last_fail_reason must be string or null"],
        )

    def test_list_last_fail_reason_is_a_violation(self) -> None:
        meta = _conformant()
        meta["last_fail_reason"] = ["a", "b"]
        self.assertEqual(
            stage_meta_type_violations(meta, step_token="compile"),
            ["last_fail_reason must be string or null"],
        )

    def test_each_key_type_violation_yields_its_clause(self) -> None:
        cases = [
            ("attempt_count", "1", "attempt_count must be integer"),
            ("verification_status", "", "verification_status must be non-empty string"),
            ("verification_status", "   ", "verification_status must be non-empty string"),
            ("verification_status", 3, "verification_status must be non-empty string"),
            ("debug_mode", "false", "debug_mode must be boolean"),
            ("context_isolated", 1, "context_isolated must be boolean"),
        ]
        for key, value, clause in cases:
            with self.subTest(key=key, value=value):
                meta = _conformant()
                meta[key] = value
                self.assertEqual(stage_meta_type_violations(meta, step_token="generate"), [clause])

    def test_violations_are_returned_in_canonical_key_order(self) -> None:
        meta = {
            "attempt_count": "1",
            "verification_status": "",
            "last_fail_reason": {},
            "debug_mode": "no",
            "context_isolated": "yes",
        }
        self.assertEqual(
            stage_meta_type_violations(meta, step_token="generate"),
            [
                "attempt_count must be integer",
                "verification_status must be non-empty string",
                "last_fail_reason must be string or null",
                "debug_mode must be boolean",
                "context_isolated must be boolean",
            ],
        )

    def test_bool_attempt_count_is_rejected(self) -> None:
        """`bool` is a subclass of `int`, so a JSON `true` slips past a bare isinstance check.
        The docs say integer, so the checker must say integer."""
        meta = _conformant()
        meta["attempt_count"] = True
        self.assertEqual(
            stage_meta_type_violations(meta, step_token="generate"),
            ["attempt_count must be integer"],
        )

    def test_constraint_reason_required_only_when_context_isolated_is_false(self) -> None:
        clause = "requires non-empty constraint_reason when context_isolated=false"

        isolated = _conformant()
        self.assertNotIn(clause, stage_meta_type_violations(isolated, step_token="generate"))

        non_isolated = _conformant()
        non_isolated["context_isolated"] = False
        self.assertEqual(
            stage_meta_type_violations(non_isolated, step_token="generate"), [clause]
        )

        justified = dict(non_isolated, constraint_reason="MCP server unavailable in sandbox")
        self.assertEqual(stage_meta_type_violations(justified, step_token="generate"), [])

        blank = dict(non_isolated, constraint_reason="   ")
        self.assertEqual(stage_meta_type_violations(blank, step_token="generate"), [clause])

    def test_non_boolean_context_isolated_is_not_double_flagged(self) -> None:
        """A non-boolean context_isolated gets its own clause; the `is False` test means it
        does not ALSO trip the constraint_reason clause."""
        meta = _conformant()
        meta["context_isolated"] = 0  # falsy but not False
        self.assertEqual(
            stage_meta_type_violations(meta, step_token="generate"),
            ["context_isolated must be boolean"],
        )

    def test_missing_keys_are_not_reported_as_type_violations(self) -> None:
        """Absent keys are missing_required_meta_keys' responsibility; reporting them in both
        places would double-count one defect."""
        empty: dict = {}
        self.assertEqual(stage_meta_type_violations(empty, step_token="generate"), [])
        self.assertEqual(
            missing_required_meta_keys(empty, step_token="generate"),
            [
                "attempt_count",
                "verification_status",
                "last_fail_reason",
                "debug_mode",
                "context_isolated",
            ],
        )

    def test_unknown_extra_keys_are_ignored(self) -> None:
        meta = dict(_conformant(), last_fail_severity="minor", ir_ref="workspace/ir/ir_001")
        self.assertEqual(stage_meta_type_violations(meta, step_token="compile"), [])


if __name__ == "__main__":
    unittest.main()
