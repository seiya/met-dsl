#!/usr/bin/env python3
"""Pure-function leaf transport (Z2) — the host-mediated `claude -p` producer channel.

A *pure leaf* is an LLM stage run as a host-mediated pure function
(`docs/design/zero_base_architecture.md` A2): the host assembles a closed context, the
model returns exactly one typed JSON document, and the host validates and writes it. The
model holds no filesystem, shell, gate, or write authority — it is launched as
`claude -p` with tools disabled and slash commands disabled, and its answer comes back in
the `--output-format json` result envelope.

This module is the STAGE-AGNOSTIC substrate of that channel: the launch flag set, the
result-envelope parser, the single-document extractor with its truncation classifier, and
the verify verdict contract. It is deliberately free of any `generate`-specific knowledge
(the `CodegenBundle` producer lives in `tools/workflow_conductor.py`; the bundle contract
in `tools/codegen_bundle.py`) so the later Z1 (`compile.generate`) and Z3 (`validate.judge`)
pure stages can reuse the same transport unchanged.

**Inert at introduction (M-A).** No caller passes `pure=True` yet; `Conductor.leaf_command`
grows the branch, the producer/verify inversions arrive in M-C/M-D. The functions here are
pure and unit-tested ahead of the wiring so the transport is settled before the dominant
leaf depends on it.

Idiom (`tools/codegen_bundle.py`, `tools/meta_contracts.py`): stdlib only, pure functions,
an explicit `_MISSING` sentinel so a present JSON `null` is never confused with an absent
key, and prefix-free violation clauses the caller frames in its own reporting idiom.
"""

from __future__ import annotations

import json
import math
from typing import Any, NamedTuple

# The prompt-contract version stamped into `bundle_meta.json` and the launch record, so a
# contract change is an observable event (A7). Bumped when the pure prompt templates, the
# fixed `PURE_SYSTEM_PROMPT`, or the transport's request shape change in a way that affects
# producer behavior.
PURE_PROMPT_CONTRACT_VERSION = "pure-6"

# The pure leaf's system prompt is REPLACED with this fixed string via `--system-prompt`. The
# default Claude Code system prompt injects per-machine DYNAMIC sections (cwd, environment,
# memory paths, git status); `--exclude-dynamic-system-prompt-sections` only relocates them
# into the first user message (still host-varying), so it does not close the context — a
# fixed `--system-prompt` does, because the dynamic sections are omitted entirely when the
# system prompt is explicit. That makes the model's total input a byte-stable function of the
# host-assembled `-p` body alone (A2). Deliberately minimal: the full persona, output
# contract, and inlined context live in the `-p` body (rendered in M-B); this only pins the
# system channel and states the pure-function shape. A change here is a prompt-contract change
# (bump `PURE_PROMPT_CONTRACT_VERSION`).
PURE_SYSTEM_PROMPT = (
    "You are a host-mediated pure function. You have no tools, no filesystem, and no shell. "
    "The user message is a complete, self-contained task. Return exactly one JSON document as "
    "your entire reply, with no surrounding prose or explanation."
)

# Schema-violation repair is a bounded in-conversation continuation (warm `--resume
# --fork-session`): the producer sees its own prior answer plus the violation excerpt and
# re-emits. Two turns caps the cost; exhaustion fails the substep with the terminal category
# recorded, never a silent "no bundle".
MAX_BUNDLE_REPAIR_TURNS = 2

# The first line of every host-rendered pure launch prompt (`-p` body). The host anchors pure
# detection on this being the very first line (`startswith`, mirroring the slim-repair
# `SLIM_REPAIR_PROMPT_SENTINEL` anchor), so a non-pure prompt that merely mentions the string
# inside its body never reads as pure, and a pure request whose prompt does not open with it is
# rejected. Defined HERE (not duplicated at each call site like the slim sentinel) so
# `orchestration_runtime.py` and `validate_pipeline_semantics.py` import the ONE literal — the
# pure prompt templates pin their line 0 against it via a parity test, closing the drift hole the
# slim sentinel's copy-paste leaves open. A change here is a prompt-contract change (bump
# `PURE_PROMPT_CONTRACT_VERSION`).
PURE_PROMPT_SENTINEL = "Pure-function leaf turn (no tools)"

