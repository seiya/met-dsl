"""Deterministic per-test verdict evaluation (R2 of the workflow scaling redesign).

Pure functions shared by the conductor (``Validate.execute`` authors ``verdict.json``
in-process from these) and the Compile-stage predicate gate
(``validate_pipeline_semantics --stage compile`` validates the DSL through the same
schema check). No filesystem, no conductor dependency — the exact shape as
``tools/dependency_graph.py``.

The predicate DSL lives in the IR at ``io_contract.test_predicates`` (a sibling of
``io_contract.test_evidence_requirements``). Each entry formalizes exactly one
``tests.md`` test's ``pass_when`` / ``judgment`` rule as a machine-evaluable predicate
over the runner's ``diagnostics.json``::

    io_contract:
      test_predicates:
        - test_id: <str>                 # one per tests.md test_id (set equality gated at Compile)
          expected_outcome: pass|xfail    # the certifying outcome when the predicate holds
          target_cases: [<case_id>, ...]  # the case_ids this predicate ranges over
          pass_when:
            all:                          # `all` is the only combinator (add any/not on real demand)
              - ref: <diagnostics ref>     # e.g. checks.input_guard.status / verdict.overall / metrics.cfl_max
                op: eq|ne|le|ge|lt|gt|includes
                value: <scalar|bool|string|list>            # OR {per_case: {<case_id>: value}} for nx-dependent thresholds
                per_case: <bool>          # optional: resolve `ref` inside each target case's diagnostics slice
                case: <case_id>           # optional: resolve `ref` inside ONE target case's slice (excl. per_case)
                na_allowed: <bool>        # optional: a null/absent lhs counts as satisfied (a "not applied" metric)

A multi-target test (a convergence sweep, a base/shifted equivariance pair) ranges its
``target_cases`` over several cases and picks its scope per condition:

- ``per_case: true`` — the condition must hold in EVERY target case (e.g. positivity at
  every resolution). Combine with a ``{per_case: {...}}`` value map for a per-case threshold.
- ``case: <case_id>`` — the condition is resolved in exactly ONE target case's slice. This is
  how a cross-case reduction is compared: the checks module accumulates across cases and emits
  the derived metric (``convergence_order``, ``symmetry_h_l2_rel``) as a per-case metric of the
  case where it first becomes computable, and the predicate reads it there.
- neither — the condition resolves against the suite-level (top-level) diagnostics object.

A ``ref`` resolves by its HEAD, over the same closed head vocabulary the Compile-stage gate
(``_check_ref``) validates:

- ``checks`` / ``verdict`` — a dotted path nested inside the diagnostics slice.
- any other head — an opaque per-case **metric address** (``metrics.*`` / ``errors.*`` /
  ``cfl.*`` / ``convergence.*``, pinned in ``diagnostics_contract.metrics``), looked up as a
  whole-string KEY of the slice's ``metrics`` map. The runner writes that map flat, keyed by the
  full dotted address (``{"metrics": {"metrics.cfl_max": 0.4}}``), so the address is never
  decomposed into path segments. An N/A metric is written as ``"<address>": null`` beside an
  ``"<address>_reason_na"`` sibling, which the ``na_allowed`` handling consumes.

The runner emits every numeric judgment already reduced to a diagnostics field (a
``checks.<id>.status`` enum / a metric-address scalar / a ``verdict.overall`` enum), so
the predicate never does arithmetic — it only compares a resolved diagnostics value
against a constant/set and conjoins the results. ``xfail_condition`` is a case-construction
fact (verified at Compile), never an evaluated runtime predicate.
"""

from __future__ import annotations

from typing import Any

# Predicate comparison operators. `includes` is set/list membership (rhs in lhs);
# the ordered ops require a numeric lhs.
_OPS: frozenset[str] = frozenset({"eq", "ne", "le", "ge", "lt", "gt", "includes"})
_ORDERED_OPS: frozenset[str] = frozenset({"le", "ge", "lt", "gt"})
_EXPECTED_OUTCOMES: frozenset[str] = frozenset({"pass", "xfail"})

