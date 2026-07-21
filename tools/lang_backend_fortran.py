"""Fortran language backend for the language-neutral published-interface representation.

Objective B (``docs`` plan ``swift-mixing-dijkstra``): ``controlled_spec`` §5.1 and the IR
``public_api.signatures`` are moving from a verbatim *Fortran* interface block to a language-neutral
*structured* representation. The Fortran-specific knowledge — how a structured signature renders to a
Fortran stanza, and how a Fortran stanza parses back to the structured form — lives HERE, in one
backend module, instead of being spread across the deterministic gates.

This module exposes two pure functions plus the struct vocabulary:

- ``parse_signatures_from_fortran(block_body)`` — a §5.1 Fortran interface block → the structured
  representation (``{module_parameters, types, procedures}``). Built on the existing
  ``validate_pipeline_semantics`` stanza splitter / normalizer, so it inherits their comment /
  continuation / case / whitespace handling.
- ``render_signatures_to_fortran(struct)`` — the inverse: the structured representation → a canonical
  Fortran interface block whose *normalized* lines (comments stripped, ``&`` joined, case-folded,
  whitespace-erased) are token-for-token what the current §5.1 gates compare.

The correctness contract is the round-trip on the REAL harness §5.1 (see
``tools/tests/test_lang_backend_fortran.py``): for every published symbol,
``normalized_stanza_lines(render(parse(real_block)))`` equals ``normalized_stanza_lines(real_block)``.
As long as that holds, switching §5.1 / the IR to the structured form leaves every downstream
signature comparison byte-for-byte unchanged — the gate renders the structured form back to the exact
Fortran lines it already knows how to compare against a generated ``.f90``.

The struct vocabulary is language-neutral (``real`` / ``integer`` / ``logical`` / ``string`` /
``derived`` — not ``character`` / ``type(...)``); the Fortran spellings are produced only by the
renderer here.
"""

from __future__ import annotations

import re
from typing import Any

from tools.validate_pipeline_semantics import (
    _fortran_logical_lines,
    _normalize_fortran_line,
    _parse_interface_stanzas,
    _split_top_level_commas,
)

# --- struct vocabulary (documentation) ----------------------------------------------------------
#
# A ``spec`` (the type of an argument / result / component) is a mapping:
#   {"type": "real"|"integer"|"logical"|"string"|"derived",
#    "kind":  <str|None>,     # numeric KIND for real/integer/logical, e.g. "dp"; None = default kind
#    "len":   <str|None>,     # character length token for string: "*", ":", "4", "case_id_len", ...
#    "name":  <str|None>,     # derived-type name for "derived"
#    "alloc": <bool>}         # the ALLOCATABLE attribute
#
# An ``entity`` (a dummy argument, a function result, or a derived-type component):
#   {"name": <str>,
#    "rank": <int>,           # 0 scalar, 1 => (:), 2 => (:,:), ...  (assumed-shape / deferred)
#    "intent": "in"|"out"|"inout"|None,   # arguments only; None for results and components
#    "spec": <spec>}
#
# A ``procedure``:
#   {"kind": "subroutine"|"function", "name": <str>, "args": [entity, ...],
#    "result": <entity|None>}   # result present iff kind == "function"
#
# A ``type`` (published derived type):
#   {"name": <str>, "components": [entity, ...]}   # each entity has intent None
#
# A ``module_parameter`` (value-pinned integer parameter referenced by the signatures):
#   {"name": <str>, "base": "integer", "value": <str>}   # e.g. dp = real64, case_id_len = 64
#
# The whole signature block:
#   {"module_parameters": [module_parameter, ...],
#    "types": [type, ...],
#    "procedures": [procedure, ...]}