# Data-only fence around an untrusted document inlined into a pure launch prompt (`tests.md`,
# `controlled_spec.md`, the IR, the bundle under repair). The pure leaf has no tools, so an
# injected instruction inside a fenced document can only corrupt the returned bundle content
# (caught downstream by `validate_bundle` / the gates / verify), never drive a side effect —
# but the fence still tells the model to treat the span as DATA, and, load-bearingly, marks the
# region for the gate-allowlist lint's scan carve-out (a `validate_pipeline_semantics --stage`
# string legitimately appearing inside an inlined doc must not fail-close the launch). The host
# neutralizes any embedded copy of these markers in a document body before fencing it.
PURE_DOC_FENCE_BEGIN = "----- BEGIN PURE INPUT DOCUMENT (data only) -----"
PURE_DOC_FENCE_END = "----- END PURE INPUT DOCUMENT -----"

# The `leaf_mode` value that selects the pure-function path, and the capability `mode` tag a
# pure launch stamps. Single source (imported by orchestration_runtime and
# validate_pipeline_semantics) so the sentinel value cannot drift between the producer and the
# validators — a typo in one inlined `"pure"` / `"pure_readonly"` literal would silently split
# the detection.
PURE_LEAF_MODE = "pure"
PURE_CAPABILITY_MODE = "pure_readonly"


def is_pure_request(request_payload: Any) -> bool:
    """True when a launch request selects the pure-function path (`leaf_mode == "pure"`,
    case/space-insensitively). THE single detection predicate — orchestration_runtime and
    validate_pipeline_semantics both delegate here so the producer and the validators cannot
    disagree about what "pure" is. An absent/other `leaf_mode` is the legacy agentic path."""
    if not isinstance(request_payload, dict):
        return False
    return str(request_payload.get("leaf_mode", "")).strip().lower() == PURE_LEAF_MODE

# A JSON `null` is a PRESENT value, not an absent key. The two are distinguished with an
# explicit sentinel (the `tools/codegen_bundle.py` idiom) so an envelope carrying
# `"result": null` falls through to its own handling instead of being read as "no key".
_MISSING = object()


# --------------------------------------------------------------------------------------
# Response-failure categories. `extract_json_document` returns one of these when the model's
# answer is not a single parseable JSON document; the caller (M-C) routes every category to a
# bounded repair turn and records the terminal one in `bundle_meta.json`. Keeping the two
# transport-level categories here (not as magic strings at the call site) lets the extractor
# and its tests agree on the exact tokens.
# --------------------------------------------------------------------------------------

RESPONSE_UNPARSEABLE = "pure_response_unparseable"
RESPONSE_TRUNCATED = "pure_response_truncated"
RESPONSE_CATEGORIES: tuple[str, ...] = (RESPONSE_UNPARSEABLE, RESPONSE_TRUNCATED)


# --------------------------------------------------------------------------------------
# Launch flags
# --------------------------------------------------------------------------------------