# A per-test evaluation result kind, folded into the top-level failure_class.
_KIND_PASS = "pass"
_KIND_PHYSICS = "physics"      # a diagnostics value was present but the comparison was false
_KIND_STRUCTURAL = "structural"  # a required diagnostics ref was absent/unresolvable (contract gap)


class PredicateError(ValueError):
    """A malformed predicate DSL surfaced during evaluation (as opposed to a
    schema-validation pass, which returns violation strings)."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _resolve_ref(obj: Any, ref: str) -> tuple[bool, Any]:
    """Resolve a dotted ``ref`` inside ``obj``. Returns ``(present, value)``; ``present``
    is False when any path segment is missing (or ``obj`` is not a mapping there)."""
    cur = obj
    for seg in ref.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return (False, None)
        cur = cur[seg]
    return (True, cur)


# Heads whose `ref` is a NESTED path into the diagnostics slice. Every other head is an opaque
# metric ADDRESS — the runner keys its `metrics` map by the whole dotted address, so decomposing
# it into path segments would never resolve. Exactly mirrors `_check_ref`'s Compile-stage
# vocabulary: that gate pins a metric ref against the declared addresses by whole-string equality,
# which is the same key this resolves.
_NESTED_REF_HEADS: frozenset[str] = frozenset({"checks", "verdict"})


def _resolve_predicate_ref(obj: Any, ref: str) -> tuple[bool, Any]:
    """Resolve a predicate ``ref`` against one diagnostics slice, dispatching on its head.

    Returns ``(present, value)``. A metric address present with a ``null`` value (the runner's
    honest-N/A encoding) resolves as ``(True, None)``; the caller's ``na_allowed`` handling
    decides whether that satisfies the condition."""
    if not isinstance(obj, dict):
        return (False, None)
    if ref.split(".", 1)[0] in _NESTED_REF_HEADS:
        return _resolve_ref(obj, ref)
    metrics = obj.get("metrics")
    if not isinstance(metrics, dict) or ref not in metrics:
        return (False, None)
    return (True, metrics[ref])


def _case_slice(diagnostics: dict[str, Any], case_id: str) -> tuple[bool, Any]:
    """The diagnostics sub-object for one case. Supports the runner per-case container
    shapes actually emitted (a ``cases``/``per_case`` map keyed by case_id, or a ``cases``
    list of ``{"case_id": ..., ...}`` objects):

    - ``cases`` / ``per_case`` is a map ``{<case_id>: {...}}``      -> container[case_id]
    - ``cases`` is a list ``[{"case_id": ..., ...}]`` (problem form) -> first match

    Returns ``(present, slice)``. ``present`` is False when a container exists but has no
    entry for ``case_id``, AND when there is NO per-case container at all: a ``per_case``
    predicate resolving a specific case against a container-less (component-flat) diagnostics
    is a genuine shape mismatch, so it must report a structural absence rather than silently
    broadcast the suite-level object to every case (which would evaluate the wrong data)."""
    if not isinstance(diagnostics, dict):
        return (False, None)
    # Consult BOTH container keys, `cases` first: if the preferred container lacks this case,
    # fall through to the other rather than reporting an absence (an off-contract runner might
    # split cases across the two). Returns the first container that actually holds `case_id`.
    for key in ("cases", "per_case"):
        container = diagnostics.get(key)
        if isinstance(container, dict):
            if case_id in container:
                return (True, container[case_id])
        elif isinstance(container, list):
            for item in container:
                if isinstance(item, dict) and item.get("case_id") == case_id:
                    return (True, item)
    return (False, None)


def _apply_op(lhs: Any, op: str, rhs: Any) -> bool:
    if op == "eq":
        return _values_equal(lhs, rhs)
    if op == "ne":
        return not _values_equal(lhs, rhs)
    if op == "includes":
        # membership in a list/tuple only — NOT a str (substring matching would be a
        # silent, surprising semantic; every real use is set membership on a string array
        # such as verdict.failed_checks). Element comparison goes through _values_equal so
        # bool/number stay disjoint (1 does not "include"-match [True]).
        if not isinstance(lhs, (list, tuple)):
            return False
        return any(_values_equal(x, rhs) for x in lhs)
    # ordered numeric comparisons
    if not (_is_number(lhs) and _is_number(rhs)):
        return False
    if op == "le":
        return lhs <= rhs
    if op == "ge":
        return lhs >= rhs
    if op == "lt":
        return lhs < rhs
    if op == "gt":
        return lhs > rhs
    raise PredicateError(f"unknown op: {op}")


def _values_equal(lhs: Any, rhs: Any) -> bool:
    """Equality with light normalization: numbers compare cross-type (1 == 1.0), and
    bools compare only to bools (True != 1) so a boolean check and a numeric metric never
    collide. Strings/enums compare verbatim."""
    if isinstance(lhs, bool) or isinstance(rhs, bool):
        return isinstance(lhs, bool) and isinstance(rhs, bool) and lhs == rhs
    if _is_number(lhs) and _is_number(rhs):
        return lhs == rhs
    return lhs == rhs


def _resolve_value(value: Any, case_id: str | None) -> tuple[bool, Any]:
    """Resolve a condition ``value``. A plain literal returns itself; a per-case map
    ``{"per_case": {<case_id>: v}}`` returns the entry for ``case_id`` (``present`` False
    when the case is not in the map)."""
    if isinstance(value, dict) and set(value.keys()) == {"per_case"}:
        table = value["per_case"]
        if not isinstance(table, dict):
            raise PredicateError("value.per_case must be a map of case_id -> value")
        if case_id is None or case_id not in table:
            return (False, None)
        return (True, table[case_id])
    return (True, value)


def _eval_condition(cond: dict[str, Any], diagnostics: dict[str, Any],
                    target_cases: list[str]) -> tuple[bool, str, dict[str, Any]]:
    """Evaluate one ``pass_when.all`` condition. Returns ``(satisfied, kind, basis)``
    where kind is one of ``_KIND_PASS`` / ``_KIND_PHYSICS`` / ``_KIND_STRUCTURAL``.

    A ``per_case`` condition holds iff it holds for EVERY target case; the first
    non-satisfying case fixes the kind (structural if the ref/case was absent, else
    physics). A ``case: <case_id>`` condition resolves ``ref`` inside that ONE case's
    diagnostics slice — the scope a cross-case reduction (a convergence order, a
    symmetry residual) is read at, since the checks module emits it as a per-case metric
    of the case where it first becomes computable. ``na_allowed`` turns an absent lhs
    into a satisfied condition."""
    ref = cond.get("ref")
    op = cond.get("op")
    # isinstance guards before the frozenset membership so an unhashable (list/map) op raises a
    # PredicateError (caught by _author_execute_verdict) rather than a bare TypeError.
    if not isinstance(ref, str) or not isinstance(op, str) or op not in _OPS:
        raise PredicateError(f"invalid condition: ref={ref!r} op={op!r}")
    per_case = bool(cond.get("per_case"))
    na_allowed = bool(cond.get("na_allowed"))
    value = cond.get("value")
    # Keyed on PRESENCE, not on a non-None value: the schema gate rejects `case: null`, so the
    # evaluator must too. Reading it as "absent" would silently widen the condition to
    # suite-level scope — evaluating different data than the author wrote.
    has_case = "case" in cond
    case_sel = cond.get("case")
    if has_case and (not isinstance(case_sel, str) or not case_sel.strip()):
        raise PredicateError(f"condition `case` must be a non-empty string (got {case_sel!r})")
    if per_case and has_case:
        # `per_case` ranges over EVERY target case; `case` pins ONE. Together they express
        # nothing coherent, and silently honoring one would evaluate a scope the author did
        # not write. The Compile schema gate rejects the pair; this is the evaluator's mirror.
        raise PredicateError(
            f"condition sets both per_case and case={case_sel!r} (mutually exclusive scopes)")

    contexts: list[tuple[str | None, Any, bool]]
    if per_case:
        if not target_cases:
            # A per-case condition with no cases to range over evaluates nothing; treat it as
            # NON-satisfying (structural), never vacuously true. (The Compile schema gate rejects
            # empty target_cases, so this is a defense-in-depth guard for a direct evaluator call
            # on an ungated/edited IR.)
            return (False, _KIND_STRUCTURAL,
                    {"ref": ref, "op": op, "evaluated": [{"reason": "no_target_cases"}]})
        contexts = []
        for cid in target_cases:
            present, sl = _case_slice(diagnostics, cid)
            contexts.append((cid, sl, present))
    elif has_case:
        cid = case_sel.strip()
        present, sl = _case_slice(diagnostics, cid)
        contexts = [(cid, sl, present)]
    else:
        contexts = [(None, diagnostics, True)]

    evaluated: list[dict[str, Any]] = []
    for cid, ctx, case_present in contexts:
        if not case_present:
            basis = {"case": cid, "ref": ref, "op": op, "satisfied": False,
                     "reason": "case_absent"}
            evaluated.append(basis)
            return (False, _KIND_STRUCTURAL, {"ref": ref, "op": op, "evaluated": evaluated})
        lhs_present, lhs = _resolve_predicate_ref(ctx, ref)
        if not lhs_present or lhs is None:
            if na_allowed:
                evaluated.append({"case": cid, "ref": ref, "op": op,
                                  "satisfied": True, "reason": "na_allowed"})
                continue
            evaluated.append({"case": cid, "ref": ref, "op": op, "satisfied": False,
                              "reason": "ref_absent"})
            return (False, _KIND_STRUCTURAL, {"ref": ref, "op": op, "evaluated": evaluated})
        val_present, rhs = _resolve_value(value, cid)
        if not val_present:
            evaluated.append({"case": cid, "ref": ref, "op": op, "satisfied": False,
                              "reason": "value_absent_for_case"})
            return (False, _KIND_STRUCTURAL, {"ref": ref, "op": op, "evaluated": evaluated})
        ok = _apply_op(lhs, op, rhs)
        evaluated.append({"case": cid, "ref": ref, "op": op, "lhs": lhs, "rhs": rhs,
                          "satisfied": ok})
        if not ok:
            return (False, _KIND_PHYSICS, {"ref": ref, "op": op, "evaluated": evaluated})
    return (True, _KIND_PASS, {"ref": ref, "op": op, "evaluated": evaluated})


def _predicate_conditions(pred: dict[str, Any]) -> list[dict[str, Any]]:
    pass_when = pred.get("pass_when")
    if not isinstance(pass_when, dict):
        raise PredicateError("pass_when must be a mapping with an `all` list")
    conds = pass_when.get("all")
    if not isinstance(conds, list) or not conds:
        raise PredicateError("pass_when.all must be a non-empty list")
    return conds


def evaluate_predicate(pred: dict[str, Any],
                       diagnostics: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Evaluate one predicate against ``diagnostics``. Returns
    ``(status, kind, basis)`` where status ∈ {expected_outcome, "fail"} and kind ∈
    {``_KIND_PASS``, ``_KIND_PHYSICS``, ``_KIND_STRUCTURAL``}."""
    expected = str(pred.get("expected_outcome") or "").strip().lower()
    if expected not in _EXPECTED_OUTCOMES:
        raise PredicateError(f"expected_outcome must be one of {sorted(_EXPECTED_OUTCOMES)}")
    target_cases = pred.get("target_cases")
    target_cases = [str(c) for c in target_cases] if isinstance(target_cases, list) else []
    conds = _predicate_conditions(pred)

    cond_bases: list[dict[str, Any]] = []
    fail_kind = _KIND_PASS
    satisfied_all = True
    for cond in conds:
        if not isinstance(cond, dict):
            raise PredicateError("each pass_when.all entry must be a mapping")
        ok, kind, basis = _eval_condition(cond, diagnostics, target_cases)
        cond_bases.append(basis)
        if not ok:
            satisfied_all = False
            # structural dominates physics (a contract gap is the more fundamental defect).
            if fail_kind != _KIND_STRUCTURAL:
                fail_kind = kind
    if satisfied_all:
        return (expected, _KIND_PASS, {"satisfied": True, "conditions": cond_bases})
    return ("fail", fail_kind, {"satisfied": False, "conditions": cond_bases})