_INTENT_RE = re.compile(r"^intent\(\s*(in|out|inout)\s*\)$", re.IGNORECASE)
_MODULE_PARAM_RE = re.compile(
    r"^\s*integer\s*,\s*parameter\s*::\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$", re.IGNORECASE
)
_PROC_HEADER_RE = re.compile(
    r"^\s*(?:pure\s+|elemental\s+|recursive\s+)*(subroutine|function)\s+"
    r"([A-Za-z0-9_]+)\s*(?:\((.*?)\))?\s*(?:result\s*\(\s*([A-Za-z0-9_]+)\s*\))?\s*$",
    re.IGNORECASE,
)
_TYPE_HEADER_RE = re.compile(
    r"^\s*type\s*(?:,\s*[^:()]*?)?::\s*([A-Za-z0-9_]+)\s*$", re.IGNORECASE
)


class SignatureParseError(ValueError):
    """A §5.1 stanza the backend could not lower to the structured form (fail-closed at callers)."""


# --- parse: Fortran interface block -> structured signatures -------------------------------------

def _split_paren_aware(text: str) -> list[str]:
    """Split ``text`` on top-level commas (commas inside parentheses are kept). Delegates to the
    validator's shared splitter so paren handling matches the rest of the pipeline."""
    return [p for p in _split_top_level_commas(text)]


def _parse_type_spec(head: str) -> dict[str, Any]:
    """Parse the leading type-spec token of a declaration's left side (before any attribute), e.g.
    ``real(dp)`` / ``character(len=case_id_len)`` / ``type(foo)`` / ``integer`` / ``logical``."""
    head = head.strip()
    low = head.lower()
    if low.startswith("character"):
        m = re.search(r"\(\s*(?:len\s*=\s*)?([^)]*?)\s*\)", head, re.IGNORECASE)
        length = m.group(1).strip() if m else "1"
        return {"type": "string", "kind": None, "len": length, "name": None, "alloc": False}
    if low.startswith("type") and "(" in head:
        m = re.search(r"\(\s*([A-Za-z0-9_]+)\s*\)", head)
        if not m:
            raise SignatureParseError(f"derived type-spec missing name: {head!r}")
        return {"type": "derived", "kind": None, "len": None, "name": m.group(1), "alloc": False}
    for base in ("real", "integer", "logical"):
        if low.startswith(base):
            m = re.search(r"\(\s*(?:kind\s*=\s*)?([A-Za-z0-9_]+)\s*\)", head, re.IGNORECASE)
            kind = m.group(1).strip() if m else None
            return {"type": base, "kind": kind, "len": None, "name": None, "alloc": False}
    raise SignatureParseError(f"unrecognized type-spec: {head!r}")


def _parse_entities(rhs: str) -> list[tuple[str, int, list[str] | None]]:
    """Parse the entity list on the right of ``::`` into ``[(name, rank, dims), ...]``. ``rank`` is
    the number of dimensions from a trailing ``(...)`` (``(:)`` -> 1, ``(:,:)`` -> 2). ``dims`` is
    ``None`` for a pure assumed-shape declaration (every dim is ``:``) and the explicit token list
    otherwise (e.g. ``coef(3)`` -> ``['3']``), so a fixed bound round-trips instead of collapsing to
    assumed-shape."""
    out: list[tuple[str, int, list[str] | None]] = []
    for ent in _split_paren_aware(rhs):
        ent = ent.strip()
        if not ent:
            continue
        # Drop any initializer (``= value``) that appears outside parentheses.
        m = re.match(r"^([A-Za-z0-9_]+)\s*(\((.*)\))?", ent)
        if not m:
            raise SignatureParseError(f"unparseable entity: {ent!r}")
        name = m.group(1)
        dims_src = m.group(3)
        if dims_src is None or not dims_src.strip():
            out.append((name, 0, None))
            continue
        toks = [t.strip() for t in _split_paren_aware(dims_src)]
        explicit = None if all(t == ":" for t in toks) else toks
        out.append((name, len(toks), explicit))
    return out