def pure_leaf_flags() -> list[str]:
    """The `claude -p` flags that turn a leaf into a pure function.

    A fresh list per call (never a shared constant) so a caller that splices it into an
    argv cannot mutate the canonical set. The set is the load-bearing contract:

    - `--safe-mode`        disables ALL ambient customizations — `CLAUDE.md`, skills,
                            plugins, MCP servers, custom commands/agents, and crucially the
                            repo's `.claude/settings.json` HOOKS (the `UserPromptSubmit`
                            hook would otherwise fire on the `-p` prompt and inject context
                            or run side effects). This is what makes the context CLOSED (A2):
                            without it, `claude -p` loads `CLAUDE.md` and runs the configured
                            hooks, so the "pure function" would still receive ambient
                            instructions. Admin policy settings still apply, and auth / model
                            / permissions work normally — so subscription billing is preserved
                            (this is why `--safe-mode`, not `--bare`, which forces API-key auth
                            and would break subscription billing).
    - `--system-prompt <PURE_SYSTEM_PROMPT>` replaces the default system prompt, which
                            otherwise carries per-machine DYNAMIC sections (cwd, env, memory
                            paths, git status). `--safe-mode` does not remove those (they are
                            the base prompt, not a customization); replacing the system prompt
                            omits them, so the model's input is a byte-stable function of the
                            host `-p` body alone (A2).
    - `--tools ""`         no file/shell/gate/write tool is available to the model
                            (`--safe-mode` disables customizations, not the built-in tools).
    - `--strict-mcp-config` defense-in-depth: no ambient MCP server even if a future
                            `--safe-mode` narrows its scope (the build-runtime server is a
                            deterministic-stage concern, never the producer's).
    - `--disable-slash-commands` defense-in-depth: no skill/slash command injects behavior.
    - `--output-format json` the answer arrives in a machine-parseable result envelope,
                            which is how the host reads `result` / `model` / `usage`
                            without touching the session transcript (~/.claude is not read).

    `--session-id`, the warm-repair `--resume <arid> --fork-session`, and the `-p <prompt>`
    body are added by `Conductor.leaf_command` around this set. `--bare` and
    `--no-session-persistence` are intentionally NOT here: `--bare` forces API-key auth
    (breaking the subscription billing the operator requires), and
    `--no-session-persistence` would break the warm-resume repair path.
    """
    return ["--safe-mode", "--system-prompt", PURE_SYSTEM_PROMPT, "--tools", "",
            "--strict-mcp-config", "--disable-slash-commands", "--output-format", "json"]


# --------------------------------------------------------------------------------------
# Result-envelope parsing
# --------------------------------------------------------------------------------------

class ResultEnvelope(NamedTuple):
    """The parsed `claude -p --output-format json` result envelope.

    `parsed` is True only when stdout was a JSON object. Every payload field is the raw
    value via `.get(key, _MISSING)`, so an absent key is `_MISSING` and a present `null`
    is `None` — the two are never conflated. `parse_error` carries a short reason when the
    envelope itself could not be read (empty stdout, non-JSON, non-object); it is None on a
    well-formed envelope regardless of what the model answered inside `result`.
    """
    parsed: bool
    result: Any
    is_error: Any
    model: Any
    usage: Any
    session_id: Any
    raw: Any
    parse_error: "str | None"


def _resolve_model(envelope: dict) -> Any:
    """The resolved model string from an envelope, or `_MISSING`.

    Tolerant of envelope-format drift (the CLI's JSON shape is not a pinned contract): a
    top-level `model` string wins; otherwise a single-key `modelUsage` map names the one
    model the leaf ran under. Anything ambiguous returns `_MISSING` rather than guessing —
    the value is recorded for provenance, never gates a decision, so a missing model is a
    provenance gap, not a fail-open."""
    model = envelope.get("model", _MISSING)
    if isinstance(model, str) and model.strip():
        return model
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict) and len(model_usage) == 1:
        (name,) = model_usage.keys()
        if isinstance(name, str) and name.strip():
            return name
    return _MISSING


def parse_result_envelope(stdout: Any) -> ResultEnvelope:
    """Parse a leaf's captured stdout as a `--output-format json` result envelope.

    Never raises: leaf stdout is untrusted, and a crash in the transport is a worse failure
    than a structured `parsed=False` the caller can route. Empty / whitespace stdout, a
    non-JSON body, and a JSON scalar/array (not an object) each yield `parsed=False` with a
    reason; the caller treats all three as "no bundle to read" and repairs or fails closed.
    """
    if not isinstance(stdout, str) or not stdout.strip():
        return ResultEnvelope(False, _MISSING, _MISSING, _MISSING, _MISSING, _MISSING,
                              None, "empty stdout")
    try:
        envelope = json.loads(stdout)
    except (ValueError, RecursionError) as exc:
        # `json.JSONDecodeError` is a `ValueError`; deeply nested hostile stdout raises
        # `RecursionError` (a `RuntimeError`, not a `ValueError`). Both are caught so the
        # transport keeps its "never raises" contract — a crash here would fail the substep
        # hard instead of routing to bounded repair.
        return ResultEnvelope(False, _MISSING, _MISSING, _MISSING, _MISSING, _MISSING,
                              None, f"stdout is not valid JSON: {exc}")
    if not isinstance(envelope, dict):
        return ResultEnvelope(False, _MISSING, _MISSING, _MISSING, _MISSING, _MISSING,
                              envelope, "result envelope is not a JSON object")
    return ResultEnvelope(
        parsed=True,
        result=envelope.get("result", _MISSING),
        is_error=envelope.get("is_error", _MISSING),
        model=_resolve_model(envelope),
        usage=envelope.get("usage", _MISSING),
        session_id=envelope.get("session_id", _MISSING),
        raw=envelope,
        parse_error=None,
    )


