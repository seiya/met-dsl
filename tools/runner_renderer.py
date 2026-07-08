"""Host-side renderer for a physics node's runner glue (R1/M3c-β).

Pure-function module (sibling of ``tools/verdict_evaluator.py`` /
``tools/dependency_graph.py``): it takes a compiled IR plus the target/harness
spec ids and returns the text of ``<spec_id>_runner.f90`` — the deterministic
"glue" main program that drives the physics node's ``<spec_id>_checks`` callbacks
and emits the standard runner outputs *through the certified
``harness_fortran_cpu`` plumbing*. Because the harness's v2 interface owns the
JSON envelope assembly and the verdict fold (§3 / §5.1 of the harness
controlled_spec), this renderer holds **no serialization knowledge**: it builds
the harness record types and calls the writers — it never formats a JSON token,
folds a verdict, or excludes an xfail itself.

Split of authorship on an M3c node:
- ``<spec_id>_model.f90`` — the physics kernel + ``__apply`` op   (LLM leaf)
- ``<spec_id>_checks.f90`` — the fixed-ABI check/getter callbacks (LLM leaf)
- ``<spec_id>_runner.f90`` — this renderer                        (host)
- ``src/Makefile``          — ``workflow_conductor._write_makefile`` (host)

The rendered runner ``use``s two modules: ``harness_fortran_cpu_model`` (the
certified plumbing) and ``<spec_id>_checks`` (the leaf's fixed-ABI callbacks,
see ``docs/workflow/CHECKS_MODULE_CONTRACT.md``). It is authored lint-clean
(``use only:``, ``! allow(C003)``, ≤100-column lines) so the deterministic
Generate.lint substep — which lints the whole ``src/`` tree — stays green.

``render_runner`` raises ``RenderError`` (→ transport fail_closed, NOT a Generate
retry) for any IR it cannot faithfully render: an unparseable ``shape_expr``, a
rank>4 snapshot variable, a snapshot variable colliding with a harness-reserved
key, a ``verdict.fields`` outside ``{overall, failed_checks}``, more than one
infrastructure dependency, or an over-long identifier. ``assert_harness_pin``
(the signature pin) is a separate fail-closed guard the conductor runs against
the *certified* harness IR signatures + source before rendering.
"""

from __future__ import annotations

from typing import Any

# The fixed ABI of the leaf-authored `<spec_id>_checks` module (see
# docs/workflow/CHECKS_MODULE_CONTRACT.md). Non-prefixed public names (module
# scope makes them collision-free) so the f2008 63-char identifier limit is not
# exceeded even for a 55-char spec_id.
CHECKS_PUBLIC_NAMES = (
    "case_setup", "case_run", "get_time",
    "get_scalar", "get_r1", "get_r2", "get_r3", "get_r4",
    "checks_compute", "metric_compute",
)

# Harness-owned snapshot keys a physics snapshot variable must not shadow.
HARNESS_RESERVED_SNAPSHOT_KEYS = frozenset({"t", "case_id", "step"})

# Fixed character widths the checks ABI pins (assumed-length intent(out) is
# disallowed, so both sides declare these exact widths).
CHECK_ID_WIDTH = 32
CHECK_STATUS_WIDTH = 4

# f2008 identifier limit; the longest derived name is `<spec_id>_checks` /
# `<spec_id>_runner` (spec_id + 7). Keep a margin: cap spec_id at 55.
MAX_SPEC_ID_LEN = 55
MAX_IDENTIFIER_LEN = 63


def spec_id_length_violation(spec_id: Any) -> str | None:
    """Spec-input bound on spec_id length — the M3d mass-opt-in prerequisite gate.

    Returns an actionable violation message when ``spec_id`` exceeds
    ``MAX_SPEC_ID_LEN``, else ``None``. On a make+fortran node the derived
    ``<spec_id>_runner``/``_checks``/``_model`` identifiers (spec_id + 7) breach the
    f2008 ``MAX_IDENTIFIER_LEN``-char limit, and on a harness-backed M3c node the
    host-rendered runner additionally fail-closes at ``_check_identifier_lengths``
    (a workflow-kill a compile.generate re-author cannot repair — the spec_id is
    node IDENTITY, not authored IR content). This helper is the canonical
    *spec-input* capture point for exactly that identity precondition, which the
    compile.static hoist deliberately excludes: bounding here — before any phase
    runs — turns an unrepairable late render-kill into an early, clear rejection.
    The renderer keeps the same bound as a defense-in-depth backstop."""
    sid = spec_id.strip() if isinstance(spec_id, str) else ""
    if len(sid) > MAX_SPEC_ID_LEN:
        return (
            f"spec_id {sid!r} is {len(sid)} chars (>{MAX_SPEC_ID_LEN}); the derived "
            f"`<spec_id>_runner`/`_checks`/`_model` identifiers would breach the f2008 "
            f"{MAX_IDENTIFIER_LEN}-char limit (and fail-close a harness-backed node's "
            f"host-render). Rename the spec to ≤{MAX_SPEC_ID_LEN} chars.")
    return None


# The deterministic Generate.lint column limit (fortitude S001). The rendered runner must stay
# within it because it is host-authored (a leaf cannot edit it to fix an overlong line).
MAX_RENDERED_LINE = 100

# The harness symbols this template calls (pinned by assert_harness_pin against
# the certified harness IR). Emitters are added per-rank on demand.
_HARNESS_TYPES = (
    "h_named", "h_check", "h_metric", "h_case_result", "h_mb_entry",
)
_HARNESS_CORE_OPS = (
    "parse_cases", "box", "write_snapshot",
    "write_metrics_basis", "write_diagnostics", "write_perf",
)


class RenderError(RuntimeError):
    """A physics IR that cannot be faithfully rendered into runner glue.

    Raised for a structural impossibility (bad shape_expr, rank>4, reserved-key
    collision, unsupported verdict.fields, >1 infra dep, over-long identifier).
    The conductor routes it to transport fail_closed — it is a spec/IR defect,
    not content a Generate retry could repair by re-authoring the model.

    ``identity=True`` marks the subset whose offending value is the node's IDENTITY
    (spec_id / a derived module-name length, or >1 infra dep) rather than authored IR
    *content*. Re-authoring the IR cannot repair an identity defect, so the compile.static
    mirror (``ir_content_violations``) excludes it — it belongs to spec-input validation,
    NOT a compile.generate warm-resume retry. Every other RenderError (``identity=False``) is
    Compile-authored content the compile gate hoists so a defect routes to compile.generate
    instead of killing the workflow at conductor render time."""

    def __init__(self, message: str, *, identity: bool = False) -> None:
        super().__init__(message)
        self.identity = identity