def _parse_decl_line(line: str) -> list[dict[str, Any]]:
    """Parse one declaration logical line into a list of ``entity`` dicts (one per declared name).
    ``intent`` is populated from the attributes; a result/component simply has ``intent=None``."""
    if "::" not in line:
        raise SignatureParseError(f"declaration without '::': {line!r}")
    left, right = line.split("::", 1)
    parts = _split_paren_aware(left)
    if not parts:
        raise SignatureParseError(f"empty type-spec: {line!r}")
    spec = _parse_type_spec(parts[0])
    intent: str | None = None
    for attr in parts[1:]:
        a = attr.strip().lower()
        if a == "allocatable":
            spec["alloc"] = True
        elif a == "parameter":
            pass  # module parameters are handled separately (outside stanzas)
        else:
            m = _INTENT_RE.match(attr.strip())
            if m:
                intent = m.group(1).lower()
            else:
                # Fail closed on an attribute the neutral structure does not model — silently
                # dropping `dimension(:)` / `optional` / `pointer` / `value` would change the ABI
                # (e.g. `real, dimension(:) :: x` parsed as a scalar). The neutral form models only
                # type/kind/len, rank (via the entity's `(:)` dimensions), intent, and allocatable.
                raise SignatureParseError(
                    f"unsupported declaration attribute {attr.strip()!r} in {line!r}; the neutral "
                    "signature models only intent / allocatable (rank comes from the entity's "
                    "dimensions, e.g. `x(:)`, not a `dimension` attribute)")
    entities: list[dict[str, Any]] = []
    for name, rank, dims in _parse_entities(right):
        ent: dict[str, Any] = {"name": name, "rank": rank, "intent": intent, "spec": dict(spec)}
        if dims is not None:
            ent["dims"] = dims
        entities.append(ent)
    return entities


def _parse_procedure(header: str, body_lines: list[str]) -> dict[str, Any]:
    m = _PROC_HEADER_RE.match(header)
    if not m:
        raise SignatureParseError(f"unparseable procedure header: {header!r}")
    kind = m.group(1).lower()
    name = m.group(2)
    arg_names = [a.strip() for a in (m.group(3) or "").split(",") if a.strip()]
    result_name = m.group(4) or (name if kind == "function" else None)
    decls: dict[str, dict[str, Any]] = {}
    for bl in body_lines:
        for ent in _parse_decl_line(bl):
            decls[ent["name"]] = ent
    args: list[dict[str, Any]] = []
    for an in arg_names:
        if an not in decls:
            raise SignatureParseError(f"{name}: argument {an!r} has no declaration")
        args.append(decls[an])
    result_entity: dict[str, Any] | None = None
    if kind == "function":
        if result_name not in decls:
            raise SignatureParseError(f"{name}: result {result_name!r} has no declaration")
        result_entity = decls[result_name]
        result_entity["intent"] = None
    return {"kind": kind, "name": name, "args": args, "result": result_entity}


def _parse_type(header: str, body_lines: list[str]) -> dict[str, Any]:
    m = _TYPE_HEADER_RE.match(header)
    if not m:
        raise SignatureParseError(f"unparseable type header: {header!r}")
    name = m.group(1)
    components: list[dict[str, Any]] = []
    for bl in body_lines:
        low = bl.strip().lower()
        if low.startswith("end type") or low.startswith("type ") or low.startswith("type::"):
            continue
        for ent in _parse_decl_line(bl):
            ent["intent"] = None
            components.append(ent)
    return {"name": name, "components": components}


def parse_signatures_from_fortran(block_body: str) -> dict[str, Any]:
    """Parse a §5.1 canonical Fortran interface block into the structured representation.

    Module ``parameter`` lines (outside every stanza) become ``module_parameters``; procedure and
    derived-type stanzas become ``procedures`` / ``types``. Raises ``SignatureParseError`` on any
    stanza the backend cannot lower (fail-closed — a caller must not silently accept a partial parse).
    """
    op_stanzas, type_stanzas, errors = _parse_interface_stanzas(block_body)
    if errors:
        raise SignatureParseError("; ".join(errors))

    module_parameters: list[dict[str, Any]] = []
    for logical in _fortran_logical_lines(block_body):
        mp = _MODULE_PARAM_RE.match(logical)
        if mp:
            module_parameters.append(
                {"name": mp.group(1), "base": "integer", "value": mp.group(2).strip()}
            )

    procedures = [
        _parse_procedure(lines[0], lines[1:]) for lines in op_stanzas.values()
    ]
    types = [
        _parse_type(lines[0], lines[1:-1]) for lines in type_stanzas.values()
    ]
    return {
        "module_parameters": module_parameters,
        "types": types,
        "procedures": procedures,
    }