# --------------------------------------------------------------------------------------
# Single-document extraction + truncation classification
# --------------------------------------------------------------------------------------

def _object_from_unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`json.loads` `object_pairs_hook` that REJECTS a duplicate key instead of silently
    keeping the last value.

    The model document is the trust boundary, and a stock parse of
    `{"verification_status": "fail", …, "verification_status": "pass"}` collapses to a
    passing dict — a duplicate key could suppress a failure the validators would otherwise
    catch. Raising here turns an ambiguous document into a parse failure, routing it to
    bounded repair rather than letting the last value win. Checked at every nesting level
    (the hook fires per object)."""
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON object key {key!r}")
        obj[key] = value
    return obj


def _reject_non_finite_constant(name: str) -> float:
    """`json.loads` `parse_constant` hook: reject the non-standard bare constants `NaN`,
    `Infinity`, `-Infinity` that Python's parser accepts by default but strict JSON forbids.

    A non-finite value would survive `validate_bundle` (a lowering-plan section like
    `precision` is an intentionally open object) and then serialize to non-standard JSON, so
    it is refused at the parse boundary and routed to bounded repair instead."""
    raise ValueError(f"non-finite JSON constant {name!r} is not allowed")


def _finite_float(token: str) -> float:
    """`json.loads` `parse_float` hook: reject a float TOKEN that overflows to a non-finite
    value (`1e999` → `inf`), which `parse_constant` does not see (it is a number, not a bare
    `Infinity`). Finite floats pass through unchanged."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError(f"non-finite JSON number {token!r} is not allowed")
    return value


def _loads_strict(text: str) -> Any:
    """`json.loads` hardened for the model-document trust boundary: duplicate object keys
    rejected (`_object_from_unique_pairs`) and non-finite values rejected (bare
    `NaN`/`Infinity` via `parse_constant`, overflow floats via `parse_float`)."""
    return json.loads(
        text,
        object_pairs_hook=_object_from_unique_pairs,
        parse_constant=_reject_non_finite_constant,
        parse_float=_finite_float,
    )


