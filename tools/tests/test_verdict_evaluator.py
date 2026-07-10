"""Unit tests for the deterministic per-test verdict evaluator (R2).

Two concerns:
  1. The evaluator/schema primitives (ops, ref/case resolution, per-case maps, na_allowed,
     failure_class reduction).
  2. Expressibility proof: every pass-rule shape found across the 12 existing tests.md files
     reduces to the DSL (the M1 acceptance criterion) — in particular shallow_water2d's
     nx-dependent thresholds, convergence order, and N/A rules.
"""

import unittest

from tools.verdict_evaluator import (
    PredicateError,
    evaluate_predicate,
    evaluate_verdict,
    validate_predicate_schema,
)


class OpsAndResolutionTest(unittest.TestCase):
    def test_ops(self) -> None:
        diag = {"metrics": {"metrics.m": 0.5, "metrics.s": "pass"},
                "checks": {"b": {"pass": True}},
                "verdict": {"overall": "pass", "failed_checks": ["cfl", "input_guard"]}}

        def one(ref, op, value):
            pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                    "pass_when": {"all": [{"ref": ref, "op": op, "value": value}]}}
            return evaluate_predicate(pred, diag)[0]

        self.assertEqual(one("metrics.m", "le", 1.0), "pass")
        self.assertEqual(one("metrics.m", "ge", 1.0), "fail")
        self.assertEqual(one("metrics.m", "lt", 0.5), "fail")
        self.assertEqual(one("metrics.m", "gt", 0.4), "pass")
        self.assertEqual(one("metrics.s", "eq", "pass"), "pass")
        self.assertEqual(one("metrics.s", "ne", "fail"), "pass")
        self.assertEqual(one("checks.b.pass", "eq", True), "pass")
        self.assertEqual(one("verdict.failed_checks", "includes", "cfl"), "pass")
        self.assertEqual(one("verdict.failed_checks", "includes", "nope"), "fail")

    def test_bool_and_number_do_not_collide(self) -> None:
        # True must not equal 1; a boolean check compared to a numeric literal fails cleanly.
        diag = {"checks": {"b": {"pass": True}}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "checks.b.pass", "op": "eq", "value": 1}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")

    def test_ordered_op_on_non_number_is_false(self) -> None:
        diag = {"metrics": {"metrics.s": "pass"}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "metrics.s", "op": "le", "value": 1.0}]}}
        status, kind, _ = evaluate_predicate(pred, diag)
        self.assertEqual(status, "fail")
        self.assertEqual(kind, "physics")

    def test_absent_ref_is_structural(self) -> None:
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "checks.gone.pass", "op": "eq", "value": True}]}}
        status, kind, _ = evaluate_predicate(pred, {"verdict": {"overall": "pass"}})
        self.assertEqual((status, kind), ("fail", "structural"))

    def test_includes_requires_list_not_string(self) -> None:
        # F4: `includes` must not substring-match a string lhs.
        diag = {"verdict": {"overall": "passed"}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "includes",
                                       "value": "pass"}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")

    def test_per_case_empty_target_cases_is_non_satisfying(self) -> None:
        # F3: a per_case condition with no target cases must not vacuously pass.
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "metrics.x", "op": "le", "value": 1.0,
                                       "per_case": True}]}}
        diag = {"cases": [{"case_id": "c", "metrics": {"metrics.x": 999}}]}
        status, kind, _ = evaluate_predicate(pred, diag)
        self.assertEqual((status, kind), ("fail", "structural"))

    def test_includes_bool_number_disjoint(self) -> None:
        # membership keeps bool/number separate: numeric 1 does not match a list-of-True.
        diag = {"verdict": {"failed_checks": [True]}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": [],
                "pass_when": {"all": [{"ref": "verdict.failed_checks", "op": "includes",
                                       "value": 1}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")

    def test_na_allowed_absent_ref_passes(self) -> None:
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c"],
                "pass_when": {"all": [{"ref": "errors.sym.l2", "op": "le", "value": 1e-9,
                                       "per_case": True, "na_allowed": True}]}}
        # case c present but the metric is absent (not applied) -> satisfied
        status, _, _ = evaluate_predicate(pred, {"cases": {"c": {"other": 1}}})
        self.assertEqual(status, "pass")


class MetricAddressResolutionTest(unittest.TestCase):
    """A `ref` whose head is neither `checks` nor `verdict` is an opaque metric ADDRESS: a
    whole-string key of the slice's flat `metrics` map, never a nested path. This is the shape
    the harness-rendered runner writes (`{"metrics": {"metrics.zero_rhs_max_abs_dev": 0.0}}`)
    and the vocabulary the Compile gate pins against `diagnostics_contract.metrics`."""

    def _pred(self, ref, **cond):
        return {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": ref, "op": "le", "value": 1.0e-10,
                                       "per_case": True, **cond}]}}

    def test_flat_address_key_resolves(self) -> None:
        # Reproduction pin for the E2E #4 ssprk2 failure: a nested-path resolution reported
        # `ref_absent` (structural_violation) on a run whose metric was present and passing.
        diag = {"per_case": {"c1": {"checks": {"zero_rhs": {"status": "pass"}},
                                    "metrics": {"metrics.zero_rhs_max_abs_dev": 0.0}}}}
        status, kind, _ = evaluate_predicate(
            self._pred("metrics.zero_rhs_max_abs_dev"), diag)
        self.assertEqual((status, kind), ("pass", "pass"))

    def test_non_metrics_heads_resolve_from_the_same_map(self) -> None:
        # `errors.*` / `cfl.*` / `convergence.*` are addresses too, not sub-objects.
        diag = {"per_case": {"c1": {"metrics": {"errors.l2": 1.0e-12, "cfl.max": 0.45}}}}
        self.assertEqual(evaluate_predicate(self._pred("errors.l2"), diag)[0], "pass")
        status, kind, _ = evaluate_predicate(
            {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
             "pass_when": {"all": [{"ref": "cfl.max", "op": "le", "value": 1.0,
                                    "per_case": True}]}}, diag)
        self.assertEqual((status, kind), ("pass", "pass"))

    def test_absent_address_is_structural(self) -> None:
        for diag in (
            {"per_case": {"c1": {"metrics": {"metrics.other": 0.0}}}},   # key absent
            {"per_case": {"c1": {"checks": {}}}},                        # no metrics map
            {"per_case": {"c1": {"metrics": []}}},                       # metrics not a map
        ):
            status, kind, basis = evaluate_predicate(self._pred("metrics.x"), diag)
            self.assertEqual((status, kind), ("fail", "structural"), diag)
            self.assertEqual(basis["conditions"][0]["evaluated"][-1]["reason"], "ref_absent")

    def test_a_nested_sub_object_is_not_decomposed(self) -> None:
        # The pre-fix (path) semantics would have resolved this; the address semantics must not.
        diag = {"per_case": {"c1": {"metrics": {"errors": {"l2": 1.0e-12}}}}}
        self.assertEqual(evaluate_predicate(self._pred("errors.l2"), diag)[1], "structural")

    def test_checks_and_verdict_heads_stay_nested(self) -> None:
        # A literal `checks.x.status` KEY in the metrics map must not hijack the checks ref.
        diag = {"per_case": {"c1": {
            "checks": {"x": {"status": "pass"}},
            "verdict": {"overall": "pass"},
            "metrics": {"checks.x.status": "fail", "verdict.overall": "fail"}}}}
        for ref in ("checks.x.status", "verdict.overall"):
            pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                    "pass_when": {"all": [{"ref": ref, "op": "eq", "value": "pass",
                                           "per_case": True}]}}
            self.assertEqual(evaluate_predicate(pred, diag)[0], "pass", ref)

    def test_null_address_is_present_and_gated_by_na_allowed(self) -> None:
        # The harness writes an honest N/A as `"<address>": null` + `"<address>_reason_na"`.
        diag = {"per_case": {"c1": {"metrics": {"metrics.x": None,
                                                "metrics.x_reason_na": "not applied"}}}}
        status, _, _ = evaluate_predicate(self._pred("metrics.x", na_allowed=True), diag)
        self.assertEqual(status, "pass")
        status, kind, _ = evaluate_predicate(self._pred("metrics.x"), diag)
        self.assertEqual((status, kind), ("fail", "structural"))

    def test_suite_level_address_resolves_without_per_case(self) -> None:
        # A non-per_case metric ref (e.g. a convergence order) reads the top-level metrics map.
        diag = {"metrics": {"convergence.n32_to_n64.analytic_h_order": 0.86}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": "convergence.n32_to_n64.analytic_h_order",
                                       "op": "ge", "value": 0.80}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")