# --- validation: fail-closed on any malformed struct ---------------------------------------------
#
# The struct is authored by an LLM (the Compile leaf transcribing §5.1 into the IR, or a §5.1
# author); a malformed shape must produce a clean ``SignatureParseError`` that the gates turn into a
# repairable violation, NEVER an uncaught KeyError/TypeError that crashes the gate with a traceback
# (the "gate-only field fabricated by the leaf -> conductor crash" bug-class). Both render entry
# points validate first, so every downstream ``dict``/``list`` index is known-safe.

_VALID_SPEC_TYPES = frozenset({"real", "integer", "logical", "string", "derived"})
_VALID_INTENTS = frozenset({"in", "out", "inout"})
_SPEC_KEYS = frozenset({"type", "kind", "len", "name", "alloc"})
_ENTITY_KEYS = frozenset({"name", "rank", "intent", "dims", "spec"})
_PROC_KEYS = frozenset({"kind", "name", "args", "result"})
_TYPE_KEYS = frozenset({"name", "components"})
_PARAM_KEYS = frozenset({"name", "base", "value"})


def _require_nonempty_str(value: Any, ctx: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignatureParseError(f"{ctx} must be a non-empty string (got {value!r})")
    return value


def _reject_unknown_keys(mapping: dict[str, Any], allowed: frozenset[str], ctx: str) -> None:
    # `key=str`: a YAML mapping can mix key types (e.g. an int key alongside string keys); a bare
    # `sorted` would raise `TypeError: '<' not supported between int and str` and escape the callers'
    # `except SignatureParseError`, crashing the gate instead of failing closed.
    unknown = sorted(set(mapping) - allowed, key=str)
    if unknown:
        raise SignatureParseError(
            f"{ctx} has unknown key(s) {unknown}; allowed: {sorted(allowed)} "
            "(a mistyped key must fail closed, not silently fall to a default)")


# Fortran 2008 caps array rank at 15; a larger value is malformed and, unbounded, would let
# ``_render_dims`` amplify one integer into a multi-GB string (OOM/hang) instead of failing closed.
_MAX_RANK = 15
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _require_identifier(value: Any, ctx: str) -> str:
    """A published NAME (symbol / argument / component / derived-type / parameter) must be a plain
    Fortran identifier. This is also the injection guard: names flow verbatim into rendered Fortran
    that is re-parsed into stanzas, so a name carrying ``::`` / a newline / ``end subroutine`` could
    otherwise smuggle or split a stanza."""
    _require_nonempty_str(value, ctx)
    if not _IDENTIFIER_RE.match(value):
        raise SignatureParseError(
            f"{ctx} must be a Fortran identifier [A-Za-z][A-Za-z0-9_]* (got {value!r})")
    return value


def _require_safe_token(value: Any, ctx: str) -> str:
    """A token that renders INSIDE parentheses — a string length, a kind, a SINGLE dimension bound
    (``character(len=<len>)`` / ``real(<kind>)`` / ``(<dim>)``) — may be ``*`` / ``:`` / a number /
    a symbol / a simple expression, but must not carry a ``)`` (which would close the enclosing
    parens and smuggle a declaration), a ``,`` (which would add dimensions to a single bound —
    ``dims: ['3,4']`` must not render the rank-2 ``(3,4)``), a ``;`` (statement separator), nor the
    other structural characters (``::``, a newline, a comment ``!``, or an opening ``(``)."""
    _require_nonempty_str(value, ctx)
    if "::" in value or any(ch in value for ch in "\n\r!(),;"):
        raise SignatureParseError(
            f"{ctx} must not contain structural Fortran characters (::, newline, !, parentheses, "
            f"comma, semicolon); got {value!r}")
    return value


def _require_parameter_value(value: str, ctx: str) -> str:
    """A module-parameter VALUE renders OUTSIDE parentheses (``parameter :: <name> = <value>``), so —
    unlike the inside-parens tokens above — it may carry balanced parens (the portable-kind idiom
    ``selected_real_kind(15, 307)``). It must still not carry a ``::`` / ``;`` (statement separator —
    ``real64; integer evil`` would emit a second declaration) / newline / comment ``!`` that would
    break or smuggle a declaration on the parameter line.

    It must also carry no character literal (a ``'`` / ``"`` quote). The value pin (both the Compile
    gate and the Generate.static source pin) compares values case- and whitespace-INSENSITIVELY, so
    ``iachar('A')`` and ``iachar('a')`` — different integer values — would compare equal, letting the
    ABI drift silently. A published module parameter that needs a character literal is unsupported;
    fail closed rather than pin it unsoundly."""
    _require_nonempty_str(value, ctx)
    if "::" in value or any(ch in value for ch in "\n\r!;\"'"):
        raise SignatureParseError(
            f"{ctx} must not contain '::', ';', a quote (a character literal cannot be pinned "
            f"case/whitespace-insensitively), a newline, or '!'; got {value!r}")
    return value


def _validate_spec(spec: Any, ctx: str) -> None:
    if not isinstance(spec, dict):
        raise SignatureParseError(f"{ctx}.spec must be a mapping (got {type(spec).__name__})")
    _reject_unknown_keys(spec, _SPEC_KEYS, f"{ctx}.spec")
    t = spec.get("type")
    # `isinstance(str)` BEFORE the frozenset membership: an unhashable `type: []` / `type: {}` would
    # otherwise raise a raw TypeError (unhashable) that escapes the callers' `except
    # SignatureParseError`, crashing the gate instead of failing closed.
    if not isinstance(t, str) or t not in _VALID_SPEC_TYPES:
        raise SignatureParseError(
            f"{ctx}.spec.type must be one of {sorted(_VALID_SPEC_TYPES)} (got {t!r})")
    # A field the renderer does not use for this `type` would be SILENTLY DROPPED at render — so
    # §5.1 and the IR could carry different authored info yet render/compare equal (fail-open).
    # Reject any inapplicable field (present and non-None); `alloc` applies to every type.
    _inapplicable = {
        "string": ("kind", "name"),
        "derived": ("kind", "len"),
        "real": ("len", "name"), "integer": ("len", "name"), "logical": ("len", "name"),
    }[t]
    for bad in _inapplicable:
        if spec.get(bad) is not None:
            raise SignatureParseError(
                f"{ctx}.spec.{bad} is not applicable to type '{t}' (it would be silently dropped at "
                "render, letting §5.1 and the IR differ yet compare equal)")
    if t == "string":
        _require_safe_token(spec.get("len"), f"{ctx}.spec.len (string length is required)")
    elif t == "derived":
        _require_identifier(spec.get("name"), f"{ctx}.spec.name (derived type name is required)")
    else:  # real / integer / logical: kind optional but a safe token when present
        if spec.get("kind") is not None:
            _require_safe_token(spec.get("kind"), f"{ctx}.spec.kind")
    alloc = spec.get("alloc")
    if alloc is not None and not isinstance(alloc, bool):
        # `not in (None, True, False)` would accept `alloc: 1` (1 == True) and render by truthiness;
        # require a real boolean.
        raise SignatureParseError(f"{ctx}.spec.alloc must be a boolean (got {alloc!r})")


def _validate_entity(ent: Any, ctx: str, *, allow_intent: bool) -> None:
    if not isinstance(ent, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(ent).__name__})")
    _reject_unknown_keys(ent, _ENTITY_KEYS, ctx)
    _require_identifier(ent.get("name"), f"{ctx}.name")
    rank = ent.get("rank", 0)
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
        raise SignatureParseError(f"{ctx}.rank must be a non-negative integer (got {rank!r})")
    if rank > _MAX_RANK:
        raise SignatureParseError(
            f"{ctx}.rank must be <= {_MAX_RANK} (Fortran maximum array rank; got {rank})")
    dims = ent.get("dims")
    if dims is not None:
        if not isinstance(dims, list) or not dims or not all(
                isinstance(d, str) and d.strip() for d in dims):
            raise SignatureParseError(
                f"{ctx}.dims must be a non-empty list of dimension strings (e.g. ['3', ':'])")
        if len(dims) > _MAX_RANK:
            raise SignatureParseError(
                f"{ctx}.dims has {len(dims)} entries (Fortran maximum array rank is {_MAX_RANK})")
        for d in dims:
            _require_safe_token(d, f"{ctx}.dims entry")
        if "rank" in ent and rank != len(dims):
            raise SignatureParseError(
                f"{ctx}: rank ({rank}) disagrees with dims length ({len(dims)})")
    intent = ent.get("intent")
    if intent is not None:
        if not allow_intent:
            raise SignatureParseError(
                f"{ctx}.intent is not allowed here (a result / component carries no intent)")
        if not isinstance(intent, str) or intent not in _VALID_INTENTS:  # isinstance guards unhashable
            raise SignatureParseError(
                f"{ctx}.intent must be one of {sorted(_VALID_INTENTS)} (got {intent!r})")
    _validate_spec(ent.get("spec"), ctx)


