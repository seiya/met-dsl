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


def _parse_entities(rhs: str) -> list[tuple[str, int]]:
    """Parse the entity list on the right of ``::`` into ``[(name, rank), ...]``. Rank is the number
    of assumed/deferred dimensions from a trailing ``(...)`` (``(:)`` -> 1, ``(:,:)`` -> 2)."""
    out: list[tuple[str, int]] = []
    for ent in _split_paren_aware(rhs):
        ent = ent.strip()
        if not ent:
            continue
        # Drop any initializer (``= value``) that appears outside parentheses.
        m = re.match(r"^([A-Za-z0-9_]+)\s*(\((.*)\))?", ent)
        if not m:
            raise SignatureParseError(f"unparseable entity: {ent!r}")
        name = m.group(1)
        dims = m.group(3)
        rank = 0 if dims is None else (len(_split_paren_aware(dims)) if dims.strip() else 0)
        out.append((name, rank))
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
            # other attributes (target/pointer/...) are not part of the published surface here
    entities: list[dict[str, Any]] = []
    for name, rank in _parse_entities(right):
        entities.append({"name": name, "rank": rank, "intent": intent, "spec": dict(spec)})
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


# --- render: structured signatures -> Fortran interface block ------------------------------------

def _render_spec(spec: dict[str, Any]) -> str:
    t = spec.get("type")
    if t == "string":
        base = f"character(len={spec.get('len', '*')})"
    elif t == "derived":
        base = f"type({spec['name']})"
    elif t in ("real", "integer", "logical"):
        base = t if not spec.get("kind") else f"{t}({spec['kind']})"
    else:
        raise SignatureParseError(f"cannot render spec type {t!r}")
    if spec.get("alloc"):
        base += ", allocatable"
    return base


def _render_dims(rank: int) -> str:
    return "" if not rank else "(" + ",".join([":"] * rank) + ")"


def _render_entity(ent: dict[str, Any]) -> str:
    spec = _render_spec(ent["spec"])
    intent = ent.get("intent")
    attr = f", intent({intent})" if intent else ""
    return f"{spec}{attr} :: {ent['name']}{_render_dims(ent['rank'])}"


def _render_procedure(proc: dict[str, Any]) -> list[str]:
    name = proc["name"]
    arg_names = ", ".join(a["name"] for a in proc["args"])
    lines: list[str] = []
    if proc["kind"] == "function":
        result = proc["result"]
        lines.append(f"function {name}({arg_names}) result({result['name']})")
    else:
        lines.append(f"subroutine {name}({arg_names})")
    for a in proc["args"]:
        lines.append(f"  {_render_entity(a)}")
    if proc["kind"] == "function":
        lines.append(f"  {_render_entity(proc['result'])}")
    lines.append(f"end {proc['kind']} {name}")
    return lines


def _render_type(t: dict[str, Any]) -> list[str]:
    lines = [f"type :: {t['name']}"]
    for c in t["components"]:
        lines.append(f"  {_render_entity(c)}")
    lines.append(f"end type {t['name']}")
    return lines


def render_signatures_to_fortran(struct: dict[str, Any]) -> str:
    """Render the structured representation back to a canonical Fortran interface block. The output's
    NORMALIZED lines are what the §5.1 gates compare; exact spacing/comments are irrelevant."""
    blocks: list[str] = []
    for mp in struct.get("module_parameters", []):
        blocks.append(f"integer, parameter :: {mp['name']} = {mp['value']}")
    for t in struct.get("types", []):
        blocks.append("\n".join(_render_type(t)))
    for proc in struct.get("procedures", []):
        blocks.append("\n".join(_render_procedure(proc)))
    return "\n\n".join(blocks) + "\n"


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