def evaluate_verdict(predicates: list[dict[str, Any]], diagnostics: dict[str, Any], *,
                     run_id: str | None = None,
                     node_key: str | None = None) -> dict[str, Any]:
    """Author the deterministic ``verdict.json`` body from the IR predicates + the
    runner's ``diagnostics.json``. Returns a dict with ``per_test`` (one
    ``{test_id, status, basis}`` per predicate, in order), the reduced ``self_verdict``,
    and the ``failure_class`` (``pass`` / ``physics_fail`` / ``structural_violation``).

    The judge leaf no longer authors this — it authors ``semantic_review.json`` only.
    """
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    # An empty predicate set is never a legitimate PASS: a node with no evaluable per-test rule
    # cannot certify. Fail structurally rather than reduce an empty per_test to `pass` (a footgun
    # for any direct caller; the conductor already guards this before calling, and Compile forbids
    # it, so this is defense-in-depth).
    if not isinstance(predicates, list) or not predicates:
        doc: dict[str, Any] = {}
        if node_key is not None:
            doc["node_key"] = node_key
        if run_id is not None:
            doc["run_id"] = run_id
        doc["self_verdict"] = "fail"
        doc["failure_class"] = "structural_violation"
        doc["per_test"] = []
        return doc
    per_test: list[dict[str, Any]] = []
    saw_structural = False
    saw_physics = False
    for pred in predicates:
        if not isinstance(pred, dict):
            raise PredicateError("each test_predicates entry must be a mapping")
        test_id = pred.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            raise PredicateError("test_predicates entry missing a non-empty test_id")
        status, kind, basis = evaluate_predicate(pred, diagnostics)
        if kind == _KIND_STRUCTURAL:
            saw_structural = True
        elif kind == _KIND_PHYSICS:
            saw_physics = True
        per_test.append({"test_id": test_id.strip(), "status": status, "basis": basis})

    counts = {"pass": 0, "fail": 0, "xfail": 0, "skipped": 0, "blocked": 0}
    for item in per_test:
        st = item["status"]
        if st in counts:
            counts[st] += 1

    # self_verdict reduce — identical rule to the post_judge derivation
    # (_author_derived_validate_artifacts): fail if any fail/blocked; else xfail iff every
    # non-skipped entry is xfail; else pass.
    if counts["fail"] > 0 or counts["blocked"] > 0:
        self_verdict = "fail"
    elif counts["xfail"] > 0 and counts["pass"] == 0:
        self_verdict = "xfail"
    else:
        self_verdict = "pass"

    if not saw_structural and not saw_physics:
        failure_class = "pass"
    elif saw_structural:
        failure_class = "structural_violation"
    else:
        failure_class = "physics_fail"

    doc: dict[str, Any] = {}
    if node_key is not None:
        doc["node_key"] = node_key
    if run_id is not None:
        doc["run_id"] = run_id
    doc["self_verdict"] = self_verdict
    doc["failure_class"] = failure_class
    doc["per_test"] = per_test
    return doc