def _validate_procedure(proc: Any, ctx: str) -> None:
    if not isinstance(proc, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(proc).__name__})")
    _reject_unknown_keys(proc, _PROC_KEYS, ctx)
    kind = proc.get("kind")
    if kind not in ("subroutine", "function"):
        raise SignatureParseError(f"{ctx}.kind must be 'subroutine' or 'function' (got {kind!r})")
    name = _require_identifier(proc.get("name"), f"{ctx}.name")
    args = proc.get("args", [])
    if not isinstance(args, list):
        raise SignatureParseError(f"{ctx}.args must be a list (got {type(args).__name__})")
    for i, arg in enumerate(args):
        _validate_entity(arg, f"{ctx}.args[{i}]", allow_intent=True)
    result = proc.get("result")
    if kind == "function":
        if not isinstance(result, dict):
            raise SignatureParseError(f"{ctx} (function {name}) requires a mapping 'result'")
        _validate_entity(result, f"{ctx}.result", allow_intent=False)
    elif result is not None:
        raise SignatureParseError(f"{ctx} (subroutine {name}) must not carry a 'result'")


def _validate_type(tdef: Any, ctx: str) -> None:
    if not isinstance(tdef, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(tdef).__name__})")
    _reject_unknown_keys(tdef, _TYPE_KEYS, ctx)
    _require_identifier(tdef.get("name"), f"{ctx}.name")
    comps = tdef.get("components", [])
    # An empty component list is Fortran-legal (an opaque tag type) and is what
    # ``_parse_type`` produces for ``type :: x`` / ``end type x``; accepting it keeps parse and
    # validate symmetric and matches the pre-B Fortran-fence gate (which accepted empty types too).
    if not isinstance(comps, list):
        raise SignatureParseError(f"{ctx}.components must be a list (got {type(comps).__name__})")
    for i, comp in enumerate(comps):
        _validate_entity(comp, f"{ctx}.components[{i}]", allow_intent=False)