class CaseResolutionTest(unittest.TestCase):
    def test_map_cases(self) -> None:
        diag = {"cases": {"c1": {"checks": {"profile_selected": True}}}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": "checks.profile_selected", "op": "eq",
                                       "value": True, "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_array_cases(self) -> None:
        diag = {"cases": [{"case_id": "n032", "metrics": {"metrics.mass_drift_rel": 1e-13}}]}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["n032"],
                "pass_when": {"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                       "value": 1e-10, "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_per_case_container_map(self) -> None:
        # Real runners emit a top-level `per_case: {case_id: {...}}` container (e.g. the
        # ssprk2 / demo_dep_top diagnostics). A per_case predicate must resolve THAT slice,
        # not the suite-level object.
        diag = {"checks": {}, "verdict": {"overall": "pass"},
                "per_case": {"l0_x": {"verdict": {"overall": "fail", "failed_checks": ["cfl"]}}}}
        pred = {"test_id": "t", "expected_outcome": "xfail", "target_cases": ["l0_x"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail",
                                       "per_case": True},
                                      {"ref": "verdict.failed_checks", "op": "includes",
                                       "value": "cfl", "per_case": True}]}}
        # resolves the per_case slice (overall=fail) not the top-level (overall=pass)
        self.assertEqual(evaluate_predicate(pred, diag)[0], "xfail")

    def test_per_case_container_metric_entries(self) -> None:
        # A per_case entry carrying only a metrics map (no checks/verdict) resolves a metric
        # address against that map.
        diag = {"per_case": {"c": {"metrics": {"metrics.max_abs_dev": 0.0}}}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c"],
                "pass_when": {"all": [{"ref": "metrics.max_abs_dev", "op": "le", "value": 1.0e-12,
                                       "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_case_slice_falls_through_to_other_container(self) -> None:
        # If the preferred `cases` container lacks the case, fall through to `per_case`.
        diag = {"cases": {"other": {"m": 1}},
                "per_case": {"c": {"verdict": {"overall": "pass"}}}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass",
                                       "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_per_case_against_flat_diagnostics_is_structural(self) -> None:
        # A per_case predicate against a container-less (component-flat) diagnostics must NOT
        # silently broadcast the top-level object to every case — it is a shape mismatch.
        diag = {"checks": {"g": {"pass": True}}, "verdict": {"overall": "pass"}}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["c1"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass",
                                       "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[1], "structural")

    def test_missing_case_is_structural(self) -> None:
        diag = {"cases": [{"case_id": "n032", "metrics": {"metrics.mass_drift_rel": 1e-13}}]}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["n999"],
                "pass_when": {"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                       "value": 1e-10, "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[1], "structural")

    def test_per_case_threshold_map(self) -> None:
        addr = "errors.analytic_h.l2_rel_tend"
        diag = {"cases": [{"case_id": "n032", "metrics": {addr: 0.21}},
                          {"case_id": "n064", "metrics": {addr: 0.11}}]}
        pred = {"test_id": "t", "expected_outcome": "pass", "target_cases": ["n032", "n064"],
                "pass_when": {"all": [{"ref": addr, "op": "le", "per_case": True,
                                       "value": {"per_case": {"n032": 2.2e-1, "n064": 1.2e-1}}}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")
        # tighten n064 beyond tolerance -> physics fail
        diag["cases"][1]["metrics"][addr] = 0.13
        self.assertEqual(evaluate_predicate(pred, diag)[0], "fail")


class VerdictReduceTest(unittest.TestCase):
    def _mk(self, statuses):
        # build predicates that trivially yield the requested statuses via verdict.overall
        preds, diag = [], {"cases": {}}
        for i, st in enumerate(statuses):
            cid = f"c{i}"
            expected = "xfail" if st == "xfail" else "pass"
            want_pass = st in ("pass", "xfail")
            diag["cases"][cid] = {"verdict": {"overall": "pass" if want_pass else "fail"}}
            preds.append({"test_id": f"t{i}", "expected_outcome": expected,
                          "target_cases": [cid],
                          "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq",
                                                 "value": "pass", "per_case": True}]}})
        return preds, diag

    def test_all_pass(self) -> None:
        preds, diag = self._mk(["pass", "pass"])
        doc = evaluate_verdict(preds, diag, run_id="r", node_key="n")
        self.assertEqual(doc["self_verdict"], "pass")
        self.assertEqual(doc["failure_class"], "pass")

    def test_all_xfail_is_xfail(self) -> None:
        preds, diag = self._mk(["xfail", "xfail"])
        self.assertEqual(evaluate_verdict(preds, diag)["self_verdict"], "xfail")

    def test_mixed_pass_and_xfail_is_pass(self) -> None:
        preds, diag = self._mk(["pass", "xfail"])
        self.assertEqual(evaluate_verdict(preds, diag)["self_verdict"], "pass")

    def test_any_fail_is_fail_physics(self) -> None:
        preds, diag = self._mk(["pass", "fail"])
        doc = evaluate_verdict(preds, diag)
        self.assertEqual(doc["self_verdict"], "fail")
        self.assertEqual(doc["failure_class"], "physics_fail")

    def test_structural_dominates_physics(self) -> None:
        # one physics fail + one structural (absent case) -> structural_violation
        preds, diag = self._mk(["fail"])
        preds.append({"test_id": "tx", "expected_outcome": "pass", "target_cases": ["absent"],
                      "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq",
                                             "value": "pass", "per_case": True}]}})
        self.assertEqual(evaluate_verdict(preds, diag)["failure_class"], "structural_violation")

    def test_empty_predicates_fail_structural_not_pass(self) -> None:
        # A node with no evaluable per-test rule must not certify as pass.
        for preds in ([], None):
            doc = evaluate_verdict(preds, {"verdict": {"overall": "pass"}},
                                   run_id="r", node_key="n")
            self.assertEqual(doc["self_verdict"], "fail")
            self.assertEqual(doc["failure_class"], "structural_violation")
            self.assertEqual(doc["per_test"], [])

    def test_missing_test_id_raises(self) -> None:
        with self.assertRaises(PredicateError):
            evaluate_verdict([{"expected_outcome": "pass", "target_cases": [],
                               "pass_when": {"all": [{"ref": "a", "op": "eq", "value": 1}]}}], {})


class SchemaTest(unittest.TestCase):
    def _kwargs(self, **over):
        base = dict(case_ids={"c1"}, test_ids=["t1"], check_ids={"g"},
                    verdict_fields={"overall", "failed_checks"}, metric_addrs=set())
        base.update(over)
        return base

    def _pred(self, **over):
        p = {"test_id": "t1", "expected_outcome": "pass", "target_cases": ["c1"],
             "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "pass"}]}}
        p.update(over)
        return p

    def test_valid(self) -> None:
        self.assertEqual(validate_predicate_schema([self._pred()], **self._kwargs()), [])

    def test_empty_required(self) -> None:
        self.assertTrue(validate_predicate_schema([], **self._kwargs()))
        self.assertTrue(validate_predicate_schema(None, **self._kwargs()))

    def test_unhashable_op_is_a_violation_not_a_crash(self) -> None:
        # A malformed op authored as a YAML list/map must yield a violation, not TypeError.
        for bad_op in ([{"eq": 1}], {"op": "eq"}):
            v = validate_predicate_schema(
                [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": bad_op,
                                                "value": "pass"}]})], **self._kwargs())
            self.assertTrue(any(".op must be one of" in x for x in v), (bad_op, v))

    def test_bad_op_and_outcome(self) -> None:
        v = validate_predicate_schema(
            [self._pred(expected_outcome="maybe",
                        pass_when={"all": [{"ref": "verdict.overall", "op": "between",
                                            "value": 1}]})], **self._kwargs())
        self.assertTrue(any("expected_outcome" in x for x in v))
        self.assertTrue(any(".op must be one of" in x for x in v))

    def test_unknown_target_case(self) -> None:
        v = validate_predicate_schema([self._pred(target_cases=["zzz"])], **self._kwargs())
        self.assertTrue(any("unknown case_id" in x for x in v))

    def test_test_id_set_mismatch(self) -> None:
        v = validate_predicate_schema([self._pred()], **self._kwargs(test_ids=["t1", "t2"]))
        self.assertTrue(any("missing tests from tests.md" in x for x in v))
        v = validate_predicate_schema([self._pred(test_id="tX")], **self._kwargs())
        self.assertTrue(any("unknown test_id" in x for x in v))

    def test_unknown_check_ref(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "checks.nope.pass", "op": "eq",
                                            "value": True}]})], **self._kwargs())
        self.assertTrue(any("diagnostics_contract.checks" in x for x in v))

    def test_unknown_verdict_field(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.mystery", "op": "eq",
                                            "value": 1}]})], **self._kwargs())
        self.assertTrue(any("verdict.mystery" in x for x in v))

    def test_metric_addr_must_be_pinned(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                            "value": 1e-10, "per_case": True}]})],
            **self._kwargs())
        self.assertTrue(any("diagnostics_contract.metrics" in x for x in v))
        # once pinned it resolves
        self.assertEqual(validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.mass_drift_rel", "op": "le",
                                            "value": 1e-10, "per_case": True}]})],
            **self._kwargs(metric_addrs={"metrics.mass_drift_rel"})), [])

    def test_metric_addr_must_be_pinned_verbatim_not_by_head(self) -> None:
        # A bare-head pin (`metrics: ["cfl"]`) behind a deeper ref (`cfl.max`) is unresolvable at
        # execute: the renderer emits one key per DECLARED address (`cfl`), while the evaluator
        # looks the whole ref up as a key -> ref_absent -> a permanent structural_violation on an
        # otherwise-correct run. Reject it at Compile, where it is repairable.
        pred = self._pred(pass_when={"all": [{"ref": "cfl.max", "op": "le", "value": 1.0,
                                              "per_case": True}]})
        v = validate_predicate_schema([pred], **self._kwargs(metric_addrs={"cfl"}))
        self.assertTrue(any("diagnostics_contract.metrics" in x for x in v), v)
        # the same ref pinned verbatim is accepted, and it is the key the runner emits
        self.assertEqual(
            validate_predicate_schema([pred], **self._kwargs(metric_addrs={"cfl.max"})), [])
        diag = {"cases": {"c1": {"metrics": {"cfl.max": 0.45}}}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "pass")

    def test_missing_value_rejected(self) -> None:
        # F1: a condition without `value` would compare against None at execute (permanent
        # physics fail); it must be caught at Compile.
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq"}]})],
            **self._kwargs())
        self.assertTrue(any("non-null `value`" in x for x in v))

    def test_non_per_case_dict_value_rejected(self) -> None:
        # A dict `value` that is not the {per_case: ...} form is malformed and would slip
        # through as a literal at execute (e.g. `ne` against a dict = vacuous true = false pass).
        for bad in ({"threshold": 0}, {"per_case": {"c1": 1}, "extra": 1}):
            v = validate_predicate_schema(
                [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "ne",
                                                "value": bad}]})], **self._kwargs())
            self.assertTrue(any("value must be a scalar" in x for x in v), (bad, v))

    def test_explicit_null_value_rejected(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq",
                                            "value": None}]})], **self._kwargs())
        self.assertTrue(any("non-null `value`" in x for x in v))

    def test_bare_verdict_ref_rejected(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict", "op": "eq", "value": "pass"}]})],
            **self._kwargs())
        self.assertTrue(any("needs a field" in x for x in v))

    def test_per_case_value_map_must_cover_all_target_cases(self) -> None:
        # A per-case threshold map missing an entry for a target case would fail that case at
        # execute (value_absent_for_case) — catch it at Compile.
        v = validate_predicate_schema(
            [self._pred(target_cases=["c1", "c2"],
                        pass_when={"all": [{"ref": "metrics.m", "op": "le", "per_case": True,
                                            "value": {"per_case": {"c1": 1.0}}}]})],
            **self._kwargs(case_ids={"c1", "c2"}, metric_addrs={"metrics.m"}))
        self.assertTrue(any("missing a threshold for target case" in x for x in v), v)
        # complete map is accepted
        self.assertEqual(validate_predicate_schema(
            [self._pred(target_cases=["c1", "c2"],
                        pass_when={"all": [{"ref": "metrics.m", "op": "le", "per_case": True,
                                            "value": {"per_case": {"c1": 1.0, "c2": 2.0}}}]})],
            **self._kwargs(case_ids={"c1", "c2"}, metric_addrs={"metrics.m"})), [])

    def test_ordered_op_requires_numeric_threshold(self) -> None:
        # An ordered op with a string threshold (e.g. YAML "1e-10") deterministically fails at
        # execute; reject it at Compile.
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.m", "op": "le", "value": "1e-10"}]})],
            **self._kwargs(metric_addrs={"metrics.m"}))
        self.assertTrue(any("must be a number for the ordered op" in x for x in v), v)
        # per-case string thresholds are also caught
        v2 = validate_predicate_schema(
            [self._pred(target_cases=["c1"],
                        pass_when={"all": [{"ref": "metrics.m", "op": "ge", "per_case": True,
                                            "value": {"per_case": {"c1": "0.8"}}}]})],
            **self._kwargs(metric_addrs={"metrics.m"}))
        self.assertTrue(any("must be a number for the ordered op" in x for x in v2), v2)
        # a numeric threshold is accepted
        self.assertEqual(validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "metrics.m", "op": "le", "value": 1e-10}]})],
            **self._kwargs(metric_addrs={"metrics.m"})), [])

    def test_verdict_ref_requires_declared_field_no_default(self) -> None:
        # With no declared verdict fields, a verdict.* ref is rejected (no seeded default).
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq",
                                            "value": "pass"}]})],
            **self._kwargs(verdict_fields=set()))
        self.assertTrue(any("verdict.overall" in x for x in v), v)

    def test_per_case_value_map_requires_per_case_flag(self) -> None:
        v = validate_predicate_schema(
            [self._pred(pass_when={"all": [{"ref": "verdict.overall", "op": "eq",
                                            "value": {"per_case": {"c1": "pass"}}}]})],
            **self._kwargs())
        self.assertTrue(any("per_case value map but per_case is not true" in x for x in v))