def _fenced_json_payload(text: str) -> tuple["str | None", "str | None"]:
    """Isolate a single fenced JSON block from `text`.

    Returns `(payload, None)` when the reply is exactly one fenced block spanning the whole
    reply (only whitespace outside), `(None, None)` when there is no fence (parse the whole
    text), or `(None, category)` when the fencing is a failure: a fence opened but never closed
    is a cut-off answer (`pure_response_truncated`); content before the opening fence, content
    after the closing fence, or an empty block is ambiguous (`pure_response_unparseable`).

    The outer fence is parsed STRUCTURALLY — anchored on the leading fence line and the FINAL
    ``` — never by counting every ``` in the text. A JSON string value may legitimately contain
    a ``` sequence (a CodegenBundle source file, a verdict message), and a marker count would
    misread such a valid single document as truncated/unparseable. Because the payload runs to
    the LAST ```, an in-string ``` stays inside the payload and parses as string data.

    The whole-reply span (nothing outside the fence) closes a multi-document trust hole: this
    path is reached only when the WHOLE text failed to parse (parse-first ran first), so a bare
    document followed by a fenced one lands here, and returning just the fenced payload would
    silently drop the bare document — object, array, OR scalar like `"fail"` — so a bare `fail`
    followed by a fenced `pass` must not read as pass. Rejecting any content outside the fence
    covers every competing document AND stray prose without a fragile "is this prose a document"
    heuristic; the system prompt tells the model to emit no surrounding prose, so a preamble
    reply is false-rejected into a bounded repair — the safe direction for a trust boundary.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        # A fence anywhere but the start means content precedes it (a competing document or
        # prose); no fence at all means "parse the whole text" (the caller then classifies).
        return (None, RESPONSE_UNPARSEABLE) if "```" in stripped else (None, None)
    newline = stripped.find("\n")
    if newline == -1:
        # "```json" with no newline: opened, no content, never closed.
        return None, RESPONSE_TRUNCATED
    if "```" in stripped[3:newline]:
        # A one-line "```json ... ```" opening is malformed.
        return None, RESPONSE_UNPARSEABLE
    rest = stripped[newline + 1:]
    if "```" not in rest:
        return None, RESPONSE_TRUNCATED          # opened, never closed
    if not rest.endswith("```"):
        return None, RESPONSE_UNPARSEABLE         # closed, then trailing content
    payload = rest[:-3]
    if not payload.strip():
        return None, RESPONSE_UNPARSEABLE
    return payload, None


def _classify_json_failure(payload: str) -> str:
    """Classify a `json.loads` failure on `payload` as truncated vs unparseable.

    Truncation (the model hit the output-token ceiling) shows as an unbalanced container —
    more `{`/`[` opened than closed — or a decode error at the very end of the input. Either
    signal returns `pure_response_truncated`; anything else is `pure_response_unparseable`.
    The two categories both route to a bounded repair turn, so a borderline misclassification
    only mislabels the operator's diagnosis excerpt, never changes the control flow. Brace
    counting is over the raw text (a string literal could hold an unbalanced brace), which is
    why the tie is broken toward the safe, bounded repair rather than a hard reject.
    """
    if payload.count("{") > payload.count("}") or payload.count("[") > payload.count("]"):
        return RESPONSE_TRUNCATED
    try:
        json.loads(payload)
    except RecursionError:
        # Deeply nested balanced input the brace count did not catch: over-limit output.
        return RESPONSE_TRUNCATED
    except json.JSONDecodeError as exc:
        if exc.pos >= len(payload.rstrip()):
            return RESPONSE_TRUNCATED
    return RESPONSE_UNPARSEABLE


def extract_json_document(result_text: Any) -> tuple[Any, "str | None"]:
    """Extract the single JSON document a pure leaf returns in its `result` text.

    Returns `(doc, None)` on success (doc is whatever `json.loads` produced — the caller's
    schema validator checks its type), or `(None, category)` where category is one of
    `RESPONSE_CATEGORIES`. A `_MISSING` result (absent key), a JSON `null` result, a
    non-string result, and an empty string are all `pure_response_unparseable` — there is no
    document to read.

    PARSE-FIRST: the whole (stripped) text is tried as JSON before any fence handling, so a
    clean document that merely mentions a ```` ``` ```` fence inside a STRING value — a
    verdict `last_fail_reason`, a Fortran comment in a bundle file — is never rejected by
    fence bookkeeping. Fence extraction is the FALLBACK for a markdown-wrapped answer,
    reached only when the whole text is not itself valid JSON; exactly one ```json fence is
    tolerated there.
    """
    if result_text is _MISSING or result_text is None or not isinstance(result_text, str):
        return None, RESPONSE_UNPARSEABLE
    text = result_text.strip()
    if not text:
        return None, RESPONSE_UNPARSEABLE
    try:
        return _loads_strict(text), None
    except (ValueError, RecursionError):
        pass
    payload, fence_category = _fenced_json_payload(text)
    if fence_category is not None:
        return None, fence_category
    if payload is None:
        # No fence, and the whole-text parse above already failed.
        return None, _classify_json_failure(text)
    try:
        return _loads_strict(payload), None
    except (ValueError, RecursionError):
        return None, _classify_json_failure(payload)


# --------------------------------------------------------------------------------------
# Verify verdict contract
# --------------------------------------------------------------------------------------

# The verify persona returns a verdict JSON; the host projects `verification_status`,
# `issue_severity`, and `last_fail_reason` onto `source_meta.json` (value-type contract:
# `tools/meta_contracts.py`) verbatim. The verdict is a distinct, richer intermediate: it adds
# a structured `findings[]` the meta has no key for, so its shape is pinned here, not in the
# meta contract. `last_fail_reason` is the single AUTHORITATIVE string the producer's
# warm-resume repair reads (as today, via `source_meta.last_fail_reason`); `findings[]` carries
# per-finding detail for diagnostics and provenance and is NOT the repair-text source. Both are
# model-authored. Vocabularies match the conductor's routing (`classify_verify_severity`):
# status is `pass`/`fail`, severity is `none`/`minor`/`major`/`critical`.
VERDICT_STATUSES: tuple[str, ...] = ("pass", "fail")
VERDICT_SEVERITIES: tuple[str, ...] = ("none", "minor", "major", "critical")
VERDICT_REQUIRED_KEYS: tuple[str, ...] = (
    "verification_status", "issue_severity", "last_fail_reason", "findings")


def verify_verdict_violations(doc: Any) -> list[str]:
    """Validate a verify verdict document; an empty list is a valid verdict.

    Schema layer first (presence, enum, type, closed keys), then the joint invariants only
    on a schema-sound document — so one defect is never reported twice and an invariant never
    defends against a mistyped field. Enum checks are EXACT: `verification_status` is the
    literal `"pass"`, so `"PASS"` is rejected rather than case-folded into a pass (a folded
    accept would let a producer's casing choice silence a fail).

    Joint invariants tie the four fields into a single coherent verdict:
    - `pass`  <=> issue_severity `none` <=> findings empty <=> last_fail_reason null;
    - `fail`   => a non-`none` severity, at least one finding, and a non-empty last_fail_reason.
    """
    if not isinstance(doc, dict):
        return ["verdict must be a JSON object"]

    violations: list[str] = []
    for key in VERDICT_REQUIRED_KEYS:
        if key not in doc:
            violations.append(f"{key} is required")
    for key in sorted((k for k in doc if k not in VERDICT_REQUIRED_KEYS), key=repr):
        violations.append(f"unknown key {key!r} (the verdict object is closed)")

    status = doc.get("verification_status", _MISSING)
    if status is not _MISSING and status not in VERDICT_STATUSES:
        violations.append(f"verification_status must be one of {', '.join(VERDICT_STATUSES)}")
    severity = doc.get("issue_severity", _MISSING)
    if severity is not _MISSING and severity not in VERDICT_SEVERITIES:
        violations.append(f"issue_severity must be one of {', '.join(VERDICT_SEVERITIES)}")
    reason = doc.get("last_fail_reason", _MISSING)
    if reason is not _MISSING and reason is not None and not isinstance(reason, str):
        violations.append("last_fail_reason must be a string or null")
    findings = doc.get("findings", _MISSING)
    if findings is not _MISSING:
        if not isinstance(findings, list):
            violations.append("findings must be an array")
        else:
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict):
                    violations.append(f"findings[{index}] must be an object")
                    continue
                summary = finding.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    violations.append(f"findings[{index}].summary must be a non-empty string")

    # Joint invariants run only on a schema-sound document, so a type error below cannot
    # cascade into a spurious invariant clause.
    if violations:
        return violations

    status = doc["verification_status"]
    severity = doc["issue_severity"]
    reason = doc["last_fail_reason"]
    findings = doc["findings"]
    if status == "pass":
        if severity != "none":
            violations.append("verification_status 'pass' requires issue_severity 'none'")
        if findings:
            violations.append("verification_status 'pass' requires an empty findings array")
        if reason is not None:
            violations.append("verification_status 'pass' requires last_fail_reason null")
    else:  # fail
        if severity == "none":
            violations.append(
                "verification_status 'fail' requires a non-'none' issue_severity")
        if not findings:
            violations.append("verification_status 'fail' requires at least one finding")
        if not (isinstance(reason, str) and reason.strip()):
            violations.append(
                "verification_status 'fail' requires a non-empty last_fail_reason")
    return violations