def _validate_module_parameter(mp: Any, ctx: str) -> None:
    if not isinstance(mp, dict):
        raise SignatureParseError(f"{ctx} must be a mapping (got {type(mp).__name__})")
    _reject_unknown_keys(mp, _PARAM_KEYS, ctx)
    _require_identifier(mp.get("name"), f"{ctx}.name")
    base = mp.get("base")
    if base is not None and base != "integer":
        # The renderer emits `integer, parameter :: <name> = <value>` unconditionally, so any other
        # `base` would be authored but silently dropped — fail closed.
        raise SignatureParseError(
            f"{ctx}.base must be 'integer' (the only module-parameter base the renderer emits); "
            f"got {base!r}")
    value = mp.get("value")
    if isinstance(value, bool):  # bool is an int subclass; `value: true` is not a parameter value
        raise SignatureParseError(f"{ctx}.value must be a number or symbol, not a boolean")
    if isinstance(value, int):
        return  # rendered as its decimal string
    _require_parameter_value(value, f"{ctx}.value")


def _validate_symbol(sig: Any, ctx: str = "signature") -> None:
    """Validate ONE published symbol (procedure or type) — the shape ``render_symbol_to_fortran``
    accepts. A struct that is neither is fail-closed."""
    if isinstance(sig, dict) and sig.get("kind") in ("subroutine", "function"):
        _validate_procedure(sig, ctx)
    elif isinstance(sig, dict) and "components" in sig:
        _validate_type(sig, ctx)
    else:
        raise SignatureParseError(
            f"{ctx} is neither a procedure (kind: subroutine/function) nor a type (components: [...])")