# --------------------------------------------------------------------------- schema

def validate_predicate_schema(
    predicates: Any,
    *,
    case_ids: set[str],
    test_ids: list[str],
    check_ids: set[str],
    verdict_fields: set[str],
    metric_addrs: set[str],
) -> list[str]:
    """Validate the ``io_contract.test_predicates`` DSL at Compile. Returns a list of
    human-readable violation strings (empty == valid). Every violation is prefixed with a
    stable token so the conductor's post-gate classifier can route it.

    Checks: structure + op/outcome enums; the predicate test_id set equals the ``tests.md``
    set (``test_ids``); every ``target_cases`` entry is a declared case; and every ``ref``
    resolves against the declared diagnostics vocabulary — ``verdict.<field>`` against
    ``verdict_fields``, ``checks.<id>...`` against ``check_ids``, and any other head against
    the ``metric_addrs`` per-case addressing pin (``diagnostics_contract.metrics``)."""
    v: list[str] = []
    if not isinstance(predicates, list) or not predicates:
        return ["io_contract.test_predicates must be a non-empty list"]

    seen_ids: list[str] = []
    for idx, pred in enumerate(predicates):
        loc = f"test_predicates[{idx}]"
        if not isinstance(pred, dict):
            v.append(f"{loc} must be a mapping")
            continue
        test_id = pred.get("test_id")
        if not isinstance(test_id, str) or not test_id.strip():
            v.append(f"{loc}.test_id must be a non-empty string")
        else:
            seen_ids.append(test_id.strip())
        outcome = str(pred.get("expected_outcome") or "").strip().lower()
        if outcome not in _EXPECTED_OUTCOMES:
            v.append(f"{loc}.expected_outcome must be one of {sorted(_EXPECTED_OUTCOMES)}")
        targets = pred.get("target_cases")
        if not isinstance(targets, list) or not targets:
            v.append(f"{loc}.target_cases must be a non-empty list")
            targets = []
        for cid in targets:
            if not isinstance(cid, str) or cid not in case_ids:
                v.append(f"{loc}.target_cases references unknown case_id ({cid!r})")
        pass_when = pred.get("pass_when")
        if not isinstance(pass_when, dict) or not isinstance(pass_when.get("all"), list) \
                or not pass_when.get("all"):
            v.append(f"{loc}.pass_when must be a mapping with a non-empty `all` list")
            continue
        for cidx, cond in enumerate(pass_when["all"]):
            cloc = f"{loc}.pass_when.all[{cidx}]"
            if not isinstance(cond, dict):
                v.append(f"{cloc} must be a mapping")
                continue
            ref = cond.get("ref")
            op = cond.get("op")
            if not isinstance(ref, str) or not ref.strip():
                v.append(f"{cloc}.ref must be a non-empty string")
            else:
                v.extend(_check_ref(cloc, ref.strip(), check_ids, verdict_fields, metric_addrs))
            # isinstance guard BEFORE the frozenset membership: a malformed `op` authored as a
            # YAML list/map is unhashable and `op in _OPS` would raise TypeError, crashing the
            # gate instead of reporting an actionable violation for warm-resume repair.
            if not isinstance(op, str) or op not in _OPS:
                v.append(f"{cloc}.op must be one of {sorted(_OPS)}")
            # `case: <case_id>` pins the condition to ONE of the predicate's own target cases.
            # It must name a case this predicate ranges over (a case outside `target_cases` is
            # evidence the predicate does not own — the metrics-basis matrix would not carry it),
            # and it is mutually exclusive with `per_case` (which ranges over all of them).
            if "case" in cond:
                case_sel = cond.get("case")
                # Compare against — and report — the same list: the STRING members of
                # `target_cases`. Stringifying a non-str member only for the message would print
                # the offending case as if it were present ("'123' is not one of ['123', 'c2']").
                # A non-str `target_cases` member is separately reported as an unknown case_id.
                str_targets = [c for c in targets if isinstance(c, str)]
                if not isinstance(case_sel, str) or not case_sel.strip():
                    v.append(f"{cloc}.case must be a non-empty string")
                elif case_sel not in str_targets:
                    v.append(f"{cloc}.case {case_sel!r} is not one of this predicate's "
                             f"target_cases ({sorted(str_targets)})")
                if bool(cond.get("per_case")):
                    v.append(f"{cloc} sets both `per_case` and `case` "
                             "(mutually exclusive condition scopes)")
            if "value" not in cond or cond.get("value") is None:
                # Every op needs a concrete rhs. A condition with no `value` (or an explicit
                # null) would compare against None at execute and permanently fail its test —
                # catch the authoring omission here rather than as an opaque physics_fail.
                v.append(f"{cloc} must have a non-null `value`")
            value = cond.get("value")
            if isinstance(value, dict):
                # The only legal dict form is the per-case threshold map {"per_case": {...}};
                # any other dict is a malformed rhs that would slip through as a literal at
                # execute (e.g. `op: ne` against a dict is vacuously true -> a false pass).
                if set(value.keys()) != {"per_case"}:
                    v.append(f"{cloc}.value must be a scalar/bool/string/list or a "
                             f"{{per_case: {{case_id: value}}}} map")
                else:
                    if not bool(cond.get("per_case")):
                        v.append(f"{cloc} has a per_case value map but per_case is not true")
                    table = value["per_case"]
                    if not isinstance(table, dict) or not table:
                        v.append(f"{cloc}.value.per_case must be a non-empty map")
                    else:
                        for cid in table:
                            if cid not in case_ids:
                                v.append(f"{cloc}.value.per_case references unknown case_id ({cid!r})")
                        # The map must supply a threshold for EVERY target case, else that case
                        # deterministically fails at execute (value_absent_for_case ->
                        # structural_violation) instead of being repaired at Compile.
                        uncovered = sorted(str(c) for c in targets if str(c) not in table)
                        if uncovered:
                            v.append(f"{cloc}.value.per_case is missing a threshold for "
                                     f"target case(s) {uncovered}")
            # An ordered comparison (le/ge/lt/gt) requires a NUMERIC rhs — a YAML string like
            # "1e-10" (or a per-case map of strings) passes the null check but at execute
            # `_apply_op` needs both sides numeric, so it deterministically returns false and a
            # correct run is misreported physics_fail. Catch the wrong-typed threshold here.
            if isinstance(op, str) and op in _ORDERED_OPS and value is not None:
                if isinstance(value, dict) and set(value.keys()) == {"per_case"} \
                        and isinstance(value["per_case"], dict):
                    for cid, tv in value["per_case"].items():
                        if not _is_number(tv):
                            v.append(f"{cloc}.value.per_case[{cid!r}] must be a number for the "
                                     f"ordered op {op!r} (got {tv!r})")
                elif not isinstance(value, dict) and not _is_number(value):
                    v.append(f"{cloc}.value must be a number for the ordered op {op!r} "
                             f"(got {value!r})")

    # test_id set equality with tests.md
    pred_set = set(seen_ids)
    if len(seen_ids) != len(pred_set):
        dups = sorted({t for t in seen_ids if seen_ids.count(t) > 1})
        v.append(f"test_predicates has duplicated test_id ({dups})")
    md_set = set(test_ids)
    missing = sorted(md_set - pred_set)
    extra = sorted(pred_set - md_set)
    if missing:
        v.append(f"test_predicates missing tests from tests.md ({missing})")
    if extra:
        v.append(f"test_predicates has unknown test_id not in tests.md ({extra})")
    return v