# --- IR extraction helpers (all defensive: tolerate missing/mistyped nodes) ---


def _dget(node: Any, key: str, default: Any = None) -> Any:
    return node.get(key, default) if isinstance(node, dict) else default


def _rank_of_shape(shape_expr: Any, var: str) -> int:
    """Rank (0..4) of a snapshot variable's ``shape_expr`` (``"scalar"`` or
    ``"[d1, d2, ...]"``). Raises RenderError for an unparseable form or rank>4."""
    if not isinstance(shape_expr, str) or not shape_expr.strip():
        raise RenderError(f"snapshot variable {var!r} has no shape_expr")
    s = shape_expr.strip()
    if s.lower() == "scalar":
        return 0
    if not (s.startswith("[") and s.endswith("]")):
        raise RenderError(
            f"snapshot variable {var!r} shape_expr {shape_expr!r} is neither "
            "'scalar' nor a '[...]' array shape")
    inner = s[1:-1].strip()
    if not inner:
        raise RenderError(
            f"snapshot variable {var!r} shape_expr {shape_expr!r} has empty dimensions")
    rank = len([d for d in inner.split(",") if d.strip()])
    if rank < 1 or rank > 4:
        raise RenderError(
            f"snapshot variable {var!r} shape_expr {shape_expr!r} has rank {rank} "
            "(the harness emitters cover rank 1..4 only)")
    return rank