class TwelveSpecExpressibilityTest(unittest.TestCase):
    """Each real tests.md pass-rule shape reduces to the DSL and evaluates correctly."""

    def test_component_boolean_check_and_guard(self) -> None:
        # demo_dep_base: a passing check + a standard "inverted" xfail guard (guard fires,
        # verdict stays pass).
        diag = {"checks": {"scale_identity": {"pass": True}, "input_guard": {"pass": True}},
                "verdict": {"overall": "pass", "failed_checks": []}}
        preds = [
            {"test_id": "l0_scale_identity_pass", "expected_outcome": "pass",
             "target_cases": ["l0_scale_identity_pass"],
             "pass_when": {"all": [{"ref": "checks.scale_identity.pass", "op": "eq", "value": True},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass"}]}},
            {"test_id": "l0_invalid_length_xfail", "expected_outcome": "xfail",
             "target_cases": ["l0_invalid_length_xfail"],
             "pass_when": {"all": [{"ref": "checks.input_guard.pass", "op": "eq", "value": True},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass"}]}},
        ]
        doc = evaluate_verdict(preds, diag)
        self.assertEqual([p["status"] for p in doc["per_test"]], ["pass", "xfail"])
        self.assertEqual(doc["self_verdict"], "pass")

    def test_component_status_style_guard_membership_xfail(self) -> None:
        # dynamics component: checks.<name>.status == "pass"; guard xfail = overall fail AND
        # failed_checks includes 'input_guard'.
        diag = {"checks": {"input_guard": {"status": "fail", "invalid_state_detected": True}},
                "verdict": {"overall": "fail", "failed_checks": ["input_guard"]}}
        pred = {"test_id": "l0_invalid_dry_state_xfail", "expected_outcome": "xfail",
                "target_cases": ["l0_invalid_dry_state_xfail"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail"},
                                      {"ref": "verdict.failed_checks", "op": "includes",
                                       "value": "input_guard"}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "xfail")

    def test_profile_per_case_membership(self) -> None:
        # profile: per-case checks + guard membership on component_compatibility.
        diag = {"cases": {
            "profile_select_default": {"checks": {"profile_selected": True},
                                       "verdict": {"overall": "pass", "failed_checks": []}},
            "profile_guard_incompatible_version": {
                "checks": {"component_compatibility": False},
                "verdict": {"overall": "fail", "failed_checks": ["component_compatibility"]}}}}
        preds = [
            {"test_id": "l0_select_default_profile_pass", "expected_outcome": "pass",
             "target_cases": ["profile_select_default"],
             "pass_when": {"all": [{"ref": "checks.profile_selected", "op": "eq", "value": True,
                                    "per_case": True},
                                   {"ref": "verdict.overall", "op": "eq", "value": "pass",
                                    "per_case": True}]}},
            {"test_id": "l0_guard_incompatible_component_version_xfail", "expected_outcome": "xfail",
             "target_cases": ["profile_guard_incompatible_version"],
             "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail",
                                    "per_case": True},
                                   {"ref": "verdict.failed_checks", "op": "includes",
                                    "value": "component_compatibility", "per_case": True}]}},
        ]
        doc = evaluate_verdict(preds, diag)
        self.assertEqual([p["status"] for p in doc["per_test"]], ["pass", "xfail"])

    def test_problem_nx_threshold_convergence_and_na(self) -> None:
        # shallow_water2d L1: per-case nx-dependent theoretical error thresholds, a
        # convergence-order check over a runner-emitted metric, per-case mass drift, and an
        # N/A (not-applied) momentum check.
        # Every metric — per-case and suite-level alike — is a flat dotted-address key of a
        # `metrics` map, exactly as the runner writes it.
        def case(cid: str, l2: float) -> dict:
            return {"case_id": cid, "metrics": {"cfl.max": 0.45, "metrics.mass_drift_rel": 1e-13,
                                                "errors.analytic_h.l2_rel_tend": l2}}

        diag = {"cases": [case("swe2d_ref_n032", 0.20), case("swe2d_ref_n064", 0.11),
                          case("swe2d_ref_n128", 0.06)],
                # convergence order derived by the runner (harness emits it under R1); referenced
                # as a top-level metric, pinned in diagnostics_contract.metrics.
                "metrics": {"convergence.n32_to_n64.analytic_h_order": 0.86,
                            "convergence.n64_to_n128.analytic_h_order": 0.87}}
        cases = ["swe2d_ref_n032", "swe2d_ref_n064", "swe2d_ref_n128"]
        pred = {"test_id": "l1_refinement_linear_wave", "expected_outcome": "pass",
                "target_cases": cases,
                "pass_when": {"all": [
                    {"ref": "cfl.max", "op": "le", "value": 1.0, "per_case": True},
                    {"ref": "metrics.mass_drift_rel", "op": "le", "value": 1e-10, "per_case": True},
                    {"ref": "errors.analytic_h.l2_rel_tend", "op": "le", "per_case": True,
                     "value": {"per_case": {"swe2d_ref_n032": 2.2e-1, "swe2d_ref_n064": 1.2e-1,
                                            "swe2d_ref_n128": 6.5e-2}}},
                    {"ref": "convergence.n32_to_n64.analytic_h_order", "op": "ge", "value": 0.80},
                    {"ref": "convergence.n64_to_n128.analytic_h_order", "op": "ge", "value": 0.80},
                    # momentum not applied for this profile -> na_allowed absorbs the null
                    {"ref": "metrics.momx_drift_rel", "op": "le", "value": 1e-10,
                     "per_case": True, "na_allowed": True}]}}
        status, kind, _ = evaluate_predicate(pred, diag)
        self.assertEqual((status, kind), ("pass", "pass"))

    def test_problem_cfl_guard_xfail(self) -> None:
        diag = {"cases": [{"case_id": "swe2d_guard", "cfl": {"max": 1.4},
                           "verdict": {"overall": "fail", "failed_checks": ["cfl"]}}]}
        pred = {"test_id": "l0_cfl_guard_xfail", "expected_outcome": "xfail",
                "target_cases": ["swe2d_guard"],
                "pass_when": {"all": [{"ref": "verdict.overall", "op": "eq", "value": "fail",
                                       "per_case": True},
                                      {"ref": "verdict.failed_checks", "op": "includes",
                                       "value": "cfl", "per_case": True}]}}
        self.assertEqual(evaluate_predicate(pred, diag)[0], "xfail")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