def _validate_struct(struct: dict[str, Any]) -> None:
    """Validate a whole ``{module_parameters, types, procedures}`` struct, fail-closed."""
    for i, mp in enumerate(struct.get("module_parameters") or []):
        _validate_module_parameter(mp, f"module_parameters[{i}]")
    for i, tdef in enumerate(struct.get("types") or []):
        _validate_type(tdef, f"types[{i}]")
    for i, proc in enumerate(struct.get("procedures") or []):
        _validate_procedure(proc, f"procedures[{i}]")


# --- render: structured signatures -> Fortran interface block ------------------------------------

def _render_spec(spec: dict[str, Any]) -> str:
    # Callers render only AFTER validation, so required fields (string `len`, derived `name`) are
    # known present; the accesses below cannot KeyError on a validated struct.
    t = spec["type"]
    if t == "string":
        base = f"character(len={spec['len']})"
    elif t == "derived":
        base = f"type({spec['name']})"
    elif t in ("real", "integer", "logical"):
        base = t if not spec.get("kind") else f"{t}({spec['kind']})"
    else:
        raise SignatureParseError(f"cannot render spec type {t!r}")
    if spec.get("alloc"):
        base += ", allocatable"
    return base


def _render_dims(rank: int, dims: list[str] | None = None) -> str:
    # Explicit `dims` (e.g. ['3'] for a fixed bound) render verbatim; otherwise `rank` assumed-shape
    # colons. This lets a signature express `coef(3)` and not only assumed-shape `(:)`.
    if dims:
        return "(" + ",".join(dims) + ")"
    return "" if not rank else "(" + ",".join([":"] * rank) + ")"


def _render_entity(ent: dict[str, Any]) -> str:
    spec = _render_spec(ent["spec"])
    intent = ent.get("intent")
    attr = f", intent({intent})" if intent else ""
    return f"{spec}{attr} :: {ent['name']}{_render_dims(ent.get('rank', 0), ent.get('dims'))}"


def _render_procedure(proc: dict[str, Any]) -> list[str]:
    name = proc["name"]
    args = proc.get("args", [])  # validation tolerates an omitted args list (a no-arg procedure);
    arg_names = ", ".join(a["name"] for a in args)  # render must too, not KeyError on proc["args"].
    lines: list[str] = []
    if proc["kind"] == "function":
        result = proc["result"]
        # Fortran forbids a `result` name equal to the function name; that form is the IMPLICIT
        # result (the function name IS the result variable), so omit the clause — emitting
        # `function f(...) result(f)` would be invalid source.
        if result["name"] == name:
            lines.append(f"function {name}({arg_names})")
        else:
            lines.append(f"function {name}({arg_names}) result({result['name']})")
    else:
        lines.append(f"subroutine {name}({arg_names})")
    for a in args:
        lines.append(f"  {_render_entity(a)}")
    if proc["kind"] == "function":
        lines.append(f"  {_render_entity(proc['result'])}")
    lines.append(f"end {proc['kind']} {name}")
    return lines


def _render_type(t: dict[str, Any]) -> list[str]:
    lines = [f"type :: {t['name']}"]
    for c in t.get("components", []):  # validation allows an omitted/empty component list (an
        lines.append(f"  {_render_entity(c)}")  # opaque tag type); render must not KeyError.
    lines.append(f"end type {t['name']}")
    return lines