def _snapshot_schema(ir: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return ``({var_name: shape_expr}, time_variable)`` from
    ``io_contract.raw_requirements.required_evidence[state_snapshots].schema``.

    Same section `_author_snapshot_schema` (workflow_conductor) reads."""
    io = _dget(ir, "io_contract", {})
    rr = _dget(io, "raw_requirements", {})
    entry = None
    for e in _dget(rr, "required_evidence", []) or []:
        if isinstance(e, dict) and e.get("artifact") == "state_snapshots":
            entry = e
            break
    if entry is None:
        raise RenderError(
            "IR io_contract has no state_snapshots required_evidence entry "
            "(a rendered runner needs the snapshot schema to emit per-case state)")
    schema = _dget(entry, "schema", {})
    variables: dict[str, str] = {}
    for v in _dget(schema, "variables", []) or []:
        if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip():
            variables[v["name"].strip()] = v.get("shape_expr")
    if not variables:
        raise RenderError("state_snapshots schema declares no variables")
    time_var = schema.get("time_variable")
    time_var = time_var.strip() if isinstance(time_var, str) and time_var.strip() else "t"
    for name in variables:
        if name in HARNESS_RESERVED_SNAPSHOT_KEYS:
            raise RenderError(
                f"snapshot variable {name!r} collides with a harness-reserved key "
                f"{sorted(HARNESS_RESERVED_SNAPSHOT_KEYS)}")
    return variables, time_var


def _case_ids(ir: dict[str, Any]) -> list[str]:
    case = _dget(ir, "case", {})
    out: list[str] = []
    for c in _dget(case, "test_case_set", []) or []:
        cid = _dget(c, "case_id")
        if isinstance(cid, str) and cid.strip():
            out.append(cid.strip())
    if not out:
        raise RenderError("IR case.test_case_set is empty (no cases to run)")
    # Duplicate case_ids would render two identical `case ('id')` labels in the runner's
    # `select case`, a hard gfortran error the leaf cannot repair (host-rendered runner).
    # Fail closed rather than emit a non-compiling, unrepairable runner.
    dups = sorted({c for c in out if out.count(c) > 1})
    if dups:
        raise RenderError(
            f"IR case.test_case_set has duplicate case_id(s) {dups}; the runner's "
            "select-case would emit overlapping case labels that do not compile")
    return out


def _test_predicates(ir: dict[str, Any]) -> list[dict[str, Any]]:
    io = _dget(ir, "io_contract", {})
    return [p for p in (_dget(io, "test_predicates", []) or []) if isinstance(p, dict)]


def _xfail_cases(ir: dict[str, Any]) -> set[str]:
    """Case ids whose failure is expected (targeted by an ``xfail`` predicate)."""
    xfail: set[str] = set()
    for p in _test_predicates(ir):
        if str(p.get("expected_outcome") or "").strip().lower() == "xfail":
            for tc in p.get("target_cases") or []:
                if isinstance(tc, str) and tc.strip():
                    xfail.add(tc.strip())
    return xfail


def _target_cases(ir: dict[str, Any], test_id: str) -> list[str]:
    """All distinct case ids targeted by the predicate(s) for ``test_id``, in
    declaration order. Used to fail-close (rather than silently record partial
    evidence) when a metrics-basis test targets more than one case."""
    seen: list[str] = []
    for p in _test_predicates(ir):
        if str(p.get("test_id") or "").strip() == test_id:
            for tc in p.get("target_cases") or []:
                if isinstance(tc, str) and tc.strip() and tc.strip() not in seen:
                    seen.append(tc.strip())
    return seen


def _per_case_vars(ir: dict[str, Any], schema_vars: dict[str, str]) -> dict[str, list[str]]:
    """Map each case_id to the ordered snapshot variables it must emit — the
    union of ``required_raw_variables`` over the tests targeting that case,
    ordered by the snapshot schema declaration order (stable JSON key order)."""
    io = _dget(ir, "io_contract", {})
    req_by_test: dict[str, list[str]] = {}
    for r in _dget(io, "test_evidence_requirements", []) or []:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("test_id") or "").strip()
        if tid:
            req_by_test[tid] = [
                v.strip() for v in (r.get("required_raw_variables") or [])
                if isinstance(v, str) and v.strip()
            ]
    schema_order = list(schema_vars)
    per_case: dict[str, set[str]] = {}
    for p in _test_predicates(ir):
        tid = str(p.get("test_id") or "").strip()
        needed = set(req_by_test.get(tid, []))
        for tc in p.get("target_cases") or []:
            if isinstance(tc, str) and tc.strip():
                per_case.setdefault(tc.strip(), set()).update(needed)
    out: dict[str, list[str]] = {}
    for cid in _case_ids(ir):
        want = per_case.get(cid, set())
        missing = [v for v in want if v not in schema_vars]
        if missing:
            raise RenderError(
                f"case {cid!r} requires raw variables {sorted(missing)} absent from the "
                "state_snapshots schema (required_raw_variables must be snapshot variables)")
        out[cid] = [v for v in schema_order if v in want]
    return out


def _checks(ir: dict[str, Any]) -> list[str]:
    io = _dget(ir, "io_contract", {})
    dc = _dget(io, "diagnostics_contract", {})
    ids: list[str] = []
    for c in _dget(dc, "checks", []) or []:
        cid = _dget(c, "id")
        if isinstance(cid, str) and cid.strip():
            ids.append(cid.strip())
    if not ids:
        raise RenderError("IR diagnostics_contract declares no checks")
    return ids


def _metrics(ir: dict[str, Any]) -> list[str]:
    """Dotted metric addresses from ``diagnostics_contract.metrics`` (may be empty)."""
    io = _dget(ir, "io_contract", {})
    dc = _dget(io, "diagnostics_contract", {})
    out: list[str] = []
    for m in _dget(dc, "metrics", []) or []:
        if isinstance(m, str) and m.strip():
            out.append(m.strip())
        elif isinstance(m, dict):
            addr = m.get("address") or m.get("name") or m.get("id")
            if isinstance(addr, str) and addr.strip():
                out.append(addr.strip())
    return out


def _verify_verdict_fields(ir: dict[str, Any]) -> None:
    io = _dget(ir, "io_contract", {})
    dc = _dget(io, "diagnostics_contract", {})
    verdict = _dget(dc, "verdict", {})
    fields = _dget(verdict, "fields", []) or []
    allowed = {"overall", "failed_checks"}
    extra = {str(f).strip() for f in fields} - allowed
    if extra:
        raise RenderError(
            f"diagnostics_contract.verdict.fields {sorted(extra)} outside the harness "
            f"v2 fold surface {sorted(allowed)} — the rendered glue only builds "
            "overall/failed_checks records")


def _test_evidence(ir: dict[str, Any]) -> list[tuple[str, list[str]]]:
    io = _dget(ir, "io_contract", {})
    out: list[tuple[str, list[str]]] = []
    for r in _dget(io, "test_evidence_requirements", []) or []:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("test_id") or "").strip()
        if not tid:
            continue
        vs = [v.strip() for v in (r.get("required_raw_variables") or [])
              if isinstance(v, str) and v.strip()]
        out.append((tid, vs))
    if not out:
        raise RenderError("IR io_contract.test_evidence_requirements is empty")
    return out


def _target_class(ir: dict[str, Any]) -> str:
    impl = _dget(ir, "impl_defaults", {})
    target = _dget(impl, "target", {})
    cls = target.get("class") if isinstance(target, dict) else None
    return cls.strip() if isinstance(cls, str) and cls.strip() else "cpu"


def _threads(ir: dict[str, Any]) -> int:
    impl = _dget(ir, "impl_defaults", {})
    ov = _dget(impl, "backend_overrides", {})
    omp = _dget(ov, "openmp", {})
    n = omp.get("num_threads") if isinstance(omp, dict) else None
    try:
        return max(1, int(n))
    except (TypeError, ValueError):
        return 1


def _infra_dep_count(ir: dict[str, Any]) -> int:
    dep = _dget(ir, "dependency", {})
    count = 0
    for d in _dget(dep, "direct_deps", []) or []:
        nk = _dget(d, "node_key") if isinstance(d, dict) else (d if isinstance(d, str) else None)
        if isinstance(nk, str) and nk.split("/", 1)[0].strip() == "infrastructure":
            count += 1
    return count


# --- code assembly ------------------------------------------------------------


def _hname(harness_spec_id: str, sym: str) -> str:
    return f"{harness_spec_id}__{sym}"


def _flit(value: str) -> str:
    """Escape a string for embedding inside a single-quoted Fortran character literal.

    IR-sourced names (case_ids, snapshot variable names, metric addresses, test_ids)
    are only required to be non-empty by the compile gates, not to be Fortran
    identifiers, so a name containing a `'` would otherwise break the generated literal
    (`case ('a'b')`). Fortran escapes an embedded apostrophe by doubling it. A control
    character (newline/tab) cannot appear in a literal at all, so it is fail-closed."""
    if any(ord(ch) < 0x20 for ch in value):
        raise RenderError(
            f"value {value!r} contains a control character and cannot be embedded in the "
            "generated Fortran source")
    return value.replace("'", "''")


def _ranks_used(ir: dict[str, Any]) -> set[int]:
    """The snapshot-variable ranks (0..4) that actually appear across the cases —
    so both the renderer and the signature pin agree on which emitters/getters
    the glue depends on."""
    schema_vars, _ = _snapshot_schema(ir)
    per_case = _per_case_vars(ir, schema_vars)
    ranks: set[int] = set()
    for vs in per_case.values():
        for v in vs:
            ranks.add(_rank_of_shape(schema_vars[v], v))
    return ranks


def _used_harness_ops(ir: dict[str, Any]) -> list[str]:
    """Unqualified harness op names the rendered glue calls (deterministic order):
    the core writers/plumbing plus only the emitters for the ranks in use."""
    ranks = _ranks_used(ir)
    ops = ["parse_cases"]
    if 0 in ranks:
        ops.append("emit_real")
    ops += [f"emit_array_r{r}" for r in sorted(r for r in ranks if r >= 1)]
    ops += ["box", "write_snapshot", "write_metrics_basis",
            "write_diagnostics", "write_perf"]
    return ops


def _check_identifier_lengths(spec_id: str, harness_spec_id: str) -> None:
    # These are node-IDENTITY defects (a re-author cannot shorten the spec_id / harness id),
    # so they are `identity=True`: the compile.static mirror excludes them (spec-input concern).
    if len(spec_id) > MAX_SPEC_ID_LEN:
        raise RenderError(
            f"spec_id {spec_id!r} is {len(spec_id)} chars (>{MAX_SPEC_ID_LEN}); "
            "the derived `<spec_id>_checks`/`_runner` identifiers would risk the "
            f"f2008 {MAX_IDENTIFIER_LEN}-char limit", identity=True)
    for derived in (f"{spec_id}_runner", f"{spec_id}_checks", f"{spec_id}_model"):
        if len(derived) > MAX_IDENTIFIER_LEN:
            raise RenderError(
                f"identifier {derived!r} is {len(derived)} chars (>{MAX_IDENTIFIER_LEN})",
                identity=True)
    for sym in (*_HARNESS_TYPES, *_HARNESS_CORE_OPS):
        name = _hname(harness_spec_id, sym)
        if len(name) > MAX_IDENTIFIER_LEN:
            raise RenderError(
                f"harness identifier {name!r} is {len(name)} chars (>{MAX_IDENTIFIER_LEN})",
                identity=True)


def ir_content_violations(ir: dict[str, Any], spec_id: str, harness_spec_id: str) -> list[str]:
    """The Compile-authored render preconditions of an M3c physics node's host-rendered runner,
    as a list of human-readable messages (``[]`` when the IR renders, or when the only defect is
    a node-identity one this deliberately excludes — see below).

    The ``compile.static`` gate (``validate_pipeline_semantics._validate_harness_render_pre
    conditions``) calls this so a defect in an M3c node's IR routes back to ``compile.generate``
    (a cheap warm re-author) instead of surfacing only as ``render_runner``'s Generate-time
    fail_closed — which, running inside the conductor's host render, kills the whole workflow
    rather than retrying (the E2E #3 ``time_variable`` failure class: a Compile-authored value ×
    a renderer fail-close at an unrecoverable position).

    It is an EXACT mirror by construction: it invokes ``render_runner`` itself (a pure,
    side-effect-free, deterministic ~ms render) with the SAME ``(ir, spec_id, harness_spec_id)``
    the conductor's ``_write_runner`` passes, and reports whatever ``RenderError`` the render
    raises. No hand-maintained list of preconditions to drift out of sync — every current and
    future content fail-close (reserved-key collision, rank>4, verdict.fields, a control char in
    an IR name, an over-100-column rendered line, …) is caught here the instant the renderer
    rejects it.

    It EXCLUDES only ``RenderError``s flagged ``identity=True`` — the node-identity defects a
    re-author cannot repair (``_check_identifier_lengths``: spec_id/derived-name length; and >1
    infra dep, itself unreachable for an M3c node, which has exactly one infra dep by
    construction). Those belong to spec-input validation, NOT a compile.generate retry; they
    remain ``render_runner`` fail-closes as a backstop (see the module docstring)."""
    try:
        render_runner(ir, spec_id, harness_spec_id)
    except RenderError as exc:
        return [] if exc.identity else [str(exc)]
    except Exception as exc:  # noqa: BLE001
        # `render_runner`'s contract is "bad IR -> RenderError", but a truthy non-iterable IR
        # field (e.g. `verdict.fields: 5`, where `_dget(...) or []` yields `5` and `for f in 5`
        # raises TypeError) can still escape as a bare exception. Running INSIDE the compile
        # validator, an uncaught exception would abort `_validate_compile_stage_impl` and discard
        # every violation the sibling gates already collected, replacing the actionable list with
        # a renderer-internals traceback. Convert it to a violation instead: the IR is unrenderable
        # either way, so it routes to compile.generate with an intelligible message.
        return [f"IR is not renderable ({type(exc).__name__}: {exc})"]
    return []


def render_runner(ir: dict[str, Any], spec_id: str, harness_spec_id: str) -> str:
    """Render ``<spec_id>_runner.f90`` from the IR alone. Deterministic and pure.

    ``harness_spec_id`` is the certified plumbing module's spec_id
    (``harness_fortran_cpu``). See module docstring for the render-error matrix.
    The returned text is the complete Fortran source (trailing newline included).
    """
    # `spec_id`/`harness_spec_id` empty and IR-not-a-mapping are node-identity/caller defects,
    # not authored content — flag identity so the compile.static mirror excludes them.
    if not isinstance(ir, dict):
        raise RenderError("IR is not a mapping", identity=True)
    spec_id = (spec_id or "").strip()
    harness_spec_id = (harness_spec_id or "").strip()
    if not spec_id:
        raise RenderError("spec_id is empty", identity=True)
    if not harness_spec_id:
        raise RenderError("harness_spec_id is empty", identity=True)
    _check_identifier_lengths(spec_id, harness_spec_id)

    infra = _infra_dep_count(ir)
    if infra > 1:
        raise RenderError(
            f"node declares {infra} infrastructure dependencies; an M3c node depends on "
            "exactly one harness (the runner glue is rendered against a single plumbing "
            "surface)", identity=True)

    # Everything from here down is Compile-authored IR *content*: any RenderError it raises is
    # `identity=False`, so `ir_content_violations` (which invokes this function) surfaces it at
    # compile.static and routes the defect to compile.generate instead of this workflow-killing
    # render. No mirroring to maintain — the gate runs THIS code.
    schema_vars, time_var = _snapshot_schema(ir)
    # The certified harness writes the per-case snapshot time under the FIXED key `t`
    # (harness controlled_spec §2/§3: `__write_snapshot(case_id, values, time)` takes a time
    # value, not a name). A physics IR that declares a different `time_variable` cannot be
    # honored by the harness — the emitted snapshot would carry `t` while the run contract
    # expects the declared name — so fail closed rather than silently render a mismatch.
    if time_var != "t":
        raise RenderError(
            f"snapshot time_variable is {time_var!r}, but the harness writes the snapshot time "
            "under the fixed key 't' (harness __write_snapshot takes a time value, not a name); "
            "declare time_variable: t for a harness-backed node")
    _verify_verdict_fields(ir)
    case_ids = _case_ids(ir)
    per_case = _per_case_vars(ir, schema_vars)
    xfail = _xfail_cases(ir)
    checks = _checks(ir)
    metrics = _metrics(ir)
    evidence = _test_evidence(ir)
    target_class = _target_class(ir)
    threads = _threads(ir)

    # ranks actually used across every case that emits, so we import/declare only
    # the emitters and buffers we need (unused `use only`/vars would trip lint).
    ranks_used: set[int] = set()
    for cid in case_ids:
        for v in per_case.get(cid, []):
            ranks_used.add(_rank_of_shape(schema_vars[v], v))
    has_scalar = 0 in ranks_used
    array_ranks = sorted(r for r in ranks_used if r >= 1)

    H = lambda sym: _hname(harness_spec_id, sym)  # noqa: E731 (local shorthand)

    # ---- module use lists ----
    emit_ops = (["emit_real"] if has_scalar else []) + [f"emit_array_r{r}" for r in array_ranks]
    harness_syms = [
        *[f"{H(t)}" for t in _HARNESS_TYPES],
        H("parse_cases"),
        *[H(op) for op in emit_ops],
        H("box"),
        H("write_snapshot"),
        H("write_metrics_basis"),
        H("write_diagnostics"),
        H("write_perf"),
    ]
    checks_syms = ["case_setup", "case_run", "get_time"]
    if has_scalar:
        checks_syms.append("get_scalar")
    checks_syms += [f"get_r{r}" for r in array_ranks]
    checks_syms.append("checks_compute")
    if metrics:  # metric_compute is only called when the node declares metrics
        checks_syms.append("metric_compute")

    lines: list[str] = []
    a = lines.append

    a("! Deterministic runner glue authored host-side by the conductor (R1/M3c).")
    a("! It drives the physics node's <spec_id>_checks callbacks and emits the standard")
    a("! runner outputs THROUGH the certified harness_fortran_cpu plumbing, which owns all")
    a("! JSON assembly and the verdict fold (harness controlled_spec §3/§5.1). This glue")
    a("! holds no serialization knowledge; it builds harness records and calls the writers.")
    a(f"program {spec_id}_runner")
    a("  use, intrinsic :: iso_fortran_env, only: real64, int64, error_unit")
    a(f"  use {harness_spec_id}_model, only: &")
    for i, sym in enumerate(harness_syms):
        sep = ", &" if i < len(harness_syms) - 1 else ""
        a(f"    {sym}{sep}")
    a(f"  use {spec_id}_checks, only: &")
    for i, sym in enumerate(checks_syms):
        sep = ", &" if i < len(checks_syms) - 1 else ""
        a(f"    {sym}{sep}")
    a("  ! allow(C003)")
    a("  implicit none")
    a("")
    a("  integer, parameter :: dp = real64")
    a("  integer, parameter :: case_id_len = 64")
    a(f"  integer, parameter :: ncheck_max = {len(checks)}")
    a("")
    a("  integer :: nargs, i, ci, ic, ln, ncases")
    a("  logical :: ok, setup_ok, run_ok")
    a("  character(len=512), allocatable :: tokens(:)")
    a("  character(len=case_id_len), allocatable :: case_ids(:)")
    a("")
    a("  integer(int64) :: clock0, clock1, clock_rate")
    a("  real(dp) :: walltime, tval")
    a("  integer :: steps_total, cells_total, steps_c, cells_c")
    a("")
    a(f"  type({H('h_case_result')}), allocatable :: results(:)")
    a(f"  type({H('h_mb_entry')}), allocatable :: mb_entries(:)")
    a(f"  type({H('h_mb_entry')}), allocatable :: snap_cache(:)")
    a(f"  type({H('h_named')}), allocatable :: vals(:), sel(:)")
    a(f"  type({H('h_check')}), allocatable :: case_checks(:)")
    if metrics:  # case_metrics is only referenced under the per-case metric block
        a(f"  type({H('h_metric')}), allocatable :: case_metrics(:)")
    a("")
    a("  integer :: ncheck_out")
    a(f"  character(len={CHECK_ID_WIDTH}) :: chk_ids(ncheck_max)")
    a(f"  character(len={CHECK_STATUS_WIDTH}) :: chk_status(ncheck_max)")
    if has_scalar:
        a("  real(dp) :: sval")
    for r in array_ranks:
        dims = ",".join(":" for _ in range(r))
        a(f"  real(dp), allocatable :: r{r}buf({dims})")
    a("  logical :: gfound")
    if metrics:
        a("  integer :: mcount, tci")
        a("  real(dp) :: mval")
        a("  logical :: mis_na, mfound")
        a("  character(len=:), allocatable :: mreason")
    else:
        a("  integer :: tci")
    a("")
    # ---- argv marshal + parse ----
    a("  ! --- read argv and parse the case set (--cases <spec> <case_id>...) --------")
    a("  nargs = command_argument_count()")
    a("  allocate(tokens(max(nargs, 1)))")
    a("  do i = 1, nargs")
    a("    call get_command_argument(i, tokens(i), length=ln)")
    a("  end do")
    a("  allocate(case_ids(max(nargs, 1)))")
    a(f"  call {H('parse_cases')}(tokens, nargs, case_ids, ncases, ok)")
    a("  if (.not. ok) then")
    a("    write(error_unit, '(A)') 'error: --cases <spec> <case_id>... required'")
    a("    error stop 1")
    a("  end if")
    a("")
    a("  allocate(results(ncases))")
    a("  allocate(snap_cache(ncases))")
    a("  steps_total = 0")
    a("  cells_total = 0")
    a("  call system_clock(count=clock0, count_rate=clock_rate)")
    a("")
    # ---- per-case loop ----
    a("  do ci = 1, ncases")
    a("    call case_setup(trim(case_ids(ci)), setup_ok)")
    a("    call case_run(trim(case_ids(ci)), steps_c, cells_c, run_ok)")
    a("    steps_total = steps_total + steps_c")
    a("    cells_total = cells_total + cells_c")
    a("    call get_time(tval)")
    a("")
    a("    ! --- per-case snapshot state (emit only this case's required variables) ---")
    a("    select case (trim(case_ids(ci)))")
    for cid in case_ids:
        vs = per_case.get(cid, [])
        a(f"    case ('{_flit(cid)}')")
        a(f"      allocate(vals({len(vs)}))")
        for k, v in enumerate(vs, start=1):
            rank = _rank_of_shape(schema_vars[v], v)
            vlit = _flit(v)
            if rank == 0:
                a(f"      call get_scalar('{vlit}', sval, gfound)")
                a(f"      vals({k}) = {H('box')}('{vlit}', &")
                a(f"        {H('emit_real')}(sval))")
            else:
                a(f"      call get_r{rank}('{vlit}', r{rank}buf, gfound)")
                a(f"      vals({k}) = {H('box')}('{vlit}', &")
                a(f"        {H(f'emit_array_r{rank}')}(r{rank}buf))")
    a("    case default")
    a("      allocate(vals(0))")
    a("    end select")
    a(f"    call {H('write_snapshot')}(trim(case_ids(ci)), vals, tval)")
    a("    snap_cache(ci)%test_id = trim(case_ids(ci))")
    a("    snap_cache(ci)%values = vals")
    a("    deallocate(vals)")
    a("")
    a("    ! --- honest per-case check results (xfail adjustment is the harness fold) ---")
    a("    call checks_compute(trim(case_ids(ci)), ncheck_out, chk_ids, chk_status)")
    a("    allocate(case_checks(ncheck_out))")
    a("    do ic = 1, ncheck_out")
    a("      case_checks(ic)%id = trim(chk_ids(ic))")
    a("      case_checks(ic)%status = chk_status(ic)")
    a("    end do")
    a("")
    if metrics:
        a("    ! --- per-case metric leaves (dotted addresses; NA carried honestly) ---")
        a(f"    allocate(case_metrics({len(metrics)}))")
        a("    mcount = 0")
        for m in metrics:
            mlit = _flit(m)
            # Wrapped: a dotted metric address makes the single-line form exceed the 100-col
            # lint limit at ~23 chars, so the address sits on the header and the out-args wrap.
            a(f"    call metric_compute(trim(case_ids(ci)), '{mlit}', &")
            a("      mval, mis_na, mreason, mfound)")
            a("    if (mfound) then")
            a("      mcount = mcount + 1")
            a(f"      case_metrics(mcount)%name = '{mlit}'")
            a("      case_metrics(mcount)%value = mval")
            a("      case_metrics(mcount)%is_na = mis_na")
            a("      if (mis_na) then")
            a("        case_metrics(mcount)%reason_na = trim(mreason)")
            a("      else")
            a("        case_metrics(mcount)%reason_na = ''")
            a("      end if")
            a("    end if")
        a("")
    a("    results(ci)%case_id = trim(case_ids(ci))")
    a(f"    results(ci)%expected_xfail = {_xfail_expr(case_ids, xfail)}")
    a("    results(ci)%checks = case_checks")
    a("    deallocate(case_checks)")
    if metrics:
        a("    results(ci)%metrics = case_metrics(1:mcount)")
        a("    deallocate(case_metrics)")
    else:
        a("    allocate(results(ci)%metrics(0))")
    a("  end do")
    a("")
    a("  call system_clock(count=clock1)")
    a("  walltime = real(clock1 - clock0, dp) / real(clock_rate, dp)")
    a("  if (walltime <= 0.0_dp) walltime = 1.0e-9_dp")
    a("")
    # ---- metrics-basis: one per-test entry, sourced from that test's FIRST target case.
    # This is a deliberate M3c-β scope choice (the plan pins it): every current M3c test is
    # 1:1 case↔test (the harness self-test, boundary_2d_periodic_copy), so the first target
    # case IS the test's case. A multi-target-case test (a convergence/resolution sweep whose
    # per-test evidence needs EVERY targeted case, e.g. error at nx=32 AND nx=64) is an R3
    # test-kind that this renderer does not yet serve — it would record only the first case's
    # evidence. R3 (property/mms/convergence, docs/design/workflow_scaling_redesign.md) extends
    # the metrics-basis shape before those specs land; until then M3c nodes are single-case
    # per test. See docs/workflow/CHECKS_MODULE_CONTRACT.md §2 (metrics-basis scope note).
    a("  ! --- per-test metrics-basis entries (first target case's primary evidence) ---")
    a(f"  allocate(mb_entries({len(evidence)}))")
    for k, (tid, req_vars) in enumerate(evidence, start=1):
        tcases = _target_cases(ir, tid)
        if not tcases:
            raise RenderError(
                f"test {tid!r} in test_evidence_requirements has no target case in "
                "any test predicate (cannot resolve its metrics-basis source case)")
        if len(tcases) > 1:
            # Fail closed rather than silently record only the first case: a multi-target
            # metrics-basis test needs EVERY targeted case's evidence, which this renderer
            # does not yet serve (see the scope note above + CHECKS_MODULE_CONTRACT.md §2).
            raise RenderError(
                f"test {tid!r} targets {len(tcases)} cases {tcases} but this renderer "
                "records per-test metrics-basis evidence from a single case only "
                "(M3c-β single-case-per-test scope; multi-target convergence/resolution "
                "sweeps are an R3 test-kind). Refusing to emit partial evidence.")
        tcase = tcases[0]
        for rv in req_vars:
            if rv not in schema_vars:
                raise RenderError(
                    f"test {tid!r} required_raw_variable {rv!r} is not a snapshot variable")
        # Wrap the case-id-bearing lines so a long case_id cannot exceed the 100-col lint limit.
        a("  tci = find_case_index(case_ids, ncases, &")
        a(f"    '{_flit(tcase)}')")
        a("  if (tci < 1) then")
        a("    write(error_unit, '(A)') 'error: target case not run: ' // &")
        a(f"      '{_flit(tcase)}'")
        a("    error stop 1")
        a("  end if")
        a(f"  mb_entries({k})%test_id = '{_flit(tid)}'")
        a(f"  allocate(sel({len(req_vars)}))")
        for j, rv in enumerate(req_vars, start=1):
            a(f"  sel({j}) = pick(snap_cache(tci)%values, '{_flit(rv)}')")
        a(f"  mb_entries({k})%values = sel")
        a("  deallocate(sel)")
    a("")
    a("  ! --- emit the run outputs (harness owns every envelope + the fold) ----------")
    a(f"  call {H('write_metrics_basis')}(mb_entries, {len(evidence)})")
    a(f"  call {H('write_diagnostics')}(results, ncases)")
    a(f"  call {H('write_perf')}(trim(case_ids(ncases)), '{_flit(target_class)}', &")
    a(f"    steps_total, cells_total, walltime, 1, {threads}, 0)")
    a("")
    a("contains")
    a("")
    a("  ! Index of `target` in the parsed case list, or -1 when absent.")
    a("  function find_case_index(ids, n, target) result(idx)")
    a("    character(len=*), intent(in) :: ids(:)")
    a("    integer, intent(in) :: n")
    a("    character(len=*), intent(in) :: target")
    a("    integer :: idx, k")
    a("    idx = -1")
    a("    do k = 1, n")
    a("      if (trim(ids(k)) == target) then")
    a("        idx = k")
    a("        return")
    a("      end if")
    a("    end do")
    a("  end function find_case_index")
    a("")
    a("  ! The boxed value named `name` from a case's cached snapshot values.")
    a(f"  function pick(vals_in, name) result(nv)")
    a(f"    type({H('h_named')}), intent(in) :: vals_in(:)")
    a("    character(len=*), intent(in) :: name")
    a(f"    type({H('h_named')}) :: nv")
    a("    integer :: k")
    a("    do k = 1, size(vals_in)")
    a("      if (trim(vals_in(k)%name) == name) then")
    a("        nv = vals_in(k)")
    a("        return")
    a("      end if")
    a("    end do")
    a("    write(error_unit, '(A)') 'error: raw variable '//trim(name)//' absent from snapshot'")
    a("    error stop 1")
    a("  end function pick")
    a("")
    a(f"end program {spec_id}_runner")
    # Safety net: the runner is host-rendered (NOT in the leaf's allowed_output_paths), so a
    # line over the deterministic Generate.lint column limit (fortitude S001, 100 cols) would
    # be an UNREPAIRABLE wedge (no leaf can edit a host file). The hot lines above are wrapped,
    # but an extreme IR-sourced name (a very long metric address / case_id / variable) could
    # still overflow a line we did not wrap — fail closed HERE (a clean RenderError → transport
    # fail_closed with an actionable message) rather than let it surface as a lint wedge.
    # A few `lines` entries embed their own `&` continuations (the multi-line `_xfail_expr`),
    # so measure per PHYSICAL line (split on embedded newlines) — measuring the joined entry
    # would false-fail a valid render whose wrapped physical lines are each within the limit.
    for entry in lines:
        for ln in entry.split("\n"):
            if len(ln) > MAX_RENDERED_LINE:
                raise RenderError(
                    f"rendered runner line exceeds {MAX_RENDERED_LINE} columns ({len(ln)}): "
                    f"{ln.strip()[:80]!r}… — an IR-sourced name (case_id / metric address / "
                    "variable) is too long for the lint column limit; shorten it")
    return "\n".join(lines) + "\n"


def _xfail_expr(case_ids: list[str], xfail: set[str]) -> str:
    """Fortran boolean expression selecting the xfail cases at runtime.

    Kept inline (per-case, in the loop) rather than a table so the runner needs
    no extra module state; short-circuits to ``.false.`` when there are none."""
    xf = [c for c in case_ids if c in xfail]
    if not xf:
        return ".false."
    terms = " .or. &\n      ".join(f"trim(case_ids(ci)) == '{_flit(c)}'" for c in xf)
    return terms


# --- harness interface signature pin (fail-closed, run before rendering) ------
#
# The only harness this renderer targets is harness_fortran_cpu (the R1 (fortran,
# cpu) plumbing). The template is written against its v2 §5.1 signatures, embedded
# below verbatim. `assert_harness_pin` checks that the *certified* harness the
# consumer will build against still publishes those exact signatures — in both its
# IR (`public_api.signatures`) and its generated model source — so a harness recert
# that silently changed the interface fails the consumer's render (drift is caught
# at the consumer, not miscompiled at Build).

EXPECTED_HARNESS_SPEC_ID = "harness_fortran_cpu"

# Verbatim copy of the harness controlled_spec §5.1 canonical interface block (v2,
# spec_version 0.2.0). If a harness recert changes §5.1, THIS block and the render
# template must be updated together (the pin message says so).
_HARNESS_V2_INTERFACE = """\
type :: harness_fortran_cpu__h_named
  character(len=:), allocatable :: name
  character(len=:), allocatable :: json
end type harness_fortran_cpu__h_named

type :: harness_fortran_cpu__h_check
  character(len=:), allocatable :: id
  character(len=4) :: status
end type harness_fortran_cpu__h_check

type :: harness_fortran_cpu__h_metric
  character(len=:), allocatable :: name
  real(dp) :: value
  logical :: is_na
  character(len=:), allocatable :: reason_na
end type harness_fortran_cpu__h_metric

type :: harness_fortran_cpu__h_case_result
  character(len=:), allocatable :: case_id
  logical :: expected_xfail
  type(harness_fortran_cpu__h_check), allocatable :: checks(:)
  type(harness_fortran_cpu__h_metric), allocatable :: metrics(:)
end type harness_fortran_cpu__h_case_result

type :: harness_fortran_cpu__h_mb_entry
  character(len=:), allocatable :: test_id
  type(harness_fortran_cpu__h_named), allocatable :: values(:)
end type harness_fortran_cpu__h_mb_entry

subroutine harness_fortran_cpu__parse_cases(tokens, ntokens, case_ids, ncases, ok)
  character(len=*), intent(in) :: tokens(:)
  integer, intent(in) :: ntokens
  character(len=case_id_len), intent(out) :: case_ids(:)
  integer, intent(out) :: ncases
  logical, intent(out) :: ok
end subroutine harness_fortran_cpu__parse_cases

function harness_fortran_cpu__emit_real(x) result(s)
  real(dp), intent(in) :: x
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_real

function harness_fortran_cpu__emit_int(i) result(s)
  integer, intent(in) :: i
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_int

function harness_fortran_cpu__emit_bool(b) result(s)
  logical, intent(in) :: b
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_bool

function harness_fortran_cpu__emit_array_r1(a) result(s)
  real(dp), intent(in) :: a(:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r1

function harness_fortran_cpu__emit_array_r2(a) result(s)
  real(dp), intent(in) :: a(:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r2

function harness_fortran_cpu__emit_array_r3(a) result(s)
  real(dp), intent(in) :: a(:,:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r3

function harness_fortran_cpu__emit_array_r4(a) result(s)
  real(dp), intent(in) :: a(:,:,:,:)
  character(len=:), allocatable :: s
end function harness_fortran_cpu__emit_array_r4

function harness_fortran_cpu__box(name, json) result(nv)
  character(len=*), intent(in) :: name
  character(len=*), intent(in) :: json
  type(harness_fortran_cpu__h_named) :: nv
end function harness_fortran_cpu__box

subroutine harness_fortran_cpu__write_snapshot(case_id, values, time)
  character(len=*), intent(in) :: case_id
  type(harness_fortran_cpu__h_named), intent(in) :: values(:)
  real(dp), intent(in) :: time
end subroutine harness_fortran_cpu__write_snapshot

subroutine harness_fortran_cpu__write_metrics_basis(entries, n)
  type(harness_fortran_cpu__h_mb_entry), intent(in) :: entries(:)
  integer, intent(in) :: n
end subroutine harness_fortran_cpu__write_metrics_basis

subroutine harness_fortran_cpu__write_diagnostics(results, n)
  type(harness_fortran_cpu__h_case_result), intent(in) :: results(:)
  integer, intent(in) :: n
end subroutine harness_fortran_cpu__write_diagnostics

subroutine harness_fortran_cpu__write_perf(case_id, target, steps, cells_updated, walltime_sec, mpi_ranks, threads_per_rank, gpu_devices)
  character(len=*), intent(in) :: case_id
  character(len=*), intent(in) :: target
  integer, intent(in) :: steps
  integer, intent(in) :: cells_updated
  real(dp), intent(in) :: walltime_sec
  integer, intent(in) :: mpi_ranks
  integer, intent(in) :: threads_per_rank
  integer, intent(in) :: gpu_devices
end subroutine harness_fortran_cpu__write_perf
"""

_PIN_DRIFT_HINT = (
    "the certified harness interface no longer matches the renderer's pinned "
    "expectation — a harness recert changed its published surface; update the "
    "runner_renderer pin (_HARNESS_V2_INTERFACE) AND the render template together, "
    "then re-certify dependent nodes")


def assert_harness_pin(
    ir: dict[str, Any],
    spec_id: str,
    harness_spec_id: str,
    harness_signatures: Any,
    harness_source: str,
) -> None:
    """Fail-closed guard the conductor runs BEFORE rendering: the certified harness
    the consumer will link against must still publish exactly the signatures this
    renderer was written for, in both its IR ``public_api.signatures`` and its
    generated model source. Any drift raises ``RenderError`` (→ transport
    fail_closed with the recert-drift hint), never a Generate content retry.

    ``harness_signatures`` is the certified harness IR's ``public_api.signatures``
    (a list of ``{symbol, interface}``); ``harness_source`` is the text of the
    certified ``<harness_spec_id>_model.f90`` (resolved via ``_certified_model_source``).
    """
    from tools.validate_pipeline_semantics import (
        _parse_interface_stanzas, _stanza_atoms, _stanza_line_set, _stanza_line_list)

    if (harness_spec_id or "").strip() != EXPECTED_HARNESS_SPEC_ID:
        raise RenderError(
            f"harness_spec_id {harness_spec_id!r} is not the pinned "
            f"{EXPECTED_HARNESS_SPEC_ID!r}; the renderer only targets that harness")

    exp_ops, exp_types, exp_errs = _parse_interface_stanzas(_HARNESS_V2_INTERFACE)
    if exp_errs:  # a renderer bug, not an input problem
        raise RenderError(f"embedded harness interface failed to parse: {exp_errs}")

    used_symbols = [_hname(harness_spec_id, op) for op in _used_harness_ops(ir)]
    used_symbols += [_hname(harness_spec_id, t) for t in _HARNESS_TYPES]

    # Certified IR signatures, keyed by symbol.
    ir_iface: dict[str, str] = {}
    for entry in (harness_signatures if isinstance(harness_signatures, list) else []):
        if isinstance(entry, dict) and isinstance(entry.get("symbol"), str) \
                and isinstance(entry.get("interface"), str):
            ir_iface[entry["symbol"].strip()] = entry["interface"]

    src_ops, src_types, _src_errs = _parse_interface_stanzas(harness_source or "")

    for symbol in used_symbols:
        exp_stanza = exp_ops.get(symbol) or exp_types.get(symbol)
        if exp_stanza is None:  # renderer bug: template depends on an un-embedded symbol
            raise RenderError(
                f"internal: renderer depends on harness symbol {symbol!r} not present in "
                "the embedded pinned interface")
        # A derived type's component layout is ordered (§5 compatibility contract); a
        # procedure's dummy declarations are order-immaterial (the header line, itself an
        # atom, pins call order), matching the two M3c-α gates.
        is_type = symbol in exp_types

        # (1) IR public_api.signatures — an interface-only stanza, so EXACT match.
        ir_text = ir_iface.get(symbol)
        if not ir_text:
            raise RenderError(
                f"certified harness IR public_api.signatures omits {symbol!r}: {_PIN_DRIFT_HINT}")
        ir_ops, ir_types, _ = _parse_interface_stanzas(ir_text)
        ir_stanza = ir_ops.get(symbol) or ir_types.get(symbol)
        ir_ok = ir_stanza is not None and (
            _stanza_line_list(ir_stanza) == _stanza_line_list(exp_stanza) if is_type
            else _stanza_line_set(ir_stanza) == _stanza_line_set(exp_stanza))
        if not ir_ok:
            raise RenderError(
                f"certified harness IR signature for {symbol!r} differs from the pinned "
                f"interface: {_PIN_DRIFT_HINT}")

        # (2) Generated model source — a procedure stanza carries its body, so the pinned
        # interface atoms must be a SUBSET of the source stanza's atoms (a type block has no
        # body, so it is compared exactly, matching _validate_infrastructure_generated_signatures).
        src_stanza = src_ops.get(symbol) or src_types.get(symbol)
        if src_stanza is None:
            raise RenderError(
                f"certified harness model source omits {symbol!r}: {_PIN_DRIFT_HINT}")
        if is_type:
            src_ok = _stanza_line_list(src_stanza) == _stanza_line_list(exp_stanza)
        else:
            have = frozenset(_stanza_atoms(src_stanza))
            src_ok = _stanza_line_set(exp_stanza).issubset(have)
        if not src_ok:
            raise RenderError(
                f"certified harness model source signature for {symbol!r} differs from the "
                f"pinned interface: {_PIN_DRIFT_HINT}")