def _check_ref(loc: str, ref: str, check_ids: set[str], verdict_fields: set[str],
               metric_addrs: set[str]) -> list[str]:
    """Resolve a predicate ``ref`` head against the declared diagnostics vocabulary."""
    head = ref.split(".", 1)[0]
    if head == "verdict":
        field = ref.split(".", 2)[1] if "." in ref else ""
        if not field:
            return [f"{loc}.ref `verdict` needs a field (e.g. verdict.overall)"]
        if field not in verdict_fields:
            return [f"{loc}.ref verdict.{field} not in diagnostics_contract.verdict.fields "
                    f"({sorted(verdict_fields)})"]
        return []
    if head == "checks":
        parts = ref.split(".")
        cid = parts[1] if len(parts) > 1 else ""
        if not cid or cid not in check_ids:
            return [f"{loc}.ref checks.{cid} not in diagnostics_contract.checks "
                    f"({sorted(check_ids)})"]
        return []
    # Any other head is a per-case metric ADDRESS; the WHOLE ref must be pinned in
    # diagnostics_contract.metrics (the intermediate per-case addressing contract). Exact match,
    # never a head prefix: the renderer emits one metric key per declared address verbatim
    # (`runner_renderer._metrics`) and `_resolve_predicate_ref` looks the ref up as a whole-string
    # key, so a `metrics: ["cfl"]` pin behind a `cfl.max` ref would emit `cfl`, resolve
    # `ref_absent`, and fail every run as a structural_violation. Reject it here, where it is
    # repairable, rather than at execute.
    if ref not in metric_addrs:
        return [f"{loc}.ref {ref} not declared in diagnostics_contract.metrics "
                f"({sorted(metric_addrs)}) — the full dotted address must be pinned verbatim"]
    return []