def render_signatures_to_fortran(struct: dict[str, Any]) -> str:
    """Render the structured representation back to a canonical Fortran interface block. The output's
    NORMALIZED lines are what the §5.1 gates compare; exact spacing/comments are irrelevant.
    Fail-closed (``SignatureParseError``) on any malformed struct — never an uncaught index error."""
    _validate_struct(struct)
    blocks: list[str] = []
    for mp in struct.get("module_parameters", []):
        blocks.append(f"integer, parameter :: {mp['name']} = {mp['value']}")
    for t in struct.get("types", []):
        blocks.append("\n".join(_render_type(t)))
    for proc in struct.get("procedures", []):
        blocks.append("\n".join(_render_procedure(proc)))
    return "\n\n".join(blocks) + "\n"


def render_symbol_to_fortran(sig: dict[str, Any]) -> str:
    """Render ONE published-symbol signature (a procedure or a derived-type struct) to its Fortran
    stanza. Used to compare a single IR ``public_api.signatures`` entry against §5.1 by rendering it
    into the same Fortran currency the existing stanza comparison uses. ``kind`` present ->
    procedure; ``components`` present -> type. Fail-closed on any malformed struct."""
    _validate_symbol(sig)
    if sig.get("kind") in ("subroutine", "function"):
        return "\n".join(_render_procedure(sig)) + "\n"
    return "\n".join(_render_type(sig)) + "\n"


_STRUCT_TOP_KEYS = ("module_parameters", "types", "procedures")


def load_structured_signatures(body: str) -> tuple[dict[str, Any], str | None]:
    """Load a structured (YAML) §5.1 signature block into the canonical struct. Returns
    ``(struct, error)``; ``error`` is non-``None`` (and ``struct`` empty) when the block is not a
    mapping of the expected shape — fail-closed at the gate. Every top key is optional but must be a
    list when present; an unknown top key is rejected so a typo cannot silently drop signatures."""
    import yaml  # local import: keep the backend import-light for pure render/parse callers

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        return ({}, f"structured §5.1 block is not valid YAML: {exc}")
    if not isinstance(data, dict):
        return ({}, "structured §5.1 block must be a YAML mapping "
                    "with keys module_parameters / types / procedures")
    unknown = sorted(set(data) - set(_STRUCT_TOP_KEYS), key=str)  # key=str: mixed key types (a YAML
    if unknown:                                                    # `1:` next to `foo:`) must not
        return ({}, f"structured §5.1 block has unknown key(s) {unknown}; "  # TypeError-crash sorted
                    f"allowed: {list(_STRUCT_TOP_KEYS)}")
    struct: dict[str, Any] = {"module_parameters": [], "types": [], "procedures": []}
    for key in _STRUCT_TOP_KEYS:
        if key not in data:
            continue  # ABSENT key -> that category is empty (a pruned §5.1 may omit e.g. types)
        val = data[key]
        # A PRESENT-but-null (`module_parameters:` / `module_parameters: null`) must fail closed,
        # NOT be silently treated as empty: an empty module_parameters would drop the dp/case_id_len
        # value pins, letting a `dp = real32` / `case_id_len = 32` drift pass the parameter check.
        if not isinstance(val, list):
            return ({}, f"structured §5.1 block's '{key}' must be a list (got "
                        f"{type(val).__name__}); a present-but-null key must fail closed")
        struct[key] = val
    return (struct, None)


# --- helpers for gate comparison (used by the deterministic gates once §5.1 is structured) --------

def normalized_stanza_index(block_body: str) -> dict[str, frozenset[str]]:
    """Map each published symbol to the frozenset of its NORMALIZED stanza lines. The canonical
    per-symbol comparison key: symbol identity + a whitespace/case/comment-insensitive line set.
    Used to compare a rendered structured block against a generated ``.f90`` (or two structured
    forms) with the exact semantics the current gates use for procedures."""
    op_stanzas, type_stanzas, errors = _parse_interface_stanzas(block_body)
    if errors:
        raise SignatureParseError("; ".join(errors))
    index: dict[str, frozenset[str]] = {}
    for name, lines in {**op_stanzas, **type_stanzas}.items():
        index[name] = frozenset(
            _normalize_fortran_line(ln) for ln in lines if _normalize_fortran_line(ln)
        )
    return index
