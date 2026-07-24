"""Deterministic workflow conductor.

Drives the Compile -> Generate -> Build -> Validate phase/substep loop in plain
Python, calling the orchestration_runtime.py bookkeeping subcommands directly and
spawning each substep BODY as an isolated leaf LLM (``claude -p`` / ``codex exec``).

Motivation (see docs/design/deterministic_conductor.md): the pre-migration path used
an LLM "orchestration agent" to drive a deterministic bookkeeping state machine. For
a trivial node the LLM makes essentially no decisions, yet every bookkeeping CLI
output accumulates in its context and is re-read every turn (cache_read grows
O(turns^2)). Moving the deterministic loop into Python removes the parent LLM's
turns, its ~70K static-protocol-doc resident load, and the per-turn accumulation;
the LLM is invoked only as a leaf for the judgement-bearing substeps
(generate/verify/judge) and, on an unclassifiable failure, a one-shot diagnostician.

This module is intentionally self-contained: it reuses the existing
orchestration_runtime.py subcommands (the stable CLI contract) and the
validate_pipeline_semantics validators rather than importing internals, so the
same guards fire as on the LLM path.

Status: M2 happy-path scaffolding. Failure routing (M3) and LLM escalation (M4)
are layered on top of the loop. The request-payload builder is validated against
real, working request.json artifacts in tools/tests/test_workflow_conductor.py.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import yaml


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_yaml(path: Path) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None

# --- phase / substep structure -------------------------------------------------

PHASE_ORDER: tuple[str, ...] = ("compile", "generate", "build", "validate")

# Ordered substeps per phase. Build is a single "step" agent (no substeps),
# represented as [None] so the loop body is uniform.
SUBSTEPS: dict[str, tuple[str | None, ...]] = {
    # compile.static is a deterministic in-process substep run by the conductor AFTER
    # compile.generate produces spec.ir.yaml/ir_meta.json and BEFORE compile.verify:
    #   - static (Conductor._compile_static_inproc): runs validate_workspace_root +
    #     check_artifact_syntax + validate_pipeline_semantics --stage compile; the verify
    #     leaf no longer invokes them, so compile.verify is a pure LLM semantic pass (the
    #     spec-cross-reference invariants V1/V3/V5) reached only on a deterministically-clean
    #     IR. A finding routes back to compile.generate via a warm-resume reopen
    #     (COMPILE_STATIC_FAILURE_ROUTING). Mirrors the static checker of generate.gate.
    "compile": ("generate", "static", "verify"),
    # generate.gate is a single deterministic in-process substep run by the conductor AFTER
    # generate.generate produces source/<id>/src/ and BEFORE generate.verify. It UNIONS the
    # three source checkers into ONE verdict (gate_meta.json), so a source with defects in
    # several classes gets ONE warm repair turn carrying all findings, not one repair turn +
    # one attempt per class:
    #   - lint   (Conductor._gate_lint_check):   runs run_linter. Always runs.
    #   - syntax (Conductor._gate_syntax_check): runs the MCP run_syntax_check compiler
    #     front-end gate (gfortran -fsyntax-only, plus optional target-compiler stages from
    #     METDSL_SYNTAX_COMPILERS) over the staged node + dependency-closure sources, so the
    #     whole class of syntax / standard-conformance compile_errors surfaces here instead
    #     of at Build (fortran-language nodes only; non-fortran passes through). Always runs
    #     (independent of lint); an unfixable-by-leaf attribution (canary / dependency-closure)
    #     raises and surfaces as a transport fail_closed, suppressing gate_meta (fail_closed
    #     dominates a co-occurring lint content-fail — the same order as today, only sooner).
    #   - static (Conductor._gate_static_check): runs validate_pipeline_semantics --stage
    #     post_generate + validate_workspace_root; the verify leaf no longer invokes them, so
    #     verify is a pure LLM semantic (G1-G7) pass reached only on a deterministically-clean
    #     source. Runs ONLY when lint AND syntax both pass — the post_generate certifier
    #     hard-fails on lint/syntax evidence whose ok flag is not true, so running static over a
    #     dirty source would double-report the same defect (recorded
    #     checkers.static.status="skipped", skipped_reason="lint_or_syntax_failed"). The gate
    #     routes its unioned findings back to generate.generate via a warm-resume reopen
    #     (GATE_FAILURE_ROUTING).
    "generate": ("generate", "gate", "verify"),
    "build": (None,),
    # validate wraps the LLM judge in two deterministic conductor-in-process gates,
    # mirroring the compile/generate deterministic-interleave model:
    #   - pre_judge  (Conductor._pre_judge_inproc):  the pre-spawn dependency-DAG readiness
    #     check (a --with-deps closure not built+validated in its own pipeline). Runs BEFORE
    #     execute so a cold judge is never spawned for an incomplete closure. A failure is a
    #     non-physics integrity blocker -> fail_closed (never warm-resumed; no judge has run).
    #   - execute    (Conductor._execute_inproc):    unchanged binary run + evidence capture.
    #   - judge      (LLM leaf):                      pure LLM semantic pass; invokes NO
    #     validator gate (ALLOWED_VALIDATE_PIPELINE_STAGES[(validate,judge)] == frozenset()).
    #   - post_judge (Conductor._post_judge_inproc):  runs `validate_pipeline_semantics
    #     --stage pre_judge` (the gate the judge leaf used to own) and CLASSIFIES the
    #     violation severity. A recoverable (leaf/judge-authored conformance) violation
    #     warm-resumes the judge in place; an orchestration-record/DAG integrity violation
    #     (or an unknown one) is fail_closed. NOTE the naming: the substep is `post_judge`
    #     (it runs AFTER the judge) but the validator STAGE it invokes is literally named
    #     `pre_judge` ("before pass-certification") — do not confuse the two.
    "validate": ("pre_judge", "execute", "judge", "post_judge"),
}

# Build is the only phase whose step_result executor is the (child) step agent;
# the substep-aware phases record the orchestration agent as executor.
SUBSTEP_AWARE_PHASES: frozenset[str] = frozenset({"compile", "generate", "validate"})

# Output basenames excluded from a producer substep's "all deliverables written"
# check: audit/process logs whose presence/placement is not a deliverable contract
# (the MCP command log placement in particular varies by build system).
_OPTIONAL_OUTPUT_BASENAMES: frozenset[str] = frozenset({
    "command_log.jsonl", "stdout.log", "stderr.log",
    "compile.stdout.log", "compile.stderr.log",
})

# Deterministic in-process build/run capture limit. The canonical per-step
# stdout/stderr log files must be FULL (untrimmed); the MCP `_run_command` trims its
# returned stdout/stderr to this byte budget, so we pass a large value to avoid losing
# detail (e.g. a big compiler error dump). The runner writes its data to JSON files,
# not stdout, so program stdout/stderr are normally tiny regardless.
_FULL_CAPTURE_LIMIT: int = 50_000_000


def child_agent_role(step: str) -> str:
    """The agent_role of the leaf child for a phase: build => step, else substep."""
    return "step" if step == "build" else "substep"


def phase_index(step: str) -> int:
    return PHASE_ORDER.index(step)


def phases_through(until_phase: str) -> tuple[str, ...]:
    return PHASE_ORDER[: phase_index(until_phase) + 1]


# --- deterministic failure-routing decision tables -----------------------------
#
# Canonical sources:
#   docs/workflow/phases/phase_03_build.md  (Build failure_category -> retry)
#   docs/workflow/phases/phase_04_validate.md  (Validate.judge failure_class x attribution)
# Kept as data so the conductor (and its unit tests) route deterministically.

# Build failure_category -> (retry_target_phase, repair_strategy)
BUILD_FAILURE_ROUTING: dict[str, tuple[str, str]] = {
    "compile_error": ("generate", "reuse"),
    "link_error": ("generate", "reuse"),
    "make_error": ("generate", "restart"),
    "dependency_violation": ("generate", "restart"),
    "validate_post_build_violation": ("generate", "restart"),
}

# Generate.gate failure_category -> (retry_target_phase, repair_strategy). The single
# deterministic gate substep unions the lint / syntax / static checkers; each per-checker
# category re-runs generate.generate with a warm resume (reuse) so the same leaf fixes its own
# source with context intact, avoiding a cold restart. This is a SAME-PHASE reopen
# (target==generate while the failing substep is generate.gate); conduct() handles that case
# specially. The table is the union of the three former per-checker tables (lint_findings /
# syntax_error / post_generate_violation / workspace_root_violation) — every non-terminal
# category routes to ("generate", "reuse"), so a union of several categories in one attempt
# routes as one warm reuse carrying all findings.
GATE_FAILURE_ROUTING: dict[str, tuple[str, str]] = {
    "syntax_error": ("generate", "reuse"),
    "lint_findings": ("generate", "reuse"),
    "post_generate_violation": ("generate", "reuse"),
    "workspace_root_violation": ("generate", "reuse"),
}

# Gate categories that are TERMINAL (fail_closed), NOT a warm Generate.generate retry: the
# failing condition is one the Generate leaf cannot repair by re-authoring its source, so retrying is
# futile. `stale_dependency_ir` — a certified dependency IR predating a carrier contract (e.g. the
# harness's public_api.module_parameters) reached Generate.gate on a resume that skipped Compile;
# the fix is a re-certification (a version bump makes dependency freshness re-run it), not a re-author.
# A terminal category dominates any co-occurring warm-retry category in classify_gate_failure.
GATE_FAILURE_TERMINAL: frozenset[str] = frozenset({"stale_dependency_ir"})

# Canonical ordering of gate failure categories in the route reason and the composed excerpt
# sections: syntax_error -> lint_findings -> static-family categories. `_gate_inproc` records
# gate_meta.failure_categories in this order (static categories only ever appear alone, since
# static runs only when lint AND syntax passed); classify_gate_failure re-sorts defensively so
# the reason is deterministic even for a synthetic multi-category input.
_GATE_CATEGORY_CANON_ORDER: tuple[str, ...] = (
    "syntax_error",
    "lint_findings",
    "workspace_root_violation",
    "post_generate_violation",
    "stale_dependency_ir",
)


def _gate_categories_canonical(categories: list[str]) -> list[str]:
    """Dedupe + canonical-order a gate failure_categories list. Unknown categories keep their
    first-seen order after the known ones, so a novel category still reaches the escalate path."""
    seen: list[str] = []
    for c in categories:
        if c and c not in seen:
            seen.append(c)
    known = [c for c in _GATE_CATEGORY_CANON_ORDER if c in seen]
    unknown = [c for c in seen if c not in _GATE_CATEGORY_CANON_ORDER]
    return known + unknown

# Compile static-gate (compile.static) failure_category -> (retry_target_phase, repair_strategy).
# The deterministic workspace_root / check_artifact_syntax / --stage compile gates run AFTER
# compile.generate and BEFORE compile.verify; a structural IR violation re-runs
# compile.generate with a warm resume (reuse), exactly like a generate.gate finding, so the
# same leaf fixes its own IR with context intact. Like the generate gate this is a SAME-PHASE reopen
# handled specially by conduct(). Compile is the first phase, so the target is necessarily the
# current phase.
COMPILE_STATIC_FAILURE_ROUTING: dict[str, tuple[str, str]] = {
    "compile_static_violation": ("compile", "reuse"),
}

# Bound on the validate.execute `failure_excerpt`, which is rendered verbatim into the slim
# repair prompt. Both axes are needed: the 50-line tail matches the binary_meta / gate_meta
# convention, but a post_execute violation is NOT line-shaped like compiler stderr — it prints
# whole dict/list payloads inline (`declared state_variables missing in snapshot files ({...})`),
# and `_snapshot_deliverable_gap` emits its expected/written/missing sets on a single line. Fifty
# such lines can exceed any prompt budget, so the tail is also capped by character count.
_EXECUTE_EXCERPT_MAX_LINES = 50
_EXECUTE_EXCERPT_MAX_CHARS = 4000
_EXECUTE_EXCERPT_TRUNCATION_MARK = "[... excerpt truncated to the last "

# Per-attempt failure excerpt bound in bundle_meta.json / verdict_meta.json. These are OBSERVABILITY
# fields (why each superseded pure attempt failed), rendered into no repair prompt — the terminal
# top-level failure_excerpt is the repair carrier — so a short cap keeps the meta small while still
# naming the failure class.
_PURE_ATTEMPT_EXCERPT_MAX_CHARS = 400


def _execute_failure_excerpt(text: str) -> str:
    """The bounded tail of an `[execute fail]` report, safe to render into a repair prompt."""
    tail = "\n".join(text.splitlines()[-_EXECUTE_EXCERPT_MAX_LINES:])
    if len(tail) <= _EXECUTE_EXCERPT_MAX_CHARS:
        return tail
    return (f"{_EXECUTE_EXCERPT_TRUNCATION_MARK}{_EXECUTE_EXCERPT_MAX_CHARS} characters ...]\n"
            + tail[-_EXECUTE_EXCERPT_MAX_CHARS:])


# Marker of the per-test predicate failure report. Deliberately NOT the `[execute fail]` literal
# the structural branch uses (`orchestration_runtime._EXECUTE_FAIL_MARKER`, which the dev resume
# directive's stderr-log fallback searches for): a predicate failure is a different failure class
# and reaches that directive through `trial_meta.json#failure_excerpt` only.
_VERDICT_FAIL_MARKER = "[execute fail: verdict]"


def _verdict_failure_report(verdict_doc: dict[str, Any]) -> str:
    """The `[execute fail: verdict]` block for a `self_verdict=fail` run.

    Names every unsatisfied condition (`ref` / `op` / case / reason or lhs-vs-rhs), not merely
    the failing test: this text is the findings a repair leaf reasons from, and `verdict.json`
    is outside the leaf's read set."""
    lines = [
        f"{_VERDICT_FAIL_MARKER} deterministic per-test verdict is fail "
        f"(failure_class={verdict_doc.get('failure_class')}); the judge leaf was not spawned. "
        f"See verdict.json#per_test for the failing predicate(s)."
    ]
    error = verdict_doc.get("predicate_error")
    if isinstance(error, str) and error.strip():
        lines.append(f"predicate_error: {error.strip()}")
    per_test = verdict_doc.get("per_test")
    for item in (per_test if isinstance(per_test, list) else []):
        if not isinstance(item, dict) or item.get("status") != "fail":
            continue
        lines.append(f"- test {item.get('test_id')}: fail")
        basis = item.get("basis")
        conditions = basis.get("conditions") if isinstance(basis, dict) else None
        for cond in (conditions if isinstance(conditions, list) else []):
            if not isinstance(cond, dict):
                continue
            evaluated = cond.get("evaluated")
            for ev in (evaluated if isinstance(evaluated, list) else []):
                if not isinstance(ev, dict) or ev.get("satisfied"):
                    continue
                parts = [f"ref={ev.get('ref', cond.get('ref'))!r}",
                         f"op={ev.get('op', cond.get('op'))!r}"]
                if ev.get("case") is not None:
                    parts.append(f"case={ev['case']!r}")
                if ev.get("reason"):
                    parts.append(f"reason={ev['reason']}")
                if "lhs" in ev:
                    parts.append(f"lhs={ev['lhs']!r}")
                if "rhs" in ev:
                    parts.append(f"rhs={ev['rhs']!r}")
                lines.append("  - " + " ".join(parts))
    return "\n".join(lines)


# Validate.execute STRUCTURAL failure_category -> (retry_target_phase, repair_strategy).
# A structural execute failure authors no verdict.json (the judge leaf never ran), so the
# defect is in the generated runner/model code, not in a physics predicate: the same class the
# judge would have reported as ("structural_violation", "code") -> ("generate", "reuse"). It is
# routed warm (reuse) with the gate's own violation text threaded through as repair findings
# (trial_meta.json#failure_excerpt), instead of a blind cold restart that discards the reason
# the run failed. `_execute_inproc` records the category; an execute failure with NO trial_meta
# (a runner runtime error, whose cause is in stderr rather than a gate report) keeps the cold
# restart. The C2 counter / Compile-reopen backstop runs first and is unaffected.
VALIDATE_EXECUTE_FAILURE_ROUTING: dict[str, tuple[str, str]] = {
    "post_execute_violation": ("generate", "reuse"),
    "snapshot_deliverable_gap": ("generate", "reuse"),
    "quality_check_mismatch": ("generate", "reuse"),
}

# Route-reason prefix for the table above: `<prefix><failure_category>`. Also the prefix of the
# no-category `validate_execute_fail` restart reason and of the per-test predicate reasons
# (`validate_execute_physics_fail` / `validate_execute_structural_violation`), so consumers must
# match on the CATEGORY suffix (a table key), never on the prefix alone.
VALIDATE_EXECUTE_REASON_PREFIX = "validate_execute_"

# Categories the table above routes to Generate that a HOST-RENDERED-runner node (M3c, see
# `_conductor_authors_runner`) cannot repair there. On such a node the leaf authors only
# `<spec_id>_model.f90` + `<spec_id>_checks.f90`; `src/<spec_id>_runner.f90` is rendered by the
# conductor from the IR (`runner_renderer.render_runner`), which emits the per-case
# `__write_snapshot` call for every `case.test_case_set[].case_id`. A missing per-case snapshot
# file is therefore decided entirely by the IR + the renderer — regenerating model/checks cannot
# add one — so it is attributed to the IR and reopens Compile, instead of burning a Generate
# attempt that provably cannot converge (the C1/C2 "regenerating one side can't fix the other"
# pattern). `post_execute_violation` and `quality_check_mismatch` stay on the Generate route even
# on an M3c node: the renderer boxes that case's required variables unconditionally (it discards
# the leaf registry's found-flag), so the key set and shapes are host-fixed by the IR, but every
# VALUE comes from the leaf's checks module — a trivial (all-zero) basis, a NaN, or a wrong metric
# is exactly what a warm repair fixes.
HOST_RENDERED_RUNNER_UNREPAIRABLE: frozenset[str] = frozenset({"snapshot_deliverable_gap"})

# --- Z2 pure-leaf CodegenBundle producer routing (M-C) -------------------------
# The pure `generate.generate` producer returns exactly one CodegenBundle JSON document;
# the host validates it (transport parse, `validate_bundle`, assembly preflight) and repairs
# a violation in a bounded in-conversation warm-resume loop (MAX_BUNDLE_REPAIR_TURNS,
# `tools/pure_leaf`). Only when that budget is exhausted does the substep fail with the
# terminal category recorded in `bundle_meta.json#failure_category`; run_phase then routes it
# here. Every category is a defect in the model's returned document, so the route is a fresh
# (generate, generate) attempt with a warm reuse repair — the same "this phase's own producer"
# recovery the deterministic-gate tables use. The route reason is `<prefix><category>`, so
# `_read_repair_findings` threads `bundle_meta.json#failure_excerpt` through the repair.
GENERATE_BUNDLE_REASON_PREFIX = "generate_bundle_"
GENERATE_BUNDLE_FAILURE_CATEGORIES: tuple[str, ...] = (
    "pure_response_unparseable",
    "pure_response_truncated",
    "bundle_schema_violation",
    "bundle_capability_unsatisfied",
    "bundle_state_binding_mismatch",
    "bundle_assembly_collision",
    "bundle_checks_abi_violation",
    "bundle_shape_unsupported",
)
GENERATE_BUNDLE_FAILURE_ROUTING: dict[str, tuple[str, str]] = {
    category: ("generate", "reuse") for category in GENERATE_BUNDLE_FAILURE_CATEGORIES
}

# --- Z2 pure-leaf verify-verdict routing (M-D) ---------------------------------
# The pure `generate.verify` reviewer returns exactly one verify-verdict JSON document; the host
# validates it (transport parse, `verify_verdict_violations`) and repairs a SCHEMA violation in a
# bounded in-conversation warm-resume loop (MAX_BUNDLE_REPAIR_TURNS, `tools/pure_leaf`). A
# schema-VALID verdict (pass OR fail) is the reviewer's answer and is NEVER repaired here — a
# `fail` verdict projects onto source_meta.json and routes through the normal verify-severity gate
# (classify_verify_severity), exactly like the agentic verify leaf. Only a persistently MALFORMED
# verdict (unparseable / truncated / schema-invalid past the repair budget) reaches this table:
# that is a verify-LEAF behavior defect, not a bundle defect, and the reviewer's own broken output
# gives the host nothing to hand a producer. So the route is a COLD generate restart (a fresh
# producer AND a fresh reviewer), the sound analog of the producer's bundle-exhaustion route.
#
# DEVIATION (documented): the plan sketched `("verify","reuse")`, but a phase reopen hands its
# repair to substep index 0 (the producer) only — `verify` is index 4 and cannot be the reopen
# target, so a cross-phase "reuse the verify session" is structurally impossible. The in-session
# reuse (warm-resume of the same reviewer) already happened inside `_run_pure_verify_substep` and
# is what the budget bounds; once exhausted, reusing that same broken session again is pointless,
# and threading a "your verdict was malformed" excerpt to the producer would risk perturbing a
# GOOD bundle. A cold restart is therefore both the only valid and the safest recovery. The
# terminal category/excerpt + per-attempt usage are still persisted (verdict_meta.json) for
# provenance and A/B metering (M-E), mirroring bundle_meta.json.
GENERATE_VERDICT_REASON_PREFIX = "generate_verdict_"
GENERATE_VERDICT_SCHEMA_VIOLATION = "verdict_schema_violation"
GENERATE_VERDICT_FAILURE_CATEGORIES: tuple[str, ...] = (
    "pure_response_unparseable",
    "pure_response_truncated",
    GENERATE_VERDICT_SCHEMA_VIOLATION,
)
GENERATE_VERDICT_FAILURE_ROUTING: dict[str, tuple[str, str]] = {
    category: ("generate", "restart") for category in GENERATE_VERDICT_FAILURE_CATEGORIES
}

# Validate.judge (failure_class, attribution) -> routing action.
# Action is one of:
#   ("generate", strategy) | ("compile", "reopen") | ("validate", "re_execute")
#   ("fail_closed", None)  -> manual intervention (spec attribution)
VALIDATE_JUDGE_ROUTING: dict[tuple[str, str], tuple[str, str | None]] = {
    ("evidence_mismatch", "code"): ("generate", "reuse"),
    ("evidence_mismatch", "ir"): ("compile", "reopen"),
    ("evidence_mismatch", "evidence"): ("validate", "re_execute"),
    ("physics_fail", "code"): ("generate", "reuse"),
    ("physics_fail", "ir"): ("compile", "reopen"),
    ("physics_fail", "spec"): ("fail_closed", None),
    ("runtime_error", "code"): ("generate", "reuse"),
    ("structural_violation", "code"): ("generate", "reuse"),
    ("structural_violation", "ir"): ("compile", "reopen"),
}


# post_judge severity classification. The `--stage pre_judge` gate accumulates FREE-TEXT
# violation strings (no structured category), each prefixed with the offending artifact
# path, so the classifier keys on that leading path token.
#
#   - recoverable   : the violation is judge-fixable by re-running the judge (warm resume).
#                     As of R2 this is scoped to the judge's ONLY deliverable —
#                     semantic_review.json (incl. the review_method literal). NOTHING else is
#                     judge-fixable: verdict.json is HOST-authored at execute, and the derived
#                     aggregate_verdict.json / summary.json / validate_meta.json are HOST-authored
#                     at post_judge (correct-by-construction from the host verdict.json). A
#                     warm-resume re-runs the judge but NOT execute, and re-derives the artifacts
#                     from the SAME verdict.json, so a violation naming any of them would repeat
#                     identically until the budget is exhausted — a conductor/derivation defect,
#                     not a judge one, so it must terminalize instead of wasting judge spawns.
#   - unrecoverable : orchestration-record / cross-pipeline dependency-DAG integrity, OR a
#                     host-authored artifact defect (verdict.json / the post_judge-derived
#                     aggregate_verdict.json / summary.json / validate_meta.json). Re-running the
#                     judge cannot fix these. Sources: _validate_orchestration_hierarchy
#                     (agent_graph.json / step_result.json / an orchestrations/ root), the
#                     cross-pipeline DAG check (lineage.json / the literal DAG messages), and the
#                     host-authored verdict/derived artifacts.
#   - unknown       : anything else (incl. execute-authored evidence) -> conservatively terminal
#                     (fail_closed) for now; a future escalate-LLM adjudicator would decide here.
_POST_JUDGE_RECOVERABLE_BASENAMES: frozenset[str] = frozenset({
    "semantic_review.json",
})
_POST_JUDGE_UNRECOVERABLE_BASENAMES: frozenset[str] = frozenset({
    "agent_graph.json", "step_result.json", "lineage.json", "verdict.json",
    "aggregate_verdict.json", "summary.json", "validate_meta.json",
})
_POST_JUDGE_UNRECOVERABLE_MARKERS: tuple[str, ...] = (
    "copy_based_artifact_reuse detected", "dependency DAG incomplete",
)


def classify_post_judge_violations(violations: list[str]) -> str:
    """Classify a post_judge `--stage pre_judge` violation list into one of
    {"recoverable", "unrecoverable", "unknown"}.

    Precedence is strict: unrecoverable > unknown > recoverable. Any single
    orchestration-record/DAG integrity violation dominates (the node is not certifiable in
    this run); a single unclassifiable line forces escalation/terminalization rather than an
    optimistic warm resume. An empty list is "unknown" (a FAIL with no parsable bullet is not
    a recoverable conformance issue)."""
    if not violations:
        return "unknown"
    saw_recoverable = False
    saw_unknown = False
    for raw in violations:
        line = raw.strip()
        # The offending artifact path is the leading token, delimited by ':' or whitespace.
        head = line.split(":", 1)[0].strip()
        token = head.split()[0] if head.split() else head
        basename = token.rsplit("/", 1)[-1]
        if (basename in _POST_JUDGE_UNRECOVERABLE_BASENAMES
                or "orchestrations/" in token
                or any(m in line for m in _POST_JUDGE_UNRECOVERABLE_MARKERS)):
            return "unrecoverable"
        if basename in _POST_JUDGE_RECOVERABLE_BASENAMES:
            saw_recoverable = True
        else:
            saw_unknown = True
    if saw_unknown:
        return "unknown"
    return "recoverable" if saw_recoverable else "unknown"


# Bound the deterministic retry/reopen loop so a persistently-failing node cannot
# spin forever; matches the operator-observed ceiling of ~3 reopens.
MAX_ATTEMPTS_PER_PHASE = 3

# The repair_reason that marks a verify re-run whose SOLE task is to re-author its own
# stage meta (`_maybe_warm_resume_verify_meta`). build_launch_request keys on it to narrow
# allowed_output_paths to the meta alone, so "re-author only the meta" is carried by the leaf's
# TRUSTED deliverable list and the file-tool write guard (see the narrowing block there for what
# does and does not enforce it) rather than by prose the leaf is told to distrust — the findings
# text sits inside the slim prompt's untrusted-data fence.
VERIFY_META_SCHEMA_REPAIR_REASON = "verify_meta_schema"

# C2 backstop: after this many consecutive execute (no-verdict) failures on a node, a
# Generate restart is deemed unable to fix the (IR-rooted) structural mismatch, so the
# defect is reattributed to the IR and Compile is reopened instead of looping Generate.
# Kept < MAX_ATTEMPTS_PER_PHASE so the escalation fires within the attempt budget.
C2_EXECUTE_FAIL_ESCALATION_THRESHOLD = 2


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of classifying a substep/phase result."""

    action: str  # advance | retry | reopen | fail_closed | escalate
    target_phase: str | None = None
    repair_strategy: str | None = None
    reason: str | None = None
    # The escalate LLM's graded severity (minor | major | critical | None). Governs
    # reuse-vs-discard via resolve_severity_directive (G5); None for non-escalate decisions.
    severity: str | None = None


class SandboxEnforcementError(RuntimeError):
    """Raised when bwrap enforcement is mandatory but a leaf cannot be sandboxed
    (no usable profile). Surfaced so the conductor terminalizes as `fail_closed` rather
    than a generic conductor error."""


def classify_build_failure(failure_category: str | None) -> RouteDecision:
    if not failure_category:
        return RouteDecision("escalate", reason="build_fail_no_category")
    routed = BUILD_FAILURE_ROUTING.get(failure_category)
    if routed is None:
        return RouteDecision("escalate", reason=f"build_unknown_category:{failure_category}")
    target, strategy = routed
    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                         reason=f"build_{failure_category}")


def classify_gate_failure(categories: list[str] | None) -> RouteDecision:
    """Route a Generate.gate union verdict from its list of per-checker failure categories.

    Precedence is strict and total:
      - empty (a FAIL with no parseable category) -> escalate("gate_fail_no_category")
      - any TERMINAL category present             -> fail_closed (dominates a co-occurring warm
                                                     category; the leaf cannot re-author its way
                                                     out of a stale certified dependency IR)
      - any UNKNOWN category present              -> escalate (a novel category the tables do not
                                                     cover; the diagnostician decides)
      - all categories known + non-terminal       -> retry ("generate", "reuse")
    The reason is `gate_<c1>+<c2>+...` in canonical order (`_gate_categories_canonical`), so
    `_read_repair_findings` (prefix `gate_`) threads gate_meta.json#failure_excerpt through the
    warm repair, and the reopen carve-out (trigger substep == "gate") accepts it."""
    ordered = _gate_categories_canonical(list(categories or []))
    if not ordered:
        return RouteDecision("escalate", reason="gate_fail_no_category")
    reason = "gate_" + "+".join(ordered)
    if any(c in GATE_FAILURE_TERMINAL for c in ordered):
        # No warm retry: a stale certified dependency IR (or any terminal category) is not
        # repairable by re-authoring source. Fail closed so the operator re-certifies instead of
        # exhausting Generate retries. Terminal dominates any co-occurring warm category.
        return RouteDecision("fail_closed", reason=reason)
    unknown = [c for c in ordered if c not in GATE_FAILURE_ROUTING]
    if unknown:
        return RouteDecision("escalate",
                             reason=f"gate_unknown_category:{'+'.join(unknown)}")
    # Every category is a known, non-terminal warm-retry category -> one warm reuse carrying the
    # unioned findings. All GATE_FAILURE_ROUTING entries share the ("generate","reuse") target.
    return RouteDecision("retry", target_phase="generate", repair_strategy="reuse",
                         reason=reason)


def classify_compile_static_failure(failure_category: str | None) -> RouteDecision:
    if not failure_category:
        return RouteDecision("escalate", reason="compile_static_fail_no_category")
    routed = COMPILE_STATIC_FAILURE_ROUTING.get(failure_category)
    if routed is None:
        return RouteDecision("escalate",
                             reason=f"compile_static_unknown_category:{failure_category}")
    target, strategy = routed
    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                         reason=f"compile_static_{failure_category}")


def classify_validate_judge(failure_class: str | None, attribution: str | None) -> RouteDecision:
    if failure_class == "pass":
        return RouteDecision("advance")
    if not failure_class or not attribution:
        return RouteDecision("escalate", reason="judge_missing_class_or_attribution")
    routed = VALIDATE_JUDGE_ROUTING.get((failure_class, attribution))
    if routed is None:
        return RouteDecision("escalate",
                             reason=f"judge_unrouted:{failure_class}/{attribution}")
    target, strategy = routed
    if target == "fail_closed":
        return RouteDecision("fail_closed", reason=f"judge_{failure_class}_spec")
    if target == "compile":
        return RouteDecision("reopen", target_phase="compile",
                             reason=f"judge_{failure_class}_ir")
    if target == "validate":
        return RouteDecision("retry", target_phase="validate", repair_strategy="re_execute",
                             reason=f"judge_{failure_class}_evidence")
    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                         reason=f"judge_{failure_class}_{attribution}")


def classify_verify_severity(issue_severity: str | None, workflow_mode: str) -> RouteDecision:
    """verify severity gate. A verify finding is NOT tolerated — it routes by severity:
    - minor          => warm (reuse) SAME-PHASE repair: re-run the phase's producer substep
                        (compile.generate / generate.generate) resuming its session, with the
                        finding injected (slim), to fix the exact issue inheriting its context.
    - major|critical => dev: fail_closed (fast operator feedback); prod: escalate (the
                        diagnostician decides reuse/restart/reopen/fail_closed)."""
    sev = (issue_severity or "none").lower()
    if sev in ("none", ""):
        return RouteDecision("advance")
    if workflow_mode == "dev" and sev in ("major", "critical"):
        return RouteDecision("fail_closed", reason=f"dev_verify_{sev}")
    if sev == "minor":
        return RouteDecision("retry", repair_strategy="reuse",
                             reason="verify_minor")
    return RouteDecision("escalate", reason=f"verify_severity_{sev}")


# --- LLM diagnostician (escalation for unclassifiable failures) -----------------

_DIRECTIVE_SCHEMA = (
    'Output EXACTLY ONE JSON object as the FINAL line, with keys:\n'
    '- "action": "retry" | "reopen" | "fail_closed"\n'
    '- "target_phase": "compile" | "generate" | null\n'
    '- "severity": "minor" | "major" | "critical"\n'
    '- "repair_strategy": "reuse" | "restart" | null\n'
    '- "reason": short string\n'
    'severity grades how disruptive the defect is and GOVERNS whether existing artifacts are '
    'reused (warm-repaired in place) or discarded (regenerated from scratch): minor -> reuse; '
    'major -> reuse by default (set repair_strategy="restart" only if the existing artifacts '
    'are too compromised to repair); critical -> discard (restart). Routing guidance: code '
    'defect OR wrong/insufficient primary evidence (the runner emits bad evidence — a bare '
    're-run reproduces it) -> action=retry target_phase=generate; IR defect -> action=reopen '
    'target_phase=compile; spec defect or genuinely unrecoverable -> action=fail_closed. '
    '(target_phase=build/validate and repair_strategy=re_execute are NOT actionable here — '
    'the conductor cannot re-run a downstream phase in place; regenerate upstream instead.)'
)


# G5: the escalate persona is the workflow-escalate SKILL body, read host-side and rendered
# into the diagnostician prompt (Option A — the read-only leaf reads nothing; everything is
# embedded). Falls back to a minimal inline persona if the SKILL is missing (partial checkout)
# so escalate never crashes. Keep this fallback and the SKILL's persona in lockstep.
_ESCALATE_SKILL_REL = "skills/workflow-escalate/SKILL.md"
_ESCALATE_PERSONA_FALLBACK = (
    "You are a workflow failure diagnostician. Read-only, one shot: reason over "
    "the artifacts below and emit a single routing directive. Do NOT write files "
    "or call tools."
)
_escalate_persona_cache: dict[str, str] = {}


# The floor an artifact keeps in the diagnosis prompt even when many artifacts compete for the
# total budget: enough to identify it and read its leading fields.
_MIN_ARTIFACT_BUDGET = 400

# The artifacts a failed phase's own leaf authored — its primary evidence. They lead the diagnosis
# context, and `_bounded_context_json` spends its budget greedily in order, so leading is what wins
# them the large slices.
_PHASE_PRIMARY_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "compile": ("ir_meta.json",),
    # gate_meta.json leads so a `gate_unknown_category:` escalate reasons over the union verdict
    # first; source_meta.json (the verify verdict) is not authored until the gate has passed.
    "generate": ("gate_meta.json", "source_meta.json"),
    "build": ("binary_meta.json",),
    "validate": ("verdict.json", "semantic_review.json", "aggregate_verdict.json",
                 "post_judge_meta.json", "pre_judge_meta.json"),
}


def _load_escalate_persona(repo_root: Path) -> str:
    """Return the workflow-escalate SKILL body (frontmatter stripped), memoized per repo_root,
    falling back to _ESCALATE_PERSONA_FALLBACK if the file is absent/unreadable."""
    key = str(repo_root)
    cached = _escalate_persona_cache.get(key)
    if cached is not None:
        return cached
    persona = _ESCALATE_PERSONA_FALLBACK
    try:
        text = (repo_root / _ESCALATE_SKILL_REL).read_text(encoding="utf-8")
        # Strip the leading YAML frontmatter (--- ... ---); the body is the persona.
        if text.startswith("---"):
            parts = text.split("---", 2)
            text = parts[2] if len(parts) == 3 else text
        body = text.strip()
        if body:
            persona = body
    except OSError:
        pass
    _escalate_persona_cache[key] = persona
    return persona


_TRUNCATION_MARKER = "\n…[truncated]"


def _serialized_len(text: str) -> int:
    """The cost of `text` once it is embedded in the output as a JSON string.

    A truncated artifact is carried as a STRING, so the final `json.dumps` escapes it a second
    time: every `"`, `\\` and newline in it doubles. Budgeting against the raw slice length would
    therefore undercount quote-dense content by up to ~2x.
    """
    return len(json.dumps(text, ensure_ascii=False))


def _truncate_to_serialized_budget(body: str, allowance: int) -> str:
    """The longest prefix of `body` whose SERIALIZED form, marker included, fits `allowance`."""
    if _serialized_len(body) <= allowance:
        return body
    lo, hi = 0, len(body)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _serialized_len(body[:mid] + _TRUNCATION_MARKER) <= allowance:
            lo = mid
        else:
            hi = mid - 1
    return body[:lo] + _TRUNCATION_MARKER


def _bounded_context_json(context: dict[str, Any], per_artifact: int = 6000,
                          total: int = 12000) -> str:
    """The failure artifacts as JSON, budgeted per artifact so every one of them appears.

    Truncating the whole dump at a single offset silently drops whichever artifacts happen to sort
    last — and the primary evidence of the failed phase is exactly what the diagnostician then
    cannot see, so it concludes "insufficient evidence" and fails closed on a leaf that was right.

    The budget is spent GREEDILY IN ORDER, which is what makes the caller's ordering meaningful:
    each artifact may take up to `per_artifact`, but never so much that a later one drops below
    `_MIN_ARTIFACT_BUDGET`. So the failed phase's own artifacts (which `_gather_failure_context`
    puts first) get the large slices, and every remaining artifact still appears — truncated to a
    marked string rather than dropped. A lone artifact gets the full `per_artifact`, so this never
    yields less evidence than the flat cap it replaced. The result is always valid JSON.

    Both the truncation and the accounting are done in SERIALIZED characters — what the prompt
    actually pays — so a quote- or newline-dense artifact cannot blow past the budget by escaping.
    """
    items = list(context.items())
    if not items:
        return "{}"
    bounded: dict[str, Any] = {}
    spent = 0
    for idx, (name, value) in enumerate(items):
        reserved = (len(items) - idx - 1) * _MIN_ARTIFACT_BUDGET
        allowance = max(_MIN_ARTIFACT_BUDGET, min(per_artifact, total - spent - reserved))
        body = json.dumps(value, indent=1, ensure_ascii=False)
        if len(body) <= allowance:
            # Kept verbatim as JSON — no second escaping pass, so it costs its own length.
            bounded[name] = value
            spent += len(body)
        else:
            truncated = _truncate_to_serialized_budget(body, allowance)
            bounded[name] = truncated
            spent += _serialized_len(truncated)
    return json.dumps(bounded, indent=1, ensure_ascii=False)


def _diagnosis_prompt(node_key: str, phase: str, failed_arids: list[str],
                      context: dict[str, Any], workflow_mode: str,
                      persona: str = _ESCALATE_PERSONA_FALLBACK) -> str:
    ctx_json = _bounded_context_json(context)
    return (
        f"{persona}\n\n"
        f"node_key: {node_key}\n"
        f"failed phase: {phase}\n"
        f"workflow_mode: {workflow_mode}\n"
        f"failed substep agent_run_ids: {failed_arids}\n\n"
        f"failure artifacts (JSON):\n{ctx_json}\n\n"
        f"{_DIRECTIVE_SCHEMA}\n"
    )


def _last_json_object(text: str) -> Any:
    """Return the last balanced top-level {...} that parses as JSON, or None."""
    best = None
    depth = 0
    start: int | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    best = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    pass
                start = None
    return best


# The only rollback targets the diagnostician may name (the LLM-authored producers the
# conductor can regenerate). `build` / `validate` are deterministic phases the conductor cannot
# re-run in place to fix a defect, so an out-of-contract target -> None -> fail_closed rather
# than a wasted reopen. Matches the _DIRECTIVE_SCHEMA / workflow-escalate SKILL enum.
_DIAGNOSTICIAN_TARGET_PHASES: frozenset[str] = frozenset({"compile", "generate"})


def _parse_directive(stdout: str) -> RouteDecision | None:
    """Parse + validate the diagnostician's JSON directive into a RouteDecision.
    Returns None on any malformed/out-of-vocabulary directive (caller fails closed)."""
    obj = _last_json_object(stdout or "")
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    if action not in ("retry", "reopen", "fail_closed"):
        return None
    target = obj.get("target_phase")
    if target is not None and target not in _DIAGNOSTICIAN_TARGET_PHASES:
        return None
    strategy = obj.get("repair_strategy")
    if strategy not in (None, "reuse", "restart"):
        strategy = None
    # G5: severity governs reuse-vs-discard (resolve_severity_directive). Default to `major`
    # when absent so legacy directives (and the pre-G5 DiagnosticianTest fixtures) still parse;
    # an out-of-vocab value also defaults to major rather than rejecting the whole directive.
    severity = obj.get("severity")
    if severity not in ("minor", "major", "critical"):
        severity = "major"
    reason = str(obj.get("reason") or "diagnostician")[:120]
    if action == "fail_closed":
        return RouteDecision("fail_closed", reason=reason, severity=severity)
    if action == "reopen":
        if target is None:
            return None
        # Preserve repair_strategy for reopen too: both the SAME-phase producer reopen and the
        # cross-phase reopen honor it (reuse → warm-resume the producer, restart/none → cold).
        return RouteDecision("reopen", target_phase=target, repair_strategy=strategy,
                             reason=reason, severity=severity)
    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                         reason=reason, severity=severity)


# G5 canonical severity -> repair_strategy policy (single source of truth). Derived with a
# bounded LLM override: minor forces reuse; major defaults to reuse but honors an explicit
# LLM restart; critical forces restart (discard). `re_execute` (validate evidence re-run) is
# orthogonal to reuse/discard and passes through unchanged. `target_phase` is NOT clamped by
# severity (the LLM's rollback distance is honored as-is; conduct's dev_phase_rollback gate
# still catches a dev cross-phase reopen). "Discard" == the existing `restart` strategy
# (_ensure_fresh_producer_id id-rotation + reopen_phase supersede; nothing is deleted).
_SEVERITY_FORCED_STRATEGY: dict[str, str] = {
    "minor": "reuse",      # forced — an LLM restart is ignored
    "critical": "restart",  # forced — an LLM reuse is ignored
}


def resolve_severity_directive(decision: RouteDecision) -> RouteDecision:
    """Normalize an escalate directive's `repair_strategy` from its `severity` per the G5
    policy. No-op for a fail_closed decision, one carrying no severity, or one with no explicit
    `target_phase` (an ambiguous/incomplete directive that must terminalize, NOT be turned into
    a same-phase producer reopen — synthesizing a strategy would make `conduct` default the
    null target to the current phase and fire the reopen branch). `re_execute` is passed through
    (orthogonal to reuse/discard)."""
    if decision.action == "fail_closed" or not decision.severity or not decision.target_phase:
        return decision
    if decision.repair_strategy == "re_execute":
        return decision
    forced = _SEVERITY_FORCED_STRATEGY.get(decision.severity)
    if forced is not None:  # minor -> reuse, critical -> restart
        strategy = forced
    else:  # major -> reuse by default; honor an explicit LLM restart (escalate-to-discard)
        strategy = "restart" if decision.repair_strategy == "restart" else "reuse"
    return replace(decision, repair_strategy=strategy)


# --- node_key / path derivation ------------------------------------------------


def node_key_safe(node_key: str) -> str:
    """component/spec_id@1.0.0 -> component__spec_id__1.0.0."""
    kind_rest, _, version = node_key.partition("@")
    kind, _, spec_id = kind_rest.partition("/")
    return f"{kind}__{spec_id}__{version}"


def spec_id_of(node_key: str) -> str:
    kind_rest, _, _ = node_key.partition("@")
    _, _, spec_id = kind_rest.partition("/")
    return spec_id


@dataclass
class NodeRefs:
    """Resolved workspace references for a node + its reserved ids.

    Mutable: a retry/reopen that re-runs a producing phase allocates a fresh
    producer id (ir/source/binary/run) so it never overwrites a prior attempt's
    artifacts; the conductor updates the relevant field in place.
    """

    node_key: str
    spec_path: str  # spec/<kind>/<domain>/<family>/<spec_id>
    ir_id: str
    pipeline_id: str
    source_id: str | None = None
    binary_id: str | None = None
    run_id: str | None = None
    source_binary_id: str | None = None

    @property
    def safe(self) -> str:
        return node_key_safe(self.node_key)

    @property
    def spec_id(self) -> str:
        return spec_id_of(self.node_key)

    @property
    def ir_ref(self) -> str:
        return f"workspace/ir/{self.safe}/{self.ir_id}"

    @property
    def pipeline_ref(self) -> str:
        return f"workspace/pipelines/{self.safe}/{self.pipeline_id}"

    def source_dir(self, source_id: str | None = None) -> str:
        return f"{self.pipeline_ref}/source/{source_id or self.source_id}"

    def binary_dir(self, binary_id: str | None = None) -> str:
        return f"{self.pipeline_ref}/binary/{binary_id or self.binary_id}"

    def run_node_dir(self, run_id: str | None = None) -> str:
        return f"{self.pipeline_ref}/runs/{run_id or self.run_id}/{self.safe}"


# --- launch-request payload builder -------------------------------------------
#
# Assembles the launch-request payload deterministically from the
# tools/prompt_templates/ templates (the payload the pre-migration LLM orchestration
# agent used to assemble by hand). Validated field-for-field
# against real working launches/*.request.json (test_workflow_conductor.py).
# NOTE: `launch_prompt_full` is intentionally OMITTED so record-launch renders the
# canonical prompt and returns it as `launch_prompt_text` (tools/prompt_templates/).

# The contract docs every LLM leaf force-reads are derived by the single canonical
# policy `orchestration_runtime.leaf_contract_doc_refs(step)` (imported lazily where
# the must-read is assembled). record-launch's `_workflow_contract_refs_for_launch`
# calls the same helper, so the two must-read assembly paths cannot drift. The
# node-specific spec artifacts (which only the conductor knows) are appended per-step
# below. Canonical rationale: docs/design/leaf_must_read_restructure.md.


def _skill_name(step: str, substep: str | None) -> str:
    return f"workflow-{step}" if substep is None else f"workflow-{step}-{substep}"


def build_launch_request(
    refs: NodeRefs,
    *,
    step: str,
    substep: str | None,
    orchestration_id: str,
    orchestration_agent_run_id: str,
    child_agent_run_id: str,
    agent_model: str,
    workflow_mode: str,
    case_ids: tuple[str, ...] = (),
    evidence_artifacts: tuple[str, ...] = ("state_snapshots",),
    exe_name: str | None = None,
    makefile_host_authored: bool = False,
    runner_host_authored: bool = False,
    repair: dict[str, str] | None = None,
    resolved_dependencies: tuple[dict[str, str], ...] = (),
    dependency_surface: tuple[dict[str, Any], ...] = (),
    exemplar: dict[str, Any] | None = None,
    warm_resume: bool = False,
    pure_leaf: bool = False,
    pure_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Construct the record-launch --request-json payload for one substep.

    case_ids is required for validate.execute (per-case raw/state_snapshots paths).
    evidence_artifacts is the IR's required raw-evidence artifact types (validate.execute
    only); it drives which raw/* paths are deliverables so an IR that does not require
    state_snapshots is not forced to produce them (phase_04 §44).
    repair carries issue_severity/repair_strategy/repair_target_agent_run_id/
    repair_reason on a retry (defaults to the literal "none" the templates use).
    resolved_dependencies are the orientation-only dependency facts (pipeline/run/verdict
    per direct dep, from `_resolve_dependency_facts`); when non-empty they are attached for
    the LLM (generate / validate) leaves so the rendered `<dependency_facts>` block lets a
    judge skip the filesystem lookup. They never gate (pure module function, no FS access).
    """
    spec = refs.spec_path
    skill = _skill_name(step, substep)
    role = child_agent_role(step)
    # Build, Validate.execute and Generate.gate run in-process (no leaf), so they carry no
    # skill / leaf prompt — only the bookkeeping the capability/phase_state need.
    deterministic = (step == "build"
                     or (step == "validate" and substep in ("pre_judge", "execute", "post_judge"))
                     or (step == "generate" and substep == "gate")
                     or (step == "compile" and substep == "static"))
    rep = {
        "issue_severity": "none",
        "repair_strategy": "none",
        "repair_target_agent_run_id": "none",
        "repair_reason": "none",
    }
    if repair:
        rep.update(repair)

    req: dict[str, Any] = {
        "agent_role": role,
        "node_key": refs.node_key,
        "step": step,
        "orchestration_id": orchestration_id,
        "agent_run_id": child_agent_run_id,
        "parent_agent_run_id": orchestration_agent_run_id,
        "agent_model": agent_model,
        "workflow_mode": workflow_mode,
        "ir_ref": refs.ir_ref,
        "pipeline_ref": refs.pipeline_ref,
    }
    if deterministic:
        req["deterministic"] = True
    else:
        req["skill_name"] = skill
        req["skill_ref"] = f"skills/{skill}/SKILL.md"
    if substep is not None:
        req["substep"] = substep
    if runner_host_authored:
        # M3c physics node (runner host-rendered). Stamp it into the payload so the
        # record-launch security-boundary path (`_payload_is_m3c_physics`) derives the
        # SAME physics-narrowed contract-doc set as this conductor path — no drift.
        req["runner_host_authored"] = True

    # Base leaf must-read = its SKILL + the contract docs from the single canonical
    # policy (AGENT_CONTRACT for every leaf; phase_01 for Compile; runner-output
    # contract for Validate.judge and for a NON-M3c runner-authoring Generate leaf;
    # M3c physics Generate drops it — see leaf_contract_doc_refs). The same helper is
    # used by record-launch, so the two assembly paths cannot drift. The node-specific
    # spec artifacts are appended per-step below.
    from tools.orchestration_runtime import leaf_contract_doc_refs
    must_read: list[str] = ([] if deterministic
                            else [f"skills/{skill}/SKILL.md",
                                  *leaf_contract_doc_refs(step, is_m3c_physics=runner_host_authored)])

    if step == "compile":
        req["dependency_ref"] = f"{spec}/deps.yaml"
        if substep == "static":
            # Deterministic in-process compile gate: the conductor authors
            # compile_static_meta.json (the only freshness-gated deliverable) from
            # validate_workspace_root + check_artifact_syntax + validate_pipeline_semantics
            # --stage compile. No leaf, no must-read (deterministic), no IR authoring.
            req["allowed_output_paths"] = [
                f"{refs.ir_ref}/compile_static_meta.json",
            ]
        else:
            # generate/verify LLM substeps read the NL spec + tests + deps. spec.ir.yaml is a
            # must-read only for verify (generate authors it).
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml" if substep == "verify" else None,
                f"{spec}/controlled_spec.md",
                f"{spec}/tests.md",
                f"{spec}/deps.yaml",
            ]
            must_read = [m for m in must_read if m]
            if substep == "verify":
                # Compile.verify authors NOTHING in spec.ir.yaml — all 5 sections (incl.
                # io_contract) are authored by Compile.generate and the deterministic
                # Compile.static gate validated the IR before verify runs. Verify's sole write
                # is ir_meta.json (verification_status), so the IR cannot be mutated post-gate
                # (which would bypass --stage compile). spec.ir.yaml stays a must-read (verify
                # reads it to check), but is NOT a write target.
                req["allowed_output_paths"] = [
                    f"{refs.ir_ref}/ir_meta.json",
                ]
            else:  # generate authors the IR + its meta
                # dependency_graph.json is deliberately NOT listed: the derived
                # closure/topo graph is conductor-authored at Compile phase start
                # (_write_dependency_graph); compile.generate authors only the IR's
                # direct_deps. Keeping it out of allowed_output_paths (and the
                # file-tool set derived from it) makes the sidecar leaf-non-writable.
                req["allowed_output_paths"] = [
                    f"{refs.ir_ref}/spec.ir.yaml",
                    f"{refs.ir_ref}/ir_meta.json",
                ]
    elif step == "generate":
        req["source_id"] = refs.source_id
        req["dependency_ref"] = refs.ir_ref
        src = refs.source_dir()
        # For any make+fortran node (leaf or dependency) the conductor authors src/Makefile
        # host-side (_write_makefile), so it is NOT a leaf output — omit it from
        # allowed_output_paths (and required outputs) exactly like lineage.json. c/cpp/mixed
        # keep LLM authoring, so the leaf still lists it there.
        make_entry = [] if makefile_host_authored else [f"{src}/src/Makefile"]
        # R1/M3c-β: on a harness-backed node the conductor host-renders the runner glue
        # (_write_runner), so the leaf authors <spec_id>_checks.f90 instead — swap it into the
        # write set. The model is leaf-authored either way. c/cpp/mixed + non-M3c fortran nodes
        # keep the leaf-authored <spec_id>_runner.f90.
        runner_or_checks = (f"{src}/src/{refs.spec_id}_checks.f90" if runner_host_authored
                            else f"{src}/src/{refs.spec_id}_runner.f90")
        if substep == "generate":
            # Contract docs come from leaf_contract_doc_refs above (node-aware: an M3c
            # physics leaf gets the checks ABI and no runner-output contract; a non-M3c
            # runner-authoring leaf keeps it). Here only node-specific spec artifacts.
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml",
                # controlled_spec.md is intentionally NOT must-read here: phase_02
                # §2-1 forbids Generate.generate from taking controlled_spec.md as
                # input (re-introducing controlled_spec-derived info is a fail), so
                # the requirement composition is read from spec.ir.yaml.algorithm.
                # tests.md stays (used for case_id coverage).
                f"{spec}/tests.md",
            ]
            # lineage.json is authored host-side by the conductor (_write_lineage), not by
            # the leaf — it sits at the pipeline root which must stay non-writable to the
            # sandboxed leaf. So it is NOT in the leaf's allowed_output_paths.
            req["allowed_output_paths"] = [
                f"{src}/src/{refs.spec_id}_model.f90",
                runner_or_checks,
                *make_entry,
                f"{src}/src/command_log.jsonl",
                f"{src}/source_meta.json",
            ]
        elif substep == "gate":
            # Deterministic in-process gate: the conductor authors gate_meta.json (the single
            # freshness-gated deliverable, unioning the lint / syntax / static checkers). The
            # lint and syntax checkers both append to the canonical src/command_log.jsonl, so it
            # MUST be listed — otherwise the gate child's FS-diff write-attribution would flag
            # those appends as unauthorized writes. The host-authored lint / syntax evidence
            # (pipeline-root, leaf-non-writable) is NOT a leaf output and is intentionally
            # omitted from allowed_output_paths. The static checker (validate_pipeline_semantics
            # --stage post_generate + validate_workspace_root) writes nothing beyond gate_meta.
            req["allowed_output_paths"] = [
                f"{src}/gate_meta.json",
                f"{src}/src/command_log.jsonl",
            ]
        else:  # verify
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml",
                f"{src}/source_meta.json",
                f"{spec}/controlled_spec.md",
                f"{spec}/tests.md",
                f"{refs.pipeline_ref}/lineage.json",
            ]
            # Verify's SOLE write is source_meta.json (verification_status): it inspects the
            # producer sources (model / runner|checks / Makefile) but never rewrites them — a
            # fail requests regeneration under a NEW source_id (SKILL workflow-generate-verify
            # Scope + Rule 3), never an in-place edit. Those sources were certified by the
            # deterministic lint / syntax / static gates that run BEFORE verify and never re-run,
            # so listing them (or the Makefile / command_log) here would let a verify turn smuggle
            # an uncertified source into Build. Narrowed to source_meta.json on ALL turns; the
            # runtime pins verify's write_root to the same file (a structural second layer under
            # the pattern-based file-tool guard).
            req["allowed_output_paths"] = [
                f"{src}/source_meta.json",
            ]
    elif step == "build":
        req["source_id"] = refs.source_id
        req["binary_id"] = refs.binary_id
        req["dependency_ref"] = refs.pipeline_ref
        must_read += [
            f"{refs.ir_ref}/spec.ir.yaml",
            f"{refs.source_dir()}/source_meta.json",
        ]
        bdir = refs.binary_dir()
        # The binary basename is the Makefile's BIN (resolved by the conductor and passed
        # in); fall back to the <spec_id>_runner name when unknown (e.g. unit fixtures).
        req["allowed_output_paths"] = [
            f"{bdir}/bin/{exe_name or (refs.spec_id + '_runner')}",
            f"{bdir}/binary_meta.json",
            f"{bdir}/command_log.jsonl",
        ]
    elif step == "validate":
        req["run_id"] = refs.run_id
        req["dependency_ref"] = refs.pipeline_ref
        rundir = refs.run_node_dir()
        if substep == "pre_judge":
            # Deterministic pre-spawn dependency-DAG readiness gate. The conductor authors
            # pre_judge_meta.json (the only freshness-gated deliverable) in-process; no leaf,
            # no must-read, no run evidence yet.
            req["allowed_output_paths"] = [f"{rundir}/pre_judge_meta.json"]
        elif substep == "post_judge":
            # Deterministic post-return `--stage pre_judge` gate + severity classifier. The
            # conductor authors post_judge_meta.json (status + violations + disposition)
            # AND, G6, the deterministically-derivable aggregate_verdict / summary /
            # validate_meta (all in-process after the judge leaf returns).
            req["allowed_output_paths"] = [
                f"{rundir}/post_judge_meta.json",
                f"{rundir}/aggregate_verdict.json",
                f"{rundir}/summary.json",
                f"{rundir}/validate_meta.json",
            ]
        elif substep == "execute":
            req["source_id"] = refs.source_id
            req["source_binary_id"] = refs.source_binary_id
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml",
                f"{refs.source_dir()}/source_meta.json",
                f"{refs.binary_dir(refs.source_binary_id)}/binary_meta.json",
            ]
            outs = [
                f"{rundir}/command_log.jsonl",
                f"{rundir}/diagnostics.json",
                f"{rundir}/perf.json",
                f"{rundir}/trial_meta.json",
                f"{rundir}/quality_check.json",
                # R2: execute authors verdict.json deterministically (per-test predicate
                # evaluation of diagnostics.json), so the judge leaf authors only
                # semantic_review.json. Moved out of the judge's allowed_output_paths (below).
                f"{rundir}/verdict.json",
                f"{rundir}/raw/metrics_basis.json",
            ]
            # Raw-evidence deliverables are IR-driven (phase_04 §44): only require the
            # artifacts the IR's required_evidence declares.
            if "state_snapshots" in evidence_artifacts:
                for cid in case_ids:
                    outs.append(f"{rundir}/raw/state_snapshots/{cid}.json")
                outs.append(f"{rundir}/raw/state_snapshots/snapshot_schema.json")
            if "execution_trace.json" in evidence_artifacts:
                outs.append(f"{rundir}/raw/execution_trace.json")
            outs += [
                f"{rundir}/stdout.log",
                f"{rundir}/stderr.log",
                f"{refs.source_dir()}/src/command_log.jsonl",
            ]
            req["allowed_output_paths"] = outs
        else:  # judge
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml",
                f"{refs.source_dir()}/source_meta.json",
                f"{refs.binary_dir()}/binary_meta.json",
                f"{spec}/tests.md",
            ]
            # R2: the judge authors ONLY semantic_review.json. verdict.json (per_test +
            # failure_class) is now deterministically host-authored at execute from the IR
            # predicates + diagnostics.json; the deterministically-derivable aggregate_verdict /
            # summary / validate_meta are conductor-authored in post_judge (G6,
            # _author_derived_validate_artifacts). The judge is a pure semantic pass.
            req["allowed_output_paths"] = [
                f"{rundir}/semantic_review.json",
            ]
    else:  # pragma: no cover - guarded by SUBSTEPS keys
        raise ValueError(f"unknown step: {step}")

    # Deterministic steps have no leaf, so no skill_must_read_refs (the conductor reads
    # what it needs in-process; the read_manifest is irrelevant for them).
    req["skill_must_read_refs"] = "" if deterministic else ",".join(must_read)
    # Orientation-only dependency facts for the LLM leaves that benefit (generate's
    # semantic authoring, validate.judge's dependency-PASS review). Deterministic phases
    # (build / validate.execute) render the minimal prompt and never read them, so they
    # are omitted there. The `<dependency_facts>` renderer drops them when empty.
    if resolved_dependencies and not deterministic and step in ("generate", "validate"):
        req["resolved_dependencies"] = list(resolved_dependencies)
    # L2: the component-dep published-surface catalog (op names only), injected ONLY into the
    # compile.generate leaf so it transcribes real dependency op names into its public_api + dep
    # operations. Rendered through the same `<dependency_facts>` placeholder (compile branch of
    # `_build_dependency_facts`). compile.verify sees the frozen IR, not this, so it is scoped to
    # the authoring substep.
    if dependency_surface and step == "compile" and substep == "generate":
        req["dependency_surface"] = list(dependency_surface)
    # R5: a conductor-resolved certified sibling exemplar, injected ONLY for the sole authoring
    # leaf (generate.generate). Prior art to raise first-attempt pass rate; the `<exemplar>`
    # renderer drops it for any other (step, substep). Not attached to warm-resume slim prompts
    # (the resumed leaf already saw it) — build_launch_request's slim branch below empties the
    # must-read but the slim renderer never reads `exemplar`, so it is naturally absent there.
    if exemplar and step == "generate" and substep == "generate":
        req["exemplar"] = exemplar
    req.update(rep)
    # A verify substep's allowed_output_paths is already narrowed to exactly its stage-meta file
    # on ALL turns (compile.verify -> ir_meta.json, generate.verify -> source_meta.json, above),
    # so a verify_meta_schema repair turn — which re-authors ONLY that meta — needs no special-case
    # narrowing here: the normal list already IS the meta. All three write-authorization layers are
    # now substep-granular, giving real defence in depth: (1) allowed_file_tool_paths -> the
    # output_manifest_write_guard hook (rejects an Edit/Write/apply_patch to any unlisted path),
    # (2) the bwrap `write_roots` (the runtime pins verify's write_root to that same stage-meta
    # file, so the rest of the source/ (resp. ir/) tree is not even RW-bound), and (3) the terminal
    # FS-diff (which reads the narrowed write_roots). Because (2)/(3) are structural and independent
    # of the pattern-based Bash-write detector, a source rewritten on a verify turn — which would
    # reach Build uncertified, the lint/syntax/static gates having already run and never re-running
    # — is refused by the sandbox itself, not merely by the hook. The constraint therefore does not
    # rely on the findings text (which the slim renderer fences as untrusted data anyway).
    # Slim warm-resume repair turn: when the conductor has decided the producer session is
    # resumable (warm_resume) AND this is a reuse repair carrying findings, mark the request
    # so the runtime renders the findings-only slim prompt and empty the must-read (the
    # resumed leaf already read them). Emptied in BOTH assembly paths (here +
    # prepare_launch_request_payload) or the launch-integrity validator rejects the prompt.
    if (warm_resume and not deterministic
            and rep.get("repair_strategy") == "reuse"
            and str(rep.get("repair_findings", "")).strip()):
        req["warm_resume"] = True
        req["skill_must_read_refs"] = ""
    # Z2 pure-function producer variant (M-C): the leaf is a host-mediated pure function with no
    # write authority, so it carries `leaf_mode=pure`, the exact transport contract version, the
    # host-assembled `pure_context` (each inlined document a data-fenced string), and EMPTY
    # write/skill fields. `allowed_output_paths` is forced empty (the host writes files[] + the
    # bundle + the Makefile AFTER the child window closes — see run_substep's pure branch), and
    # the three skill fields are emptied (no SKILL is read). Applied LAST so it overrides the
    # generate branch's leaf-authored output set. On a warm-resume repair the resumed session
    # already holds the context, so pure_context is omitted (the validator exempts it for a
    # warm+reuse+findings request); a cold launch/fallback carries it.
    if pure_leaf:
        from tools.pure_leaf import PURE_LEAF_MODE, PURE_PROMPT_CONTRACT_VERSION
        req["leaf_mode"] = PURE_LEAF_MODE
        req["prompt_contract_version"] = PURE_PROMPT_CONTRACT_VERSION
        req["allowed_output_paths"] = []
        req["skill_name"] = ""
        req["skill_ref"] = ""
        req["skill_must_read_refs"] = ""
        if pure_context is not None:
            req["pure_context"] = dict(pure_context)
    return req


# --- runtime CLI + leaf spawn primitives --------------------------------------


# The output-token ceiling handed to a claude leaf (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`), = the
# synchronous output limit of the current frontier models. THINKING TOKENS COUNT AGAINST
# max_tokens: at the CLI's default (measured: 64,000) a leaf that thinks hard about a hard node
# hits the ceiling and is cut off having emitted thinking ONLY — no text, no tool_use — a fully
# billed, fully wasted turn. Two such turns (`stop_reason=max_tokens`, thinking-only) cost 24.9
# minutes and 128k tokens in the E2E #4 audit. Raising the ceiling does not make a leaf spend
# more; it only stops a leaf that needed the room from being truncated into nothing.
#
# The conductor does NOT pin the leaf model (`leaf_command` passes no `--model`; the leaf runs
# whatever the operator's claude config resolves to). 128,000 is the ceiling of the Opus 4.8 /
# Sonnet 5 tier; a model whose output limit is lower (Haiku 4.5 caps at 64,000) rejects this
# value, and rejects it on EVERY launch: `API Error: 400 {"type":"invalid_request_error",
# "message":"max_tokens: 128000 > 64000 ..."}`. That failure is deliberately classified
# `llm_client_error` (deterministic, never retried) so it surfaces at once with the API's own
# message rather than as a phantom outage. Lower this constant to the ceiling of the model the
# leaves actually run.
LEAF_MAX_OUTPUT_TOKENS = 128000


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


# A leaf that dies on an LLM-infrastructure error exits nonzero with no artifacts, which the
# conductor can only report as `leaf_transport_error: leaf_exit=1` — indistinguishable from a crash,
# an OOM, or a genuine transport fault. The leaf's own stdout/stderr (which the conductor already
# pipes and persists) names the cause, so classify it into a tag the [FAIL] line can carry. Reading
# the leaf's `~/.claude` transcript is deliberately NOT an option here (access boundary); the piped
# output is the only evidence source.
#
# The patterns must not fire on ordinary leaf output. A bare `429` / `529` / `5xx` substring
# matches a traceback frame (`File "x.py", line 429`), an array subscript, a duration or a token
# count, so a status code is recognized only next to a word that makes it an API STATUS — and the
# words that carry meaning on their own are matched on word boundaries.
#
# A bare `error` is deliberately NOT such a word, though it reads like one: `error: index 429 out
# of bounds for array u(500)` and `gfortran: error at line 504 of model.f90` both satisfy it. That
# was survivable while the tags only decorated a fail_closed reason; it is not now that they decide
# whether to RE-LAUNCH the leaf, because a deterministic compiler error or crash would be retried
# three times and then reported as a provider outage. `status` is out for the same reason — it is
# this repo's own vocabulary (gate status, verdict status, step_result status), so `status: 500
# checks failed` would read as an HTTP 500. `\bhttp\b` is word-bounded so it does not match inside
# `https://host:443`, whose port would otherwise be read as a status code.
_API_STATUS_CONTEXT = (
    r"(?:api error|\bhttp\b|\bstatus_code\b|\brejected\b)\D{0,12}")

# "...is the whole message": the phrase ENDS THE LINE (bar a closing quote/bracket and a final
# period). Anchored to end-of-line and not to punctuation generally, because `,` `;` `:` continue a
# sentence rather than closing one — `write(*,*) 'stream error: ', err_est` and `A premature close,
# or a missing flush, truncates the snapshot.` are leaf prose, and a retry armed by them would
# re-run a deterministic failure three times. A transport library's report has nothing after the
# phrase (`Error: Premature close`, `TypeError: fetch failed`).
_TERMINAL = r"\b(?=[.'\")\]]*\s*$)"

# The CLI's observed usage-limit abort lead-in, shared VERBATIM by the classifier's `llm_usage_limit`
# pattern and by `_CLI_USAGE_ABORT_LINE_RE` (which arms the `--wait-usage-reset` wait), so the two can
# never drift into disagreeing about the same family. Two properties are load-bearing:
#   * `^\s*` — the message LEADS the line. Without the anchor this is the loosest pattern in the
#     table AND sits at rank 0, so it steals the tag from more specific ones and from leaf prose:
#     `API Error: 429 rate_limit_error - you've hit your rate limit` would tag `llm_usage_limit`
#     instead of `llm_rate_limit` (costing its two retries), `... 400 invalid_request_error you've
#     hit your prompt limit` would read as a quota stop instead of `llm_client_error`, and a leaf
#     writing `you've hit your CFL limit, so dt must shrink` would terminalize the substep. The
#     classifier matches line-by-line, so `^` is a line anchor.
#   * `[^\n]{0,40}` window — the window word is NOT enumerated, so `weekly`, `Opus weekly`, `5-hour`,
#     `Claude Opus 4 weekly` and whatever the CLI invents next are all covered. A token-counted
#     budget silently excluded the multi-word and decimal-version forms.
#   * a trailing RESET-INSTANT CUE — the anchor alone does not save a leaf that OPENS a line in the
#     second person: `you've hit your CFL limit, so dt must shrink` would take rank 0 and cost the
#     substep its transient retries. The cue is deliberately a `resets` FOLLOWED BY A TIME (a digit
#     or a weekday), not a bare advice word: `try again` / `upgrade` are ordinary engineering
#     English, and `you've hit your CFL limit — try again with a smaller dt` would have sailed
#     through, as would `... limit; the halo index resets each sweep`. Stating WHEN is what makes a
#     quota message a quota message — and it is the only cue the reset parsers can consume anyway.
#     "WHEN" must be a CLOCK token — a bare digit is not enough, and the imperative `reset` does not
#     count. `you've hit your iteration limit - reset max_iter to 500`, `... the halo index resets to
#     0 each sweep` and `... the counter resets at step 3` are all ordinary prose that a
#     `resets?`-plus-any-digit cue admitted, taking rank 0 from a genuine transport tag on stderr.
#     The vocabulary is otherwise kept generous (12h and 24h clock, `in <n> hours`, weekday long or
#     abbreviated, `tomorrow`, `midnight`, `noon`, `next week`) because a cue MISS is not harmless
#     for the `weekly` /
#     `Opus weekly` / `5-hour` windows: those carry neither "reached" nor a bare `usage limit` /
#     `session limit`, so nothing else in the table covers them and the run terminalizes UNTAGGED —
#     the round-3 failure, one wording at a time. (The `usage` / `session` windows DO fall back.)
#   * NO LITERAL WHITESPACE anywhere in this string. It is interpolated into `_CLI_USAGE_ABORT_LINE_RE`,
#     which is compiled `re.VERBOSE` — that strips unescaped spaces, so a literal `try again` would
#     silently become `tryagain` there and the two regexes would disagree while documented as
#     identical. Use `\s`. `test_the_shared_lead_in_is_verbose_safe` pins this.

# The quota windows the CLI names, shared by the classifier's `<window> limit reached` alternative
# and by `_CLI_USAGE_ABORT_LINE_RE`'s machine-form alternative so they cannot enumerate differently.
_USAGE_LIMIT_WINDOWS = r"(?:usage|session|weekly|hourly|\d+-hour)"

_HIT_YOUR_LIMIT_BODY = (
    r"you(?:['’]?ve|\s+have)?\s+hit\s+your\b"
    # The window is otherwise unconstrained (see below), which makes it overlap the OTHER tags'
    # vocabulary: `You've hit your rate limit · resets 3pm (Asia/Tokyo)` took rank 0 as a quota stop,
    # removing the transient retries a rate limit is entitled to and arming a multi-hour wait for a
    # seconds-long throttle. These families have their own tags (`llm_rate_limit`,
    # `llm_client_error`) or are prompt-size failures, so they are excluded by name rather than by
    # enumerating the quota windows — an allowlist would re-break the "a window the CLI invents next
    # is still covered" property this alternative exists for.
    # `[\s_-]{0,3}` not `\s+`: the CLI's own rate-limit vocabulary spells it `rate limit`,
    # `rate-limit` and `rate_limit` (cf. `_LEAF_INFRA_ERROR_PATTERNS`'s `rate[ _-]?limit_error`), and
    # a hyphen slipped straight past a whitespace-only exclusion.
    r"(?![^\n]{0,40}\b(?:rate|request|prompt|context|token|output)[\s_-]{0,3}limit\b)"
    r"[^\n]{0,40}\blimit\b"
    r"(?=[^\n]{0,80}\bresets\b[^\n]{0,30}"
    r"(?:\d{1,2}(?::\d{2})?\s*[ap]m\b"                    # 3pm / 10:20pm
    r"|\d{1,2}:\d{2}\b"                                   # 18:00
    # The unit needs a trailing boundary AND its plural spelled out, or `h` matches the `h` of
    # `resets in 2 hundred steps` and ordinary prose satisfies the reset cue.
    r"|\bin\s+\d+\s*(?:hrs|hr|hours|hour|h|mins|min|minutes|minute)\b"   # in 2 hours
    r"|\b(?:mon|tues?|wednes|thurs?|fri|satur?|sun)(?:day)?\b"
    r"|\b(?:tomorrow|midnight|noon|next\s+week)\b))")

# ARMING form (`_CLI_USAGE_ABORT_LINE_RE`): the message must LEAD the line, full stop. The `^` is
# belt-and-braces — `_is_cli_usage_abort_line` uses `.match()`, which already anchors — so it is
# deliberately unpinned by any test (a no-op mutation): it exists so a future `.match()` -> `.search()`
# edit cannot quietly widen arming. The TAGGABLE form's anchor below IS load-bearing (the classifier
# uses `.search()`) and is pinned by `test_the_hit_your_limit_alternative_does_not_steal_other_tags`
# — whose counterexamples must use a QUOTA window, or the non-quota-window exclusion below rejects
# them first and the anchor goes unpinned (which is exactly what happened when that exclusion landed).
_USAGE_ABORT_HIT_YOUR_LIMIT = r"^\s*" + _HIT_YOUR_LIMIT_BODY

# TAGGING form (the classifier): the same body, also allowed to lead the CLI envelope's `result`
# field. In the ENVELOPED shape the line leads with `{"type":"result"...`, so a strictly
# line-anchored pattern cannot see the message at all — and the fallback phrases only cover the
# `usage` / `session` windows, so an enveloped `You've hit your 5-hour limit · resets ...` (a shape
# only a `--output-format json` launch produces, i.e. the whole pure surface) terminalized UNTAGGED:
# no `llm_usage_limit`, no wait, not even a decline to grep. That is round-3's defect surviving one
# shape further in. Deliberately NOT shared with the arming form: arming an envelope goes through
# `_cli_abort_envelope_result`, whose CLI-authored-key gates are the trust boundary, and letting a
# bare `"result":"` prefix arm directly would hand a leaf a 200-char forgery that skips those gates.
# Tagging is the weaker power (it can only REMOVE a re-launch), and this prefix adds zero tags across
# all 1422 recorded leaf logs.
# The `"result":"` prefix is deliberately UNBOUNDED (`[^\n]*`, not `[^\n]{0,N}`): the key's offset
# is set by the CLI's key ORDER, which has already changed once under us — 128..202 chars in the
# 2026-07-19/21/23 envelopes, but 1132..1424 in every envelope the CURRENT CLI writes (`usage` and
# `modelUsage` now precede `result`). Any positional bound is a latent re-break of this exact fix the
# next time the CLI reorders. The alternation is `^`-anchored so there is one start position per
# line, making the greedy prefix a single linear scan.
_USAGE_ABORT_HIT_YOUR_LIMIT_TAGGABLE = (
    r"^(?:\s*|\{[^\n]*\"result\"\s*:\s*\")" + _HIT_YOUR_LIMIT_BODY)

# Ordered MOST severe first — the tuple index is the severity rank (see `_classify_leaf_infra_error`).
# A usage limit is a hard stop that costs hours; a rate limit or an overload is transient. Reporting
# the hard stop as the transient one sends the operator back to a run that cannot start.
#
# The patterns are matched against a failed leaf's captured output, which for a `claude -p` leaf is
# mostly the MODEL'S OWN PROSE — so every one of them has to survive a leaf that writes "the
# rate-limiting step", "the generic interface is overloaded across ranks", or "Newton iteration
# limit reached", and a compiler that writes "call of overloaded 'update(double)'". Hence the word
# boundaries, the required error context, and the qualifier on `limit reached`.
_LEAF_INFRA_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # `(?<!not your )` — the CLI's 429 message literally reads "Server is temporarily limiting
    # requests (not your usage limit)". Tagging that a usage limit would be exactly backwards.
    # `{_USAGE_ABORT_HIT_YOUR_LIMIT}` — the CLI's OBSERVED abort family (`You've hit your session
    # limit · resets 5:50pm (Asia/Tokyo)`). Only the `session` member was tagged before, and by the
    # bare `session limit` alternative: the sibling windows (`weekly`, `Opus weekly`, `5-hour`) carry
    # no "reached", so they matched NOTHING and a real quota stop terminalized UNTAGGED — no
    # `llm_usage_limit`, no wait, and not even a `leaf_usage_limit_wait_declined` to grep for.
    ("llm_usage_limit", re.compile(
        r"(?<!not your )\busage limit\b|\bsession limit\b"
        rf"|\b{_USAGE_LIMIT_WINDOWS}\s+limit\s+reached\b"
        rf"|{_USAGE_ABORT_HIT_YOUR_LIMIT_TAGGABLE}"
        r"|\bcredit balance is too low\b|\bquota\b[^\n]{0,20}exceed")),
    # `overloaded` is a WORD, not a marker: this repo's own prose ("Overloaded the `__box` generic
    # so ranks 0..3 share one writer") and every compiler's overload diagnostic (`error: call of
    # overloaded 'update(double)' is ambiguous`, `Error: Type mismatch; overloaded generic __box`)
    # contain it, usually next to `error`. Only the API's own terse forms count: the error-type
    # token, a 529 in an API status context, `Overloaded` AS the whole message, and the CLI's
    # capacity notice. `{_TERMINAL}` is what separates `Error: Overloaded` (the API) from `error:
    # call of overloaded 'update(double)' ...` (the compiler) — the sentence continues.
    ("llm_overloaded", re.compile(
        rf"\boverloaded_error\b|{_API_STATUS_CONTEXT}\b529\b"
        rf"|\berror\b[^\n]{{0,20}}\boverloaded{_TERMINAL}"
        r"|^\s*overloaded\s*$|\bexperiencing high load\b")),
    # Same discipline for the rate limit. `rate limit` unqualified is ordinary technical English —
    # "diffusion rate limits the timestep", "the scheme is rate limited by diffusion" — so the bare
    # form counts only when it IS the message (`{_TERMINAL}`), and otherwise an explicit API shape
    # is required. A genuine rate limit worded some other way still falls through to the
    # `^api error` catch-all (retryable, only with a shorter backoff), so tightening here can cost
    # a backoff schedule but never a retry; a false positive, by contrast, costs three re-launches
    # of a deterministic failure.
    ("llm_rate_limit", re.compile(
        r"\brate[ _-]?limit_error\b"
        r"|\brate[ _-]?limit(?:ed|s)?\b[^\n]{0,12}\b(?:exceeded|error|reached|hit)\b"
        # The bare form only when it ENDS the line, or opens a dash clause (the CLI's `rate
        # limited — wait and retry`). Not before `.` or `,`: `The CFL condition sets the rate
        # limit.` and `The scheme is rate-limited, so dt shrinks.` are ordinary technical English.
        r"|\brate[ _-]?limit(?:ed)?\b(?=\s*(?:$|[—–-]\s))"
        rf"|{_API_STATUS_CONTEXT}\b429\b"
        r"|\btoo many requests\b|\btemporarily limiting requests\b")),
    # A 4xx from the API: the REQUEST is wrong (a bad/expired credential, an unsupported
    # parameter, an oversized prompt), so every re-launch reproduces it byte-for-byte. Ranked
    # ABOVE the transport tag and deliberately kept OUT of _RETRYABLE_LEAF_INFRA_TAGS: without
    # it the `^api error` catch-all below would swallow a 4xx and retry it three times, then
    # report a deterministic misconfiguration as a provider outage the operator should wait out.
    # The concrete case this repo can cause itself: a leaf model whose output ceiling is below
    # LEAF_MAX_OUTPUT_TOKENS answers every launch with
    # `API Error: 400 {"type":"invalid_request_error","message":"max_tokens: 128000 > 64000 ..."}`.
    # Two 4xx are NOT client errors and are excluded so they fall through to a retryable tag:
    # 429 (a rate limit — already matched at a more severe rank above) and 408 (Request Timeout —
    # genuinely transient, and the exact fault the transient retry exists for).
    # The plain-English forms are load-bearing, not decoration: the CLI often renders a 4xx with no
    # status code at all (`API Error: prompt is too long: 235000 tokens > 200000 maximum`), and
    # without them the `^api error` catch-all below would take it and RETRY it. `prompt is too
    # long` is the second failure this repo can inflict on itself — the conductor injects the R5
    # exemplar, the dependency facts and the must-read docs into a cold generate prompt, and an
    # oversized prompt reproduces byte-for-byte on every re-launch.
    ("llm_client_error", re.compile(
        rf"{_API_STATUS_CONTEXT}\b4(?!08\b|29\b)\d\d\b"
        r"|\binvalid_request_error\b|\bauthentication_error\b|\bpermission_error\b"
        r"|\bnot_found_error\b|\brequest_too_large\b"
        r"|\bprompt is too long\b|\brequest body too large\b"
        r"|\binvalid (?:api key|x-api-key|bearer token)\b"
        r"|\boauth token (?:has )?expired\b|\bplease run /login\b")),
    ("llm_permission_probe_unavailable", re.compile(
        r"temporarily unavailable, so auto mode cannot determine")),
    # LEAST severe, and deliberately LAST: a transient transport fault (the connection to the
    # API died mid-stream). It is a strict fallback — the quota/overload patterns above are
    # matched first, so `stream disconnected: Too Many Requests` stays `llm_rate_limit` and only
    # an otherwise-unnamed transport failure lands here.
    #
    # A real incident this exists for: a `compile.verify` leaf left exactly one line —
    # `API Error: Connection closed mid-response. The response above may be incomplete.` — which
    # matched nothing above, fail-closed the whole run, and cost 6.8 hours until a human
    # `--resume`d. The tag makes it retryable (_RETRYABLE_LEAF_INFRA_TAGS).
    #
    # The trailing `^\s*api error\b` is a deliberate CATCH-ALL for transport wording we have not
    # seen yet: the claude CLI opens a LINE with `API Error:` only when the API/transport layer
    # failed, and a leaf's Fortran prose does not open a line that way. It is anchored (not a
    # substring match) precisely so a model sentence that merely mentions an API error cannot arm
    # a retry, and the `llm_client_error` tag above intercepts the deterministic 4xx subset before
    # it reaches here. Everything before it is word-bounded / error-context-qualified for the same
    # reason as the patterns above — the leaf's captured output is mostly the MODEL'S OWN PROSE, so
    # `the solver timed out after 500 iterations`, `the connection between cells 3 and 4`, and
    # `access='stream'` must all stay unmatched.
    # `{_TERMINAL}` = the phrase ENDS the line. Applied to the phrases a leaf's own prose can also
    # open with — `the premature close of the file unit`, `the fetch failed for the dependency
    # facts`, `write(*,*) 'stream error: ', err_est` — so only the transport library's terse form
    # (`Error: Premature close`, `TypeError: fetch failed`) matches. `read timed out` is
    # deliberately absent for the same reason: it is too close to ordinary English to earn a retry
    # on its own.
    ("llm_transport_flake", re.compile(
        r"\bconnection closed mid-response\b"
        r"|\bconnection (?:reset|refused|aborted|closed unexpectedly)\b"
        r"|\b(?:econnreset|econnrefused|econnaborted|epipe|etimedout|enotfound|eai_again)\b"
        r"|\bsocket hang up\b"
        rf"|\bfetch failed{_TERMINAL}|\bpremature close{_TERMINAL}"
        rf"|\bstream (?:disconnected|interrupted|aborted)\b|\bstream error{_TERMINAL}"
        r"|\bnetwork (?:error|is unreachable)\b"
        r"|\b(?:request|connection|socket|upstream|gateway) timed out\b"
        rf"|\bbad gateway{_TERMINAL}|\bservice unavailable{_TERMINAL}"
        rf"|\binternal server error{_TERMINAL}"
        r"|\btypeerror: terminated\b"
        # 408 Request Timeout is transient and is excluded from `llm_client_error` — but that
        # exclusion only makes it RETRYABLE if it matches here. Without these two alternatives a
        # 408 rendered without the `API Error:` line prefix (`HTTP 408`, codex's `last status: 408
        # Request Timeout`) would match nothing at all and fail the run closed.
        rf"|{_API_STATUS_CONTEXT}\b408\b|\brequest timeout{_TERMINAL}"
        rf"|{_API_STATUS_CONTEXT}\b5\d\d\b"
        r"|^\s*api error\b")),
)

# Infra tags the conductor RETRIES in place (bounded, with backoff) instead of fail-closing the
# run. Everything else stays terminal, and each exclusion is deliberate:
#   - `llm_usage_limit` is a hard stop lasting hours. Retrying it burns the budget in seconds and
#     only delays the operator's `--resume`. (Manual BY DESIGN; see deterministic_followups L5.)
#   - `llm_client_error` (4xx) is a rejected REQUEST: an expired credential, an unsupported
#     parameter, an oversized prompt. Every re-launch sends the same request and gets the same 4xx.
#   - `llm_permission_probe_unavailable` needs an operator/config fix, not another attempt.
#   - an UNCLASSIFIABLE nonzero exit (crash, OOM, hook denial) is deterministic: retrying it just
#     hides the same failure behind 3x the wall-clock.
_RETRYABLE_LEAF_INFRA_TAGS = frozenset({
    "llm_transport_flake", "llm_overloaded", "llm_rate_limit"})
MAX_LEAF_TRANSIENT_RETRIES = 2  # => at most 3 launches of the same substep
# Per-tag backoff, indexed by the 0-based attempt that just died. A transport flake is usually
# gone on the next connection; an overload/rate limit needs the server side to recover, so it
# waits materially longer before spending another (billed) launch. `_DEFAULT` keeps a tag added to
# _RETRYABLE_LEAF_INFRA_TAGS without a schedule here from raising KeyError mid-phase (which would
# crash the conductor AFTER the dead attempt was already finalized, instead of failing closed).
_DEFAULT_LEAF_RETRY_BACKOFF: tuple[float, ...] = (10.0, 30.0)
_LEAF_RETRY_BACKOFF_SECONDS: dict[str, tuple[float, ...]] = {
    "llm_transport_flake": (2.0, 10.0),
    "llm_overloaded": (15.0, 60.0),
    "llm_rate_limit": (30.0, 90.0),
}

# Tags that may be promoted OUT OF STDOUT over a match already found in stderr. Deliberately just
# these two: stdout is a `claude -p` leaf's own prose, so letting it outrank stderr in general
# would hand the retry decision to whatever the model happened to write. These two are the
# exception because (a) the CLI reports both as its RESULT TEXT — i.e. on stdout, with stderr
# often empty (the E2E #4 incident line arrived exactly that way) — and (b) both are
# NON-RETRYABLE, so promoting them can only ever remove a re-launch, never add one:
#   - a usage limit retried is three re-launches into a multi-hour hard stop, burning the budget
#     the post-reset resume needs;
#   - a 4xx retried is three re-launches of a request the API rejects identically every time.
_CROSS_STREAM_PROMOTING_TAGS = frozenset({"llm_usage_limit", "llm_client_error"})

# A TRANSIENT retry notice — the CLI prints `API Error (429 …) · Retrying in 1 seconds… (attempt
# 1/10)` and then RECOVERS. Blaming a recovered retry for a leaf that actually died of a hook
# denial is the same misdiagnosis this classifier exists to prevent, only inverted. Matched
# narrowly on the notice's own shape: a TERMINAL message may still mention retries (codex emits
# `exceeded retry limit, last status: 429 Too Many Requests`) and must stay classifiable.
_LEAF_RETRY_NOTICE_RE = re.compile(r"\bretrying\b|attempt \d+/\d+")

# --wait-usage-reset (opt-in): a usage limit is a multi-hour HARD STOP, so the conductor's DEFAULT
# stays fail_closed (a manual `--resume` after the reset — see deterministic_followups L5). When the
# operator opts in AND the dead leaf's terminal usage-limit line carries a RESOLVABLE reset instant,
# the conductor waits it out IN PLACE and re-launches the same substep — a same-run, substep-granular
# resume instead of a next-day fresh run. Two forms resolve, tried in order on the SAME terminal line:
#   (1) MACHINE form — a trailing `|<unix-epoch>` (`_parse_usage_reset_epoch`);
#   (2) HUMAN form — a wall-clock time-of-day + a parenthesized IANA timezone
#       ("resets 10:20pm (Asia/Tokyo)"), resolved to an epoch by `_parse_usage_reset_human`.
# The real CLI emits form (2), not (1), so (2) is what actually arms the wait in practice; (1) is
# kept first for backward-compat and any future machine envelope. A human reset WITHOUT a
# parenthesized IANA TZ ("resets 6:10pm"), or without a time-of-day ("resets Monday"), is NOT
# resolved — the reset instant is not guessed from the host's local TZ (that would make the wait
# depend on where the conductor runs and could wake into a still-shut window), so it declines to
# fail_closed and emits `leaf_usage_limit_wait_declined` for visibility.
# The STREAM matters as much as the wording: the CLI aborts with that line on STDOUT and an EMPTY
# stderr, so the terminal line is resolved stderr-first with a narrow stdout carve-out
# (`_sole_content_usage_limit_line` — one short line that OPENS with the abort wording). Reading
# stderr alone made this feature inert against the real CLI, which is how an opted-in run still
# fail_closed; a per-line "nothing but usage limits" test would have been just as inert in the other
# direction, since a pure leaf's whole stdout is a single JSON line. All three bounds are hard,
# and — with the +margin, the 6h cap, and the nearest-occurrence resolution — a resolved human
# instant is safe even a few minutes stale (it floors to a margin-only relaunch):
MAX_USAGE_LIMIT_WAITS = 1  # per substep; a distinct budget from the transient-retry retries above
# The session window is 5h; a reset further out than this is a weekly limit or a misparsed epoch,
# neither of which the in-place wait should sit on — fall back to fail_closed.
MAX_USAGE_LIMIT_WAIT_SECONDS = 6 * 3600
# Sleep slightly PAST the reset instant: the re-launch's record-launch runs a preflight live-probe
# (TTL-driven), and waking a hair early would find the window still shut and fail the probe.
USAGE_LIMIT_WAIT_MARGIN_SECONDS = 120
# The machine-form reset suffix a usage-limit leaf may carry: `...usage limit reached|1752200000`.
# Ten digits pins it to a plausible unix-second epoch (through year 2286) and keeps an ordinary
# `|<number>` in the model's own prose from being read as a reset time.
_USAGE_RESET_EPOCH_RE = re.compile(r"\|(\d{10})\s*$")
# The human-form reset the real CLI emits: `... resets 10:20pm (Asia/Tokyo)` (also `resets at 5pm`,
# `resets 12am`). The time-of-day requires an am/pm marker (so a weekday word `resets Monday` never
# matches); the TZ must be a parenthesized IANA `Area/City` name — a reset without one is NOT
# resolved (the instant is never guessed from the host-local TZ; see the design note above).
_USAGE_RESET_HUMAN_TIME_RE = re.compile(
    r"resets\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
# The parenthesized IANA zone name is passed UNCHANGED to `ZoneInfo` (the validator), so the
# charset must admit every IANA name shape: letters/digits/underscore, plus the `+`/`-` that
# appear in fixed-offset zones (`Etc/GMT+5`, `Etc/GMT-14`) and hyphenated cities
# (`America/Port-au-Prince`). The first segment starts with a letter (every IANA area does) and at
# least one `/`-segment is required, so a plain parenthetical word or `(1/2)` is never mistaken for
# a zone. Anchoring the WHOLE token to `)` is load-bearing: a narrower charset would stop at the
# first `+`/`-`, then fail the `)` anchor and DECLINE a valid zone — it never truncates to a
# different (wrong-offset) zone, so the failure was a missed wait, not a wrong one.
_USAGE_RESET_HUMAN_TZ_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+)\)")
# A human wall-clock reset is printed to the minute, and the leaf-death → conductor-processing lag
# plus clock skew can leave the terminal message a few minutes stale. Resolve to the occurrence
# NEAREST `now` (yesterday/today/tomorrow at that wall time) that is no more than this far in the
# past, so a just-passed reset floors to a margin-only relaunch instead of jumping to tomorrow (and
# being declined by the 6h cap). Only ever reaches into the PAST, so it can never manufacture a
# large positive wait.
_USAGE_RESET_HUMAN_GRACE_SECONDS = 900
# The stdout carve-out's shape test (`_sole_content_usage_limit_line`): the CLI's usage-limit abort
# is a SHORT line that OPENS with the limit itself. The recorded incidents are 58 and 59 chars
# (`You've hit your session limit · resets 5:50pm (Asia/Tokyo)` — 60/61 bytes as written, the `·`
# being 2 bytes and the trailing newline 1); the ceiling leaves room for a longer wording
# without admitting any leaf output — the smallest ORDINARY (non-error) single-line leaf
# envelope in any recorded workspace is 1530 chars.
_CLI_USAGE_ABORT_LINE_MAX_CHARS = 200
# A PARSE-COST guard on the CLI's `--output-format json` envelope, not a security bound: it only
# keeps `json.loads` off a pathologically large line. The security work is done by `allow_envelope`
# (only a `--output-format json` launch, where the leaf cannot forge the CLI's own keys) and by the
# INNER text facing every abort-shape clause including `_CLI_USAGE_ABORT_LINE_MAX_CHARS`.
# Sized ABOVE every recorded envelope (largest 47370 chars) on purpose. A tight bound here would be
# the inertness bug again: envelope size is dominated by the CLI's own accounting blocks (`usage`,
# `modelUsage`, timings — 705..1578 chars, median 1292 across the 112 recorded envelopes), not by
# `result`, so the one recorded abort envelope is small (771 chars) only because that leaf died at
# `num_turns: 1`. A 1500-char cap would have declined ~27% of abort envelopes synthesised from the
# recorded accounting blocks — a silent `no_reset_time` decline per envelope shape.
_CLI_ABORT_ENVELOPE_MAX_CHARS = 65536
# Anchored at line start, and deliberately NARROWER than the classifier's `usage limit|session limit`
# phrase match: a leaf's own sentence may CONTAIN the phrase, but the CLI's abort LEADS with it.
# Covers the observed human form (`You've hit your <window> limit · resets ...`) and the machine form
# kept for backward-compat (`Claude AI usage limit reached|<epoch>`, also bare). A wording this does
# not recognise declines the wait — i.e. degrades to today's fail_closed, the safe direction, and
# emits `leaf_usage_limit_wait_declined` so the next unrecognised envelope is greppable rather than
# silent (the failure mode that hid the stderr-only bug for two rounds).
_CLI_USAGE_ABORT_LINE_RE = re.compile(
    # The first alternative is the classifier's own lead-in, reused VERBATIM (already `^`-anchored),
    # so a wording the classifier tags as this family is never one the wait silently declines.
    rf"""{_USAGE_ABORT_HIT_YOUR_LIMIT}                           # You've hit your <window> limit
      # The MACHINE form, and nothing looser. The window list mirrors the classifier's
      # `<window> limit reached` alternative EXACTLY (same shared constant, same `reached`), and the
      # trailing `|<epoch>` is required because that suffix is the whole reason this alternative
      # exists — it is the only shape it can arm, and the CLI has never actually emitted it (0 of 711
      # recorded stdout logs; the observed abort always takes the lead-in alternative above).
      # Without those two markers the alternative degenerates to "<window> limit at line start",
      # which an agentic leaf's OWN one-line prose satisfies: stdout `Session limit resets at 5pm
      # (Asia/Tokyo)` after an unrelated death (a hook denial) armed a real multi-hour wait.
      # A future human-worded `usage limit reached · resets 3pm (Asia/Tokyo)` — no lead-in, no epoch
      # — therefore declines. That is the safe direction and it is not silent: the classifier still
      # tags it (bare `usage limit`), so `leaf_usage_limit_wait_declined` fires with the evidence
      # line attached.
      | ^\s*(?:claude(?:\s+ai)?\s+)?{_USAGE_LIMIT_WINDOWS}\s+limit\s+reached\b
        (?=[^\n]*\|\d{{10}}\s*$)""",
    re.IGNORECASE | re.VERBOSE)


def _stream_terminal_usage_limit_line(stream: str) -> str | None:
    """The LAST line of ONE stream that the `llm_usage_limit` pattern matches, skipping recovered
    retry-notice banners, or None when there is none. The per-stream scan behind
    `_terminal_usage_limit_line`."""
    usage_pattern = _LEAF_INFRA_ERROR_PATTERNS[0][1]
    lines = (stream or "").splitlines()
    terminal: str | None = None
    for idx, line in enumerate(lines):
        # Skip a recovered retry-notice banner AND the line it continues from, exactly as the
        # classifier does — a `Retrying... (attempt 1/10)` line the run survived is not terminal.
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        if (_LEAF_RETRY_NOTICE_RE.search(line.lower())
                or _LEAF_RETRY_NOTICE_RE.search(nxt.lower())):
            continue
        if not usage_pattern.search(line.lower()):
            continue
        terminal = line  # last usage-limit line wins
    return terminal


def _sole_line(stream: str | None) -> str:
    """The single non-blank line of `stream`, or "" when it has none or more than one."""
    lines = [line for line in (stream or "").splitlines() if line.strip()]
    return lines[0] if len(lines) == 1 else ""


def _cli_abort_envelope_result(line: str) -> str | None:
    """The `result` text of the CLI's OWN error envelope, or None when `line` is not one.

    Only ever reached for a leaf launched with `--output-format json` (see the `allow_envelope`
    gate in `_sole_content_usage_limit_line`), because only there does the CLI author an envelope:
    the recorded incidents show one shape per launch mode — a bare abort line for the agentic
    launches (5 of 6) and, for the one PURE launch, the same message carried in `result`:

        {"type":"result","is_error":true,"api_error_status":429,...,
         "result":"You've hit your session limit · resets 12:30pm (Asia/Tokyo)",
         "terminal_reason":"api_error"}

    The gating keys are CLI-AUTHORED, not model-authored: a leaf writes `result`'s TEXT, but
    `is_error` / `api_error_status` / `terminal_reason` are stamped by the CLI wrapper, and a leaf
    that finished normally carries `is_error:false` with no error status. That argument holds ONLY
    where the CLI actually writes the envelope — hence the caller's `allow_envelope` gate. Unwrapping
    therefore hands the leaf no new way to arm the wait: the inner text must clear every abort-shape
    clause afterwards, length included. `_CLI_ABORT_ENVELOPE_MAX_CHARS` is only a parse-cost guard —
    deliberately far above every recorded envelope, because sizing it to the one recorded ABORT
    envelope (771 chars) would decline the fatter ones the CLI's own accounting blocks produce."""
    if len(line) > _CLI_ABORT_ENVELOPE_MAX_CHARS:
        return None
    if '"is_error"' not in line:            # cheap reject: no envelope, no parse
        return None
    try:
        doc = json.loads(line)
    except Exception:
        return None
    if not isinstance(doc, dict) or doc.get("is_error") is not True:
        return None
    if doc.get("terminal_reason") != "api_error" and doc.get("api_error_status") is None:
        return None
    result = doc.get("result")
    return result if isinstance(result, str) else None


def _is_cli_usage_abort_line(line: str) -> bool:
    """The abort-SHAPE test for ONE line: short enough, not a recovered retry banner, opening with
    the limit, and a line the classifier's own usage pattern matches (so this can only ever narrow —
    never contradict — the `llm_usage_limit` tag that reached the wait).

    That last clause is now PROVABLY IMPLIED by the first three and is kept as a defensive invariant,
    not because any input needs it: both alternatives of `_CLI_USAGE_ABORT_LINE_RE` are subsets of
    the classifier's `llm_usage_limit` pattern (the lead-in alternative is the taggable form minus
    the envelope prefix; the machine alternative shares `_USAGE_LIMIT_WINDOWS` and the same
    `limit`-then-`reached` wording — spelled with a whitespace CLASS on BOTH sides, since a literal space on one side
    made `usage  limit  reached|<epoch>` match the arming pattern and not the classifier, leaving
    the implication true only modulo whitespace). Mutating it away therefore survives the suite by construction — like
    the `^` in the arming pattern — and `test_arming_implies_the_classifier_would_tag` asserts the
    IMPLICATION instead, so a future widening of either alternative that broke it still fails."""
    if len(line) > _CLI_USAGE_ABORT_LINE_MAX_CHARS:
        return False
    lowered = line.lower()
    if _LEAF_RETRY_NOTICE_RE.search(lowered):
        return False
    if not _CLI_USAGE_ABORT_LINE_RE.match(line):
        return False
    return bool(_LEAF_INFRA_ERROR_PATTERNS[0][1].search(lowered))


def _sole_content_usage_limit_line(stdout: str, *, allow_envelope: bool) -> str | None:
    """A leaf's stdout when it is NOTHING BUT the CLI's own usage-limit abort — the message alone, or
    the CLI's error envelope carrying it — else None.

    This is the narrow carve-out that lets the REAL CLI arm the wait; a stderr-only rule never armed
    it in production, which is how the opted-in E2E run still fail_closed. TWO recorded shapes, both
    with an EMPTY stderr, and BOTH must be admitted — this function was written against the first
    alone and stayed inert for the pure loops, the same bug one layer in. The direction this code
    RELIES on is `envelope => the launch was --output-format json`, which holds by construction
    (only the pure loops pass those flags); the converse is not assumed — a pure launch may still
    abort bare, and the bare path accepts it. Across 711 recorded leaf stdout logs an envelope in
    fact appears iff the launch was pure, with zero exceptions.
      * BARE — all 5 agentic incidents across the recorded workspaces: there is no envelope to
        carry the message, so stdout is the message, ~59 chars;
      * ENVELOPED — the single PURE incident: stdout is the `--output-format json` result envelope
        with the message in `result` and the CLI's own `is_error` / `api_error_status` /
        `terminal_reason` keys stamped alongside — see `_cli_abort_envelope_result`, which unwraps
        it (gated on `allow_envelope`) before the shape clauses run against the inner text.

    Every clause exists to keep the leaf's OWN OUTPUT SURFACE from arming a multi-hour wait, and the
    obvious weaker formulations do NOT hold:
      * `len(lines) == 1` — NOT "every line matches the usage pattern". A pure leaf's entire stdout
        is ONE line (a single-line JSON envelope, newlines escaped: 15 of the 46 stdout logs in the
        live workspace and 112 of 711 across every recorded workspace, up to 47 kB), so a per-line test is vacuous for it — any model-authored text
        INSIDE the envelope would satisfy it.
      * `<= _CLI_USAGE_ABORT_LINE_MAX_CHARS` — a leaf that produced work produces far more than a
        one-line abort message, so length alone excludes every recorded envelope. Applied to the
        INNER text when unwrapping, since the envelope itself is legitimately longer.
      * `_CLI_USAGE_ABORT_LINE_RE` anchored at line start, NOT the broad classifier pattern, whose
        first alternative is the bare phrase `usage limit` / `session limit` — an ordinary English
        sentence ("I could not finish: the session limit was reached, resets 11pm (Asia/Tokyo)")
        contains it, and a leaf's one-paragraph result text is also a single line. The abort message
        LEADS with the limit; a leaf discussing one does not.
    The classifier pattern is required too, so this can only ever narrow — never contradict — the
    tag that reached the wait. A leaf can still produce a message the CLI would produce, but only by
    producing nothing else at all, and only while exiting nonzero; the outcome is bounded by the wait
    budget and the 6h cap."""
    line = _sole_line(stdout)
    if not line:
        return None
    if _is_cli_usage_abort_line(line):
        return line
    # Not the bare shape — try the CLI's own error envelope, then apply the SAME clauses to the
    # message it carries (never to the envelope, which is CLI-framed but leaf-filled). Only a leaf
    # launched with `--output-format json` HAS a CLI-authored envelope: an agentic leaf's stdout is
    # its own text, so a JSON line there is model-written and its `is_error` / `api_error_status`
    # keys prove nothing. The record is unambiguous — across 711 recorded leaf stdout logs an
    # envelope appears iff the launch was pure, with zero exceptions.
    if not allow_envelope:
        return None
    inner = _cli_abort_envelope_result(line)
    if inner is None:
        return None
    inner_lines = [text for text in inner.splitlines() if text.strip()]
    if len(inner_lines) != 1 or not _is_cli_usage_abort_line(inner_lines[0]):
        return None
    return inner_lines[0]


def _terminal_usage_limit_line(stderr: str, stdout: str, *,
                               allow_envelope: bool) -> str | None:
    """The TERMINAL usage-limit line of a dead leaf — the LAST line the `llm_usage_limit` pattern
    matches, skipping recovered retry-notice banners — or None when there is none.

    STDERR FIRST (the trusted CLI error channel). stdout is consulted only when stderr named no
    usage limit at all, and then only through the `_sole_content_usage_limit_line` carve-out — see
    there for why that stays safe against a leaf's own untrusted prose. Note this is STRICTLY
    NARROWER than `_classify_leaf_infra_error`'s cross-stream rule, not a mirror of it: that rule
    (`_CROSS_STREAM_PROMOTING_TAGS`) lets a stdout match OUTRANK a stderr one, whereas here a stderr
    usage-limit line always wins and stdout may only fill a stderr silence. Since `llm_usage_limit`
    is the classifier's most severe tag, a stderr usage-limit line is also what the classifier tagged
    from, so the wait still resolves against the very line the run was tagged from.

    Selecting the TERMINAL line makes the wait AGREE with `_classify_leaf_infra_error`, which tags
    the run from that same line (most-severe-then-last): the wait is governed by the cause that
    actually terminated the leaf, never by an earlier message the run went on to survive. Shared by
    the machine-epoch and human-reset parsers so they resolve against the identical line."""
    return (_stream_terminal_usage_limit_line(stderr)
            or _sole_content_usage_limit_line(stdout, allow_envelope=allow_envelope))


def _parse_usage_reset_epoch(stderr: str, stdout: str, *, allow_envelope: bool) -> int | None:
    """The unix-second reset epoch a usage-limit leaf carried as a trailing `|<10-digit>` on its
    TERMINAL usage-limit line, or None when absent (a human-worded reset, or none at all).

    MACHINE FORM ONLY. The human-worded form ("resets 10:20pm (Asia/Tokyo)") is resolved separately
    by `_parse_usage_reset_human`; `_usage_reset_wait_plan` tries this machine parser FIRST and falls
    back to the human parser on the same terminal line (via `_terminal_usage_limit_line`).

    Ten digits pins the suffix to a plausible unix-second epoch and keeps a stray `|1234567890` in
    the model's own prose from being read as a reset time (only the terminal usage-limit line is
    considered). When that line carries NO epoch (a human-worded weekly limit, or a session limit
    the machine envelope simply omits) the result is None even if an earlier line had one — the wait
    is governed by the cause that actually terminated the leaf, not by an epoch the run survived.

    `stdout` participates only through `_terminal_usage_limit_line`'s sole-content carve-out."""
    line = _terminal_usage_limit_line(stderr, stdout, allow_envelope=allow_envelope)
    if line is None:
        return None
    match = _USAGE_RESET_EPOCH_RE.search(line)
    return int(match.group(1)) if match else None


def _parse_usage_reset_human(stderr: str, now: float, stdout: str, *,
                             allow_envelope: bool) -> int | None:
    """The unix-second reset epoch resolved from a HUMAN-worded reset on the TERMINAL usage-limit
    line — a wall-clock time-of-day + a parenthesized IANA timezone, e.g. `resets 10:20pm
    (Asia/Tokyo)` — or None when the line is not resolvable.

    Returns None (declines, no wait) when: there is no terminal usage-limit line; the line has no
    `h[:mm](am|pm)` time-of-day (a weekday-worded `resets Monday`); or the line has no parenthesized
    IANA timezone. The timezone is REQUIRED — the instant is never guessed from the conductor host's
    local TZ, which would make the wait depend on where the run executes and could resolve to a
    plausible-but-wrong instant that wakes into a still-shut window.

    Resolution: the wall time is matched to the occurrence NEAREST `now` among yesterday / today /
    tomorrow (in the parsed TZ) that is no more than `_USAGE_RESET_HUMAN_GRACE_SECONDS` in the past.
    This picks the next upcoming reset, or a just-passed one when the message is minutes stale
    (which then floors, in `_usage_reset_wait_plan`, to a margin-only relaunch), and it handles both
    midnight-wrap directions. The caller's 6h cap declines an occurrence resolved further out (a
    message stale beyond the grace flips cleanly to tomorrow and is capped). A DST fold/gap can skew
    the resolved instant by up to 1h on the 1-2 days/year a transition lands in-window; that is
    absorbed by the +margin and the relaunch preflight probe (the same residual the machine path
    carries). NEVER raises (a bad/unknown TZ or missing tzdata → None), matching its siblings.

    `now` is passed in (not read here) so the machine and human paths and the wait math all see one
    `time.time()` instant, and so the resolution is deterministically testable. `stdout` participates
    only through `_terminal_usage_limit_line`'s sole-content carve-out — and it is the stream the
    real CLI actually uses, so this is the path that arms the wait in practice."""
    try:
        line = _terminal_usage_limit_line(stderr, stdout, allow_envelope=allow_envelope)
        if line is None:
            return None
        time_match = _USAGE_RESET_HUMAN_TIME_RE.search(line)
        if time_match is None:
            return None
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        meridiem = time_match.group(3).lower()
        if not (1 <= hour <= 12) or not (0 <= minute <= 59):
            return None
        if meridiem == "am":
            hour24 = 0 if hour == 12 else hour
        else:  # pm
            hour24 = 12 if hour == 12 else hour + 12
        # Take the first parenthesized token `ZoneInfo` ACCEPTS, not merely the first that matches
        # the shape: an earlier non-zone parenthetical that happens to look like `Area/City`
        # (`(opus/sonnet)`, `(plan-1/of-2)`) must not shadow the real timezone later on the line and
        # decline an otherwise resolvable reset. Still fail-closed — a line with no acceptable zone
        # yields None.
        tz = None
        for tz_match in _USAGE_RESET_HUMAN_TZ_RE.finditer(line):
            try:
                tz = ZoneInfo(tz_match.group(1))
                break
            except Exception:
                continue
        if tz is None:
            return None
        today = datetime.fromtimestamp(now, tz).date()
        candidates = [
            datetime(d.year, d.month, d.day, hour24, minute, tzinfo=tz).timestamp()
            for d in (today - timedelta(days=1), today, today + timedelta(days=1))
        ]
        eligible = [e for e in candidates if e >= now - _USAGE_RESET_HUMAN_GRACE_SECONDS]
        return int(min(eligible)) if eligible else None
    except Exception:
        return None


# --wait-usage-reset, PRIMARY reset source (issue #8). Everything above SCRAPES the reset instant out
# of a dead leaf's UNTRUSTED stdout, which is why it needs the whole anti-forgery apparatus
# (`allow_envelope`, `_cli_abort_envelope_result`, the abort-shape clauses) — and even then the
# scraped line carries no DATE (yesterday/today/tomorrow is guessed within a 15-min grace) and no
# WINDOW NAME (the 6h cap stands in for "probably not the weekly one").
#
# The HOST can simply ask instead: `claude --output-format json -p /usage` is a local slash command
# that spends 0 tokens (`num_turns: 0`, ~1.0s on the recorded run) and answers with the server's own
# accounting, dates and window names included:
#
#     You are currently using your subscription to power your Claude Code usage
#
#     Current session: 31% used · resets Jul 25, 3:49am (Asia/Tokyo)
#     Current week (all models): 86% used · resets Jul 28, 9:59am (Asia/Tokyo)
#     Current week (Fable): 33% used · resets Jul 28, 10am (Asia/Tokyo)
#
# Because the CONDUCTOR runs it, there is no forgery surface at all: no leaf authored these bytes, so
# NONE of the abort-shape clauses above apply here and none are duplicated below. The probe is tried
# FIRST and the scrape remains the fallback, so every failure mode degrades to exactly today's
# behavior.
#
# OPEN QUESTION (deliberately unanswered here): whether `/usage` still answers once the quota is
# actually exhausted — the one state that cannot be reproduced on demand. That is why the probe is
# primary-with-fallback rather than a replacement, and why `leaf_usage_limit_probe` records the raw
# outcome of EVERY attempt: the next real incident answers it from the event stream, without a
# purpose-built experiment. (The sibling lesson from the scrape's two failed rounds: an invisible
# decline hides the defect.)
# Generous against a ~1.0s observed probe: the cost of a slow probe is a delayed fallback, while the
# cost of a tight timeout is losing the primary source on a loaded host.
USAGE_PROBE_TIMEOUT_SECONDS = 60
# SECURITY floor on arming the wait from a probe row — see `_probe_reset_for_evidence`. The
# classifier's `llm_usage_limit` tag can come from the leaf's OWN stdout prose
# (`_CROSS_STREAM_PROMOTING_TAGS`), and the probe path does not pass through the abort-shape clauses
# that catch that. The server-observed usage percentage is the replacement gate, and it must be a
# FULLY exhausted window (100%): the probe's job is to CORROBORATE that the named window is out of
# quota, and a window with headroom (95..99%) does not — the leaf cannot have been stopped by a limit
# it had not reached, so such a death is a mis-attribution or a local-approximation artifact, and the
# correct action is to decline to the scrape (whose abort-shape clauses still decide) rather than sit
# on a multi-hour wait. `/usage`'s percentage is explicitly approximate and local-only, so a genuine
# exhaustion may read under 100 on this host; that only ever costs the probe a decline-to-scrape (the
# safe direction) and the `leaf_usage_limit_probe` event records the observed percentage, so a real
# incident reporting e.g. 99 is the evidence that would justify lowering this — never a guess.
USAGE_PROBE_EXHAUSTED_MIN_PCT = 100
# Bounded raw evidence for `leaf_usage_limit_probe`. Sized to cover the whole window block of the
# recorded live response (~270 chars once whitespace is collapsed): the excerpt is the ONLY record of
# what `/usage` said when it could not be parsed, which is precisely the exhausted-quota response the
# open question is about, so clipping it at the sibling decline's 160 would cut it off mid-window.
_USAGE_PROBE_EXCERPT_MAX_CHARS = 400
_USAGE_PROBE_MONTHS = {name: idx for idx, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
# One `/usage` window row. `re.VERBOSE` DROPS literal spaces, so every gap is spelled `\s+`/`\s*`.
# The window label is `session` or `week<anything but a colon>` — the real response names two week
# rows (`Current week (all models)`, `Current week (<model>)`), and the label is kept whole so the
# event says which one matched. The date is fully specified (`Jul 25`), which is the whole reason the
# probe beats the scrape: no yesterday/today/tomorrow guess. The minutes are OPTIONAL because the
# real response prints `10am` for an on-the-hour reset. The `^` is redundant with the caller's
# `.match()` and is kept deliberately (a stats line like `  92% of your usage ...` must never be read
# as a window); the test asserts the PATTERN's own anchoring so neither spelling can be dropped
# silently on the strength of the other.
_USAGE_PROBE_ROW_RE = re.compile(
    r"""^\s*Current\s+(?P<window>session|week[^:\n]*?)\s*:\s*
        (?P<pct>\d{1,3})%\s+used\b
        [^\n]*?
        \bresets\s+(?P<month>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2})\s*,\s*
        (?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\b""",
    re.IGNORECASE | re.VERBOSE)
# The window FAMILY named by the dead leaf's own abort line, matched against the probe's row labels.
# `\b` on both sides is load-bearing: an enveloped abort's classifier evidence is raw JSON, whose
# `"session_id"` must not be read as the session window (the underscore is a word char, so the `\b`
# after `session` keeps `\bsessions?\b` from matching there either). Both families admit the plural
# for symmetry — a wording of `sessions limit` / `weekly limit` alike names its family — but never
# fail OPEN by doing so: a family this does not recognise merely declines to the scrape. The
# classifier's other windows (`usage`, `hourly`, `<n>-hour` — `_USAGE_LIMIT_WINDOWS`) have no
# counterpart row, so they match nothing and fall back to the scrape.
_USAGE_PROBE_EVIDENCE_FAMILY_RES = (
    ("session", re.compile(r"\bsessions?\b", re.IGNORECASE)),
    ("week", re.compile(r"\bweek(?:ly|s)?\b", re.IGNORECASE)),
)


def _parse_usage_probe_rows(result_text: str, now: float) -> list[dict[str, Any]]:
    """The window rows of a `/usage` probe response's `result` text, as
    `{window_label, family, used_pct, reset_epoch}` — `[]` when it names none.

    HOST-AUTHORED INPUT: the conductor ran the probe itself, so this text is the CLI's own, not a
    leaf's. None of the anti-forgery shape clauses that guard the stdout scrape
    (`_sole_content_usage_limit_line` and friends) apply, and none are repeated here.

    Per-LINE degradation on purpose: a line this does not recognise is skipped, so a future wording
    change costs the rows it touched and nothing else (the caller then finds no matching window and
    falls back to the scrape). NEVER raises, matching its scrape siblings.

    The response prints a month/day but no YEAR, so the year is resolved the same way the scrape
    resolves its missing date: the previous, current, and next years (in the parse timezone) are
    tried in ascending order and the EARLIEST occurrence not more than `_USAGE_RESET_HUMAN_GRACE_SECONDS`
    in the past is taken — the `min(eligible)` semantics of the scrape's yesterday/today/tomorrow
    resolution. The previous year matters only at the New Year boundary (a Dec-31 reset seen just
    after midnight on Jan 1, still within grace, resolves to the just-passed occurrence rather than
    one ~a year out); the next year handles the forward Dec→Jan wrap; the Feb-29 case fails the
    non-leap years and takes the next leap year. `now` is passed in so one `time.time()` instant
    governs the whole decision and the resolution is deterministically testable.

    The wall-clock is resolved through `datetime(...).timestamp()`, so a DST fold/gap can skew the
    instant by up to 1h on the 1-2 days/year a transition lands in-window — the same residual
    `_parse_usage_reset_human` carries, absorbed the same way (the +margin and the relaunch preflight
    probe). Not worth a fold-aware resolution the scrape does not have."""
    rows: list[dict[str, Any]] = []
    for line in (result_text or "").splitlines():
        try:
            match = _USAGE_PROBE_ROW_RE.match(line)
            if match is None:
                continue
            month = _USAGE_PROBE_MONTHS.get(match.group("month").lower()[:3])
            if month is None:
                continue
            hour = int(match.group("hour"))
            minute = int(match.group("minute") or 0)
            if not (1 <= hour <= 12) or not (0 <= minute <= 59):
                continue
            if match.group("meridiem").lower() == "am":
                hour24 = 0 if hour == 12 else hour
            else:
                hour24 = 12 if hour == 12 else hour + 12
            # Same first-zone-`ZoneInfo`-ACCEPTS idiom as the human scrape: `(all models)` /
            # `(Fable)` in the label are rejected by the shape, and a model name that happened to
            # look like `Area/City` is rejected by `ZoneInfo` rather than shadowing the real zone.
            tz = None
            for tz_match in _USAGE_RESET_HUMAN_TZ_RE.finditer(line):
                try:
                    tz = ZoneInfo(tz_match.group(1))
                    break
                except Exception:
                    continue
            if tz is None:
                continue
            reset_epoch: float | None = None
            base_year = datetime.fromtimestamp(now, tz).year
            # PREVIOUS / current / next year, ascending, taking the EARLIEST occurrence not more
            # than the grace in the past — the same `min(eligible)` resolution the scrape's
            # yesterday/today/tomorrow parser uses. The previous year is load-bearing at the New Year
            # boundary: `/usage` run just after midnight on Jan 1 can still report a Dec 31 reset that
            # just passed (within grace); without `base_year - 1` the earliest candidate would be
            # Dec 31 of THIS year — ~a year out — which the caller then "resolves" and declines under
            # the 6h cap WITHOUT trying the scrape, killing an otherwise-recoverable relaunch. The
            # next year still handles the forward Dec→Jan wrap, and Feb-29 falls through invalid
            # years to the next leap year.
            for year in (base_year - 1, base_year, base_year + 1):
                try:
                    candidate = datetime(year, month, int(match.group("day")),
                                         hour24, minute, tzinfo=tz).timestamp()
                except ValueError:      # e.g. Feb 29 of a non-leap year
                    continue
                if candidate >= now - _USAGE_RESET_HUMAN_GRACE_SECONDS:
                    reset_epoch = candidate
                    break
            if reset_epoch is None:
                continue
            # Sanitize the label at CONSTRUCTION, not just where the raw response is excerpted:
            # `json.loads` turns an escaped `\ud800` in the CLI's response into a REAL lone
            # surrogate, and this label is emitted verbatim on `leaf_usage_limit_probe`
            # (`windows` / `matched_window`) and, once matched, on `leaf_usage_limit_wait` /
            # `_declined` (`window`). Any of those would fail `emit`'s
            # `json.dumps(..., ensure_ascii=False)` and turn a fail_closed usage-limit path into a
            # conductor crash — the same round-trip the excerpt uses, applied once at the source so
            # every downstream emit of the label is safe. (`family` / `used_pct` / `reset_epoch` are
            # safe literals and integers.)
            label = " ".join(match.group("window").split()).encode(
                "utf-8", "backslashreplace").decode("utf-8")
            rows.append({
                "window_label": label,
                "family": "session" if label.lower().startswith("session") else "week",
                "used_pct": int(match.group("pct")),
                "reset_epoch": int(reset_epoch),
            })
        except Exception:
            continue
    return rows


def _probe_reset_for_evidence(evidence: str,
                              rows: list[dict[str, Any]]) -> tuple[str, int | None, str | None]:
    """`(outcome, reset_epoch, window_label)` for arming the wait from probe rows.

    `outcome` is `resolved` (and then `reset_epoch` is set) or the reason the probe declined, which
    the caller emits verbatim on `leaf_usage_limit_probe` — the decline reasons ARE the field
    evidence this feature collects, so they are returned rather than collapsed into None.

    WINDOW AGREEMENT IS REQUIRED (operator decision). The dead leaf's own abort line names the window
    that stopped it (`You've hit your session limit …`); only a probe row of that family may arm the
    wait. Waking on the wrong window is the failure this excludes structurally: a weekly stop matched
    to the session row would sleep a couple of hours and then fail the relaunch preflight against a
    window still shut. `window_unmatched` covers both "the abort named no family this probe reports"
    (`usage` / `hourly` / `<n>-hour`) and "it named one but the probe listed no such row";
    `window_ambiguous` covers a family whose rows disagree on the instant (the real response's two
    week rows can reset a minute apart) — a disagreement is not resolved by picking one.

    `window_not_exhausted` is the SECURITY gate, not a sanity check. The `llm_usage_limit` tag that
    reached the wait may have been promoted out of the leaf's own stdout prose
    (`_CROSS_STREAM_PROMOTING_TAGS`), and the recorded Codex P2 incident is exactly that: a leaf that
    died of a HOOK DENIAL, printing `Session limit resets at 5pm`, armed a real multi-hour wait until
    the abort-shape clauses were tightened. The probe path bypasses those clauses, so the
    server-observed usage percentage replaces them — a session row always EXISTS (at 31%, say), so
    without this gate the probe would re-open that hole with better date parsing. The floor is FULL
    exhaustion (`USAGE_PROBE_EXHAUSTED_MIN_PCT`, 100): a window with headroom (95..99%) has demonstrably
    NOT been reached, so it cannot be the cause of the death and the probe must not corroborate it —
    such a row declines to the scrape, where the abort-shape clauses still stand. The gate is applied
    to the HIGHEST row of the family: a `week` stop is caused by whichever of its rows is full, not by
    the least-used one."""
    families = {family for family, pattern in _USAGE_PROBE_EVIDENCE_FAMILY_RES
                if pattern.search(evidence or "")}
    if len(families) != 1:      # named none, or named both — no unambiguous window to match
        return ("window_unmatched", None, None)
    family = families.pop()
    matched = [row for row in rows if row.get("family") == family]
    if not matched:
        return ("window_unmatched", None, None)
    epochs = {int(row["reset_epoch"]) for row in matched}
    if len(epochs) != 1:
        return ("window_ambiguous", None, None)
    top = max(matched, key=lambda row: int(row.get("used_pct") or 0))
    label = str(top.get("window_label") or family)
    if int(top.get("used_pct") or 0) < USAGE_PROBE_EXHAUSTED_MIN_PCT:
        return ("window_not_exhausted", None, label)
    return ("resolved", epochs.pop(), label)


class UsageResetWaitPlan(NamedTuple):
    """What `_usage_reset_wait_plan` decided: how long to sleep, to which instant, from WHICH source
    and (probe only) for which window. A NamedTuple, so the positional `(wait_seconds, reset_epoch)`
    reading the plan had before the probe existed still holds. `reset_source` / `window` exist to be
    emitted: an operator reading `leaf_usage_limit_wait` must be able to tell a host-observed reset
    from one scraped out of a dead leaf's stdout, since only the latter can be wrong about the
    window."""
    wait_seconds: float
    reset_epoch: int
    reset_source: str
    window: str | None


def _classify_leaf_infra_error(stderr: str, stdout: str = "") -> tuple[str, str] | None:
    """(tag, evidence_line) when a failed leaf's captured output names an LLM-infrastructure
    cause; None when nothing matches (the caller then keeps its generic reporting).

    Within a stream the MOST SEVERE tag wins (a usage limit outranks a transient rate limit the
    same run may also have logged), and among equally severe matches the LAST one — the terminal
    message, not one the run went on to survive.

    `stderr` is authoritative: stdout carries a `claude -p` leaf's OWN PROSE, which may well
    discuss "the rate-limiting step" of a numerical scheme, so a stdout match may override a
    stderr match only for a tag in _CROSS_STREAM_PROMOTING_TAGS. Otherwise stdout is consulted
    solely when stderr named nothing — which is the common case, since the CLI reports an
    infrastructure failure as its result text (the E2E #4 incident had an empty stderr).
    """
    best: tuple[int, str, str] | None = None
    best_stream: int | None = None
    for stream_idx, stream in enumerate((stderr, stdout)):
        lines = (stream or "").splitlines()
        for idx, line in enumerate(lines):
            # Skip the notice itself AND the line it continues from: the banner is sometimes wrapped
            # as `API Error (429 ...)` / `· Retrying in 1 seconds... (attempt 1/10)`.
            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            if _LEAF_RETRY_NOTICE_RE.search(line.lower()) or _LEAF_RETRY_NOTICE_RE.search(
                    nxt.lower()):
                continue
            lowered = line.lower()
            for rank, (tag, pattern) in enumerate(_LEAF_INFRA_ERROR_PATTERNS):
                if pattern.search(lowered):
                    same_stream = stream_idx == best_stream
                    if best is None:
                        wins = True
                    elif same_stream:
                        # The last equally-or-more severe line of the stream: the terminal message.
                        wins = rank <= best[0]
                    else:
                        # stdout over a stderr match: only a promoting tag, and only upward.
                        wins = rank < best[0] and tag in _CROSS_STREAM_PROMOTING_TAGS
                    if wins:
                        best = (rank, tag, " ".join(line.split())[:160])
                        best_stream = stream_idx
                    break
    return (best[1], best[2]) if best is not None else None


@dataclass
class Conductor:
    """Holds invariant context and the primitive operations of the loop."""

    repo_root: Path
    orchestration_id: str
    orchestration_agent_run_id: str
    backend: str
    env: dict[str, str]
    # Unpinned spec-side alias (never a pinned version — that would go stale as
    # versions update). The EXACT version each leaf actually ran is resolved from
    # its session transcript and recorded onto its agent_runs row in _agent_run_json.
    agent_model: str = "opus"
    workflow_mode: str = "dev"
    # The resolved backend command (may be a wrapper with flags, e.g. from
    # --llm-command); empty falls back to the bare backend name.
    llm_command: str = ""
    # --wait-usage-reset (opt-in, default OFF): when a leaf dies of an `llm_usage_limit` whose
    # terminal line carries a RESOLVABLE reset instant (machine epoch, else TZ-anchored human form —
    # the latter is what the real CLI emits), wait it out in place and re-launch the substep instead
    # of fail-closing the run for a next-day manual `--resume`. Off keeps the prior behavior exactly.
    wait_usage_reset: bool = False

    def emit(self, event: str, **fields: Any) -> None:
        """Write one JSONL info event to stdout (the conductor runs in-process
        under run_workflow.py, so these join its node-level event stream)."""
        payload = {
            "status": "info",
            "event": event,
            "orchestration_id": self.orchestration_id,
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def runtime(self, args: list[str]) -> dict[str, Any]:
        """Call an orchestration_runtime.py subcommand; return parsed JSON stdout."""
        proc = subprocess.run(
            ["python3", "tools/orchestration_runtime.py", *args],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}"
            raise RuntimeError(f"runtime {args[0]} failed: {detail}")
        out = proc.stdout.strip()
        return json.loads(out) if out else {}

    def new_agent_run_id(self) -> str:
        proc = subprocess.run(
            ["python3", "tools/new_agent_run_id.py"],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"new_agent_run_id failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _resolve_reuse_resume(self, repair: dict[str, str] | None,
                              phase: str, substep: str | None) -> str | None:
        """The producer session id to warm-`--resume`, or None for a cold launch.

        Resolved BEFORE building the launch request (not after) so the slim-vs-full prompt
        choice, what `record_launch` persists, and what `spawn_leaf` actually sends all stay
        consistent. Warm resume is ALWAYS active for a `reuse` repair (claude only): resume the
        producer leaf's session so the repair inherits its context (and design intent) instead
        of cold-starting. The warm/cold choice is therefore driven by the failure
        classification's `repair_strategy`: deterministic-gate findings (lint/static/
        compile_static) route `reuse` -> warm; `restart` stays cold (no resume) to avoid
        anchoring on the defective reasoning — that strategy-driven selection is intentionally
        preserved (LLM verify-attributed restarts stay cold; reuse repairs warm-resume). The
        producer's session id == its agent_run_id (pinned via --session-id at its own launch),
        so it is addressable by repair_target_agent_run_id. Warm-resume only if the producer's
        session transcript still exists (Claude Code may have expired/GC'd it); if it is gone,
        fall back to a cold launch (return None) rather than letting `claude --resume <missing>`
        fail the leaf and fail-close the phase."""
        if not (self.backend == "claude"
                and repair is not None
                and repair.get("repair_strategy") == "reuse"):
            return None
        target = str(repair.get("repair_target_agent_run_id") or "").strip()
        if not target or target == "none":
            return None
        if self._claude_session_resumable(target):
            return target
        self.emit("resume_session_unavailable", phase=phase, substep=substep or "", target=target)
        return None

    def _claude_session_resumable(self, session_id: str) -> bool:
        """True if a claude session transcript for `session_id` still exists under
        ~/.claude/projects/*/<session_id>.jsonl. Used to decide whether a warm
        `--resume` is viable or must fall back to a cold launch (the session may have
        been expired/GC'd by Claude Code)."""
        if not isinstance(session_id, str) or not session_id.strip():
            return False
        try:
            proj = Path.home() / ".claude" / "projects"
            return bool(sorted(proj.glob(f"*/{session_id.strip()}.jsonl")))
        except OSError:
            return False

    def leaf_command(
        self,
        prompt_text: str,
        *,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        pure: bool = False,
    ) -> list[str]:
        """Headless command to run one substep body as an isolated leaf agent.
        Honors a custom llm_command (wrapper + flags) so the conductor launches the
        same executable/model as the configured backend, not a hard-coded binary.

        For the claude backend, `session_id` pins the leaf's Claude Code session id
        to its `agent_run_id` (so the per-arid transcript is addressable and a later
        repair can `--resume` it). `resume_session_id` (claude only) resumes a prior
        leaf's session for context inheritance on a minor-fix `repair_strategy=reuse`,
        forked into the new session so the prior transcript is not mutated. Guards key
        on the active_child marker (= the new arid), not the session, so the resumed
        repair is still evaluated against its own manifest.

        `pure=True` (Z2) launches a HOST-MEDIATED PURE FUNCTION rather than an agentic
        session: `tools/pure_leaf.pure_leaf_flags()` disables every tool, MCP server, and
        slash command and selects the JSON result envelope, so the model returns exactly one
        typed document and holds no write path. Claude-only by construction — a codex pure
        leaf fails closed here (there is no migrated codex producer; the operator decision is
        claude-only for Z2), well before spawn, so the ValueError names the misconfiguration
        instead of a downstream parse failure."""
        if pure and self.backend != "claude":
            raise ValueError(
                f"pure leaf mode is claude-only; backend {self.backend!r} has no pure "
                "producer (Z2 fail-closed)")
        base = shlex.split(self.llm_command) if self.llm_command.strip() else [self.backend]
        if self.backend == "claude":
            # `-p` runs non-interactively; the committed .claude/settings.json supplies
            # MCP build-runtime registration + permission grants (see preflight gate).
            flags: list[str] = []
            if resume_session_id:
                flags += ["--resume", resume_session_id, "--fork-session"]
            if session_id:
                flags += ["--session-id", session_id]
            if pure:
                from tools.pure_leaf import pure_leaf_flags
                flags += pure_leaf_flags()
            return [*base, *flags, "-p", prompt_text]
        if self.backend == "codex":
            return [*base, "exec", prompt_text]
        raise ValueError(f"unsupported backend for leaf spawn: {self.backend}")

    def _bwrap_enabled(self) -> bool:
        """bwrap leaf sandboxing is unconditionally MANDATORY (Phase-2; Linux+bwrap
        only). The FS-diff write-authorization model (`_validate_actual_write_paths`
        authorizes a leaf write purely by write_roots containment) is only sound while
        bwrap actually confines each leaf to its write_roots, so there is no opt-out: a
        host that cannot sandbox the leaf fails closed at launch rather than running
        unconfined (an unconfined leaf + FS-diff would authorize writes anywhere). The
        method is retained as a single seam for the call sites; it always returns True."""
        return True

    def _ensure_codex_feature_cache(self) -> None:
        """Host-side: probe the codex hooks feature ONCE per orchestration and persist the
        result to the leaf-unwritable cache (orchestration-dir root, RO inside the bwrap
        sandbox), so the in-sandbox codex hook reads a host-certified value it cannot
        forge. No-op for non-codex backends and after the first call (memoized). The probe
        runs the SAME command prefix the leaf runs (`leaf_command`'s `base` — a custom
        `--llm-command` wrapper, else the bare backend), so it certifies the executable the
        leaf will actually use, not a hardcoded `codex`. A leaf can never write this cache
        (the prior design wrote it from the in-sandbox hook into the leaf-writable hooks/
        dir).

        Fails closed when the feature is NOT certified (hooks disabled or the probe errored)
        and the requirement is on: a codex leaf whose PreToolUse/PostToolUse file-access
        hooks would not fire must not launch at all — the in-sandbox gate fail-closes only if
        the hook actually runs, which it does not when the hooks feature is off, so recording
        a disabled cache without blocking would leave the leaf unguarded by the hook layer.
        Honours the same `METDSL_REQUIRE_CODEX_HOOKS_FEATURE` opt-out the hook does."""
        if self.backend != "codex":
            return
        if getattr(self, "_codex_feature_cache_written", False):
            return
        from tools.hooks.codex_feature import probe_and_write_codex_feature_cache
        # Mirror leaf_command()/_readonly_sandbox_profile: the leaf's invocation prefix is
        # the parsed llm_command (with any wrapper flags), else the bare backend.
        command = shlex.split(self.llm_command) if self.llm_command.strip() else [self.backend]
        enabled, detail = probe_and_write_codex_feature_cache(
            repo_root=self.repo_root, orchestration_id=self.orchestration_id,
            command=command or [self.backend])
        # Read the requirement from self.env (the same env the leaf's hook inherits via
        # _child_env), defaulting to required — matches the hook's gate semantics.
        require_raw = self.env.get("METDSL_REQUIRE_CODEX_HOOKS_FEATURE", "1").strip().lower()
        hooks_required = require_raw not in {"0", "false", "no"}
        if hooks_required and not enabled:
            # Fail closed BEFORE memoizing, so this never degrades into an allow on a retry.
            raise SandboxEnforcementError(
                f"codex hooks feature not certified for orchestration "
                f"{self.orchestration_id} ({detail}); refusing to launch a codex leaf whose "
                "file-access hooks would not fire (fail-closed)")
        self._codex_feature_cache_written = True

    def _sandbox_profile_for(self, child_arid: str) -> dict[str, Any] | None:
        """The bwrap profile record-launch wrote for this child, or None."""
        path = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                / "sandbox_profiles" / f"{child_arid}.json")
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return doc if isinstance(doc, dict) else None

    def _readonly_sandbox_profile(self) -> dict[str, Any]:
        """A read-only bwrap profile for a leaf with no record-launch (the failure
        diagnostician): repo read-only, no write_roots, tmp-only scratch + backend
        auth/session home + the hooks/audit bookkeeping dirs. Raises
        SandboxEnforcementError if the host cannot build the profile (so the caller can
        fail closed instead of crashing or launching unconfined)."""
        from tools.orchestration_runtime import build_readonly_bwrap_profile
        base = shlex.split(self.llm_command) if self.llm_command.strip() else [self.backend]
        backend_command = base[0] if base else self.backend
        try:
            return build_readonly_bwrap_profile(
                repo_root=self.repo_root,
                orchestration_id=self.orchestration_id,
                agent_run_id=self.orchestration_agent_run_id,
                backend_command=backend_command,
                backend_type=self.backend,
            )
        except (ValueError, OSError) as exc:
            raise SandboxEnforcementError(
                f"read-only diagnostician sandbox profile unavailable: {exc}") from exc

    def spawn_leaf(
        self,
        prompt_text: str,
        child_env: dict[str, str],
        *,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        child_arid: str | None = None,
        profile: dict[str, Any] | None = None,
        pure: bool = False,
    ) -> ProcResult:
        # Host-certify the codex hooks feature into the leaf-unwritable cache before the
        # codex leaf launches (the in-sandbox hook reads it read-only; see
        # _ensure_codex_feature_cache). Memoized; no-op for claude.
        self._ensure_codex_feature_cache()
        argv = self.leaf_command(
            prompt_text, session_id=session_id, resume_session_id=resume_session_id, pure=pure)
        # Wrap the leaf in the bwrap sandbox that record-launch already built (repo
        # read-only; writes confined to the child's write_roots + workspace/tmp).
        # record-launch records sandbox_enforced=True for every backend, so applying it
        # here makes that record true (the conductor leaf is otherwise unconfined).
        # Applies to both claude and codex — both get a profile at launch. A caller may
        # pass an explicit `profile` for a leaf that has no record-launch profile keyed
        # by child_arid (the read-only diagnostician; see escalate()).
        if self._bwrap_enabled():
            # Fail closed: enforcement is mandatory and record-launch records
            # sandbox_enforced=true, so ANY leaf without a usable profile — a missing/
            # invalid one (older orchestration resumed, corrupted/deleted file) or a
            # caller that supplies neither an explicit profile nor a child_arid — must
            # NOT silently fall back to an unconfined launch.
            if profile is None:
                profile = self._sandbox_profile_for(child_arid) if child_arid else None
            if profile is None:
                raise SandboxEnforcementError(
                    "bwrap enforcement is mandatory but no usable sandbox profile is "
                    f"available for this leaf (child_arid={child_arid!r}); refusing to "
                    "launch unconfined (fail-closed)")
            from tools.orchestration_runtime import render_bwrap_command
            try:
                argv = render_bwrap_command(profile=profile, command_argv=argv)
            except ValueError as exc:
                # A structurally invalid/corrupted profile (missing repo_root/tmp_dir,
                # bad file pin, …) must also fail closed as a sandbox error, not bubble
                # up as a generic conductor error.
                raise SandboxEnforcementError(
                    f"sandbox profile for {child_arid} is invalid: {exc}") from exc
        try:
            proc = subprocess.run(
                argv, cwd=self.repo_root, env=child_env, text=True, capture_output=True, check=False,
            )
        except FileNotFoundError as exc:
            # The leaf executable could not be found. Under mandatory bwrap argv[0] is
            # `bwrap`, so a missing binary means the host cannot sandbox the leaf at all
            # (e.g. the startup preflight was bypassed via
            # METDSL_ORCHESTRATION_ASSUME_BWRAP on a host where bwrap is absent — the
            # probe lied). Funnel it into the SAME fail-closed path as a missing/invalid
            # profile rather than letting a raw OSError bubble up as a generic
            # conductor_error: every "leaf cannot be sandboxed" condition terminalizes
            # consistently as a sandbox-enforcement failure.
            if self._bwrap_enabled():
                raise SandboxEnforcementError(
                    f"cannot launch sandboxed leaf — executable not found "
                    f"(bwrap missing on this host?): {exc}") from exc
            raise
        return ProcResult(proc.returncode, proc.stdout, proc.stderr)

    def read_parent_return_token(self, child_arid: str) -> str:
        path = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                / "launches" / f"{child_arid}.parent_return_token")
        return path.read_text(encoding="utf-8").strip()

    # -- bookkeeping subcommand wrappers --------------------------------------

    def _oid_args(self) -> list[str]:
        return ["--repo-root", ".", "--orchestration-id", self.orchestration_id]

    def record_launch(self, child_arid: str, request: dict[str, Any]) -> dict[str, Any]:
        response = {
            "agent_run_id": child_arid,
            "agent_session_id": child_arid,
            "started_at": _iso_now(),
            "backend": self.backend,
        }
        return self.runtime([
            "record-launch", *self._oid_args(),
            "--parent-agent-run-id", self.orchestration_agent_run_id,
            "--child-agent-run-id", child_arid,
            "--request-json", json.dumps(request),
            "--response-json", json.dumps(response),
        ])

    def finalize_child(self, child_arid: str, return_token: str, reply_text: str,
                       agent_run_json: dict[str, Any]) -> dict[str, Any]:
        return self.runtime([
            "finalize-child", *self._oid_args(),
            "--agent-run-id", child_arid,
            "--return-token", return_token,
            "--reply-text", reply_text,
            "--agent-run-json", json.dumps(agent_run_json),
        ])

    def _write_lineage(self, refs: NodeRefs) -> list[dict[str, str]]:
        """Author/refresh the pipeline `lineage.json` host-side (runtime-owned).

        `lineage.json` lives at the pipeline root, which must stay non-writable to the
        sandboxed leaf (the root contains the future source/binary/runs areas, and the
        Edit/Write tools' atomic temp-sibling+rename would need the whole root writable).
        So the conductor — which runs unconfined and already holds every id — writes it,
        matching `docs/WORKSPACE_LAYOUT.md` ("added by each phase ... runtime"). Called at
        each pipeline phase start after the producer id is reserved; idempotent, it
        accumulates the stage ids (source_id at generate, +binary_id at build, +run_id at
        validate). `direct_dependency_status` maps each direct dependency to "ready" — the
        conductor only reaches here once `workflow_launch_check` confirmed readiness.

        Returns the resolved dependency facts (the same list stored on `resolved_dependencies`
        below) so the caller can inject them into the leaf launch prompt without re-reading
        disk. `[]` for a leaf node or when no dependency resolves on disk."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        dep = ir.get("dependency") if isinstance(ir, dict) else None
        direct_deps = dep.get("direct_deps") if isinstance(dep, dict) else None
        status: dict[str, str] = {}
        for d in direct_deps or []:
            nk = d.get("node_key") if isinstance(d, dict) else d
            if isinstance(nk, str) and nk.strip():
                status[nk.strip()] = "ready"
        # Orientation-only resolved dependency facts (pipeline/run/verdict each direct dep
        # was certified to). Best-effort, never raises; persisted additively so a later
        # read (and the launch-prompt injection) need not re-derive them.
        from tools.orchestration_runtime import _resolve_dependency_facts
        facts = _resolve_dependency_facts(self.repo_root, refs.ir_ref)
        lineage = {
            "node_key": refs.node_key,
            "spec_ref": refs.spec_path,
            "ir_ref": refs.ir_ref,
            "dependency_ref": refs.ir_ref,
            "pipeline_id": refs.pipeline_id,
            "source_id": refs.source_id,
            "binary_id": refs.binary_id,
            "run_id": refs.run_id,
            "direct_dependency_status": status,
            "resolved_dependencies": facts,
        }
        path = self.repo_root / refs.pipeline_ref / "lineage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(lineage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return facts

    def _write_dependency_graph(self, refs: NodeRefs) -> dict[str, str] | None:
        """Author `<ir_ref>/dependency_graph.json` host-side at Compile phase start.

        The derived dependency graph — `all_nodes` (each with `topo_level`) and
        `transitive_deps` (each with `via`) — is a pure function of `deps.yaml` +
        `spec_catalog.yaml` (`tools.dependency_graph.build_dependency_graph`), so
        the conductor authors it deterministically instead of trusting the
        compile.generate LLM (which could mutate topo_level, drop a transitive
        edge, or diverge the closure from deps.yaml). Sister of `_write_lineage`
        / `_write_makefile` — a host-author precedent. The IR retains only the
        low-mutation directly-read `direct_deps` (with the semantic `operations`);
        the sidecar carries NO `operations`. The sidecar lives under `<ir_ref>/`
        (a leaf-non-writable managed path, like `compile_static_meta.json`), so it
        is authored host-side, not by any compile leaf.

        Returns the builder's `{reason, detail}` error dict on failure (deps.yaml
        / catalog structurally broken — fail-closed, NO partial sidecar written),
        else None. Called only for the compile phase (the only phase whose
        producer is the IR)."""
        from tools.dependency_graph import build_dependency_graph
        graph, err = build_dependency_graph(
            self.repo_root, target_spec_ref=refs.spec_path,
            target_node_key=refs.node_key)
        if err is not None:
            return err
        path = self.repo_root / refs.ir_ref / "dependency_graph.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return None

    def _write_dependency_surface(self, refs: NodeRefs) -> list[dict[str, Any]]:
        """Author `<ir_ref>/dependency_surface.json` host-side at Compile start: for each COMPONENT
        direct dependency, its published operation NAME surface (`published_operations` + a `source`
        tag), resolved from the dep's certified IR public_api (the L1 pin) else its certified source.

        The consumer IR does not exist yet at compile start, so the direct-dep set is read from the
        just-authored `dependency_graph.json` (a pure function of deps.yaml + spec_catalog.yaml),
        NOT from a not-yet-existing consumer IR. Two consumers read this ONE snapshot: the
        compile.generate leaf is SHOWN the catalog (so it transcribes real op names into its
        public_api + dep operations), and the deterministic L3 membership gate
        (`_validate_component_dep_operations_membership`) checks the authored dep operations against
        the SAME file — pinning prompt and gate to one snapshot, TOCTOU-free across a mid-phase dep
        re-cert. Sibling of `_write_dependency_graph`; the sidecar lives under `<ir_ref>/` (a
        leaf-non-writable managed path). Overwritten each compile attempt, so it never stales.

        Returns the resolved surface list (also written) so run_phase can thread it into the
        compile.generate launch payload without re-reading. Best-effort: a missing/unreadable graph
        yields `[]` and writes no sidecar (L3 then inert); an unresolvable dep yields an
        `unresolved` entry. Called only for the compile phase (whose producer is the IR)."""
        from tools.orchestration_runtime import _resolve_component_dep_surface
        graph_path = self.repo_root / refs.ir_ref / "dependency_graph.json"
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        surface = _resolve_component_dep_surface(self.repo_root, refs.node_key, graph)
        doc = {
            "generated_by": "workflow_conductor._write_dependency_surface",
            "consumer_node_key": refs.node_key,
            "dependencies": surface,
        }
        path = self.repo_root / refs.ir_ref / "dependency_surface.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return surface

    def _is_leaf_node(self, refs: NodeRefs) -> bool:
        """A node whose `dependency.direct_deps` is explicitly present and empty. An absent
        dependency block / absent direct_deps returns False (undeterminable -> treat as
        non-leaf, matching the runtime's `_impl_is_leaf_node` which returns None there).

        NOTE: leaf-ness no longer gates `src/Makefile` authorship — the conductor authors it
        for every make+fortran node (leaf OR dependency; see `_conductor_authors_makefile` /
        `_write_makefile`'s Model B branch). Retained as the canonical leaf predicate for the
        leaf concept itself (and its agreement with `_impl_is_leaf_node`)."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        dep = ir.get("dependency") if isinstance(ir, dict) else None
        if not isinstance(dep, dict) or "direct_deps" not in dep:
            return False
        return not dep.get("direct_deps")

    def _read_toolchain(self, refs: NodeRefs) -> dict[str, str]:
        """The structured `impl_defaults.toolchain`/`target` fields the host-side authors
        share. Single read so the Makefile FC/FFLAGS derivation, the lint preset pick, and
        the syntax gate's std/openmp flags cannot diverge from each other. `compiler` is
        the OPTIONAL `toolchain.compiler` (docs/IMPL_PLAN_SPEC.md) — empty string when the
        spec does not pin one (the environment default, gfortran, is then used)."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        tc = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        target = (impl.get("target") or {}) if isinstance(impl, dict) else {}
        return {
            "language": str(tc.get("language") or "fortran").lower(),
            "standard": str(tc.get("standard") or "f2008").lower(),
            "build_system": str(tc.get("build_system") or "make").lower(),
            "compiler": str(tc.get("compiler") or "").strip(),
            "backend": str(target.get("backend") or "").lower(),
        }

    def _conductor_authors_makefile(self, refs: NodeRefs) -> bool:
        """The conductor authors `src/Makefile` iff build_system=make AND language=fortran —
        exactly the scope of `_write_makefile`, for BOTH leaf and dependency nodes. The
        dependency Makefile is as deterministic as the leaf one (the closure + per-dep object
        rules come from the conductor-authored `dependency_graph.json` sidecar's `all_nodes`;
        Model B), so the conductor
        authors it too and the generate leaf must not. Single source of truth for the live
        author call AND the write-authorization removal, so they cannot disagree (which would
        orphan the Makefile, or leave it double-owned). c/cpp/mixed keep LLM authoring."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        tc = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        return (str(tc.get("build_system") or "make").lower() == "make"
                and str(tc.get("language") or "fortran").lower() == "fortran")

    def _conductor_authors_runner(self, refs: NodeRefs) -> bool:
        """The conductor host-renders `src/<spec_id>_runner.f90` (R1/M3c-β) iff the node is a
        make+fortran PHYSICS node with exactly one `infrastructure` (runner-harness) direct
        dependency. On such a node the runner is glue over the certified harness plumbing + the
        leaf-authored `<spec_id>_checks.f90`, so it is a pure function of the IR + the harness
        interface (`tools/runner_renderer.render_runner`) — the leaf authors model+checks, not
        the runner. Nodes without an infra dep keep the leaf-authored runner (legacy path); an
        `infrastructure` node authors its own self-test runner (not glue). Migration is per-node
        (add the infra dep to deps.yaml + recompile), with no flag day. Single source of truth for
        the live render call (`_write_runner`), the write-authorization swap (`build_launch_request`
        / `phase_required_outputs`), and the Makefile CHECKS rule, so they cannot disagree."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        if not isinstance(ir, dict):
            return False
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        tc = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        if str(tc.get("build_system") or "make").lower() != "make":
            return False
        if str(tc.get("language") or "fortran").lower() != "fortran":
            return False
        meta = (ir.get("meta") or {}) if isinstance(ir, dict) else {}
        if str(meta.get("spec_kind") or "").strip() == "infrastructure":
            return False
        return len(self._infra_direct_deps(ir)) == 1

    def _pure_leaf_substep(self, refs: NodeRefs, phase: str, substep: str | None) -> bool:
        """True when this substep runs as a Z2 host-mediated pure-function leaf.

        Since M-F the generate-executor is no longer selectable (legacy execution removed; `pure`
        is the only executor), so this dispatch is decided purely by the node shape: the claude
        backend (the ONLY pure producer; codex fail-closes in `leaf_command`), the two generate LLM
        substeps, and the node's M3c shape (`_conductor_authors_makefile` ∧
        `_conductor_authors_runner`) — the shape the CodegenBundle v1 producer can express (the leaf
        authors model+checks; the host renders the runner glue + the Makefile).

        A non-M3c node (harness self-test, c/cpp/mixed, a physics node with no infra dep) has no
        bundle representation for its runner, and a codex node has no pure producer, so those fall
        through to the shared AGENTIC leaf loop in `run_substep`. That is a recorded RESIDUAL of the
        migration scope, not a selectable executor: their invocation record still stamps
        `generate_executor=pure` (a provenance stamp), and they are not rejected on resume.

        Both generate LLM substeps go pure on an M3c claude node: `(generate, generate)` (the
        CodegenBundle producer, M-C) and `(generate, verify)` (the verdict reviewer, M-D). The two
        are dispatched to their own loops in `run_substep`. Deterministic generate substeps
        (lint/syntax/static) are never pure — they run in-process regardless — and compile.verify
        stays agentic (Z2 migrates the generate phase only)."""
        if self.backend != "claude":
            return False
        if (phase, substep) not in (("generate", "generate"), ("generate", "verify")):
            return False
        return self._conductor_authors_makefile(refs) and self._conductor_authors_runner(refs)

    @staticmethod
    def _infra_direct_deps(ir: dict[str, Any]) -> list[str]:
        """The `infrastructure/...` direct-dependency node_keys of an IR (the harness deps)."""
        dep = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
        out: list[str] = []
        for d in (dep.get("direct_deps") or []) if isinstance(dep, dict) else []:
            nk = d.get("node_key") if isinstance(d, dict) else (d if isinstance(d, str) else None)
            if isinstance(nk, str) and nk.strip() and nk.split("/", 1)[0].strip() == "infrastructure":
                out.append(nk.strip())
        return out

    def _write_runner(self, refs: NodeRefs) -> None:
        """Host-render `src/<spec_id>_runner.f90` for an M3c node (see `_conductor_authors_runner`).

        Resolves the single infrastructure dependency's CERTIFIED harness — the exact
        `<harness>_model.f90` Build stages/links (`_certified_model_source`) and the IR
        `public_api.signatures` that source was certified against — runs the signature pin
        (`assert_harness_pin`), then renders the runner from the IR alone (`render_runner`).
        The certified harness IR is resolved STRUCTURALLY and BOUND to the linked source's
        lineage — via `binary_meta.source_ir_id` (the host-authored ir_id the certified binary's
        source was generated from), falling back to the latest certified IR (`_certified_ir_dir`)
        for binaries predating that field. Never from a leaf-authored `source_meta.json` field.
        Binding to the binary's origin IR (not the globally-latest passing IR) prevents a false
        interface-drift failure when a same-version compile reopen advances the latest IR past the
        certified binary; the pin exact-matches the resolved IR's embedded interface
        (drift ⇒ fail, identical ⇒ pass).

        Raises RuntimeError on an unresolvable/unbuilt harness (a build precondition — run
        `--with-deps` first), a harness-interface drift (the pin), or an unrenderable IR (the
        render-error matrix). run_phase routes the raise to transport fail_closed (operator
        `--resume`), NOT a Generate content retry. Mirrors `_write_makefile` (host-authored,
        runtime-owned, before the substeps run so the write is outside the FS-diff window)."""
        from tools.runner_renderer import render_runner, assert_harness_pin, RenderError
        from tools.orchestration_runtime import (
            _certified_binary_meta, _certified_ir_dir, _is_safe_path_token,
            _latest_pipeline_dir, _model_source_from_binary_meta)
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        infra = self._infra_direct_deps(ir)
        if len(infra) != 1:
            raise RuntimeError(
                f"M3c runner authoring for {refs.node_key} requires exactly one infrastructure "
                f"dependency, found {infra}")
        harness_nk = infra[0]
        harness_sid = spec_id_of(harness_nk)
        safe = node_key_safe(harness_nk)
        pipe_dir = _latest_pipeline_dir(self.repo_root / "workspace" / "pipelines" / safe)
        if pipe_dir is None:
            raise RuntimeError(
                f"harness dependency {harness_nk}: no ready pipeline under "
                f"workspace/pipelines/{safe} to render {refs.spec_id}_runner.f90 against "
                f"(build the dependency closure first, e.g. run_workflow.py --with-deps)")
        # Select the certified binary ONCE: `source_text` (the model source) and `source_ir_id`
        # (its IR provenance) both come from this single snapshot, so a binary published between
        # two selections cannot pair a source with a mismatched IR lineage (the TOCTOU a split
        # `_certified_model_source` + separate latest-binary lookup would allow).
        bsel = _certified_binary_meta(pipe_dir)
        model_src = _model_source_from_binary_meta(pipe_dir, harness_sid, bsel[1]) \
            if bsel is not None else None
        if model_src is None:
            raise RuntimeError(
                f"harness dependency {harness_nk}: cannot resolve certified "
                f"{harness_sid}_model.f90 under {self._rel(pipe_dir)} (harness not built ready — "
                f"run_workflow.py --with-deps first)")
        source_text = model_src.read_text(encoding="utf-8")
        bmeta = bsel[1]  # the SAME binary meta the source came from — one snapshot, no TOCTOU
        # The certified harness IR (public_api.signatures) to pin against is resolved STRUCTURALLY
        # (never from the leaf-authored OPTIONAL `source_meta.json` `ir_ref`, absent from the
        # required-meta contract) AND bound to the linked source's lineage. `binary_meta.source_ir_id`
        # (host-authored at Build) is the ir_id the certified binary's source was generated from;
        # pinning against THAT IR — not the globally-latest passing IR — keeps the pinned IR and
        # the pinned source in the same generation, so a same-version compile reopen that advances
        # the latest IR past the certified binary cannot raise a false interface drift. Binaries
        # predating the field fall back to `_certified_ir_dir` (latest certified IR), which equals
        # the source's origin IR whenever no such reopen skew exists.
        kind_rest, _, harness_ver = harness_nk.partition("@")
        harness_kind = kind_rest.partition("/")[0]
        harness_ir_dir: Path | None = None
        # Legacy binaries predate the field (KEY ABSENT) and fall back; a binary that carries the
        # key AT ALL is bound strictly — a null / non-string / unresolvable value is corrupt
        # lineage, not a legacy binary. Keying on presence (not `is None`) keeps a `null` value
        # out of the fallback branch, matching the "ONLY absence falls back" invariant.
        has_source_ir_id = "source_ir_id" in bmeta
        src_ir_id = bmeta.get("source_ir_id")
        if not has_source_ir_id:
            # Legacy binary predating the field: fall back to the latest certified IR.
            # SAFETY INVARIANT: at a fixed version the controlled_spec §5.1 interface is fixed, and
            # the IR validator (`_validate_ir_signatures_against_section51`) pins every certified
            # IR's `public_api.signatures` == §5.1. So ALL passing certified IRs at the same
            # `(kind, id, version)` carry IDENTICAL signatures, and the pin compares those against
            # the renderer's embedded interface — hence WHICH same-version passing IR the fallback
            # picks cannot change the pin verdict. A signature divergence between two same-version
            # passing IRs can arise ONLY from a §5.1 edit without a version bump (a version-
            # discipline contract violation governed by R6-lite freshness), not normal operation.
            # The `source_ir_id` binding above is exact-provenance defense-in-depth on top of this.
            harness_ir_dir = _certified_ir_dir(
                self.repo_root, harness_kind, harness_sid, harness_ver)
        else:
            # A PRESENT source_ir_id must resolve to a real IR dir. A present-but-unresolvable
            # link (unsafe token, or a dir that does not exist) is corrupt lineage, NOT an
            # occasion to silently fall back to the globally-latest IR — that would reintroduce
            # the exact false-drift the binding prevents. Fail closed instead.
            if isinstance(src_ir_id, str) and _is_safe_path_token(src_ir_id.strip()):
                cand = self.repo_root / "workspace" / "ir" / safe / src_ir_id.strip()
                if cand.is_dir():
                    harness_ir_dir = cand
            if harness_ir_dir is None:
                raise RuntimeError(
                    f"harness dependency {harness_nk}: certified binary records "
                    f"source_ir_id={src_ir_id!r} but no IR dir resolves at "
                    f"workspace/ir/{safe}/<source_ir_id> (corrupt lineage) — re-certify the "
                    f"harness (run_workflow.py --with-deps)")
        ir_meta = _read_json(harness_ir_dir / "ir_meta.json") \
            if harness_ir_dir is not None else None
        if not (isinstance(ir_meta, dict)
                and str(ir_meta.get("verification_status", "")).strip().lower() == "pass"):
            # A build precondition, NOT interface drift: no certified IR to pin against.
            raise RuntimeError(
                f"harness dependency {harness_nk}: no certified IR (ir_meta.json "
                f"verification_status=pass) bound to the linked source under workspace/ir/{safe} "
                f"to pin {refs.spec_id}_runner.f90 against — run_workflow.py --with-deps first")
        harness_ir = _read_yaml(harness_ir_dir / "spec.ir.yaml") or {}
        pub = harness_ir.get("public_api") if isinstance(harness_ir, dict) else None
        harness_signatures: Any = pub.get("signatures") if isinstance(pub, dict) else None
        # "Usable" mirrors both assert_harness_pin's ir_iface build AND the validator's
        # non-empty-field rule (_validate_ir_signatures_against_section51): at least one entry
        # with a NON-BLANK str `symbol` and a NON-EMPTY mapping `signature` (the language-neutral
        # structured form — Objective B; a blank/absent field is malformed under that contract, not
        # a real signature). A missing / empty / all-malformed list is an incomplete certified
        # artifact (a build precondition), routed here as RuntimeError so the pin's RenderError stays
        # reserved for genuine interface drift (a present, usable signature that no longer matches).
        if not (isinstance(harness_signatures, list) and any(
                isinstance(e, dict)
                and isinstance(e.get("symbol"), str) and e["symbol"].strip()
                and isinstance(e.get("signature"), dict) and e["signature"]
                for e in harness_signatures)):
            raise RuntimeError(
                f"harness dependency {harness_nk}: certified IR under "
                f"{self._rel(harness_ir_dir)} carries no usable public_api.signatures to pin "
                f"against (re-certify the harness)")
        try:
            assert_harness_pin(ir, refs.spec_id, harness_sid, harness_signatures, source_text)
        except RenderError as e:
            # A pin failure on the LEGACY-fallback path (no source_ir_id) matched against the
            # latest certified IR, not a provenance-bound one. Per the same-version signature
            # invariant that can only mislead under a version-bump contract violation — but name
            # the fallback so this reads as an actionable hint, never a misdiagnosis: rebuilding
            # the harness stamps source_ir_id and binds the pin to the exact origin IR.
            if not has_source_ir_id:
                raise RenderError(
                    f"{e} [legacy harness binary carries no source_ir_id, so the pin matched the "
                    f"latest certified IR under {self._rel(harness_ir_dir)}; if this is a "
                    f"stale-IR false drift, rebuild the harness (run_workflow.py --with-deps) to "
                    f"stamp source_ir_id and bind the pin to the source's origin IR]") from e
            raise
        runner_text = render_runner(ir, refs.spec_id, harness_sid)
        path = self.repo_root / refs.source_dir() / "src" / f"{refs.spec_id}_runner.f90"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(runner_text, encoding="utf-8")

    def _dependency_closure_nodes(self, refs: NodeRefs) -> list[str]:
        """Dependency node_keys in compile order (deepest first) from the closure sidecar.

        The complete closure is `dependency_graph.json`'s `all_nodes` (the conductor-authored
        derived graph; see `_write_dependency_graph`). `all_nodes` includes the target itself,
        so self is excluded here; the remainder — direct + transitive deps — is the build
        closure, ordered by `topo_level` ascending (deepest deps, which provide modules the
        shallower ones `use`, compile first). Reading the sidecar's `all_nodes` directly
        replaces the old union of the IR's `direct_deps[]` + `transitive_deps[]` (the derived
        graph no longer lives in the IR). The node_keys carry the resolved `@<version>`, so the
        staging path (`_stage_dependency_sources`) and the Makefile object names
        (`_dependency_closure` -> spec_ids) derive from a single ordered list and cannot disagree
        on which dep / which version. The spec_id basenames must nonetheless be unique across the
        closure (the staged `<spec_id>_model.f90` / object rules are keyed on the bare spec_id);
        a same-spec_id clash (diamond) raises here (L6)."""
        from tools.validate_pipeline_semantics import _read_dependency_graph_sidecar
        graph = _read_dependency_graph_sidecar(self.repo_root, refs.ir_ref) or {}
        all_nodes = graph.get("all_nodes") if isinstance(graph, dict) else None
        levels: dict[str, int] = {}
        closure: list[str] = []
        seen: set[str] = set()
        for n in all_nodes or []:
            if not (isinstance(n, dict) and isinstance(n.get("node_key"), str)):
                continue
            nk = n["node_key"].strip()
            if not nk or nk == refs.node_key or nk in seen:
                continue
            seen.add(nk)
            levels[nk] = n.get("topo_level") or 0
            closure.append(nk)
        closure.sort(key=lambda nk: levels.get(nk, 0))
        # L6 guard: the Model B staged source basename (`<spec_id>_model.f90`) and the
        # Makefile object rules (`$(OBJDIR)/<spec_id>_model.o`) are keyed on the bare
        # spec_id (kind/@version dropped), and the dep's generated source declares a Fortran
        # `module <spec_id>_model`. Two distinct closure node_keys sharing a spec_id (a
        # diamond: `component/foo@1.0.0` + `component/foo@2.0.0`, or `component/foo` +
        # `model/foo`) would silently clobber each other (last-write-wins stage + duplicate
        # `.o` rules + a duplicate module). Version-qualifying the basename alone would not
        # fix the module-name clash, so fail closed with an actionable cause until proper
        # multi-version support (module renaming) lands.
        by_sid: dict[str, list[str]] = {}
        for nk in closure:
            by_sid.setdefault(spec_id_of(nk), []).append(nk)
        clashes = {sid: nks for sid, nks in by_sid.items() if len(nks) > 1}
        if clashes:
            raise RuntimeError(
                f"dependency closure for {refs.node_key} has spec_id basename collisions "
                f"{clashes}: the Model B staged source `<spec_id>_model.f90` and Makefile "
                f"`<spec_id>_model.o`/`module <spec_id>_model` are keyed on the bare spec_id, "
                f"so two deps sharing a spec_id (differing version/kind) would clobber each "
                f"other. Version-qualify the object/staged/module basenames before allowing "
                f"multi-version/diamond closures (deterministic_followups.md L6).")
        return closure

    def _dependency_closure(self, refs: NodeRefs) -> list[str]:
        """Dependency spec_ids in compile order (deepest first) — the `<dep>_model.o`/`.f90`
        basenames the deterministic dependency Makefile (`_write_makefile` non-leaf branch)
        compiles + links. Derived from `_dependency_closure_nodes` so the Makefile object
        names and the staged source filenames stay in lockstep."""
        return [spec_id_of(nk) for nk in self._dependency_closure_nodes(refs)]

    def _write_makefile(self, refs: NodeRefs) -> None:
        """Author the `src/Makefile` host-side (runtime-owned), deterministically.

        For a leaf node (no dependencies) the Makefile is a pure function of the IR: the
        pinned `<spec_id>_model/runner.f90` names, the fixed runner->model `use`-graph, and
        the structured `impl_defaults.toolchain`/`target` flags. Authoring it here removes a
        class of generate regenerate-loops (Makefile-shape failures) and the long Makefile
        contract the generate leaf would otherwise internalize, and makes the build
        reproducible. Mirrors `_write_lineage` (runtime-owned artifact). Scoped to
        build_system=make + language=fortran; c/cpp/mixed fall back to LLM authoring. The
        post_generate validators still run against this file as a safety net.

        Imposes `BIN ?= <spec_id>_runner` (overridable so Build/Validate.execute can pin the
        canonical binary name) and FFLAGS derived from toolchain.standard + target.backend.

        A non-empty dependency closure (Model B, docs/design) emits per-dep object rules +
        a `DEP_OBJS` link list; the conductor stages each `<dep>_model.f90` into `$(OBJDIR)`
        before `make` (`_stage_dependency_sources`, called from `_build_inproc`). `run_phase`
        authors this for every make+fortran node (leaf or dependency) — see
        `_conductor_authors_makefile`.
        """
        tc = self._read_toolchain(refs)
        language = tc["language"]
        standard = tc["standard"]
        build_system = tc["build_system"]
        if build_system != "make" or language != "fortran":
            return  # c/cpp/mixed (or non-make) keep LLM authoring — out of scope.
        backend = tc["backend"]
        # The optional toolchain.compiler pins FC (a Fujitsu frt build only needs this IR
        # field plus a run_syntax_check adapter); unset keeps the gfortran default.
        fc = tc["compiler"] or "gfortran"

        model = f"{refs.spec_id}_model"
        runner = f"{refs.spec_id}_runner"
        checks = f"{refs.spec_id}_checks"
        exe = self._resolve_exe_name(refs)  # canonical <spec_id>_runner
        # R1/M3c-β: a harness-backed physics node also compiles a leaf-authored
        # <spec_id>_checks.f90 (which `use`s the model kernel) between the model and the
        # host-rendered runner: model.o <- checks.o <- runner.o, checks.o added to the link.
        authors_runner = self._conductor_authors_runner(refs)
        # CASES default baked from the IR so a local `make all test` runs the full
        # case set standalone; Validate.execute overrides CASES/SPEC via the env so
        # `make test` invokes the runner identically to run_program (`--cases <spec>
        # <case_id>...`). The runner takes the spec path positionally but does not
        # read it, so the `SPEC ?=` default is a harmless placeholder.
        cases_default = " ".join(self.read_case_ids(refs))
        flags = f"-std={standard} -O2"
        if backend == "openmp":
            flags += " -fopenmp"
        flags += " -J$(OBJDIR) -I$(OBJDIR)"

        # Dependency closure (Model B). Empty for leaf nodes -> the blocks
        # below collapse to "" and the leaf template is emitted byte-for-byte.
        closure = self._dependency_closure(refs)
        dep_objs_line = ""
        dep_rules = ""
        model_dep_prereq = ""
        link_dep_prereq = ""
        if closure:
            dep_objs = " ".join(f"$(OBJDIR)/{d}_model.o" for d in closure)
            dep_objs_line = f"\nDEP_OBJS = {dep_objs}\n"
            model_dep_prereq = " $(DEP_OBJS)"
            link_dep_prereq = "$(DEP_OBJS) "
            # Deepest-first: each dep object depends on all deeper dep objects so their
            # `.mod` exist first (conservative over-ordering — safe for correctness). The
            # conductor stages `<dep>_model.f90` into $(OBJDIR) before make.
            parts = []
            for i, d in enumerate(closure):
                deeper = " ".join(f"$(OBJDIR)/{closure[j]}_model.o" for j in range(i))
                deeper = (deeper + " ") if deeper else ""
                parts.append(
                    f"$(OBJDIR)/{d}_model.o: $(OBJDIR)/{d}_model.f90 {deeper}| $(OBJDIR)\n"
                    f"\t$(FC) $(FFLAGS) -c $(OBJDIR)/{d}_model.f90 -o $(OBJDIR)/{d}_model.o\n")
            dep_rules = "\n" + "\n".join(parts)

        # M3c checks-module blocks (empty for a non-M3c node -> the template is emitted
        # byte-for-byte as before). checks.o `use`s the model, so it depends on MODEL_OBJ; the
        # runner links against it, so it is a runner prereq + link input.
        checks_src_decl = f"CHECKS_SRC = {checks}.f90\n" if authors_runner else ""
        checks_obj_decl = f"CHECKS_OBJ = $(OBJDIR)/{checks}.o\n" if authors_runner else ""
        checks_prereq = "$(CHECKS_OBJ) " if authors_runner else ""
        checks_rule = (
            "$(CHECKS_OBJ): $(CHECKS_SRC) $(MODEL_OBJ) | $(OBJDIR)\n"
            "\t$(FC) $(FFLAGS) -c $(CHECKS_SRC) -o $(CHECKS_OBJ)\n\n"
            if authors_runner else "")

        template = f"""\
# Deterministic Makefile authored by the conductor (build_system=make, language=fortran).
# Out-of-source capable: OBJDIR/BINDIR/RUNDIR default to "." and are overridden by
# Build (compile_project) and Validate.execute (run_quality_checks).

# FC is pinned with := (not ?=): make ships a built-in FC=f77 (origin default), and ?= does
# NOT override a default-origin variable, so `FC ?= gfortran` would silently leave FC=f77.
# The pinned value is impl_defaults.toolchain.compiler when the spec sets it, else gfortran.
# The dirs/BIN stay ?= because Build/Validate.execute inject them via command line / env.
# SPEC/CASES stay ?= because Validate.execute injects them via the make-test env so the
# `make test` re-run invokes the runner identically to run_program (`--cases <spec> <ids>`);
# the ?= defaults keep a local `make all test` runnable standalone.
FC      := {fc}
OBJDIR  ?= .
BINDIR  ?= .
RUNDIR  ?= .
FFLAGS  ?= {flags}

BIN ?= {exe}
SPEC ?= spec.ir.yaml
CASES ?= {cases_default}

MODEL_SRC  = {model}.f90
{checks_src_decl}RUNNER_SRC = {runner}.f90

MODEL_OBJ  = $(OBJDIR)/{model}.o
{checks_obj_decl}RUNNER_OBJ = $(OBJDIR)/{runner}.o
{dep_objs_line}
.PHONY: all test clean
.DEFAULT_GOAL := all

all: $(BINDIR)/$(BIN)
{dep_rules}
$(MODEL_OBJ): $(MODEL_SRC){model_dep_prereq} | $(OBJDIR)
\t$(FC) $(FFLAGS) -c $(MODEL_SRC) -o $(MODEL_OBJ)

{checks_rule}$(RUNNER_OBJ): $(RUNNER_SRC) {checks_prereq}$(MODEL_OBJ) | $(OBJDIR)
\t$(FC) $(FFLAGS) -c $(RUNNER_SRC) -o $(RUNNER_OBJ)

$(BINDIR)/$(BIN): {link_dep_prereq}$(MODEL_OBJ) {checks_prereq}$(RUNNER_OBJ) | $(BINDIR)
\t$(FC) $(FFLAGS) {link_dep_prereq}$(MODEL_OBJ) {checks_prereq}$(RUNNER_OBJ) -o $(BINDIR)/$(BIN)

# $(sort ...) dedups the target list: when OBJDIR==BINDIR (in-source make, both ".")
# it collapses to a single target, avoiding the harmless `target '.' given more than
# once` warning (and without two recipes for the same target).
$(sort $(OBJDIR) $(BINDIR)):
\tmkdir -p $@

test:
\ttest -x $(BINDIR)/$(BIN) || {{ echo "error: $(BINDIR)/$(BIN) not built; run 'make all' first" >&2; exit 1; }}
\tmkdir -p $(RUNDIR)/raw/state_snapshots
\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)

clean:
\trm -f $(OBJDIR)/*.o $(OBJDIR)/*.mod $(BINDIR)/$(BIN)
"""
        path = self.repo_root / refs.source_dir() / "src" / "Makefile"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template, encoding="utf-8")

    # -- Z2 pure-function CodegenBundle producer (M-C) ------------------------
    # The pure `generate.generate` producer returns one CodegenBundle JSON document; the host
    # validates it, assembles it against the IR + the certified harness, and writes the files +
    # the derived Makefile. The leaf holds no write authority, so every artifact is host-written
    # AFTER the child window closes (finalize_child). The methods below are the host side of
    # that channel: context assembly, bundle validation + assembly preflight, artifact writes,
    # and the bundle-derived Makefile. Gated by `_pure_leaf_substep`; unreachable on the agentic
    # leaf path (existing suite green proves the agentic path is byte-for-byte unchanged).

    def _pure_harness_node_key(self, ir: dict[str, Any]) -> str | None:
        """The ONE harness a pure leaf negotiates against: its node's single `infrastructure`
        direct dependency, or None when there is not exactly one.

        The SINGLE resolution shared by the context assembly (`_build_pure_context`, which shows
        the leaf that harness's manifest) and the acceptance layer (`_pure_bundle_violations`,
        which negotiates `capability_requirements` against it). One source, so the capabilities
        the leaf is shown are by construction the capabilities it is judged against. A pure node
        is M3c by `_pure_leaf_substep`, so it has exactly one infra dep; None is the fail-closed
        answer for anything else (nothing provided, every requirement unsatisfied)."""
        infra = self._infra_direct_deps(ir)
        return infra[0] if len(infra) == 1 else None

    def _build_pure_context(self, refs: NodeRefs) -> dict[str, str]:
        """Assemble the closed context a pure `generate.generate` leaf sees, each value a plain
        string the renderer data-fences. All data is host-resolved from disk here (the leaf has
        no filesystem): the harness capability manifest (A6), the node's toolchain/target
        defaults, the lowered IR, the tests, and the host-rendered runner. Mirrors the must-read
        set the agentic `generate.generate` leaf reads.

        Per phase_02 §2-1 the producer does NOT read controlled_spec.md — `spec.ir.yaml` is the
        sole carrier of the algorithm a generate leaf implements. (A pure-5 INTERIM carve-out once
        inlined controlled_spec.md into this producer context to close a producer-blind /
        checker-sighted asymmetry against a thin `compile` roll; a pure-8 removal was reverted, an
        unrelated pure-9 distillation kept it live, and it was finally removed at pure-10 once the
        `compile`-side IR self-sufficiency guarantees landed — the strengthened lowering rule plus
        the deterministic `Compile.static` local-op lowering presence floor. The pure
        `generate.verify` reviewer still reads controlled_spec.md by design — §2-2, see
        `_build_pure_verify_context` — because it verifies the source against it.)

        The runner is injected VERBATIM (no interface extraction): it is the consumer of the
        checks-module ABI the leaf must author against, and `docs/workflow/CHECKS_MODULE_CONTRACT.md`
        — where the agentic leaf reads that ABI — is unreachable from a tool-less leaf. Injecting
        the rendered artifact rather than a distilled restatement keeps the ABI's dynamic surface
        (which names the runner actually imports) exact by construction. `run_phase` renders it
        before any generate substep runs, so it is always on disk here."""
        from tools.codegen_bundle import harness_capability_manifest_document_for
        ir_text = ""
        ir_path = self.repo_root / refs.ir_ref / "spec.ir.yaml"
        try:
            ir_text = ir_path.read_text(encoding="utf-8")
        except OSError:
            ir_text = ""
        tests_text = ""
        try:
            tests_text = (self.repo_root / refs.spec_path / "tests.md").read_text(encoding="utf-8")
        except OSError:
            tests_text = ""
        ir = _read_yaml(ir_path) or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        # A missing runner RAISES rather than degrading to "" the way ir/tests do above: the
        # empty string would satisfy the renderer's presence check and ship a prompt whose ABI
        # section is blank, which is precisely the defect this injection fixes. The caller
        # converts this into a fail_closed transport outcome (no leaf is spawned).
        runner_path = self.repo_root / refs.source_dir() / "src" / f"{refs.spec_id}_runner.f90"
        try:
            runner_text = runner_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            # UnicodeError too: a corrupt/tampered runner raises UnicodeDecodeError, which is a
            # ValueError — NOT an OSError — so catching OSError alone would let it escape as a
            # bare decode error instead of this named, fail-closed contract.
            raise RuntimeError(
                f"pure_runner_document_missing: {runner_path}: {exc}") from exc
        return {
            "harness_capabilities": json.dumps(
                harness_capability_manifest_document_for(self._pure_harness_node_key(ir)),
                indent=2, ensure_ascii=False),
            "target_profile": json.dumps(impl, indent=2, ensure_ascii=False),
            "ir_document": ir_text,
            "tests_document": tests_text,
            "runner_document": runner_text,
        }

    def _pure_bundle_violations(self, refs: NodeRefs,
                                doc: Any) -> tuple[str, str] | None:
        """Validate + assembly-preflight a producer's CodegenBundle. Returns None for a clean
        bundle, else `(failure_category, findings_text)` for the bounded repair / bundle_meta.

        Fail-closed layers, in order (each stops at the first that fails so one defect is one
        report): `validate_bundle` (schema + cross-field invariants) -> single-node unit shape
        -> harness capability negotiation (the manifest MUST exist — an undeclared harness
        satisfies nothing) -> state_variable ∈ IR algorithm.state_variables -> the M3c literal
        name constraint the host-rendered runner glue `use`s -> the fixed checks-module ABI ->
        `derive_build_graph` cross-origin object/module collisions. The pure leaf has
        no tools, so a corrupted bundle can only propagate as content here — these layers are what
        catch it before any file is written.

        The ABI layer makes a mis-authored checks module a BOUNDED in-conversation repair instead
        of what it was before: a `Generate.gate` syntax-checker failure that reopened the whole phase, and that
        the producer could only answer by re-guessing an ABI it had never been shown.

        The layers themselves live in `codegen_bundle.pure_bundle_contract_violation` (the SINGLE
        source shared with the deterministic post-generate tamper gate, so the two cannot drift);
        this method only assembles the conductor-side inputs (IR state vars, resolved harness
        capabilities, the build-graph derivation) and delegates."""
        from tools.codegen_bundle import (
            pure_bundle_contract_violation, harness_provided_capabilities,
            published_operations_from_ir)
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        # Capability negotiation against the SINGLE infrastructure (harness) dependency, resolved
        # by the same `_pure_harness_node_key` that narrows the manifest the leaf is SHOWN — so a
        # capability the context advertises is always one this layer accepts. Its manifest MUST be
        # declared (None => nothing provided => every requirement unsatisfied, fail-closed).
        harness_nk = self._pure_harness_node_key(ir)
        provided = harness_provided_capabilities(harness_nk) if harness_nk else None
        algorithm = (ir.get("algorithm") or {}) if isinstance(ir, dict) else {}
        return pure_bundle_contract_violation(
            doc, node_key=refs.node_key, spec_id=refs.spec_id,
            ir_state_variables=(algorithm.get("state_variables") or []),
            harness_provided=provided, harness_label=harness_nk,
            build_graph=lambda d: self._build_pure_bundle_graph(refs, d),
            ir_published_operations=published_operations_from_ir(ir))

    def _build_pure_bundle_graph(self, refs: NodeRefs, doc: Any) -> dict[str, Any]:
        """The deterministic build graph of a bundle (`derive_build_graph`), with the closure,
        toolchain, host glue, and dependency edges the host owns. Raises RuntimeError on a
        cross-origin collision or a staged-dependency straddle (surfaced as an assembly failure
        by the caller). The single derivation source for both the assembly preflight and the
        Makefile renderer, so the graph the host validates is the graph it builds."""
        from tools.codegen_bundle import derive_build_graph
        from tools.validate_pipeline_semantics import _read_dependency_graph_sidecar
        tc = self._read_toolchain(refs)
        toolchain = {
            "language": tc["language"], "standard": tc["standard"],
            "build_system": tc["build_system"], "backend": tc["backend"],
        }
        if tc["compiler"]:
            toolchain["compiler"] = tc["compiler"]
        closure_nodes = self._dependency_closure_nodes(refs)
        # Dependency edges from the conductor-authored sidecar, so a staged dep depending on an
        # absorbed member is rejected (a v1 single-node unit has none, but the check is honest).
        sidecar = _read_dependency_graph_sidecar(self.repo_root, refs.ir_ref) or {}
        edges: dict[str, list[str]] = {}
        for entry in (sidecar.get("all_nodes") or []) if isinstance(sidecar, dict) else []:
            if not isinstance(entry, dict):
                continue
            nk = entry.get("node_key")
            deps = [d.get("node_key") if isinstance(d, dict) else d
                    for d in (entry.get("direct_deps") or [])]
            if isinstance(nk, str):
                edges[nk] = [d for d in deps if isinstance(d, str)]
        return derive_build_graph(
            doc, dependency_closure=tuple(closure_nodes), toolchain=toolchain,
            host_glue_sources=(f"{refs.spec_id}_runner.f90",),
            dependency_edges=edges or None)

    def _render_pure_makefile_from_graph(self, refs: NodeRefs, graph: dict[str, Any]) -> str:
        """Render `src/Makefile` from a bundle's derived build graph (`derive_build_graph`).

        Unlike the IR-shaped `_write_makefile` (which assumes a fixed model/checks/runner set —
        still the live Makefile author for Model B dependencies and non-M3c agentic leaves), this
        compiles EXACTLY the graph's `compile_units` — so a bundle that declares a helper /
        internal_module file is built too. The overridable FC/OBJDIR/BINDIR/RUNDIR/BIN/
        SPEC/CASES surface and the test/clean targets match the IR-shaped Makefile so Build
        (`compile_project`) and Validate.execute (`run_quality_checks`) drive it identically.
        Source paths: a `staged:` dep is `$(OBJDIR)/<name>` (staged by `_stage_dependency_sources`
        before make), a `bundle:` / `glue:` source is a filename in the src/ cwd. Objects live
        under `$(OBJDIR)`; the conservative total prerequisite order comes from the graph."""
        tc = self._read_toolchain(refs)
        fc = tc["compiler"] or "gfortran"
        flags = f"-std={tc['standard']} -O2"
        if tc["backend"] == "openmp":
            flags += " -fopenmp"
        flags += " -J$(OBJDIR) -I$(OBJDIR)"
        exe = self._resolve_exe_name(refs)
        cases_default = " ".join(self.read_case_ids(refs))

        def _src_path(source: str) -> str:
            kind, _, name = source.partition(":")
            return f"$(OBJDIR)/{name}" if kind == "staged" else name

        compile_units = graph.get("compile_units") or []
        rules: list[str] = []
        for unit in compile_units:
            src = _src_path(str(unit.get("source", "")))
            obj = f"$(OBJDIR)/{unit.get('object')}"
            prereqs = " ".join(f"$(OBJDIR)/{o}" for o in (unit.get("prerequisite_objects") or []))
            prereqs = (prereqs + " ") if prereqs else ""
            rules.append(
                f"{obj}: {src} {prereqs}| $(OBJDIR)\n"
                f"\t$(FC) $(FFLAGS) -c {src} -o {obj}")
        link_objs = " ".join(f"$(OBJDIR)/{o}" for o in (graph.get("link") or {}).get("objects") or [])
        rules_block = "\n\n".join(rules)
        return f"""\
# Deterministic Makefile authored by the conductor from the CodegenBundle build graph
# (Z2 pure producer). Out-of-source capable: OBJDIR/BINDIR/RUNDIR default to "." and are
# overridden by Build (compile_project) and Validate.execute (run_quality_checks).
FC      := {fc}
OBJDIR  ?= .
BINDIR  ?= .
RUNDIR  ?= .
FFLAGS  ?= {flags}

BIN ?= {exe}
SPEC ?= spec.ir.yaml
CASES ?= {cases_default}

.PHONY: all test clean
.DEFAULT_GOAL := all

all: $(BINDIR)/$(BIN)

{rules_block}

$(BINDIR)/$(BIN): {link_objs} | $(BINDIR)
\t$(FC) $(FFLAGS) {link_objs} -o $(BINDIR)/$(BIN)

# $(sort ...) dedups the target list when OBJDIR==BINDIR (in-source make, both ".").
$(sort $(OBJDIR) $(BINDIR)):
\tmkdir -p $@

test:
\ttest -x $(BINDIR)/$(BIN) || {{ echo "error: $(BINDIR)/$(BIN) not built; run 'make all' first" >&2; exit 1; }}
\tmkdir -p $(RUNDIR)/raw/state_snapshots
\tcd $(RUNDIR) && $(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)

clean:
\trm -f $(OBJDIR)/*.o $(OBJDIR)/*.mod $(BINDIR)/$(BIN)
"""

    def _write_pure_bundle_artifacts(self, refs: NodeRefs, doc: dict[str, Any],
                                     graph: dict[str, Any]) -> list[str]:
        """Write a validated bundle's artifacts host-side, AFTER the producer's child window
        closes: each `files[]` entry to `src/<logical_path>`, the canonical `codegen_bundle.json`
        (the accepted document, for provenance + verify's input), and the bundle-derived
        `src/Makefile`. The host holds every path and runs unconfined, so these writes are not
        leaf-attributed (finalize_child already closed the FS-diff window). Returns the written
        source paths (repo-relative) for logging. The runner glue is host-rendered separately
        (`_write_runner`, at phase start)."""
        src_dir = self.repo_root / refs.source_dir() / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        src_root = src_dir.resolve()
        written: list[str] = []
        for entry in doc.get("files") or []:
            if not isinstance(entry, dict):
                continue
            logical = str(entry.get("logical_path") or "")
            content = entry.get("content")
            if not logical or not isinstance(content, str):
                continue
            target = src_dir / logical
            # Defense-in-depth containment: `validate_bundle`'s `logical_path_violations`
            # already rejected any absolute / `..` / non-normalized path before this doc was
            # accepted, so a path escaping src/ here means a validator bypass — fail closed
            # loudly rather than writing outside the source tree. (Belt-and-suspenders over the
            # gated pass path; never fires on a validated bundle.)
            if not target.resolve().is_relative_to(src_root):
                raise RuntimeError(
                    f"pure bundle file {logical!r} resolves outside the source tree "
                    f"({target.resolve()} not under {src_root}) — refusing to write")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(f"{refs.source_dir()}/src/{logical}")
        # The accepted bundle, host-authored at the source root (leaf-non-writable), for
        # provenance and as the verify persona's input (M-D). Not a leaf deliverable.
        bundle_path = self.repo_root / refs.source_dir() / "codegen_bundle.json"
        bundle_path.write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        makefile = self.repo_root / refs.source_dir() / "src" / "Makefile"
        makefile.write_text(self._render_pure_makefile_from_graph(refs, graph), encoding="utf-8")
        return written

    def _write_bundle_meta(self, refs: NodeRefs, *, result: str,
                           failure_category: str | None, failure_excerpt: str | None,
                           attempts: int, per_attempt: list[dict[str, Any]]) -> None:
        """Author `<source_dir>/bundle_meta.json` — the pure producer's terminal record and the
        freshness-gated deliverable of the pure `generate.generate` substep. Carries the outcome
        (`result` pass/fail), the terminal `failure_category` + `failure_excerpt` on exhaustion
        (`_read_repair_findings` reads the excerpt for the outer repair route), the attempt count,
        the prompt-contract version (an A7 observable event), and per-attempt model/usage from the
        CLI result envelope (the ~/.claude-free provenance source). A re-derivable value is not
        introduced beyond what only the transcript would otherwise hold."""
        from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
        meta: dict[str, Any] = {
            "result": result,
            "failure_category": failure_category,
            "attempts": attempts,
            "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
            "per_attempt": per_attempt,
        }
        if failure_excerpt:
            meta["failure_excerpt"] = failure_excerpt
        path = self.repo_root / refs.source_dir() / "bundle_meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _run_pure_generate_substep(self, refs: NodeRefs, phase: str, substep: str | None,
                                   repair: dict[str, str] | None,
                                   resolved_dependencies: tuple[dict[str, str], ...]
                                   ) -> "SubstepOutcome":
        """Run `generate.generate` as a Z2 pure-function producer: spawn a tool-less `claude -p`
        leaf that returns one CodegenBundle, validate + assembly-preflight it, repair a violation
        in a bounded warm-resume loop, finalize the accepted attempt with an EMPTY output_refs
        row, and ONLY THEN write the bundle's artifacts host-side.

        The finalize-before-write ordering is load-bearing: the pure capability's empty
        write_roots make ANY write inside the child window an unauthorized write
        (`_validate_actual_write_paths`), so the host must close the window (finalize_child)
        before it writes files[] / codegen_bundle.json / bundle_meta.json / Makefile. Reversing
        the order attributes the host writes to the dying leaf and fails closed — pinned by a
        conformance test."""
        from tools.pure_leaf import (
            parse_result_envelope, extract_json_document, MAX_BUNDLE_REPAIR_TURNS,
            RESPONSE_UNPARSEABLE, _MISSING)
        # Assembling the context reads host-owned artifacts and RAISES on a missing one
        # (`pure_runner_document_missing`). run_substep's callers must never see an exception —
        # recover it as the same fail_closed transport outcome a failed `_write_runner` produces.
        # A host artifact the conductor itself renders cannot be repaired by a generate retry, so
        # fail_closed (operator --resume) is the correct terminus, not a reopen. No leaf has been
        # spawned yet, so the arid here names no child window; it only labels the outcome row.
        try:
            pure_context = self._build_pure_context(refs)
        except Exception as exc:  # noqa: BLE001 — any context-assembly failure must recover
            self.emit("pure_context_assembly_failed", node_key=refs.node_key,
                      detail=str(exc)[:200])
            return SubstepOutcome(
                self.new_agent_run_id(), "fail", [], 1,
                ("pure_context_assembly_failed", f"{type(exc).__name__}: {exc}"),
                time.time(), 1)
        per_attempt: list[dict[str, Any]] = []
        resume_session_id: str | None = None
        prior_document: str | None = None
        last_excerpt: str | None = None
        # An OUTER cross-phase reopen (run_phase routed a terminal bundle failure to
        # (generate, reuse)) threads the prior producer's arid + its bundle_meta findings excerpt
        # here. Seed the loop so the FIRST attempt warm-resumes that session (when still
        # resumable) and carries the diagnosis, rather than cold-restarting and re-deriving
        # everything from pure_context — the M-C carry-forward contract. When the session is gone,
        # resume_session_id stays None and the first attempt is a cold launch (findings dropped,
        # the safe degradation the pure launch template has no slot for).
        if repair and str(repair.get("repair_strategy", "")).strip() == "reuse":
            target = str(repair.get("repair_target_agent_run_id", "")).strip()
            if target and target != "none" and self._claude_session_resumable(target):
                resume_session_id = target
                # The outer-reopen excerpt is threaded into the first repair turn's
                # `repair_findings` (a UTF-8-persisted prompt). Its writers under the pure executor
                # (bundle_meta / source_meta) are already surrogate-safe, so this is safe by that
                # invariant today; normalize at capture anyway so the seed is safe by construction
                # (identity on clean text) rather than relying on every upstream writer staying so.
                seed = str(repair.get("repair_findings", "")).strip() or None
                last_excerpt = (seed.encode("utf-8", "backslashreplace").decode("utf-8")
                                if seed is not None else None)
        # R5: resolve the certified sibling exemplar ONCE, above the loop — it is
        # attempt-invariant (the selector never raises; a failure just omits it), mirroring the
        # agentic path. It is attached per-attempt only when that attempt renders the LAUNCH
        # template: the repair template (`pure_bundle_repair.txt`) has no `<exemplar>` slot, so
        # attaching it to a repair turn would ship the payload with nothing rendering it.
        exemplar = self._resolve_exemplar(refs)
        attempt = 0
        usage_waits = 0
        while True:
            child_arid = self.new_agent_run_id()
            warm = (resume_session_id is not None
                    and self._claude_session_resumable(resume_session_id))
            # A repair turn is any turn with a session to resume (an inner repair — resume_session_id
            # was set to the prior attempt below — or the seeded outer reopen). `warm` may still be
            # False if that session has since been GC'd, in which case the repair renders as a
            # cold fallback (prior_document + re-inlined context) but keeps the reuse repair shape.
            repair_payload: dict[str, str] | None = None
            if resume_session_id is not None:
                repair_payload = {
                    "issue_severity": "major",
                    "repair_strategy": "reuse",
                    "repair_target_agent_run_id": resume_session_id,
                    "repair_reason": "pure_bundle_repair",
                }
                if last_excerpt:
                    repair_payload["repair_findings"] = last_excerpt
            # Same predicate the render dispatch uses to choose the launch vs repair template
            # (`_render_launch_prompt_template`): a repair payload WITHOUT findings still renders
            # the full launch prompt. True for a cold first attempt and for an outer reopen seeded
            # with no findings excerpt (which also re-sends pure_context) — both want the
            # exemplar; every inner repair turn carries findings and does not.
            renders_launch_prompt = (
                repair_payload is None
                or not str(repair_payload.get("repair_findings", "")).strip())
            request = build_launch_request(
                refs, step=phase, substep=substep,
                orchestration_id=self.orchestration_id,
                orchestration_agent_run_id=self.orchestration_agent_run_id,
                child_agent_run_id=child_arid,
                agent_model=self.agent_model, workflow_mode=self.workflow_mode,
                makefile_host_authored=True, runner_host_authored=True,
                repair=repair_payload,
                resolved_dependencies=resolved_dependencies,
                exemplar=(exemplar if renders_launch_prompt else None),
                warm_resume=warm,
                pure_leaf=True,
                # On a warm reuse repair the resumed session already holds the context, so it is
                # omitted — but ONLY when the validator's exemption holds (warm + reuse +
                # findings). A cold launch, or a cold-fallback repair (session GC'd), carries the
                # full context; the cold fallback also carries the prior document to correct.
                pure_context=(None if (warm and repair_payload is not None
                                       and repair_payload.get("repair_findings"))
                              else pure_context),
            )
            if repair_payload is not None and not warm and prior_document:
                request["prior_document"] = prior_document
            rec = self.record_launch(child_arid, request)
            launched_at = time.time()
            proc = self.spawn_leaf(
                rec["launch_prompt_text"], self._child_env(child_arid),
                session_id=child_arid,
                resume_session_id=(resume_session_id if warm else None),
                child_arid=child_arid, pure=True)
            self._persist_leaf_output(child_arid, proc)
            token = self.read_parent_return_token(child_arid)

            envelope = parse_result_envelope(proc.stdout)
            model = None if envelope.model is _MISSING else envelope.model
            usage = None if envelope.usage is _MISSING else envelope.usage
            attempt_record: dict[str, Any] = {
                "agent_run_id": child_arid, "model": model, "usage": usage}
            per_attempt.append(attempt_record)

            infra_error: tuple[str, str] | None = None
            category: str | None = None
            findings: str | None = None
            # The parsed document (whatever json.loads produced), kept for the prior-document
            # carry-forward even when it later fails validation; `accepted_doc` is set ONLY when
            # the bundle passes every layer.
            parsed_bundle: dict[str, Any] | None = None
            accepted_doc: dict[str, Any] | None = None
            if proc.returncode != 0:
                # A nonzero leaf exit is a transport/infra failure (not a content defect the
                # bundle repair can fix): finalize fail and let run_phase route it fail_closed.
                infra_error = _classify_leaf_infra_error(proc.stderr or "", proc.stdout or "")
                category = "pure_transport"
                findings = self._leaf_failure_summary(proc)
            elif not envelope.parsed or envelope.is_error is True:
                category = RESPONSE_UNPARSEABLE
                findings = ("the CLI result envelope was unparseable or reported is_error: "
                            + (str(envelope.parse_error or "")[:400] or "no result document"))
            else:
                extracted, extract_category = extract_json_document(envelope.result)
                if extract_category is not None:
                    category = extract_category
                    findings = ("the reply was not a single parseable JSON document "
                                f"({extract_category})")
                elif not isinstance(extracted, dict):
                    category = RESPONSE_UNPARSEABLE
                    findings = "the reply parsed to a non-object JSON value (expected a bundle)"
                else:
                    parsed_bundle = extracted
                    result = self._pure_bundle_violations(refs, extracted)
                    if result is not None:
                        category, findings = result
                    else:
                        # A bundle can pass every content layer yet not be persistable:
                        # `json.loads` accepts a lone surrogate (e.g. `"\ud800"` inside a
                        # files[].content or a metadata string), but UTF-8-encoding it to author
                        # codegen_bundle.json / src/<file> (`_write_pure_bundle_artifacts`,
                        # ensure_ascii=False) raises. Catch that HERE as a schema violation
                        # (repairable — the producer re-emits valid text) rather than accepting it
                        # and letting the later host-write raise, which the pass branch's except
                        # would mis-route through pure_host_write_failed (a transport fail_closed)
                        # instead of a bounded repair. Mirrors the verify reviewer's check.
                        try:
                            json.dumps(extracted, ensure_ascii=False).encode("utf-8")
                        except UnicodeEncodeError:
                            category = "bundle_schema_violation"
                            findings = ("the bundle contains characters that cannot be encoded as "
                                        "UTF-8 (e.g. an unpaired surrogate); re-emit the bundle "
                                        "with valid text")
                        else:
                            accepted_doc = extracted

            status = "pass" if category is None else "fail"
            if status != "pass":
                # A transport death ("pure_transport") has NO fixable document, so it must not
                # overwrite the repair carriers (`prior_document` / `last_excerpt`). Today transport
                # is terminal so this never mattered; --wait-usage-reset makes a repair turn
                # reachable AFTER a transport wait, and a repair turn that shipped "Connection closed
                # mid-response" as its `repair_findings` would mislead the producer. So only a
                # CONTENT failure updates the carriers; a transport attempt leaves the prior content
                # failure's carriers intact for the retry that follows the wait (a fresh cold launch
                # when there was none).
                if category != "pure_transport":
                    # Carry the prior document into a cold-fallback repair: re-serialize the parsed
                    # bundle (even though it failed validation), else the raw reply text.
                    prior_document = (json.dumps(parsed_bundle, indent=2, ensure_ascii=False)
                                      if parsed_bundle is not None
                                      else (envelope.result if isinstance(envelope.result, str)
                                            else None))
                    # Both `last_excerpt` (-> the repair turn's `repair_findings` AND bundle_meta's
                    # failure_excerpt) and `prior_document` (-> the cold-fallback repair prompt) are
                    # echoed into a request/meta persisted as UTF-8. Leaf-derived text can carry an
                    # unpaired surrogate (the encodability case caught above, or a raw reply the CLI
                    # passed through), which would raise UnicodeEncodeError at that write — a crash
                    # instead of a repair turn / fail_closed. Normalize any non-encodable code point
                    # to its readable backslash escape at capture so every downstream write is safe
                    # by construction. Mirrors the verify reviewer.
                    last_excerpt = (findings.encode("utf-8", "backslashreplace").decode("utf-8")
                                    if findings is not None else None)
                    if prior_document is not None:
                        prior_document = prior_document.encode(
                            "utf-8", "backslashreplace").decode("utf-8")
                # Record why THIS attempt failed on its own per_attempt row (observability only —
                # ADDITIVE fields; the terminal top-level failure_excerpt stays the repair carrier
                # `_read_repair_findings` reads). A superseded attempt is otherwise a bare
                # arid/model/usage row that says nothing about the failure it burned a turn on. The
                # excerpt here is computed LOCALLY from this attempt's findings so a transport row
                # is still labeled without touching the repair carriers above.
                this_excerpt = (findings.encode("utf-8", "backslashreplace").decode("utf-8")
                                if findings is not None else None)
                attempt_record["failure_category"] = category
                attempt_record["failure_excerpt"] = (
                    this_excerpt[:_PURE_ATTEMPT_EXCERPT_MAX_CHARS] if this_excerpt else None)
                self.emit("pure_bundle_attempt_failed", node_key=refs.node_key,
                          substep=substep, attempt=len(per_attempt), failure_category=category,
                          detail=(this_excerpt or "")[:200])

            reply = (f"status: {status}\nleaf rc={proc.returncode}\n"
                     f"category: {category or 'none'}")
            # A pure row carries EMPTY output_refs, so `result_summary` is the ONLY thing that can
            # speak for it: `_validate_agent_summary_text` requires a terminal row with no
            # output_refs to explain itself (a rule written for fail rows, but a pure PASS is
            # equally output-less and must satisfy it). Leaving this None on pass makes
            # finalize-child reject every passing pure leaf — the executor cannot complete.
            result_summary = (
                f"pure_generate_fail: {category}" if status != "pass"
                else f"pure_generate_pass: bundle accepted (attempts={len(per_attempt)})"
            )
            # Finalize the attempt FIRST (close the child FS-diff window) — the pure terminal row
            # carries an EMPTY output_refs (the host has written nothing yet). ONLY AFTER this may
            # the host write the bundle artifacts.
            self.finalize_child(
                child_arid, token, reply,
                self._agent_run_json(refs, phase, substep, child_arid, status,
                                     [], result_summary, agent_model_override=model, pure=True))

            if status == "pass":
                assert accepted_doc is not None
                try:
                    graph = self._build_pure_bundle_graph(refs, accepted_doc)
                    self._write_pure_bundle_artifacts(refs, accepted_doc, graph)
                    self._write_bundle_meta(
                        refs, result="pass", failure_category=None, failure_excerpt=None,
                        attempts=len(per_attempt), per_attempt=per_attempt)
                except Exception as exc:  # noqa: BLE001 — any host-write failure must recover
                    # finalize_child ALREADY recorded this attempt as a passing terminal `substep`
                    # row, but a host-side write AFTER the window closed failed (ENOSPC, a
                    # permission/IO error, or a late assembly RuntimeError). No step_result will be
                    # written, so without recovery this passing row is an UN-VOUCHED orphan the
                    # completion gate (`_validate_orchestration_completion_for_pass`) rejects on the
                    # eventual resume (which rotates to a fresh source_id and never revisits it).
                    # Tombstone the earlier repair attempts here (as the pass/fail branches do) and
                    # route fail_closed via a non-zero leaf_returncode — run_phase's transport branch
                    # then tombstones THIS child_arid orphan too and fail-closes for an operator
                    # resume, rather than auto-retrying a write that a full disk would only repeat.
                    if attempt > 0:
                        self._add_superseded_run_ids(
                            [a["agent_run_id"] for a in per_attempt[:-1]],
                            reason=f"pure_host_write_failed_superseded: {type(exc).__name__}")
                    return SubstepOutcome(child_arid, "fail", [], 1,
                                          ("pure_host_write_failed", f"{type(exc).__name__}: {exc}"),
                                          launched_at, len(per_attempt))
                # Tombstone the superseded producer attempts of a repaired pass: each earlier
                # attempt was finalized as a terminal `substep` row, but only THIS (passing)
                # arid goes into the step_result's substep_agent_run_ids, so the earlier arids
                # are un-vouched orphans the completion gate would reject at run end. Same
                # treatment as the terminal-fail branch below and the transient-retry loop.
                if attempt > 0:
                    self._add_superseded_run_ids(
                        [a["agent_run_id"] for a in per_attempt[:-1]],
                        reason=f"pure_bundle_repair_superseded_pass: attempts={len(per_attempt)}")
                return SubstepOutcome(child_arid, "pass", [], proc.returncode,
                                      None, launched_at, len(per_attempt))

            # --wait-usage-reset (opt-in): a transport death carrying a resolvable usage-limit
            # reset (in practice the CLI's TZ-anchored human form) is waited out in place and the
            # SAME turn re-launched, rather than falling
            # to the terminal fail branch for a next-day --resume. Nothing else here treats a
            # transport death as repairable, so the wait is its only in-loop recovery. `attempt` and
            # `resume_session_id` are UNCHANGED (a wait is not a repair turn): a cold first attempt
            # retries cold; an interrupted repair turn re-runs against the same carriers (which the
            # bookkeeping guard above kept intact). The dead arid was already finalized above, so the
            # tombstone lands outside its write window; per_attempt keeps its row.
            # Gate on the classified tag (not merely a nonzero exit): `pure_transport` is set for ANY
            # nonzero leaf exit, so require `llm_usage_limit` explicitly — the same guard run_substep
            # uses — so a non-usage crash whose prose happens to match the usage pattern is not waited.
            if (category == "pure_transport" and infra_error is not None
                    and infra_error[0] == "llm_usage_limit"):
                plan = self._usage_reset_wait_plan(
                    proc, usage_waits, node_key=refs.node_key, step=phase, substep=substep,
                    dead_agent_run_id=child_arid, evidence=infra_error[1],
                    allow_envelope=True)   # pure leaves ARE `--output-format json`
                if plan is not None:
                    self._wait_for_usage_reset(
                        node_key=refs.node_key, step=phase, substep=substep,
                        dead_agent_run_id=child_arid, wait_seconds=plan.wait_seconds,
                        reset_epoch=plan.reset_epoch, reset_source=plan.reset_source,
                        window=plan.window, wait_attempt=usage_waits + 1)
                    usage_waits += 1
                    continue
            # A content violation within budget: warm-resume the SAME producer session for a
            # bounded repair. A transport failure ("pure_transport") is NOT bundle-repairable —
            # it has no fixable document — so it is excluded here and routed fail_closed by
            # run_phase's transport branch (leaf_returncode != 0).
            can_repair = (category is not None and category != "pure_transport"
                          and attempt < MAX_BUNDLE_REPAIR_TURNS)
            if not can_repair:
                # Terminal: record bundle_meta with THIS (final) attempt's category/excerpt for the
                # outer route (_read_repair_findings). `category`/`this_excerpt` are used rather than
                # the `last_*` repair carriers: on a transport terminal the carriers were deliberately
                # NOT overwritten (the bookkeeping guard above), so `last_excerpt` may hold a prior
                # content failure's text — the meta must describe the transport death that actually
                # terminated the substep, not a stale carrier (and for a content exhaustion the two
                # are identical). Tombstone the superseded producer attempts so a later completion
                # vouch does not trip on the un-vouched arids (transport-dead waited attempts are
                # already tombstoned by their wait). The bundle_meta write is the LAST host action
                # here; it can still fail (ENOSPC, or a leaf-controlled failure_excerpt that is not
                # UTF-8 encodable), so guard it the same way the pass path guards its writes — a
                # host-write failure must recover as a fail_closed transport outcome, never escape
                # run_substep uncaught and crash the conductor. Mirrors the verify reviewer.
                try:
                    self._write_bundle_meta(
                        refs, result="fail", failure_category=category,
                        failure_excerpt=this_excerpt, attempts=len(per_attempt),
                        per_attempt=per_attempt)
                except Exception as exc:  # noqa: BLE001 — any host-write failure must recover
                    if attempt > 0:
                        self._add_superseded_run_ids(
                            [a["agent_run_id"] for a in per_attempt[:-1]],
                            reason=f"pure_host_write_failed_superseded: {type(exc).__name__}")
                    return SubstepOutcome(
                        child_arid, "fail", [], 1,
                        ("pure_host_write_failed", f"{type(exc).__name__}: {exc}"),
                        launched_at, len(per_attempt))
                if attempt > 0:
                    self._add_superseded_run_ids(
                        [a["agent_run_id"] for a in per_attempt[:-1]],
                        reason=f"pure_bundle_repair_superseded: {category}")
                return SubstepOutcome(child_arid, "fail", [], proc.returncode,
                                      infra_error, launched_at, len(per_attempt))
            # Set up the next (repair) turn: resume this attempt's session.
            resume_session_id = child_arid
            attempt += 1

    # --- Z2 pure-leaf verify reviewer (M-D) ------------------------------------
    # The pure `generate.verify` reviewer returns exactly one verify-verdict JSON document; the
    # host validates it (`verify_verdict_violations`), repairs a schema violation in a bounded
    # warm-resume loop, finalizes the attempt with an EMPTY output_refs row, and ONLY THEN authors
    # source_meta.json (the verdict projection) host-side. A schema-VALID verdict — pass OR fail —
    # is the reviewer's answer and terminates the loop; only a malformed verdict is repaired. This
    # mirrors the producer's loop; the differences are that the reviewer's terminal deliverable is
    # a verdict (not a bundle) and that a valid `fail` verdict is a legitimate substep FAIL routed
    # by the normal verify-severity gate, not a bundle/verdict category failure.

    def _build_pure_verify_context(self, refs: NodeRefs) -> dict[str, str]:
        """Assemble the closed context a pure `generate.verify` reviewer sees, each value a plain
        string the renderer data-fences. Unlike the producer, the reviewer DOES read
        controlled_spec.md (the human-authored behavioral contract it verifies against — phase_02
        allows it for verify but forbids it for generate) and the producer's `codegen_bundle.json`
        (the artifact under review), but NOT the host-rendered runner/Makefile glue (deterministic,
        not the reviewer's concern). All host-resolved from disk here (the leaf has no filesystem)."""
        def _read(rel: str) -> str:
            try:
                return (self.repo_root / rel).read_text(encoding="utf-8")
            except OSError:
                return ""
        return {
            "controlled_spec_document": _read(f"{refs.spec_path}/controlled_spec.md"),
            "tests_document": _read(f"{refs.spec_path}/tests.md"),
            "ir_document": _read(f"{refs.ir_ref}/spec.ir.yaml"),
            "bundle_document": _read(f"{refs.source_dir()}/codegen_bundle.json"),
        }

    def _write_verdict_meta(self, refs: NodeRefs, *, result: str,
                            failure_category: str | None, failure_excerpt: str | None,
                            attempts: int, per_attempt: list[dict[str, Any]]) -> None:
        """Author `<source_dir>/verdict_meta.json` — the pure reviewer's per-attempt record. It
        mirrors bundle_meta.json: the outcome (`result` = whether a schema-valid verdict was
        obtained, NOT the pass/fail of that verdict), the terminal `failure_category`/
        `failure_excerpt` on schema exhaustion (`classify_failure`'s verdict route reads the
        category), the attempt count, the prompt-contract version, and per-attempt model/usage from
        the CLI result envelope. source_meta.json cannot hold this (its schema is fixed by
        meta_contracts and carries no per-attempt usage), and the envelope usage is not recoverable
        from any other artifact — so this file is a justified (non-redundant) persistence, exactly
        as bundle_meta.json is for the producer."""
        from tools.pure_leaf import PURE_PROMPT_CONTRACT_VERSION
        meta: dict[str, Any] = {
            "result": result,
            "failure_category": failure_category,
            "attempts": attempts,
            "prompt_contract_version": PURE_PROMPT_CONTRACT_VERSION,
            "per_attempt": per_attempt,
        }
        if failure_excerpt:
            meta["failure_excerpt"] = failure_excerpt
        path = self.repo_root / refs.source_dir() / "verdict_meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _write_verify_source_meta(self, refs: NodeRefs, verdict: dict[str, Any], *,
                                  attempts: int) -> None:
        """Project a schema-valid verify verdict onto the canonical `source_meta.json` host-side,
        AFTER the reviewer's child window closes. The projection uses ONLY the existing stage-meta
        keys (meta_contracts) plus the legacy `issue_severity` the verify-severity gate keys on —
        no new schema. `last_fail_reason` carries the verdict's reason (null on pass), which
        `_read_repair_findings` threads into the producer repair on a `fail` route. Never called on
        a schema-exhausted attempt (proof-of-work: no valid verdict => no meta)."""
        src_dir = self.repo_root / refs.source_dir()
        src_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "source_id": refs.source_id,
            "node_key": refs.node_key,
            "attempt_count": attempts,
            "verification_status": verdict["verification_status"],
            "issue_severity": verdict["issue_severity"],
            "last_fail_reason": verdict["last_fail_reason"],
            "debug_mode": self.workflow_mode == "dev",
            "context_isolated": True,
        }
        (src_dir / "source_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _run_pure_verify_substep(self, refs: NodeRefs, phase: str, substep: str | None,
                                 resolved_dependencies: tuple[dict[str, str], ...]
                                 ) -> "SubstepOutcome":
        """Run `generate.verify` as a Z2 pure-function reviewer: spawn a tool-less `claude -p`
        reviewer that returns one verify verdict, validate it, repair a schema violation in a
        bounded warm-resume loop, finalize the accepted attempt with an EMPTY output_refs row, and
        ONLY THEN author source_meta.json host-side.

        PERSONA SEPARATION (operator hard rule): the reviewer always spawns a FRESH `--session-id`
        and the only session ever warm-resumed is the reviewer's OWN prior attempt (assigned
        `resume_session_id = child_arid` inside this loop). It never seeds from an external arid —
        in particular never from the producer session — so a resumed turn is structurally
        guaranteed to be a verify session, and the generate↔verify context is never shared. (On a
        cross-phase reopen the reviewer is dispatched with repair=None anyway — run_phase hands the
        phase repair to the producer at index 0 — so there is no external seed to accept.)

        The finalize-before-write ordering is load-bearing for the same reason as the producer:
        the pure capability's empty write_roots make any write inside the child window an
        unauthorized write, so the host closes the window (finalize_child) before it authors
        source_meta.json / verdict_meta.json."""
        from tools.pure_leaf import (
            parse_result_envelope, extract_json_document, verify_verdict_violations,
            MAX_BUNDLE_REPAIR_TURNS, RESPONSE_UNPARSEABLE, _MISSING)
        pure_context = self._build_pure_verify_context(refs)
        per_attempt: list[dict[str, Any]] = []
        resume_session_id: str | None = None
        prior_document: str | None = None
        last_excerpt: str | None = None
        attempt = 0
        usage_waits = 0
        while True:
            child_arid = self.new_agent_run_id()
            warm = (resume_session_id is not None
                    and self._claude_session_resumable(resume_session_id))
            repair_payload: dict[str, str] | None = None
            if resume_session_id is not None:
                repair_payload = {
                    "issue_severity": "major",
                    "repair_strategy": "reuse",
                    "repair_target_agent_run_id": resume_session_id,
                    "repair_reason": "pure_verdict_repair",
                }
                if last_excerpt:
                    repair_payload["repair_findings"] = last_excerpt
            request = build_launch_request(
                refs, step=phase, substep=substep,
                orchestration_id=self.orchestration_id,
                orchestration_agent_run_id=self.orchestration_agent_run_id,
                child_agent_run_id=child_arid,
                agent_model=self.agent_model, workflow_mode=self.workflow_mode,
                makefile_host_authored=True, runner_host_authored=True,
                repair=repair_payload,
                resolved_dependencies=resolved_dependencies,
                warm_resume=warm,
                pure_leaf=True,
                # Same context-omission rule as the producer: a warm reuse repair's resumed session
                # already holds the context (the validator exempts it); a cold launch or a
                # cold-fallback repair (session GC'd) carries the full context.
                pure_context=(None if (warm and repair_payload is not None
                                       and repair_payload.get("repair_findings"))
                              else pure_context),
            )
            if repair_payload is not None and not warm and prior_document:
                request["prior_document"] = prior_document
            rec = self.record_launch(child_arid, request)
            launched_at = time.time()
            proc = self.spawn_leaf(
                rec["launch_prompt_text"], self._child_env(child_arid),
                session_id=child_arid,
                resume_session_id=(resume_session_id if warm else None),
                child_arid=child_arid, pure=True)
            self._persist_leaf_output(child_arid, proc)
            token = self.read_parent_return_token(child_arid)

            envelope = parse_result_envelope(proc.stdout)
            model = None if envelope.model is _MISSING else envelope.model
            usage = None if envelope.usage is _MISSING else envelope.usage
            attempt_record: dict[str, Any] = {
                "agent_run_id": child_arid, "model": model, "usage": usage}
            per_attempt.append(attempt_record)

            infra_error: tuple[str, str] | None = None
            category: str | None = None
            findings: str | None = None
            parsed_verdict: dict[str, Any] | None = None
            accepted_verdict: dict[str, Any] | None = None
            if proc.returncode != 0:
                # A nonzero reviewer exit is a transport/infra failure (not a verdict the repair can
                # fix): finalize fail and let run_phase route it fail_closed.
                infra_error = _classify_leaf_infra_error(proc.stderr or "", proc.stdout or "")
                category = "pure_transport"
                findings = self._leaf_failure_summary(proc)
            elif not envelope.parsed or envelope.is_error is True:
                category = RESPONSE_UNPARSEABLE
                findings = ("the CLI result envelope was unparseable or reported is_error: "
                            + (str(envelope.parse_error or "")[:400] or "no result document"))
            else:
                extracted, extract_category = extract_json_document(envelope.result)
                if extract_category is not None:
                    category = extract_category
                    findings = ("the reply was not a single parseable JSON document "
                                f"({extract_category})")
                elif not isinstance(extracted, dict):
                    category = RESPONSE_UNPARSEABLE
                    findings = "the reply parsed to a non-object JSON value (expected a verdict)"
                else:
                    parsed_verdict = extracted
                    violations = verify_verdict_violations(extracted)
                    if not violations:
                        # A verdict can be schema-sound yet not persistable: `json.loads` accepts a
                        # lone surrogate (e.g. `"\ud800"` in last_fail_reason), but UTF-8-encoding it
                        # to author source_meta.json raises. Catch that HERE as a schema violation
                        # (repairable — the reviewer re-emits valid text) rather than accepting it and
                        # letting the later host-write raise, which would mis-route a schema-valid
                        # `fail` through the host-write-failed transport branch instead of the
                        # severity gate. The contract is that every schema-valid verdict reaches its
                        # routing; an unencodable one is not schema-valid.
                        try:
                            json.dumps(extracted, ensure_ascii=False).encode("utf-8")
                        except UnicodeEncodeError:
                            violations = [
                                "verdict contains characters that cannot be encoded as UTF-8 "
                                "(e.g. an unpaired surrogate); re-emit the verdict with valid text"]
                    if violations:
                        category = GENERATE_VERDICT_SCHEMA_VIOLATION
                        findings = "; ".join(violations)[:1000]
                    else:
                        accepted_verdict = extracted

            # A schema-valid verdict terminates the loop regardless of pass/fail; the SUBSTEP status
            # is the verdict's own verification_status (a `fail` verdict is a legitimate verify
            # rejection, not a transport/schema error). A malformed verdict has category set and is
            # repaired below within budget.
            if accepted_verdict is not None:
                verify_status = accepted_verdict["verification_status"]
                reply = (f"verify verdict: {verify_status}\nleaf rc={proc.returncode}\n"
                         f"severity: {accepted_verdict['issue_severity']}")
                # Non-None on BOTH outcomes: the pure row's output_refs is empty, so this is the
                # only field that can satisfy `_validate_agent_summary_text`'s "a terminal row with
                # no output_refs must explain itself" rule. See the producer's mirror above.
                result_summary = (
                    f"pure_verify_pass: verdict {verify_status} "
                    f"(severity={accepted_verdict['issue_severity']}, "
                    f"attempts={len(per_attempt)})"[:400]
                    if verify_status == "pass"
                    else f"pure_verify_fail: {accepted_verdict['last_fail_reason']}"[:400]
                )
                # Finalize FIRST (close the child FS-diff window); the pure row carries EMPTY
                # output_refs. ONLY AFTER this may the host author source_meta.json / verdict_meta.
                self.finalize_child(
                    child_arid, token, reply,
                    self._agent_run_json(refs, phase, substep, child_arid, verify_status,
                                         [], result_summary, agent_model_override=model, pure=True))
                try:
                    self._write_verify_source_meta(
                        refs, accepted_verdict, attempts=len(per_attempt))
                    self._write_verdict_meta(
                        refs, result="pass", failure_category=None, failure_excerpt=None,
                        attempts=len(per_attempt), per_attempt=per_attempt)
                except Exception as exc:  # noqa: BLE001 — any host-write failure must recover
                    # A host-side write AFTER the window closed failed (ENOSPC, IO error). The
                    # attempt is already a terminal `substep` row, so without recovery it is an
                    # un-vouched orphan. Mirror the producer's pure_host_write_failed path: tombstone
                    # earlier attempts and route fail_closed via a non-zero leaf_returncode so
                    # run_phase's transport branch tombstones this arid too and the operator resumes
                    # (rather than auto-retrying a write a full disk would only repeat).
                    if attempt > 0:
                        self._add_superseded_run_ids(
                            [a["agent_run_id"] for a in per_attempt[:-1]],
                            reason=f"pure_verify_host_write_failed_superseded: {type(exc).__name__}")
                    return SubstepOutcome(child_arid, "fail", [], 1,
                                          ("pure_verify_host_write_failed", f"{type(exc).__name__}: {exc}"),
                                          launched_at, len(per_attempt))
                # Tombstone superseded reviewer attempts of a repaired verdict (each earlier attempt
                # was finalized as a terminal `substep` row, but only THIS arid is vouched).
                if attempt > 0:
                    self._add_superseded_run_ids(
                        [a["agent_run_id"] for a in per_attempt[:-1]],
                        reason=f"pure_verdict_repair_superseded: verify_status={verify_status}")
                return SubstepOutcome(child_arid, verify_status, [], proc.returncode,
                                      None, launched_at, len(per_attempt))

            # A malformed / transport reply. Record the terminal record fields and carry the prior
            # document into a cold-fallback repair (re-serialize the parsed verdict if any, else the
            # raw reply text). A transport death ("pure_transport") has NO fixable verdict, so it
            # must not overwrite the repair carriers (`prior_document` / `last_excerpt`) — mirrors
            # the producer, so a repair turn that follows a --wait-usage-reset wait carries the prior
            # SCHEMA failure's findings, not the transport summary.
            if category != "pure_transport":
                prior_document = (json.dumps(parsed_verdict, indent=2, ensure_ascii=False)
                                  if parsed_verdict is not None
                                  else (envelope.result if isinstance(envelope.result, str)
                                        else None))
                # Both `last_excerpt` (-> the repair turn's `repair_findings` AND verdict_meta's
                # failure_excerpt) and `prior_document` (-> the cold-fallback repair prompt) are
                # echoed into a request/meta that is persisted as UTF-8. Leaf-derived diagnostic text
                # can carry an unpaired surrogate (the `verdict_schema_violation` case the UTF-8
                # check above catches), which would raise UnicodeEncodeError at that write — a crash
                # instead of a repair turn / a fail_closed. Normalize any non-encodable code point to
                # its readable backslash escape at capture so every downstream write is safe by
                # construction.
                last_excerpt = (findings.encode("utf-8", "backslashreplace").decode("utf-8")
                                if findings is not None else None)
                if prior_document is not None:
                    prior_document = prior_document.encode(
                        "utf-8", "backslashreplace").decode("utf-8")
            # Record why THIS reviewer attempt failed on its own per_attempt row (observability only —
            # ADDITIVE fields; mirrors the producer). A schema-VALID verdict returned above already,
            # so this branch is only a malformed/transport reply, never a legitimate `fail` verdict.
            # The excerpt is computed LOCALLY so a transport row is still labeled without touching the
            # repair carriers above.
            this_excerpt = (findings.encode("utf-8", "backslashreplace").decode("utf-8")
                            if findings is not None else None)
            attempt_record["failure_category"] = category
            attempt_record["failure_excerpt"] = (
                this_excerpt[:_PURE_ATTEMPT_EXCERPT_MAX_CHARS] if this_excerpt else None)
            self.emit("pure_verdict_attempt_failed", node_key=refs.node_key,
                      substep=substep, attempt=len(per_attempt), failure_category=category,
                      detail=(this_excerpt or "")[:200])
            reply = (f"verify verdict: none\nleaf rc={proc.returncode}\n"
                     f"category: {category or 'none'}")
            result_summary = f"pure_verify_fail: {category}"
            self.finalize_child(
                child_arid, token, reply,
                self._agent_run_json(refs, phase, substep, child_arid, "fail",
                                     [], result_summary, agent_model_override=model, pure=True))

            # --wait-usage-reset (opt-in): a transport death carrying a resolvable usage-limit
            # reset (in practice the CLI's TZ-anchored human form) is waited out in place and the
            # SAME turn re-launched, rather than falling
            # to the terminal fail branch. `attempt` / `resume_session_id` are UNCHANGED (a wait is
            # not a repair turn; persona separation is preserved — the reviewer only ever resumes its
            # OWN prior attempt). The dead arid was finalized above, so the tombstone is outside its
            # write window. Mirrors the producer loop — including the explicit `llm_usage_limit` tag
            # guard (a `pure_transport` category is set for ANY nonzero exit; only a usage limit is
            # waited, matching run_substep).
            if (category == "pure_transport" and infra_error is not None
                    and infra_error[0] == "llm_usage_limit"):
                plan = self._usage_reset_wait_plan(
                    proc, usage_waits, node_key=refs.node_key, step=phase, substep=substep,
                    dead_agent_run_id=child_arid, evidence=infra_error[1],
                    allow_envelope=True)   # pure leaves ARE `--output-format json`
                if plan is not None:
                    self._wait_for_usage_reset(
                        node_key=refs.node_key, step=phase, substep=substep,
                        dead_agent_run_id=child_arid, wait_seconds=plan.wait_seconds,
                        reset_epoch=plan.reset_epoch, reset_source=plan.reset_source,
                        window=plan.window, wait_attempt=usage_waits + 1)
                    usage_waits += 1
                    continue
            # A schema violation within budget: warm-resume the SAME reviewer session for a bounded
            # repair. A transport failure ("pure_transport") has no fixable verdict, so it is
            # excluded and routed fail_closed by run_phase's transport branch (leaf_returncode != 0).
            can_repair = (category is not None and category != "pure_transport"
                          and attempt < MAX_BUNDLE_REPAIR_TURNS)
            if not can_repair:
                # Terminal: record verdict_meta with THIS (final) attempt's category/excerpt for the
                # outer route (classify_failure's verdict table). `category`/`this_excerpt` are used
                # rather than the `last_*` repair carriers: on a transport terminal the carriers were
                # deliberately NOT overwritten (the bookkeeping guard above), so `last_excerpt` may
                # hold a prior schema failure's text — the meta must describe the transport death that
                # terminated the substep (and for a schema exhaustion the two are identical).
                # source_meta.json is intentionally NOT written (proof-of-work: no schema-valid
                # verdict this attempt). Tombstone superseded arids (transport-dead waited attempts
                # are already tombstoned by their wait). The verdict_meta write is the LAST host
                # action here; it can still fail (ENOSPC, or a leaf-controlled failure_excerpt that is
                # not UTF-8 encodable), so guard it the same way the accepted-verdict path guards its
                # writes — a host-write failure must recover as a fail_closed transport outcome, never
                # escape run_substep uncaught.
                try:
                    self._write_verdict_meta(
                        refs, result="fail", failure_category=category,
                        failure_excerpt=this_excerpt, attempts=len(per_attempt),
                        per_attempt=per_attempt)
                except Exception as exc:  # noqa: BLE001 — any host-write failure must recover
                    if attempt > 0:
                        self._add_superseded_run_ids(
                            [a["agent_run_id"] for a in per_attempt[:-1]],
                            reason=f"pure_verify_host_write_failed_superseded: {type(exc).__name__}")
                    return SubstepOutcome(
                        child_arid, "fail", [], 1,
                        ("pure_verify_host_write_failed", f"{type(exc).__name__}: {exc}"),
                        launched_at, len(per_attempt))
                if attempt > 0:
                    self._add_superseded_run_ids(
                        [a["agent_run_id"] for a in per_attempt[:-1]],
                        reason=f"pure_verdict_repair_superseded: {category}")
                return SubstepOutcome(child_arid, "fail", [], proc.returncode,
                                      infra_error, launched_at, len(per_attempt))
            # Set up the next (repair) turn: resume this attempt's OWN reviewer session (persona
            # separation — never an external/producer arid).
            resume_session_id = child_arid
            attempt += 1

    def write_step_result(self, node_key: str, step: str, executor_arid: str,
                          result: dict[str, Any]) -> dict[str, Any]:
        return self.runtime([
            "write-step-result", *self._oid_args(),
            "--node-key", node_key, "--step", step,
            "--agent-run-id", executor_arid,
            "--result-json", json.dumps(result),
        ])

    def check_step_completed(self, node_key: str, step: str) -> dict[str, Any] | None:
        out = self.runtime([
            "check-step-completed", *self._oid_args(),
            "--node-key", node_key, "--step", step,
        ])
        return out if isinstance(out, dict) and out.get("integrity") == "ok" else None

    def workflow_launch_check(self, node_key: str, step: str, require_child_agent: str) -> dict[str, Any]:
        out = self.runtime([
            "workflow-launch-check", *self._oid_args(),
            "--node-key", node_key, "--step", step,
            "--require-child-agent", require_child_agent,
            "--backend", self.backend,
        ])
        if out.get("status") != "pass":
            raise RuntimeError(
                f"workflow-launch-check blocked {step}: {out.get('reason_code')} {out.get('reason_detail')}")
        return out

    def reserve_root(self, node_key: str, step: str, reserved_id: str, by_arid: str) -> dict[str, Any]:
        return self.runtime([
            "reserve-phase-root", *self._oid_args(),
            "--node-key", node_key, "--step", step,
            "--reserved-id", reserved_id,
            "--reserved-by-agent-run-id", by_arid,
        ])

    def set_status(self, status: str, reason_code: str | None = None,
                   reason_detail: str | None = None) -> dict[str, Any]:
        args = ["set-status", *self._oid_args(), "--status", status]
        if reason_code:
            args += ["--reason-code", reason_code]
        if reason_detail:
            args += ["--reason-detail", reason_detail]
        return self.runtime(args)

    def reopen_phase(self, node_key: str, from_phase: str, trigger_arid: str,
                     reason: str) -> dict[str, Any]:
        return self.runtime([
            "reopen-phase", *self._oid_args(),
            "--node-key", node_key, "--from-phase", from_phase,
            "--trigger-agent-run-id", trigger_arid, "--reason", reason,
        ])

    def _add_superseded_run_ids(self, run_ids: list[str], reason: str) -> dict[str, Any]:
        """Tombstone substep arids of a phase attempt that fail-closed on a leaf transport
        error (it wrote no step_result), so a later --resume can reach pass: the orphaned
        terminalized substeps are exempted from the completion vouch (see runtime
        add_superseded_run_ids). No-op caller-side when run_ids is empty."""
        return self.runtime([
            "add-superseded-runs", *self._oid_args(),
            "--reason", reason, "--run-ids", *run_ids,
        ])

    # -- substep outcome (deterministic, reads canonical artifacts) -----------

    def _child_env(self, child_arid: str) -> dict[str, str]:
        env = dict(self.env)
        env["METDSL_ORCHESTRATION_ID"] = self.orchestration_id
        env["TMPDIR"] = str(self.repo_root / "workspace" / "tmp" / child_arid)
        # Lift the claude leaf's output ceiling off the CLI default (see LEAF_MAX_OUTPUT_TOKENS:
        # thinking is billed against it, so the default truncates a hard leaf mid-think). Set
        # here — not in `.claude/settings.json` — so it stays a property of the CONDUCTOR'S leaf
        # contract and does not leak into the operator's own interactive sessions. bwrap passes
        # the environment through (no --clearenv), so this reaches the leaf's `claude` process.
        # codex reads a different config surface and is left alone.
        if self.backend == "claude":
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(LEAF_MAX_OUTPUT_TOKENS)
        return env

    def read_case_ids(self, refs: NodeRefs) -> tuple[str, ...]:
        """Per-case ids from the compiled IR (for validate.execute output paths)."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml")
        if ir is None:
            return ()
        case = ir.get("case") if isinstance(ir, dict) else None
        tcs = case.get("test_case_set") if isinstance(case, dict) else None
        if not isinstance(tcs, list):
            return ()
        # strip() so the case_id identity is the SAME across the runner argv
        # (--cases), the expected raw/state_snapshots/<case_id>.json deliverable
        # path, and the validator's _case_ids_for_execution exemption set.
        #
        # This case_id becomes a FILESYSTEM PATH — the runner writes
        # raw/state_snapshots/<case_id>.json — so a `/` or `..` would let the run write outside
        # its directory. The Compile gate `_validate_case_ids` rejects such an id for every node,
        # but this is the shared runtime boundary (M3c and non-M3c alike), so drop any unsafe
        # token here too: never put a traversal string on the argv, even from a hand-crafted IR
        # that bypassed Compile. A dropped case is simply absent from the run (and from the
        # deliverable set this same list feeds); if a predicate still references it, the
        # metrics-basis matrix reports the missing (test_id, case_id). Either way the run stays
        # in-directory — the dropped case never produces an out-of-bounds write.
        from tools.runner_renderer import _CASE_ID_TOKEN_RE
        return tuple(sorted(
            tok for c in tcs
            if isinstance(c, dict) and isinstance(c.get("case_id"), str)
            and (tok := c["case_id"].strip())
            and _CASE_ID_TOKEN_RE.match(tok) and ".." not in tok
        ))

    def _read_evidence_artifacts(self, refs: NodeRefs) -> tuple[str, ...]:
        """IR-declared required raw-evidence artifact types for validate.execute
        allowed_output_paths. Returns the IR's actual artifacts with NO fallback so the
        deliverable set stays identical to what `_promote_run_evidence` /
        `_author_snapshot_schema` (which read the same `_required_evidence_artifacts`)
        produce — a fallback here would require evidence the promoter never creates
        (fail-closed) and violate phase_04 §44 for IRs that declare no state_snapshots."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        return tuple(self._required_evidence_artifacts(ir))

    def _resolve_exemplar(self, refs: NodeRefs) -> dict[str, Any] | None:
        """R5: host-resolve a certified sibling exemplar (model+runner source) for the
        generate.generate leaf, or None. Best-effort — the selector never raises, and any
        failure simply omits the exemplar (Generate proceeds without it)."""
        from tools.orchestration_runtime import _resolve_exemplar_source
        try:
            return _resolve_exemplar_source(self.repo_root, refs.ir_ref)
        except Exception:
            return None

    def _stage_meta_contract_findings(self, refs: NodeRefs, phase: str) -> list[str]:
        """Contract findings against the phase's verify meta (compile -> ir_meta.json,
        generate -> source_meta.json): missing required keys + value-type violations, from the
        one canonical definition (tools/meta_contracts) the runtime write gate and the
        validator sweeps also use.

        This is the WRITE-POINT detector for a defect class that is otherwise UNREPAIRABLE:
        the runtime's `_validate_step_meta_payload` only runs for a PASS step_result, so a
        FAILED verify leaf could persist a schema-violating meta (e.g. `last_fail_reason` as a
        structured incident dict) unchecked — and since a Generate reopen rotates a fresh
        source dir and deletes nothing, the violation is immutable and no repair loop
        converges on it (E2E #4). Detecting it while the authoring leaf's session is still
        resumable is what makes it fixable at all.

        Stateless by construction: the meta is re-read and re-checked on demand, so no new
        persisted artifact is introduced and the findings cannot go stale. A missing or
        non-dict meta returns [] — that is an ordinary verify failure (or a leaf that wrote
        nothing), handled by the normal severity/freshness gates, not this class.
        """
        from tools.meta_contracts import (
            STAGE_META_FILENAME_BY_STEP,
            missing_required_meta_keys,
            stage_meta_type_violations,
        )

        if phase not in STAGE_META_FILENAME_BY_STEP:
            return []
        meta_filename = STAGE_META_FILENAME_BY_STEP[phase]
        # Same meta-path synthesis as classify_failure / _read_repair_findings.
        meta_dir = refs.ir_ref if phase == "compile" else refs.source_dir()
        meta = _read_json(self.repo_root / meta_dir / meta_filename)
        if not isinstance(meta, dict):
            return []
        findings = [
            f"{meta_filename} missing required key {k!r}"
            for k in missing_required_meta_keys(meta, step_token=phase)
        ]
        findings += [
            f"{meta_filename} {clause}"
            for clause in stage_meta_type_violations(meta, step_token=phase)
        ]
        return findings

    def _stage_meta_path(self, refs: NodeRefs, phase: str) -> Path | None:
        """Absolute path of the phase's verify meta, or None for a phase that has none."""
        from tools.meta_contracts import STAGE_META_FILENAME_BY_STEP

        if phase not in STAGE_META_FILENAME_BY_STEP:
            return None
        meta_dir = refs.ir_ref if phase == "compile" else refs.source_dir()
        return self.repo_root / meta_dir / STAGE_META_FILENAME_BY_STEP[phase]

    def _stage_meta_authored_since(self, refs: NodeRefs, phase: str, min_mtime: float) -> bool:
        """True if the phase's verify meta was (re)written at/after `min_mtime` — i.e. by the
        substep launched then. Same mtime test the freshness clause of determine_substep_status
        uses, so "the leaf wrote it" means the same thing in both places."""
        path = self._stage_meta_path(refs, phase)
        if path is None:
            return False
        try:
            return path.stat().st_mtime >= min_mtime
        except OSError:
            return False

    def _verify_session_resumable(self, verify_arid: str) -> bool:
        """True if the failed verify leaf's session can actually be warm-resumed. Mirrors the
        preconditions `_resolve_reuse_resume` applies at launch (claude backend + a surviving
        session transcript), consulted BEFORE the repair turn so the loop never spawns a cold
        leaf that cannot see its findings."""
        return self.backend == "claude" and self._claude_session_resumable(verify_arid)

    def determine_substep_status(self, refs: NodeRefs, phase: str, substep: str | None,
                                 allowed_output_paths: list[str],
                                 min_mtime: float = 0.0) -> tuple[str, list[str]]:
        """Deterministically classify a substep from the artifacts it produced.

        verify/judge read the canonical status field; the producing substeps
        (generate/execute/build) pass when their deliverables exist AND were
        (re)written during this attempt (mtime >= min_mtime), so a retry/reopen
        that reuses an artifact directory cannot pass on a prior attempt's stale
        outputs. The downstream verify/judge then certifies the content.
        """
        output_refs = [p for p in allowed_output_paths if (self.repo_root / p).exists()]

        def _fresh_deliverables_written(paths: list[str]) -> bool:
            # All DELIVERABLE outputs (excluding the audit/process logs whose placement
            # varies by build system) exist AND were authored in this attempt
            # (mtime >= min_mtime), so a retry/reopen never passes on stale files.
            required = [p for p in paths if Path(p).name not in _OPTIONAL_OUTPUT_BASENAMES]
            present = [p for p in required if (self.repo_root / p).exists()]
            if len(present) != len(required):
                return False
            return all((self.repo_root / p).stat().st_mtime >= min_mtime for p in present)

        if (phase == "generate" and substep == "generate"
                and self._pure_leaf_substep(refs, phase, substep)):
            # Z2 pure producer freshness (M-C 修正1): the pure `generate.generate` has NO
            # leaf-authored deliverables (allowed_output_paths == []); its freshness-gated
            # outputs are the HOST-written bundle_meta.json (result==pass) and codegen_bundle.json,
            # both authored AFTER the child window closes. A stale-artifact reuse on a retry is
            # prevented by the mtime guard on codegen_bundle.json (the source dir is rotated per
            # attempt anyway). This branch is defensive — the pure substep computes its own status
            # from validate_bundle — but keeps the freshness contract stated in one place.
            bmeta = _read_json(self.repo_root / refs.source_dir() / "bundle_meta.json") or {}
            bundle = self.repo_root / refs.source_dir() / "codegen_bundle.json"
            fresh = bundle.exists() and bundle.stat().st_mtime >= min_mtime
            status = "pass" if (bmeta.get("result") == "pass" and fresh) else "fail"
            return status, output_refs
        if phase == "compile" and substep == "static":
            # Deterministic compile gate: the conductor-authored compile_static_meta records the
            # workspace_root + check_artifact_syntax + --stage compile verdict. A violation is
            # status=fail with rc 0, so the substep fails here and classify_compile_static_failure
            # routes back to compile.generate (warm resume), not transport fail_closed.
            # compile_static_meta.json is the only freshness-gated deliverable.
            meta = _read_json(self.repo_root / refs.ir_ref / "compile_static_meta.json") or {}
            status = "pass" if (meta.get("status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        elif phase == "compile" and substep == "verify":
            # The pure-semantic verify leaf's SOLE deliverable is ir_meta.json; it must
            # RE-AUTHOR it this attempt (verification_status + a refreshed idempotent field) to
            # pass — an inspect-only verify that writes nothing cannot terminate pass (the SKILL
            # contract). The freshness gate is load-bearing here: Compile.generate authors
            # ir_meta.json and may leave verification_status=pass (the --stage compile gate only
            # requires a non-empty string), and the gate that used to force verify to do work
            # (its own end-of-substep --stage compile) moved to Compile.static, so without this
            # a no-op verify (exit 0, no rewrite) would pass on generate's stale status.
            # allowed_output_paths == [ir_meta.json], so _fresh_deliverables_written checks
            # exactly that file's mtime against this substep's launch time.
            # ...and it must satisfy the stage-meta contract: a verify that certifies its own
            # phase with a schema-violating meta would persist an unrepairable artifact (the
            # write gate only checks PASS step_results, so nothing else catches it here).
            meta = _read_json(self.repo_root / refs.ir_ref / "ir_meta.json") or {}
            status = "pass" if (meta.get("verification_status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)
                                and not self._stage_meta_contract_findings(refs, phase)) else "fail"
        elif phase == "generate" and substep == "verify":
            # Same freshness requirement as compile.verify: post-G1 generate.verify is a pure
            # semantic pass whose meta deliverable is source_meta.json, and it must RE-AUTHOR it
            # this attempt to pass (an inspect-only verify that writes nothing cannot terminate
            # pass). Without the gate a no-op verify (exit 0, no rewrite) would pass on a stale
            # verification_status=pass that generate.generate left. The gate is scoped to
            # source_meta.json ONLY — generate.verify's allowed_output_paths also lists the
            # producer sources (model/runner.f90) it does NOT rewrite, so checking the whole set
            # would false-fail a verify that legitimately only re-authors source_meta.json.
            # The stage-meta contract is enforced here too (see the compile.verify note above):
            # a pass-status meta whose last_fail_reason is a dict / whose keys are missing
            # cannot certify the phase.
            src_meta = f"{refs.source_dir()}/source_meta.json"
            meta = _read_json(self.repo_root / src_meta) or {}
            status = "pass" if (meta.get("verification_status") == "pass"
                                and _fresh_deliverables_written([src_meta])
                                and not self._stage_meta_contract_findings(refs, phase)) else "fail"
        elif phase == "validate" and substep == "pre_judge":
            # Deterministic pre-spawn DAG readiness: the conductor-authored pre_judge_meta
            # records whether every --with-deps closure node is built+validated in its own
            # pipeline. A not-ready closure is status=fail with rc 0, so the substep fails
            # here and classify_failure routes it to fail_closed (integrity blocker).
            meta = _read_json(self.repo_root / refs.run_node_dir() / "pre_judge_meta.json") or {}
            status = "pass" if (meta.get("status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        elif phase == "validate" and substep == "judge":
            # R2 judge: a PURE LLM semantic pass authoring ONLY semantic_review.json. The
            # per-test verdict (verdict.json) is now deterministically host-authored at execute
            # from the IR predicates, and a physics/contract fail there fails the execute
            # substep before the judge is ever spawned. So when the judge runs, verdict is
            # already ∈ {pass, xfail}; the judge passes iff its own semantic finding agrees the
            # node is clean: semantic_review.decision == "pass". A decision=="fail" (a
            # fabrication / consistency finding on otherwise-passing tests) breaks run_phase
            # before post_judge; classify_failure then routes it (via the diagnostician).
            #
            # The same freshness requirement as every other LLM substep, and load-bearing for the
            # SAME reason as compile.verify's: a judge that writes nothing this window must not
            # pass on an artifact from an earlier attempt. The run dir is NOT rotated between
            # attempts of one phase (`_ensure_fresh_producer_id` runs once per phase), so without
            # this a judge leaf that authored `decision: "pass"` and THEN died on a transient
            # transport fault would be tombstoned, retried, and the retry — finding the dead
            # attempt's file already in place and rewriting nothing — would certify the node on an
            # artifact authored by a leaf that never completed, vouched to an arid that never
            # wrote it. semantic_review.json is the judge's ONLY allowed output path, so gating on
            # the whole set is exact.
            sem = _read_json(self.repo_root / refs.run_node_dir() / "semantic_review.json") or {}
            status = "pass" if (str(sem.get("decision") or "").strip().lower() == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        elif phase == "validate" and substep == "post_judge":
            # Deterministic post-return gate: the conductor-authored post_judge_meta records
            # the `--stage pre_judge` verdict (orchestration-record + cross-pipeline DAG
            # integrity). A violation is status=fail with rc 0; run_phase reads its
            # `disposition` to decide warm-resume-judge vs fail_closed. This is where the old
            # judge-gate AND now lives (a certified-pass node must clear this gate).
            meta = _read_json(self.repo_root / refs.run_node_dir() / "post_judge_meta.json") or {}
            status = "pass" if (meta.get("status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        elif phase == "build":
            # Deterministic build: the conductor-authored binary_meta records the compile
            # + post_build-gate verdict. A content failure (compile/link error, post_build
            # violation) is verification_status=fail with rc 0, so the substep fails here
            # and classify_build_failure routes it to Generate (not transport fail_closed).
            meta = _read_json(self.repo_root / refs.binary_dir() / "binary_meta.json") or {}
            status = "pass" if (meta.get("verification_status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        elif phase == "generate" and substep == "gate":
            # Deterministic union gate: the conductor-authored gate_meta records the unioned
            # lint / syntax / static verdict under a single gate_status. Any checker finding is
            # gate_status=fail with rc 0, so the substep fails here and classify_gate_failure
            # routes back to generate.generate (warm resume), not transport fail_closed.
            # gate_meta.json is the only freshness-gated deliverable (command_log.jsonl is an
            # optional basename). A syntax attribution RuntimeError writes NO gate_meta and
            # returns rc 1 upstream (transport fail_closed), so gate_meta is absent -> fail here
            # too, but the rc 1 path already terminalized before reaching this read.
            meta = _read_json(self.repo_root / refs.source_dir() / "gate_meta.json") or {}
            status = "pass" if (meta.get("gate_status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        elif phase == "validate" and substep == "execute":
            # Deterministic execute: trial_meta.status reflects run_program +
            # quality_check + post_execute gate; content failures (rc 0) route via the
            # validate tables / diagnostician. A run_program runtime error writes no
            # trial_meta, so the missing-status read fails the substep here too.
            meta = _read_json(self.repo_root / refs.run_node_dir() / "trial_meta.json") or {}
            status = "pass" if (meta.get("status") == "pass"
                                and _fresh_deliverables_written(allowed_output_paths)) else "fail"
        else:
            # remaining producing substeps (compile.generate / generate.generate): pass
            # only when ALL DELIVERABLE outputs were written this attempt (mtime guard);
            # the audit/process logs (optional basenames) are excluded. The downstream
            # verify certifies the content.
            status = "pass" if _fresh_deliverables_written(allowed_output_paths) else "fail"
        return status, output_refs

    def _judge_semantic_decision(self, refs: NodeRefs) -> str:
        """Normalized `semantic_review.json#decision` for the judge substep (lower-cased,
        stripped; `""` when the file or field is absent). Used by run_phase to decide whether a
        failed judge substep can safely write a `fail` step_result: the pre_phase_complete hook
        forbids a `fail` step_result unless the decision is present and `"fail"`, so a `pass`
        (or missing/empty) decision must skip the write and route as a conformance violation
        rather than crash the runtime write-step-result."""
        sem = _read_json(self.repo_root / refs.run_node_dir() / "semantic_review.json") or {}
        return str(sem.get("decision") or "").strip().lower()

    def _agent_run_json(self, refs: NodeRefs, phase: str, substep: str | None,
                        child_arid: str, status: str,
                        output_refs: list[str],
                        result_summary: str | None = None,
                        agent_model_override: str | None = None,
                        pure: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_run_id": child_arid,
            "agent_role": child_agent_role(phase),
            "agent_backend": self.backend,
            "status": status,
            "started_at": _iso_now(),
            "finished_at": _iso_now(),
            "agent_session_id": child_arid,
            "context_id": child_arid,
            "context_isolated": True,
            "node_key": refs.node_key,
            # record_agent_run does NOT infer step/substep from the launch request
            # (it backfills only parent_agent_run_id / agent_model); the pre_judge
            # substep-linkage check needs them, so supply them here.
            "step": phase,
        }
        if substep is not None:
            payload["substep"] = substep
        # Record the EXACT model the leaf actually ran, resolved from its own session
        # transcript (the leaf's session id == child_arid, pinned via --session-id at
        # launch). This is the runtime-resolved ground truth that replaces the unpinned
        # alias carried in the launch request — record_agent_run only setdefaults the
        # alias, so a value set here wins. Claude only; a codex leaf's transcript lives
        # outside ~/.claude, so it keeps the launch-request alias. If unresolvable
        # (no transcript yet, or a leaf that crashed before any assistant message),
        # we leave agent_model absent and let record_agent_run backfill the alias.
        if agent_model_override and str(agent_model_override).strip():
            # Z2 pure leaf: the model the leaf ran under comes from the CLI result envelope
            # (`--output-format json`), NOT the session transcript (~/.claude is not read on the
            # pure path). A resolved override wins and skips the transcript lookup.
            payload["agent_model"] = str(agent_model_override).strip()
        elif pure:
            # Pure path with no envelope model (a provenance gap): leave agent_model ABSENT for
            # record_agent_run to backfill the launch-request alias. Crucially, do NOT fall back
            # to the transcript resolver — the pure channel must not read ~/.claude (operator
            # access boundary); the envelope is the only provenance source.
            pass
        elif self.backend == "claude":
            from tools.orchestration_runtime import resolve_claude_model_from_transcript
            resolved = resolve_claude_model_from_transcript(child_arid)
            if resolved:
                payload["agent_model"] = resolved
        if status == "pass":
            payload["output_refs"] = output_refs
        # `_validate_agent_summary_text` requires a summary/reason token on any terminal row
        # that publishes no output_refs — such a row has nothing else to speak for it. Two
        # kinds qualify, and the condition must name both:
        #   - a FAIL row (the original case): it sets no output_refs at all.
        #   - since Z2, a pure leaf's PASS row: its output_refs is EMPTY by contract (the host
        #     writes the deliverables only after the child window closes).
        # Keying this on `status != "pass"` alone — the former `elif` — silently dropped the
        # summary from every passing pure leaf, so finalize-child rejected it and the pure
        # executor could never complete a real run (found by the first billed E2E).
        if (status != "pass" or not output_refs) and result_summary and result_summary.strip():
            payload["result_summary"] = result_summary.strip()
        return payload

    # -- deterministic (non-LLM) substep execution ----------------------------
    # Build and Validate.execute are contractually non-LLM (deterministic compile /
    # run), so the conductor ALWAYS runs their body IN-PROCESS (no `claude -p` leaf) by
    # calling the build-runtime MCP tool handlers directly. Validate.judge stays an LLM
    # leaf (its independent semantic check is essential).

    @staticmethod
    def _is_deterministic_substep(phase: str, substep: str | None) -> bool:
        return (phase == "build"
                or (phase == "validate" and substep in ("pre_judge", "execute", "post_judge"))
                or (phase == "generate" and substep == "gate")
                or (phase == "compile" and substep == "static"))

    def _capability_token(self, child_arid: str) -> str:
        path = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                / "capabilities" / f"{child_arid}.json")
        cap = _read_json(path) or {}
        token = str(cap.get("capability_token", "")).strip()
        if not token:
            raise RuntimeError(f"deterministic step: missing capability_token at {path}")
        return token

    def _resolve_exe_name(self, refs: NodeRefs) -> str:
        """The canonical execution binary basename: `<spec_id>_runner`.

        Build and Validate.execute IMPOSE this name on the Makefile (Build via the make
        command line, Validate.execute via the make_test environment — which requires the
        Makefile's `BIN ?=` overridable form, enforced by post_generate). The binary name
        is thus deterministic and consistent with the runner source/program names, instead
        of varying with whatever default `BIN` the generator chose."""
        return f"{refs.spec_id}_runner"

    @staticmethod
    def _require_make_build_system(build_system: str, phase: str) -> None:
        """The in-process deterministic bodies hard-code the in-source Make layout
        (OBJDIR/BINDIR/RUNDIR overrides, make_test preset, binary under binary/<id>/bin,
        Make command-log placement). Non-Make toolchains (cmake/meson/ninja) would be
        silently misplaced, so fail loudly until in-process support is implemented for
        them. All current specs are build_system=make."""
        if str(build_system).strip().lower() != "make":
            raise RuntimeError(
                f"deterministic in-process {phase} supports build_system=make only "
                f"(got {build_system!r}); non-Make toolchains are not implemented for the "
                f"in-process path")

    @staticmethod
    def _classify_build_failure_category(return_code: int, stderr: str) -> str:
        """Mechanical classification per phase_03_build.md (no LLM)."""
        s = (stderr or "").lower()
        if "no rule to make target" in s:
            return "make_error"
        if "undefined reference" in s or "unresolved external" in s:
            return "link_error"
        return "compile_error"

    @staticmethod
    def _extract_failure_source_refs(stderr: str, src_ref: str) -> list[str]:
        """Source paths the compiler/linker named in its error output, rebased under
        the canonical `<src_ref>` so Generate can target only the offending files
        (phase_03 retry trigger). Best-effort: empty when nothing parseable."""
        names: set[str] = set()
        for m in re.finditer(r"([\w./-]+\.(?:f90|f95|f|c|cc|cxx|cpp|h|hpp))",
                             stderr or "", re.IGNORECASE):
            names.add(Path(m.group(1)).name)
        return sorted(f"{src_ref}/{n}" for n in names)

    def _run_deterministic_substep(self, refs: NodeRefs, phase: str, substep: str | None,
                                   child_arid: str, request: dict[str, Any]) -> ProcResult:
        """Run a non-LLM substep body in-process and return a ProcResult shaped like a
        leaf's (returncode 0 == clean conductor run; a content failure such as a
        compile error is still rc 0 and routed via binary_meta.failure_category).
        A nonzero rc means a conductor-side/MCP-gate failure -> transport fail_closed."""
        try:
            cap_token = self._capability_token(child_arid)
            if phase == "build":
                out = self._build_inproc(refs, child_arid, cap_token)
            elif phase == "validate" and substep == "pre_judge":
                out = self._pre_judge_inproc(refs, child_arid, cap_token)
            elif phase == "validate" and substep == "post_judge":
                out = self._post_judge_inproc(refs, child_arid, cap_token)
            elif phase == "validate" and substep == "execute":
                out = self._execute_inproc(refs, child_arid, cap_token)
            elif phase == "generate" and substep == "gate":
                out = self._gate_inproc(refs, child_arid, cap_token)
            elif phase == "compile" and substep == "static":
                out = self._compile_static_inproc(refs, child_arid, cap_token)
            else:
                raise RuntimeError(f"no deterministic body for {phase}.{substep}")
        except Exception as exc:  # noqa: BLE001 - surfaced as transport failure
            return ProcResult(1, "", f"deterministic_{phase}_error: {exc}")
        return ProcResult(int(out.get("returncode", 0)), out.get("stdout", ""), out.get("stderr", ""))

    def _stage_dependency_sources(self, refs: NodeRefs, obj_dir: Path) -> list[str]:
        """Model B (docs/design): stage each dependency-closure `<dep>_model.f90` into the
        per-run build tmp `$(OBJDIR)` so the conductor-authored dependency Makefile
        (`_write_makefile` non-leaf branch) compiles + links the closure. Never touches the
        canonical `src/` — phase_02 §41 carve-out: a transient `$(OBJDIR)` stage is not a
        canonical-tree copy, so it is not the forbidden dependency mix-in.

        Each dep's model source is resolved from the dep's latest ready pipeline, then from the
        **certified binary** (`_latest_meta_under(.../binary/*/binary_meta.json)` — the same
        binary `_verify_dep_stage` certifies readiness against) via its `source_source_id` ->
        `source/<source_source_id>/src/<dep>_model.f90`. Binding to the certified binary's
        source (not the pipeline `lineage.json`, which tracks the latest *generated* source)
        guarantees the staged code is the exact source the ready binary/verdict was built from.
        node_keys carry `@<version>`, so the per-version workspace path is unambiguous.

        Returns the repo-relative refs of the staged sources (deepest-first). Raises on an
        unresolvable dependency: a missing dep source means the dependency was not built
        ready (run `--with-deps` first), which is a build precondition failure routed to
        transport fail_closed (operator --resume), NOT a content failure the generate retry
        loop could fix.

        No-op (returns []) unless the node is make ∧ fortran — staging is paired with the
        conductor-authored Fortran Makefile (`_write_makefile` non-leaf branch), which is the
        only consumer of the staged `<dep>_model.f90`. For a c/cpp/mixed dependency node the
        Generate child still owns the (LLM-authored) Makefile and its own dependency build, so
        the conductor must not stage Fortran sources (they do not exist under those names)."""
        from tools.orchestration_runtime import _certified_model_source, _latest_pipeline_dir
        if not self._conductor_authors_makefile(refs):
            return []
        nodes = self._dependency_closure_nodes(refs)
        if not nodes:
            # Defense-in-depth: a genuine leaf has empty `direct_deps`. If `direct_deps` is
            # non-empty yet the closure (the direct+transitive union) still resolves empty, the
            # `direct_deps` entries have no resolvable `node_key` — a malformed IR violating the
            # compile closure contract (phase_01 §V4). The Makefile would have been authored
            # leaf-shaped (no DEP_OBJS) and the node's `use <dep>_model` would fail Build as a
            # missing-module compile error that misroutes to a Generate retry it cannot fix.
            # Fail closed here with a clear cause instead.
            ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
            dep = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
            if dep.get("direct_deps"):
                raise RuntimeError(
                    f"empty build closure for {refs.node_key} despite non-empty "
                    f"dependency.direct_deps: the closure now derives from the "
                    f"conductor-authored dependency_graph.json sidecar's all_nodes, so this "
                    f"means the sidecar is missing/unreadable/leaf-shaped (recompile to "
                    f"re-author it; phase_01 §V4 closure contract)")
            return []
        obj_dir.mkdir(parents=True, exist_ok=True)
        staged: list[str] = []
        for nk in nodes:
            safe = node_key_safe(nk)
            sid = spec_id_of(nk)
            # Bind the staged source to the SAME binary the readiness gate certified, NOT to
            # the pipeline-level lineage.json. `_verify_dep_stage` certifies the latest
            # `binary/*/binary_meta.json` (selected by id) and binds the aggregate_verdict to
            # it; that binary records the source it was actually built from in
            # `source_source_id`. The pipeline lineage.json, by contrast, tracks the latest
            # GENERATED source, which a Generate retry may have advanced past the certified
            # binary's source (newer source, not yet rebuilt/validated) — staging from lineage
            # would then compile the depending node against UNVERIFIED dependency code. Use the
            # certified binary's `source_source_id` so the staged source == the validated one.
            # `_certified_model_source` is the single-sourced selection the Generate-time
            # interface hint (`_resolve_dependency_facts`) reads too, so the interface a
            # consumer is SHOWN equals the source Build COMPILES (no drift).
            # Locate the dependency's own pipeline by its EXACT sidecar-pinned version. The
            # sidecar pins the highest catalog version satisfying the consumer constraint
            # (matching run_workflow's node_label / `--with-deps` scheduling), so a correctly
            # built closure has that exact version's pipeline. If it is absent, FAIL CLOSED
            # rather than substitute a sibling version: staging a different version could link
            # stale/constraint-incompatible dependency code, and the version-tolerant readiness
            # gate (which accepts any matching version) diverging from exact-version staging is
            # the L6-deferred multi-version concern — kept fail-closed until that lands. (All
            # current specs are single-version, so the pinned version == the built version.)
            pipe_dir = _latest_pipeline_dir(
                self.repo_root / "workspace" / "pipelines" / safe)
            if pipe_dir is None:
                raise RuntimeError(
                    f"dependency {nk}: no ready pipeline under workspace/pipelines/{safe} "
                    f"to stage {sid}_model.f90 from (build the dependency closure first, "
                    f"e.g. run_workflow.py --with-deps)")
            model_src = _certified_model_source(pipe_dir, sid)
            if model_src is None:
                raise RuntimeError(
                    f"dependency {nk}: cannot resolve certified {sid}_model.f90 under "
                    f"{self._rel(pipe_dir)} (no binary_meta.json / no source_source_id / "
                    f"missing source file; dependency not built ready — "
                    f"run_workflow.py --with-deps first)")
            shutil.copy2(model_src, obj_dir / f"{sid}_model.f90")
            staged.append(self._rel(model_src))
        return staged

    def _build_inproc(self, refs: NodeRefs, child_arid: str, cap_token: str) -> dict[str, str]:
        """Deterministic Build: in-process compile_project + binary_meta + post_build gate."""
        import sys as _sys
        mcp_dir = str(self.repo_root / "mcp_servers")
        if mcp_dir not in _sys.path:
            _sys.path.insert(0, mcp_dir)
        from build_runtime_server import tool_compile_project

        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        toolchain = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        language = str(toolchain.get("language") or "fortran")
        build_system = str(toolchain.get("build_system") or "make")
        self._require_make_build_system(build_system, "build")

        src_dir = self.repo_root / refs.source_dir() / "src"
        bin_dir = self.repo_root / refs.binary_dir() / "bin"
        obj_dir = self.repo_root / "workspace" / "tmp" / child_arid / "build"
        exe = self._resolve_exe_name(refs)
        # DEPENDENCY BUILD (Model B, docs/design): for a make∧fortran node with dependencies,
        # stage each closure `<dep>_model.f90` into obj_dir ($(OBJDIR)) BEFORE compile, so the
        # conductor-authored dependency Makefile (_write_makefile non-leaf branch) compiles +
        # links the closure. Self-gated: a no-op for a leaf node (empty closure) and for
        # c/cpp/mixed nodes (LLM-authored Makefile owns its own dependency build). A staging
        # failure raises -> _run_deterministic_substep catches it as a transport fail_closed
        # (build precondition: the dependency must be built ready first). The transient OBJDIR
        # stage never touches canonical src/ (phase_02 §41 carve-out).
        self._stage_dependency_sources(refs, obj_dir)

        result = tool_compile_project({
            "project_dir": str(src_dir),
            # The MCP orchestration gate resolves the orchestration root from repo_root
            # (defaulting to project_dir); pass our repo_root so it finds the capability.
            "repo_root": str(self.repo_root),
            "language": language,
            "build_system": build_system,
            # OBJDIR/BINDIR out-of-source overrides + BIN imposed to the canonical
            # <spec_id>_runner (command-line override wins over any Makefile BIN
            # assignment). Validate.execute imposes the same BIN via the make_test env;
            # see phase_03_build.md.
            "extra_args": [f"OBJDIR={obj_dir}", f"BINDIR={bin_dir}", f"BIN={exe}"],
            "capture_limit": _FULL_CAPTURE_LIMIT,
            "orchestration_id": self.orchestration_id,
            "agent_run_id": child_arid,
            "capability_token": cap_token,
        })
        ok = bool(result.get("ok"))
        # return_code is None on a subprocess timeout; treat that as a build failure.
        rc = result.get("return_code") or 1
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        # A compile that reports success but did NOT produce the binary at the imposed
        # bin/<spec_id>_runner (Build passes BIN=<spec_id>_runner) means the Makefile's
        # build rule does not honor $(BIN). Treat as a build failure that regenerates the
        # Makefile rather than writing a pass binary_meta pointing at a missing file (which
        # desyncs from determine_substep_status -> inconsistent escalate/fail_closed).
        binary_missing = ok and not (bin_dir / exe).is_file()
        if binary_missing:
            ok = False
        # `command_log_ref` from the handler is cwd-relative (`_path_to_ref` uses
        # Path.cwd()), which is unreliable for the in-process caller — derive it from
        # our repo_root + the known canonical placement instead. Make's in-source build
        # writes the log to <src>/command_log.jsonl (project_dir = src_dir).
        command_log_ref = self._rel(src_dir / "command_log.jsonl")

        # Full (untrimmed) per-step compiler logs in the binary dir (build has no
        # canonical stdout/stderr.log otherwise — only the lean command_log audit).
        bdir = self.repo_root / refs.binary_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "compile.stdout.log").write_text(stdout, encoding="utf-8")
        (bdir / "compile.stderr.log").write_text(stderr, encoding="utf-8")

        dep = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
        direct_deps = dep.get("direct_deps") or []
        dep_keys = [d.get("node_key") if isinstance(d, dict) else d for d in direct_deps]
        # The dependency-encapsulation contract (phase_03 §23-25,53) is enforced by the
        # post_build gate below (`validate_pipeline_semantics --stage post_build` →
        # validate_post_build_violation); binary_meta.dependency_check is metadata.
        # `resolved` is "match" only when the build itself succeeded.

        binary_meta: dict[str, Any] = {
            "binary_id": refs.binary_id,
            "node_key": refs.node_key,
            "pipeline_id": refs.pipeline_id,
            "attempt_count": 1,
            "verification_status": "pass" if ok else "fail",
            "last_fail_reason": "" if ok else "compile",
            "status": "pass" if ok else "fail",
            "validation_stage": "post_build",
            "source_source_id": refs.source_id,
            # The ir_id the linked source was generated from (a compile reopen re-numbers ir_id
            # under the SAME pipeline, so this binds this binary to its exact origin IR — read by
            # `_write_runner` to pin a consumer's harness runner against the same-lineage IR).
            "source_ir_id": refs.ir_id,
            "build_system": build_system,
            "compiler": result.get("compiler") or "",
            "binary_artifact_ref": f"binary/{refs.binary_id}/bin/{exe}",
            "command_id": result.get("command_id"),
            "command_log_ref": command_log_ref,
            "command_log_path": command_log_ref,
            "build_log_ref": command_log_ref,
            "dependency_check": {"direct_deps": dep_keys,
                                 "resolved": "match" if ok else "unresolved"},
            "failure_category": None,
            "failure_source_refs": [],
            "failure_excerpt": None,
        }
        if binary_missing:
            # Makefile build-rule defect -> restart (regenerate the Makefile).
            binary_meta["failure_category"] = "make_error"
            binary_meta["last_fail_reason"] = "binary_not_built_at_bindir"
            binary_meta["failure_excerpt"] = (
                f"compile reported success but no binary at bin/{exe} (imposed BIN); the "
                f"Makefile build rule must produce $(BINDIR)/$(BIN)")
            binary_meta["failure_source_refs"] = [f"{self._rel(src_dir)}/Makefile"]
        elif not ok:
            binary_meta["failure_category"] = self._classify_build_failure_category(rc, stderr)
            binary_meta["failure_excerpt"] = "\n".join(stderr.splitlines()[-50:])
            # Point Generate at the offending source(s) (phase_03 retry trigger).
            binary_meta["failure_source_refs"] = self._extract_failure_source_refs(
                stderr, self._rel(src_dir))

        meta_path = self.repo_root / refs.binary_dir() / "binary_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(binary_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # A content failure (compile/link error, post_build violation) is recorded in
        # binary_meta (verification_status=fail + failure_category) and returns rc 0 so
        # run_phase routes it via classify_build_failure -> Generate (NOT transport
        # fail_closed). determine_substep_status reads binary_meta.verification_status,
        # so a gate failure on an otherwise-built binary still fails the substep.
        if ok:
            gate = subprocess.run(
                ["python3", "tools/validate_pipeline_semantics.py", "--stage", "post_build",
                 "--pipeline-root", refs.pipeline_ref, "--source-id", refs.source_id or ""],
                cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
            if gate.returncode != 0:
                binary_meta.update({
                    "verification_status": "fail", "status": "fail",
                    "failure_category": "validate_post_build_violation",
                    "failure_excerpt": "\n".join((gate.stdout + gate.stderr).splitlines()[-50:]),
                })
                meta_path.write_text(
                    json.dumps(binary_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                stderr += "\n[post_build gate fail]\n" + gate.stdout + gate.stderr
        return {"returncode": 0, "stdout": stdout, "stderr": stderr}

    def _gate_inproc(self, refs: NodeRefs, child_arid: str,
                     cap_token: str) -> dict[str, str]:
        """Deterministic Generate.gate: run the three source checkers (lint, syntax, static) as
        a single unioned substep and author ONE gate_meta.json. Replaces the former
        lint/syntax/static substeps so a source with defects in several classes gets ONE warm
        repair turn carrying every finding (1 attempt = 1 union verdict = 1 warm repair turn),
        instead of one repair turn + one Generate attempt per class.

        Order and semantics:
          - lint   (_gate_lint_check):   always runs; writes lint_evidence (even on fail).
          - syntax (_gate_syntax_check): always runs (independent of lint); writes
            syntax_evidence. An attribution error (canary / dependency closure) raises out of
            here, so gate_meta is NOT written and the substep returns rc 1 (transport
            fail_closed) — fail_closed dominates a co-occurring lint content-fail, the same order
            as the pre-merge sequential substeps, only surfaced sooner.
          - static (_gate_static_check): runs ONLY when lint AND syntax both pass. The
            post_generate certifier hard-fails on lint/syntax evidence whose ok flag is not true,
            so running it over a dirty source would double-report the same defect; skip is
            recorded as checkers.static.status="skipped", skipped_reason="lint_or_syntax_failed".

        gate_status = pass iff every checker passed (static counted as pass only when it actually
        ran and passed). failure_categories / the composed excerpt are in canonical order
        (syntax_error -> lint_findings -> static family). A content failure returns rc 0 so
        run_phase routes it via classify_gate_failure -> generate.generate (warm resume);
        determine_substep_status reads gate_meta.gate_status. The DRIFT GUARD
        (test_mcp_grant_table_matches_conductor_call_sites) walks the `self._gate_*_check(` calls
        BELOW to derive this substep's gated-tool set, so keep them as explicit method calls."""
        lint = self._gate_lint_check(refs, child_arid, cap_token)
        syntax = self._gate_syntax_check(refs, child_arid, cap_token)
        lint_ok = lint.get("status") == "pass"
        syntax_ok = syntax.get("status") == "pass"
        if lint_ok and syntax_ok:
            static = self._gate_static_check(refs, child_arid, cap_token)
        else:
            # Skip static: its post_generate certifier hard-fails on non-ok lint/syntax evidence,
            # so it could only echo the failure already recorded above.
            static = {
                "status": "skipped",
                "skipped_reason": "lint_or_syntax_failed",
                "failure_category": None,
                "failure_excerpt": None,
            }
        static_ran = static.get("status") in ("pass", "fail")
        static_ok = static.get("status") == "pass"
        gate_ok = lint_ok and syntax_ok and static_ok

        # Canonical order: syntax_error -> lint_findings -> static family. static categories only
        # ever appear alone (static runs only when lint AND syntax passed).
        categories: list[str] = []
        if not syntax_ok and syntax.get("failure_category"):
            categories.append(str(syntax["failure_category"]))
        if not lint_ok and lint.get("failure_category"):
            categories.append(str(lint["failure_category"]))
        if static_ran and not static_ok and static.get("failure_category"):
            categories.append(str(static["failure_category"]))
        categories = _gate_categories_canonical(categories)

        # Composed excerpt: per-checker sections in the same canonical order, each tagged so the
        # repair leaf can tell which checker reported what. Per-checker caps were already applied
        # inside each helper (lint 50-line / syntax 80-line / static 50-line tails), so the worst
        # case (~130-210 lines) still fits the slim repair prompt.
        sections: list[str] = []
        if not syntax_ok and syntax.get("failure_excerpt"):
            sections.append("[syntax]\n" + str(syntax["failure_excerpt"]))
        if not lint_ok and lint.get("failure_excerpt"):
            sections.append("[lint]\n" + str(lint["failure_excerpt"]))
        if static_ran and not static_ok and static.get("failure_excerpt"):
            sections.append("[static]\n" + str(static["failure_excerpt"]))
        failure_excerpt = "\n".join(sections) if sections else None

        gate_meta: dict[str, Any] = {
            "source_id": refs.source_id,
            "node_key": refs.node_key,
            "pipeline_id": refs.pipeline_id,
            "attempt_count": 1,
            "gate_status": "pass" if gate_ok else "fail",
            "verification_status": "pass" if gate_ok else "fail",
            "status": "pass" if gate_ok else "fail",
            "checkers": {"lint": lint, "syntax": syntax, "static": static},
            "failure_categories": categories,
            "failure_excerpt": failure_excerpt,
        }
        meta_path = self.repo_root / refs.source_dir() / "gate_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(gate_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # A content failure returns rc 0 so run_phase routes it via classify_gate_failure ->
        # generate.generate (warm resume), NOT transport fail_closed.
        return {"returncode": 0,
                "stdout": "",
                "stderr": "" if gate_ok else (failure_excerpt or "")}

    def _gate_lint_check(self, refs: NodeRefs, child_arid: str,
                         cap_token: str) -> dict[str, Any]:
        """Generate.gate lint checker: in-process run_linter over source/<id>/src/, plus a
        host-side (leaf-non-writable) lint evidence certificate. Returns the `lint` section of
        gate_meta (status / preset / language / run_linter / failure_category / failure_excerpt);
        `_gate_inproc` composes the single gate_meta.json verdict. Lint findings are a CONTENT
        failure (status="fail") the gate routes to generate.generate via a warm-resume reopen; a
        genuine tool/infra error raises and surfaces as a transport fail_closed. The evidence is
        written even on a content fail (ok=false), which the post_generate certifier depends on."""
        import sys as _sys
        mcp_dir = str(self.repo_root / "mcp_servers")
        if mcp_dir not in _sys.path:
            _sys.path.insert(0, mcp_dir)
        from build_runtime_server import tool_run_linter
        # Same language->preset table the post_generate validator certifies against, so the
        # preset the conductor RUNS cannot drift from the preset the validator EXPECTS.
        from tools.validate_pipeline_semantics import _LINT_PRESET_FOR_LANGUAGE
        from tools.hooks.lint_evidence import write_lint_evidence

        language = self._read_toolchain(refs)["language"]
        preset = _LINT_PRESET_FOR_LANGUAGE.get(language)
        if preset is None:
            # No static-lint mapping for this language: a precondition error, not a content
            # failure the generate retry loop could fix -> transport fail_closed.
            raise RuntimeError(
                f"generate.gate lint check: toolchain.language={language!r} has no static lint preset "
                f"mapping (expected one of {sorted(_LINT_PRESET_FOR_LANGUAGE)})")

        src_dir = self.repo_root / refs.source_dir() / "src"
        # Canonical lint command-log placement: <src>/command_log.jsonl (same file the
        # generate leaf created). _run_command appends, so the lint record is added. The
        # handler's own command_log_ref is cwd-relative/unreliable for the in-process
        # caller, so derive the canonical repo-relative ref ourselves (as _build_inproc does).
        command_log_ref = self._rel(src_dir / "command_log.jsonl")
        result = tool_run_linter({
            "preset": preset,
            "project_dir": str(src_dir),
            "repo_root": str(self.repo_root),
            "command_log_path": str(src_dir / "command_log.jsonl"),
            "capture_limit": _FULL_CAPTURE_LIMIT,
            "orchestration_id": self.orchestration_id,
            "agent_run_id": child_arid,
            "capability_token": cap_token,
        })

        # Normalize single vs mixed (2 sub-runs) into a uniform run_linter entry list.
        run_entries: list[dict[str, Any]] = []
        excerpts: list[str] = []
        if preset == "mixed":
            ok = bool(result.get("ok"))
            for sub in result.get("runs") or []:
                run_entries.append({
                    "preset": str(sub.get("sub_preset") or sub.get("preset") or ""),
                    "command_id": str(sub.get("command_id") or ""),
                    "command_log_ref": command_log_ref,
                    "ok": bool(sub.get("ok")),
                })
                excerpts.append((sub.get("stdout", "") or "") + (sub.get("stderr", "") or ""))
        else:
            ok = bool(result.get("ok"))
            run_entries.append({
                "preset": preset,
                "command_id": str(result.get("command_id") or ""),
                "command_log_ref": command_log_ref,
                "ok": ok,
            })
            excerpts.append((result.get("stdout", "") or "") + (result.get("stderr", "") or ""))

        failure_excerpt = None
        if not ok:
            failure_excerpt = "\n".join("\n".join(e.splitlines()[-50:]) for e in excerpts)

        # Host-side, leaf-non-writable certificate the post_generate validator certifies
        # against. The evidence keys (preset/command_id/command_log_ref) are exactly what
        # _validate_generate_lint_command_logs needs; the leaf cannot forge it (pipeline
        # root is read-only inside the sandbox, like lineage.json). Written even on a content
        # fail (ok=false) — the certifier reads the evidence regardless, so it must exist.
        write_lint_evidence(
            pipeline_root=self.repo_root / refs.pipeline_ref,
            source_id=refs.source_id or "",
            preset=preset,
            ok=ok,
            run_linter=[{
                "preset": e["preset"],
                "command_id": e["command_id"],
                "command_log_ref": e["command_log_ref"],
            } for e in run_entries],
        )

        # Return the `lint` section of gate_meta; _gate_inproc composes the single verdict.
        return {
            "status": "pass" if ok else "fail",
            "preset": preset,
            "language": language,
            "run_linter": run_entries,
            "failure_category": None if ok else "lint_findings",
            "failure_excerpt": failure_excerpt,
        }

    def _gate_syntax_check(self, refs: NodeRefs, child_arid: str,
                           cap_token: str) -> dict[str, Any]:
        """Generate.gate syntax checker: in-process run_syntax_check (a real compiler
        front-end, gfortran -fsyntax-only) over the staged node + dependency-closure
        sources, plus a host-side (leaf-non-writable) syntax evidence certificate. Returns the
        `syntax` section of gate_meta (status / language / stages / skipped_reason /
        failure_category / failure_excerpt); `_gate_inproc` composes the single gate_meta.json
        verdict. This catches the whole class of syntax / standard-conformance compile_errors
        BEFORE Build (where they would force the expensive regenerate->rebuild loop) — replacing
        the retired post_generate text heuristics that could only mimic gfortran one observed
        failure at a time.

        Compiler findings are a CONTENT failure (status="fail") the gate routes to
        generate.generate via a warm-resume reopen — UNLESS
        the failure is one the leaf cannot fix. A failing stage is attributed by re-running
        the compiler over isolated source sets and reading its VERDICT (never the diagnostics
        text): a canary valid under every standard (fails => the invocation itself is
        unviable, typically a toolchain.standard the driver rejects), then the staged
        dependency closure alone (fails => the closure is at fault — either a defective
        certified source or a node standard too narrow for it). Both are a transport
        fail_closed naming what to fix, since the leaf authors neither the IR nor a
        dependency's certified source. A missing MANDATORY gfortran (or a genuine tool/infra
        error) raises and surfaces as a transport fail_closed likewise — an environment
        problem, not something the generate retry loop could fix. Optional additional stages
        from METDSL_SYNTAX_COMPILERS (comma-separated adapter ids, e.g. "gfortran,frt" — the
        future target-compiler second stage) are recorded as skipped when their binary is not
        installed, so one configuration runs on machines with and without the target compiler.

        Staging: each compiler stage gets its own throwaway dir under
        workspace/tmp/<child_arid>/syntax/<compiler>/ holding the node's src *.f90 plus
        the certified dependency-closure `<dep>_model.f90` (`_stage_dependency_sources`).
        Module files are compiler-/version-specific, so stages never share a dir and
        never touch Build's $(OBJDIR). Non-fortran languages (c/cpp/mixed/cuda_*) pass
        through: gfortran cannot check them, Build stays their backstop."""
        import sys as _sys
        mcp_dir = str(self.repo_root / "mcp_servers")
        if mcp_dir not in _sys.path:
            _sys.path.insert(0, mcp_dir)
        from build_runtime_server import (
            _FORTRAN_SYNTAX_SOURCE_SUFFIXES,
            _SYNTAX_COMPILER_ADAPTERS,
            SYNTAX_CANARY_SOURCE,
            tool_run_syntax_check,
        )
        from tools.hooks.syntax_evidence import write_syntax_evidence

        # Single source of truth for the free-form Fortran suffix set: the tool that owns
        # source discovery. The conductor's "no source to check" test and the tool's
        # discover-and-order set must not drift.
        suffixes = _FORTRAN_SYNTAX_SOURCE_SUFFIXES
        tc = self._read_toolchain(refs)
        language = tc["language"]
        src_dir = self.repo_root / refs.source_dir() / "src"
        command_log_ref = self._rel(src_dir / "command_log.jsonl")

        ok = True
        failure_category: str | None = None
        failure_excerpt: str | None = None
        skipped_reason: str | None = None
        stages: list[dict[str, Any]] = []

        node_sources = sorted(
            p for p in src_dir.iterdir()
            if p.is_file() and p.suffix.lower() in suffixes
        ) if src_dir.is_dir() else []

        if language != "fortran":
            skipped_reason = f"language={language}: no syntax-check adapter (fortran only)"
        elif not node_sources:
            ok = False
            failure_category = "syntax_error"
            failure_excerpt = (
                f"{self._rel(src_dir)}: no free-form Fortran source "
                f"({'/'.join(suffixes)}) to syntax-check"
            )
        else:
            # Resolve + stage the dependency-closure `<dep>_model.f90` ONCE into a shared
            # cache dir (not per compiler — the resolved source set is identical across
            # stages; only the per-compiler `.mods` must stay isolated). `<dep>_model.f90`
            # sources the node `use`s must be present or gfortran reports "Cannot open
            # module file", which the syntax gate would misdiagnose as a content error.
            #   * make+fortran: staging copies each certified dep model, or RAISES (a clean
            #     transport fail_closed) if a dep is not yet certified — the same
            #     `--with-deps` precondition Build enforces (`_stage_dependency_sources`).
            #   * non-make fortran with dependencies: the conductor does not own that node's
            #     Makefile, so staging is a no-op and the gate cannot resolve `use
            #     <dep>_model`. Running gfortran anyway would misclassify the unresolved
            #     module as a content `syntax_error` and warm-resume generate.generate in a
            #     futile loop (the regenerated source references the same real module). Fail
            #     closed cleanly instead — such a node is unbuildable regardless (Build's
            #     `_require_make_build_system` rejects non-make fortran).
            deps_dir = (self.repo_root / "workspace" / "tmp" / child_arid
                        / "syntax" / "_deps")
            deps_dir.mkdir(parents=True, exist_ok=True)
            staged_deps = self._stage_dependency_sources(refs, deps_dir)
            if not staged_deps and self._dependency_closure_nodes(refs):
                raise RuntimeError(
                    f"generate.gate syntax check: cannot stage dependency modules for build_system="
                    f"{tc['build_system']!r} (only make+fortran staging is supported); the "
                    f"syntax gate would misdiagnose an unresolved `use <dep>_model` as a "
                    f"content error and loop — fail closed (this node is unbuildable anyway)")
            dep_files = [p for p in deps_dir.iterdir() if p.is_file()]

            raw = self.env.get("METDSL_SYNTAX_COMPILERS", "gfortran")
            compilers = [c.strip().lower() for c in raw.split(",") if c.strip()]
            # gfortran is the mandatory gate regardless of the env list's content/order:
            # it is the one stage post_generate certification requires to have passed.
            if "gfortran" in compilers:
                compilers.remove("gfortran")
            compilers.insert(0, "gfortran")
            for compiler in compilers:
                # An entry with no registered adapter (e.g. a future `frt` listed before its
                # adapter ships) is recorded skipped, not crashed: the tool would raise
                # ValueError for an unknown compiler, which — unlike the "binary not
                # installed" skip — would propagate as a transport fail_closed even though
                # the mandatory gfortran stage passed. gfortran must always be registered.
                if compiler not in _SYNTAX_COMPILER_ADAPTERS:
                    if compiler == "gfortran":
                        raise RuntimeError(
                            "generate.gate syntax check: gfortran has no registered syntax-check "
                            "adapter (build-tooling bug)")
                    stages.append({
                        "compiler": compiler,
                        "status": "skipped",
                        "reason": f"no registered syntax-check adapter for {compiler}",
                    })
                    continue
                stage_dir = (self.repo_root / "workspace" / "tmp" / child_arid
                             / "syntax" / compiler)
                stage_dir.mkdir(parents=True, exist_ok=True)
                for p in node_sources:
                    shutil.copy2(p, stage_dir / p.name)
                for p in dep_files:
                    shutil.copy2(p, stage_dir / p.name)
                result = tool_run_syntax_check({
                    "compiler": compiler,
                    "std": tc["standard"],
                    "openmp": tc["backend"] == "openmp",
                    "project_dir": str(stage_dir),
                    "repo_root": str(self.repo_root),
                    "command_log_path": str(src_dir / "command_log.jsonl"),
                    "capture_limit": _FULL_CAPTURE_LIMIT,
                    "orchestration_id": self.orchestration_id,
                    "agent_run_id": child_arid,
                    "capability_token": cap_token,
                })
                if result.get("skipped"):
                    if compiler == "gfortran":
                        raise RuntimeError(
                            f"generate.gate syntax check: mandatory gfortran stage unavailable "
                            f"({result.get('reason')})")
                    stages.append({
                        "compiler": compiler,
                        "status": "skipped",
                        "reason": str(result.get("reason") or ""),
                    })
                    continue
                stage_ok = bool(result.get("ok"))
                stages.append({
                    "compiler": compiler,
                    "status": "pass" if stage_ok else "fail",
                    "compiler_version": result.get("compiler_version"),
                    "command_id": str(result.get("command_id") or ""),
                    "command_log_ref": command_log_ref,
                })
                if not stage_ok:
                    excerpt = ((result.get("stdout", "") or "")
                               + (result.get("stderr", "") or ""))

                    def _sub_check(sub_dir: Path) -> dict[str, Any]:
                        """Re-run THIS stage's adapter+flags over an isolated source set, to
                        attribute the failure by the compiler's own verdict. The command log
                        stays in the throwaway dir (no command_log_path override): these runs
                        certify nothing, so keeping them out of the node's canonical
                        <src>/command_log.jsonl keeps that log the record of the gate proper."""
                        return tool_run_syntax_check({
                            "compiler": compiler,
                            "std": tc["standard"],
                            "openmp": tc["backend"] == "openmp",
                            "project_dir": str(sub_dir),
                            "repo_root": str(self.repo_root),
                            "capture_limit": _FULL_CAPTURE_LIMIT,
                            "orchestration_id": self.orchestration_id,
                            "agent_run_id": child_arid,
                            "capability_token": cap_token,
                        })

                    # Attribution step 1 — is the INVOCATION itself viable? `std` comes from
                    # the LLM-authored IR (impl_defaults.toolchain.standard) and goes straight
                    # into `-std=<value>`: an unknown value (`-std=2008`, the elided-`f` form)
                    # makes the driver reject the command line, so no source is ever parsed and
                    # every file "fails" at once. Compiling a canary valid under every standard
                    # tells the two apart by the compiler's own verdict — no enumeration of the
                    # stds a given compiler VERSION accepts (`f2023` exists on GCC>=13 only, so
                    # any hard-coded set is wrong on some machine). The leaf cannot rewrite the
                    # IR, so a broken invocation is a transport fail_closed, never a retry; and
                    # attributing it to the dependency closure (which fails the same broken argv
                    # for the same reason) would send the operator to re-certify healthy nodes.
                    canary_dir = (self.repo_root / "workspace" / "tmp" / child_arid
                                  / "syntax" / f"{compiler}_canary")
                    canary_dir.mkdir(parents=True, exist_ok=True)
                    (canary_dir / "metdsl_syntax_canary.f90").write_text(
                        SYNTAX_CANARY_SOURCE, encoding="utf-8")
                    canary = _sub_check(canary_dir)
                    if not canary.get("skipped") and not canary.get("ok"):
                        canary_excerpt = ((canary.get("stdout", "") or "")
                                          + (canary.get("stderr", "") or ""))
                        raise RuntimeError(
                            f"generate.gate syntax check: the {compiler} invocation is not viable — it "
                            f"rejects even a canary source valid under every standard, so the "
                            f"failure is the invocation, not the sources. Check "
                            f"impl_defaults.toolchain.standard={tc['standard']!r} (it is passed "
                            f"verbatim as -std=<value>; spell it the way the compiler names it, "
                            f"e.g. `f2008`, not `2008`) and the compiler installation. The leaf "
                            f"does not author the IR, so no retry of this node can clear it.\n"
                            + "\n".join(canary_excerpt.splitlines()[-20:]))

                    # Attribution step 2 —
                    # A failure the STAGED DEPENDENCY sources cause is not a content failure
                    # this node's leaf can repair: `<dep>_model.f90` is the dependency's
                    # CERTIFIED source, outside source/<source_id>/src/ and outside the leaf's
                    # write_roots. Warm-resuming generate.generate would regenerate the node's
                    # own files and hit the identical finding — the same futile loop the
                    # non-make staging branch above already fails closed on. The dependency
                    # itself must be regenerated / re-certified (its own Generate.gate syntax
                    # check enforces the same rules), which is an operator decision, not a retry.
                    # Reachable because a dependency certified BEFORE a gate rule was added
                    # (e.g. the promoted -Werror=unused-* classes) is not clean by induction —
                    # only a dependency certified under the current gate is.
                    #
                    # Attribution re-runs the SAME adapter over the dependency closure ALONE
                    # (it is self-contained: `_stage_dependency_sources` stages the transitive
                    # closure, so every `use` among the deps resolves within the set). Deciding
                    # this by the compiler's own verdict rather than by reading the diagnostics
                    # keeps it exact and format-agnostic: a dep basename merely APPEARING in
                    # the excerpt proves nothing (gfortran prints default-on warnings —
                    # -Wampersand, -Wtabs, -Wunderflow — for a dep that is perfectly clean,
                    # while the sole Error sits in the node's own leaf-fixable source), and
                    # mistaking that for a dependency defect would convert a self-repairable
                    # finding into a permanent fail_closed. A future adapter with a different
                    # diagnostic format needs no change here.
                    #
                    # Residual: the OTHER staged file the leaf cannot write is the M3c
                    # host-rendered `<spec_id>_runner.f90` — it sits inside src/ (so it is a
                    # node source here) yet outside allowed_output_paths. It gets no probe:
                    # it `use`s the leaf-authored checks module, so it is not self-contained
                    # and cannot be compiled alone. It is held clean at the source instead —
                    # tools/tests/test_runner_renderer.py compiles the rendered runner under
                    # these exact promoted flags, so a renderer edit that emitted an unused
                    # dummy or local could not ship green.
                    if staged_deps and dep_files:
                        probe_dir = (self.repo_root / "workspace" / "tmp" / child_arid
                                     / "syntax" / f"{compiler}_deps_probe")
                        probe_dir.mkdir(parents=True, exist_ok=True)
                        for p in dep_files:
                            shutil.copy2(p, probe_dir / p.name)
                        probe = _sub_check(probe_dir)
                        if not probe.get("skipped") and not probe.get("ok"):
                            probe_excerpt = ((probe.get("stdout", "") or "")
                                             + (probe.get("stderr", "") or ""))
                            # Two causes put a failure here, and the leaf can fix NEITHER, so
                            # both take this one fail_closed: the dependency's certified source
                            # is defective (certified before a gate rule existed), or this
                            # node's declared standard is narrower than the sound closure needs.
                            # The message names both and attaches the compiler's diagnostics,
                            # which say which ("Unused dummy argument …" vs "Fortran 2008: The
                            # symbol 'real64' … is not in the selected standard"). Deciding it
                            # here would take the dependency's OWN certified standard — what its
                            # certification actually asserted. A permissive-standard re-check is
                            # NOT a substitute: `-std=gnu` also accepts what the gate means to
                            # reject (a non-constant STOP code, `implicit none (external)`, GNU
                            # extensions), so a genuinely defective dependency would pass it and
                            # be reported as sound, sending the operator to widen this node's
                            # standard to accommodate nonconforming code.
                            raise RuntimeError(
                                f"generate.gate syntax check: the certified dependency closure does not "
                                f"pass the {compiler} gate under this node's "
                                f"impl_defaults.toolchain.standard={tc['standard']!r}, and this "
                                f"node's leaf can fix neither the closure (it lies outside "
                                f"source/<source_id>/src/) nor the IR. Either the dependency's "
                                f"certified source is defective — regenerate and re-certify it, "
                                f"its own Generate.gate syntax check enforces the same rules — or this "
                                f"node's declared standard rejects a sound closure, in which "
                                f"case fix toolchain.standard (Build would compile the same "
                                f"closure under the same -std). The diagnostics below say which. "
                                f"Staged: {', '.join(staged_deps)}\n"
                                + "\n".join(probe_excerpt.splitlines()[-40:]))
                    ok = False
                    failure_category = "syntax_error"
                    tail = "\n".join(excerpt.splitlines()[-80:])
                    failure_excerpt = (
                        f"[{compiler} {tc['standard']} syntax check fail]\n{tail}"
                        if failure_excerpt is None
                        else failure_excerpt
                        + f"\n[{compiler} {tc['standard']} syntax check fail]\n{tail}")

        # Host-side, leaf-non-writable certificate the post_generate validator certifies
        # against (mirrors write_lint_evidence). Only written when the gate actually ran
        # stages (fortran nodes); certification requires it for language=fortran only.
        if language == "fortran" and stages:
            write_syntax_evidence(
                pipeline_root=self.repo_root / refs.pipeline_ref,
                source_id=refs.source_id or "",
                ok=ok,
                stages=stages,
            )

        # Return the `syntax` section of gate_meta; _gate_inproc composes the single verdict.
        return {
            "status": "pass" if ok else "fail",
            "language": language,
            "stages": stages,
            "skipped_reason": skipped_reason,
            "failure_category": failure_category,
            "failure_excerpt": failure_excerpt,
        }

    def _gate_static_check(self, refs: NodeRefs, child_arid: str,
                           cap_token: str) -> dict[str, Any]:
        """Generate.gate static checker: run the purely-static post_generate gates that the
        verify leaf used to own (so verify is now a pure LLM semantic G1-G7 pass reached only
        on a deterministically-clean source). Returns the `static` section of gate_meta (status /
        failure_category / failure_excerpt); `_gate_inproc` composes the single gate_meta.json
        verdict and only calls this when lint AND syntax both passed. Runs
        validate_workspace_root.py (bare, as the leaf did - no --write-scope-baseline) and
        validate_pipeline_semantics --stage post_generate, in the same order/idiom as the
        post_build gate in _build_inproc. A violation is a CONTENT failure (status="fail" +
        failure_category) the gate routes to generate.generate via a warm-resume reopen; only an
        unexpected error surfaces as a transport fail_closed (caught in
        _run_deterministic_substep). The gate wrote lint_evidence + syntax_evidence earlier this
        attempt (both ok, since this runs only on their pass), so the post_generate certification
        certifies conductor-owned evidence.
        """
        status = "pass"
        failure_category: str | None = None
        failure_excerpt: str | None = None
        stderr = ""

        # workspace_root first (global layout/scope), then post_generate (src/io_contract).
        ws = subprocess.run(
            ["python3", "tools/validate_workspace_root.py"],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
        if ws.returncode != 0:
            status = "fail"
            failure_category = "workspace_root_violation"
            failure_excerpt = "\n".join((ws.stdout + ws.stderr).splitlines()[-50:])
            stderr = "[workspace_root gate fail]\n" + ws.stdout + ws.stderr
        else:
            pg = subprocess.run(
                ["python3", "tools/validate_pipeline_semantics.py", "--stage", "post_generate",
                 "--pipeline-root", refs.pipeline_ref, "--source-id", refs.source_id or ""],
                cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
            if pg.returncode != 0:
                from tools.validate_pipeline_semantics import STALE_DEPENDENCY_IR_MARKER
                status = "fail"
                # A stale-dependency-IR violation is TERMINAL, not a warm Generate retry (the leaf
                # cannot repair a certified dependency IR); classify_gate_failure fail_closes any
                # union verdict that carries this category (GATE_FAILURE_TERMINAL).
                failure_category = ("stale_dependency_ir"
                                    if STALE_DEPENDENCY_IR_MARKER in (pg.stdout + pg.stderr)
                                    else "post_generate_violation")
                failure_excerpt = "\n".join((pg.stdout + pg.stderr).splitlines()[-50:])
                stderr = "[post_generate gate fail]\n" + pg.stdout + pg.stderr

        # Return the `static` section of gate_meta; _gate_inproc composes the single verdict.
        # `stderr` (the tagged gate-fail block) is retained for parity with the other checkers
        # but is not consumed by _gate_inproc — the composed excerpt drives the repair.
        del stderr
        return {
            "status": status,
            "failure_category": failure_category,
            "failure_excerpt": failure_excerpt,
            "skipped_reason": None,
        }

    def _compile_static_inproc(self, refs: NodeRefs, child_arid: str,
                               cap_token: str) -> dict[str, str]:
        """Deterministic Compile.static: run the purely-static IR gates the verify leaf used to
        own (so verify is now a pure LLM semantic pass — the spec-cross-reference invariants
        V1/V3/V5 — reached only on a deterministically-clean IR). Runs, in the same order/idiom
        as the post_build gate in _build_inproc, the three gates the old compile.verify runbook
        emitted: validate_workspace_root.py (bare), check_artifact_syntax.py on
        spec.ir.yaml + ir_meta.json, then validate_pipeline_semantics --stage compile. A
        violation is a CONTENT failure (status=fail + failure_category, rc 0) routed by
        classify_compile_static_failure back to compile.generate via a warm-resume reopen; only
        an unexpected error surfaces as a transport fail_closed (caught in
        _run_deterministic_substep). The --stage compile validator is read-only on the IR and the
        only artifact written is compile_static_meta.json under the substep's own write_root
        (refs.ir_ref), so it needs no host write-exemption (authorized by containment).
        """
        status = "pass"
        failure_category: str | None = None
        failure_excerpt: str | None = None
        stderr = ""

        ir_ref = refs.ir_ref
        # workspace_root (global layout) -> syntax (yaml/json well-formed) -> --stage compile
        # (structural IR invariants). The first failing gate short-circuits.
        gates = [
            (["python3", "tools/validate_workspace_root.py"], "workspace_root"),
            (["python3", "tools/check_artifact_syntax.py", "--expect-top", "object",
              f"{ir_ref}/spec.ir.yaml", f"{ir_ref}/ir_meta.json"], "artifact_syntax"),
            (["python3", "tools/validate_pipeline_semantics.py", "--stage", "compile",
              "--ir-ref", ir_ref], "compile_stage"),
        ]
        for cmd, label in gates:
            proc = subprocess.run(
                cmd, cwd=self.repo_root, env=self.env, text=True,
                capture_output=True, check=False)
            if proc.returncode != 0:
                status = "fail"
                failure_category = "compile_static_violation"
                failure_excerpt = "\n".join((proc.stdout + proc.stderr).splitlines()[-50:])
                stderr = f"[compile {label} gate fail]\n" + proc.stdout + proc.stderr
                break

        compile_static_meta: dict[str, Any] = {
            "ir_id": refs.ir_id,
            "node_key": refs.node_key,
            "attempt_count": 1,
            "status": status,
            "verification_status": status,
            "failure_category": failure_category,
            "failure_excerpt": failure_excerpt,
        }
        meta_path = self.repo_root / ir_ref / "compile_static_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(compile_static_meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")

        # Content failure returns rc 0 so run_phase routes it via classify_compile_static_failure
        # -> compile.generate (warm resume). determine_substep_status reads the meta status.
        return {"returncode": 0, "stdout": "", "stderr": stderr}

    # -- deterministic Validate.execute (run + quality_check + evidence promote) --

    @staticmethod
    def _required_evidence_artifacts(ir: dict[str, Any]) -> list[str]:
        """IR-declared required raw-evidence artifact types (closed set:
        metrics_basis.json / execution_trace.json / state_snapshots)."""
        io = (ir.get("io_contract") or {}) if isinstance(ir, dict) else {}
        rr = (io.get("raw_requirements") or {}) if isinstance(io, dict) else {}
        out: list[str] = []
        for e in rr.get("required_evidence") or []:
            if isinstance(e, dict) and e.get("required") and e.get("artifact"):
                out.append(str(e["artifact"]))
        return out

    def _promote_run_evidence(self, run_tmp: Path, node_dir: Path,
                              artifacts: list[str]) -> list[str]:
        """Promote the runner's `run/` output to the canonical run node dir.
        Selective per artifact type (NOT a blind copytree): the runner's auxiliary
        per-case files (e.g. execution_trace_<case>.json) are deterministically dropped.
        Returns the repo-relative raw_artifact_refs of what was promoted."""
        node_dir.mkdir(parents=True, exist_ok=True)
        for name in ("diagnostics.json", "perf.json"):
            src = run_tmp / name
            if src.exists():
                shutil.copy2(src, node_dir / name)
        raw_dst = node_dir / "raw"
        raw_dst.mkdir(parents=True, exist_ok=True)
        raw_refs: list[str] = []
        node_ref = self._rel(node_dir)
        mb = run_tmp / "raw" / "metrics_basis.json"
        if mb.exists():
            shutil.copy2(mb, raw_dst / "metrics_basis.json")
            raw_refs.append(f"{node_ref}/raw/metrics_basis.json")
        for art in artifacts:
            if art == "state_snapshots":
                sdst = raw_dst / "state_snapshots"
                sdst.mkdir(parents=True, exist_ok=True)
                for f in sorted((run_tmp / "raw" / "state_snapshots").glob("*.json")):
                    shutil.copy2(f, sdst / f.name)
                    raw_refs.append(f"{node_ref}/raw/state_snapshots/{f.name}")
            elif art == "execution_trace.json":
                src = run_tmp / "raw" / "execution_trace.json"
                if src.exists():
                    shutil.copy2(src, raw_dst / "execution_trace.json")
                    raw_refs.append(f"{node_ref}/raw/execution_trace.json")
        return raw_refs

    def _author_snapshot_schema(self, ir: dict[str, Any], node_dir: Path) -> str | None:
        """Author raw/state_snapshots/snapshot_schema.json from the IR schema +
        the per-case files actually present. Deterministic (no judgment)."""
        io = (ir.get("io_contract") or {}) if isinstance(ir, dict) else {}
        rr = (io.get("raw_requirements") or {}) if isinstance(io, dict) else {}
        entry = next((e for e in (rr.get("required_evidence") or [])
                      if isinstance(e, dict) and e.get("artifact") == "state_snapshots"), None)
        if entry is None:
            return None
        sdir = node_dir / "raw" / "state_snapshots"
        if not sdir.exists():
            return None
        schema = entry.get("schema") or {}
        present = {f.name for f in sdir.glob("*.json") if f.name != "snapshot_schema.json"}
        # Order samples by IR test_case_set declaration order (fallback: sorted).
        # strip() to match the stripped case_id identity read_case_ids imposes on
        # the runner argv / on-disk <case_id>.json name (else a whitespace-bearing
        # case_id misses `present` and silently drops to sorted order).
        case = (ir.get("case") or {}) if isinstance(ir, dict) else {}
        tcs = case.get("test_case_set") or [] if isinstance(case, dict) else []
        ordered = [f"{c['case_id'].strip()}.json" for c in tcs
                   if isinstance(c, dict) and isinstance(c.get("case_id"), str)
                   and c["case_id"].strip()
                   and f"{c['case_id'].strip()}.json" in present]
        samples = ordered + sorted(present - set(ordered))
        doc = {
            "variables": schema.get("variables", []),
            "time_variable": schema.get("time_variable"),
            "time_shape_expr": schema.get("time_shape_expr"),
            "min_samples": entry.get("min_samples", 1),
            "samples": samples,
        }
        (sdir / "snapshot_schema.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return f"{self._rel(node_dir)}/raw/state_snapshots/snapshot_schema.json"

    def _snapshot_deliverable_gap(self, snapshots_dir: Path, case_ids: list[str],
                                  artifacts: list[str]) -> str:
        """Diagnostic for a per-case snapshot deliverable mismatch, else "".

        Validate.execute's deliverable gate (build_launch_request) requires one
        raw/state_snapshots/<case_id>.json per case. The runner names snapshots
        freely, so a fixed/sequential name (snapshot_0001.json) or a combined file
        leaves the expected <case_id>.json absent and the deliverable gate fails
        with no recorded cause. This returns an actionable message (expected vs
        written vs missing) so the failure routes to Generate with a clear reason
        instead of an opaque deliverable-missing fail. Empty when snapshots are not
        required or every expected <case_id>.json is present.

        ``snapshots_dir`` is the runner's THIS-attempt output dir (the per-run tmp
        raw/state_snapshots), so the written set is fresh by construction — a stale
        correctly-named file from a prior attempt in the canonical node dir cannot
        mask a real gap (matching the gate's mtime freshness semantics).
        """
        if "state_snapshots" not in artifacts or not case_ids:
            return ""
        present = ({f.name for f in snapshots_dir.glob("*.json")
                    if f.name != "snapshot_schema.json"}
                   if snapshots_dir.exists() else set())
        expected = {f"{cid}.json" for cid in case_ids}
        missing = sorted(expected - present)
        if not missing:
            return ""
        return (
            "[execute fail: snapshot deliverable mismatch] Validate.execute requires "
            "one raw/state_snapshots/<case_id>.json per case. "
            f"expected={sorted(expected)}; runner wrote={sorted(present)}; "
            f"missing={missing}. Name each snapshot exactly <case_id>.json, built "
            "from the case_id passed via --cases (e.g. trim(case_id)//'.json'). "
            "Canonical: phase_02_generate.md / phase_04_validate.md §43."
        )

    @staticmethod
    def _author_quality_check(node_dir: Path, run_diag: dict[str, Any],
                              qc_diag: dict[str, Any], run_cmd_id: str | None,
                              qc_cmd_id: str | None, preset: str,
                              threads: int) -> str:
        """quality_check.json = deterministic value-equality of run_program vs the
        make-test re-run (per phase_04 §4-1). Returns the top-level status."""
        def _check_map(d: dict[str, Any]) -> dict[str, Any]:
            return {k: (v.get("status") if isinstance(v, dict) else v)
                    for k, v in (d.get("checks") or {}).items()}

        run_checks, qc_checks = _check_map(run_diag), _check_map(qc_diag)
        run_verdict, qc_verdict = run_diag.get("verdict"), qc_diag.get("verdict")
        verdict_available = bool(run_verdict) and bool(qc_verdict)
        diagnostics_match = run_checks == qc_checks
        verdict_match = run_verdict == qc_verdict
        run_cases = {c.get("case_id"): c.get("verdict")
                     for c in run_diag.get("cases") or [] if isinstance(c, dict)}
        qc_cases = {c.get("case_id"): c.get("verdict")
                    for c in qc_diag.get("cases") or [] if isinstance(c, dict)}
        per_case = {cid: (run_cases.get(cid) == qc_cases.get(cid)) for cid in run_cases}
        checks_match = {k: (run_checks.get(k) == qc_checks.get(k)) for k in run_checks}
        status = "pass" if (verdict_available and diagnostics_match and verdict_match) else "fail"
        doc = {
            "status": status,
            "preset": preset,
            "checks": {
                "verdict_available": verdict_available,
                "diagnostics_match": diagnostics_match,
                "verdict_match": verdict_match,
            },
            "comparison": {
                "reference": {"source": "run_program", "command_id": run_cmd_id,
                              "threads_per_rank": threads, "verdict": run_verdict},
                "candidate": {"source": f"run_quality_checks/{preset}", "command_id": qc_cmd_id,
                              "threads_per_rank": "make_default", "verdict": qc_verdict},
                "diagnostics_checks_match": checks_match,
                "per_case_verdict_match": per_case,
            },
            "notes": ("conductor in-process: run_program (threads_per_rank=1) and "
                      f"{preset} re-run diagnostics checks and verdicts compared."),
        }
        (node_dir / "quality_check.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return status

    def _rel(self, path: Path) -> str:
        """repo-root-relative POSIX path for canonical refs."""
        return str(path.relative_to(self.repo_root)).replace("\\", "/")

    def _execute_inproc(self, refs: NodeRefs, child_arid: str, cap_token: str) -> dict[str, Any]:
        """Deterministic Validate.execute: in-process run_program + run_quality_checks,
        promote the runner's primary evidence to the canonical run node dir, author the
        agent-owned metadata (snapshot_schema/quality_check/trial_meta/stdout/stderr),
        then run the post_execute gate. The runner's evidence bytes are never authored
        by an LLM (preserving Validate.judge's non-fabrication independence)."""
        import sys as _sys
        mcp_dir = str(self.repo_root / "mcp_servers")
        if mcp_dir not in _sys.path:
            _sys.path.insert(0, mcp_dir)
        from build_runtime_server import tool_run_program, tool_run_quality_checks

        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
        toolchain = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
        target = (impl.get("target") or {}) if isinstance(impl, dict) else {}
        target_class = str(target.get("class") or "cpu")
        threads = 1
        self._require_make_build_system(
            str(toolchain.get("build_system") or "make"), "validate.execute")

        node_dir = self.repo_root / refs.run_node_dir()
        src_dir = self.repo_root / refs.source_dir() / "src"
        bin_dir = self.repo_root / refs.binary_dir(refs.source_binary_id) / "bin"
        exe = self._resolve_exe_name(refs)
        binary = (bin_dir / exe).resolve()
        ir_spec = (self.repo_root / refs.ir_ref / "spec.ir.yaml").resolve()
        run_tmp = self.repo_root / "workspace" / "tmp" / child_arid / "run"
        qc_tmp = self.repo_root / "workspace" / "tmp" / child_arid / "qc_run"
        obj_tmp = self.repo_root / "workspace" / "tmp" / child_arid / "build"
        cmd_log = node_dir / "command_log.jsonl"
        qc_cmd_log = src_dir / "command_log.jsonl"
        case_ids = list(self.read_case_ids(refs))

        # The runner opens raw/ paths relatively (cwd=RUNDIR); pre-create them.
        (run_tmp / "raw" / "state_snapshots").mkdir(parents=True, exist_ok=True)
        qc_tmp.mkdir(parents=True, exist_ok=True)

        # R2 invariant guard: clear any pre-existing verdict.json / trial_meta.json in this run
        # node dir so that, after this substep, `<file> present` ⟺ `THIS execute authored it`.
        # Both files carry a routing decision that a stale copy would corrupt:
        #   - verdict.json: a structural failure returns WITHOUT authoring one, and
        #     classify_failure routes on `verdict.json#failure_class` — a stale verdict would
        #     misroute a runner failure as a predicate failure (escalate/dev fail_closed instead
        #     of the Generate/C2 path).
        #   - trial_meta.json: the runner runtime-error branch returns WITHOUT authoring one, and
        #     classify_failure reads `trial_meta.json#failure_category` (B1) — a stale trial_meta
        #     would misroute a runtime error as a warm, findings-carrying structural repair.
        # run_id rotation (_ensure_fresh_producer_id) already gives each attempt a fresh dir,
        # making this a no-op in practice; enforcing it here removes the reliance on that
        # external invariant for two correctness-critical routing decisions.
        for _stale in ("verdict.json", "trial_meta.json"):
            _prev = node_dir / _stale
            if _prev.exists():
                _prev.unlink()

        gate_args = {"orchestration_id": self.orchestration_id,
                     "agent_run_id": child_arid, "capability_token": cap_token,
                     # so the MCP orchestration gate resolves the right orchestration root
                     "repo_root": str(self.repo_root)}

        # 1. run_program (primary evidence) — include spec.ir.yaml.case per phase_04 §4-1.
        res_run = tool_run_program({
            "project_dir": str(run_tmp),
            "command": [str(binary), "--cases", str(ir_spec), *case_ids],
            "target": {"class": target_class},
            "threads_per_rank": threads,
            "command_log_path": str(cmd_log),
            "capture_limit": _FULL_CAPTURE_LIMIT,
            **gate_args,
        })
        stdout = res_run.get("stdout", "") or ""
        stderr = res_run.get("stderr", "") or ""
        if not res_run.get("ok"):
            # Runtime error is a CONTENT failure (buggy generated code): rc 0 so run_phase
            # routes it via the validate tables / diagnostician, not transport fail_closed.
            # No trial_meta is written, so determine_substep_status fails this substep.
            return {"returncode": 0, "stdout": stdout,
                    "stderr": stderr + "\n[run_program failed: runtime_error]"}

        # 2. run_quality_checks (make_test re-run; output to a SEPARATE tmp).
        res_qc = tool_run_quality_checks({
            "project_dir": str(src_dir),
            "preset": "make_test",
            # BIN imposed to the canonical <spec_id>_runner so `make test`'s
            # `$(BINDIR)/$(BIN)` guard resolves the same binary Build produced. make_test
            # passes overrides via the environment only, which overrides the Makefile's
            # `BIN ?=` form (enforced by post_generate).
            # SPEC/CASES imposed so `make test` invokes the runner identically to
            # run_program (`--cases <spec.ir.yaml> <case_id>...`) — without this the test
            # target's `--cases $(SPEC) $(CASES)` would fall back to the Makefile's baked
            # defaults; pinning them to the authoritative run_program spec/case set keeps the
            # quality_check a true apples-to-apples value comparison (the runner requires
            # `--cases` and aborts without it).
            # No dependency-source staging here (unlike _build_inproc): `make test` only runs
            # the already-built binary (the `test:` target has no build prerequisite, so it
            # never recompiles), so the closure `.f90`/`.mod` are not needed in OBJDIR.
            "env": {"OBJDIR": str(obj_tmp), "BINDIR": str(bin_dir),
                    "RUNDIR": str(qc_tmp), "BIN": str(exe),
                    "SPEC": str(ir_spec), "CASES": " ".join(case_ids)},
            "command_log_path": str(qc_cmd_log),
            "capture_limit": _FULL_CAPTURE_LIMIT,
            **gate_args,
        })

        # 3. promote primary evidence (selective per artifact type) + author metadata.
        artifacts = self._required_evidence_artifacts(ir)
        raw_refs = self._promote_run_evidence(run_tmp, node_dir, artifacts)
        schema_ref = self._author_snapshot_schema(ir, node_dir)
        if schema_ref:
            raw_refs.append(schema_ref)
        # Per-case snapshot deliverable check (build_launch_request requires one
        # raw/state_snapshots/<case_id>.json per case). Compute a clear diagnostic
        # here so a misnamed/combined snapshot fails with an actionable cause rather
        # than the opaque determine_substep_status deliverable-presence fail. Read
        # the runner's THIS-attempt tmp output (fresh), not the promoted node dir.
        snapshot_gap = self._snapshot_deliverable_gap(
            run_tmp / "raw" / "state_snapshots", case_ids, artifacts)

        run_diag = _read_json(run_tmp / "diagnostics.json") or {}
        qc_diag = _read_json(qc_tmp / "diagnostics.json") or {}
        qc_status = self._author_quality_check(
            node_dir, run_diag, qc_diag, res_run.get("command_id"),
            res_qc.get("command_id"), "make_test", threads)

        (node_dir / "stdout.log").write_text(stdout, encoding="utf-8")
        (node_dir / "stderr.log").write_text(stderr, encoding="utf-8")

        # The repo revision THIS run's evidence (and, on the structural-failure branch below,
        # its `failure_excerpt`) was produced under. The dev `--resume` directive compares it
        # against the revision at resume time and declines to inject findings that a later
        # source change may have invalidated (B4). Stamped on the failing run rather than read
        # from `orchestration_meta.repo_revision`, which is frozen at the orchestration's first
        # start and would drift permanently once any commit lands mid-run.
        from tools.orchestration_runtime import _capture_repo_revision

        trial_meta = {
            "run_id": refs.run_id,
            "node_key": refs.node_key,
            "repo_revision": _capture_repo_revision(self.repo_root),
            "pipeline_id": refs.pipeline_id,
            "source_source_id": refs.source_id,
            "source_binary_id": refs.source_binary_id,
            "runner_command": shlex.join([exe, "--cases", self._rel(ir_spec), *case_ids]),
            "process_trace_ref": self._rel(cmd_log),
            "source_command_ref": {
                "run_program": {"tool_name": "run_program",
                                "command_id": res_run.get("command_id"),
                                "command_log_ref": self._rel(cmd_log)},
                "run_quality_checks": {"tool_name": "run_quality_checks",
                                       "command_id": res_qc.get("command_id"),
                                       "command_log_ref": self._rel(qc_cmd_log)},
            },
            "raw_artifact_refs": raw_refs,
            "environment": {
                "target_class": target_class,
                "backend": str(toolchain.get("backend") or "openmp"),
                "threads_per_rank": threads,
                "openmp_env": {"OMP_NUM_THREADS": str(threads), "OMP_THREAD_LIMIT": str(threads)},
            },
            "status": "pass" if qc_status == "pass" else "fail",
        }
        (node_dir / "trial_meta.json").write_text(
            json.dumps(trial_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        # 4. gates: artifact syntax + post_execute structural check.
        syn = subprocess.run(
            ["python3", "tools/check_artifact_syntax.py", "--format", "json",
             "--expect-top", "object",
             str(node_dir / "diagnostics.json"), str(node_dir / "perf.json"),
             str(node_dir / "quality_check.json")],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
        gate = subprocess.run(
            ["python3", "tools/validate_pipeline_semantics.py", "--stage", "post_execute",
             "--pipeline-root", refs.pipeline_ref, "--run-id", refs.run_id or ""],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False)
        structural_ok = (syn.returncode == 0 and gate.returncode == 0
                         and qc_status == "pass" and not snapshot_gap)
        if not structural_ok:
            # Structural content failure (bad/missing evidence): record it in
            # trial_meta.status (read by determine_substep_status) and return rc 0 so
            # run_phase routes it via the validate tables / diagnostician, NOT transport
            # fail_closed. No verdict.json is authored — classify_failure's execute branch
            # sees no failure_class and routes to Generate (regenerate the runner/code).
            block = ("\n[execute fail]\n" + syn.stdout + syn.stderr
                     + gate.stdout + gate.stderr)
            if snapshot_gap:
                block += "\n" + snapshot_gap
            # Actionable cause when the make-test candidate emitted no diagnostics/verdict:
            # the `test` target must invoke the runner with `--cases $(SPEC) $(CASES)` (the
            # runner requires `--cases` and aborts without it). run_program's diagnostics
            # being present while the candidate's is absent isolates the test-target form as
            # the cause rather than a buggy runner.
            if qc_status != "pass" and not qc_diag.get("verdict") and run_diag.get("verdict"):
                block += (
                    "\n[execute fail: quality_check] the make-test re-run emitted no "
                    "diagnostics.json/verdict while run_program's is present — the Makefile "
                    "`test`/`check` target must invoke the runner with `--cases $(SPEC) "
                    "$(CASES)` (the runner requires `--cases`); see "
                    "docs/workflow/RUNNER_OUTPUT_CONTRACT.md §5 / phase_04_validate.md §4-1.")
            stderr += block
            # Classify the structural failure for classify_failure's execute branch (B1): the
            # category selects the warm `("generate","reuse")` route out of
            # VALIDATE_EXECUTE_FAILURE_ROUTING and the (bounded) excerpt becomes the repair leaf's
            # findings, so the violation text that failed the run is what the leaf gets to fix.
            # Precedence is report-quality only (all three route identically): a gate/syntax
            # report is the most specific, a snapshot gap next, quality_check last. The runner
            # runtime-error branch above returns BEFORE any trial_meta is written — a MISSING
            # trial_meta is therefore the on-disk discriminator for that (cold-restart) kind.
            if syn.returncode != 0 or gate.returncode != 0:
                failure_category = "post_execute_violation"
            elif snapshot_gap:
                failure_category = "snapshot_deliverable_gap"
            else:
                failure_category = "quality_check_mismatch"
            trial_meta["status"] = "fail"
            trial_meta["failure_category"] = failure_category
            trial_meta["failure_excerpt"] = _execute_failure_excerpt(block)
            (node_dir / "trial_meta.json").write_text(
                json.dumps(trial_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            return {"returncode": 0, "stdout": stdout, "stderr": stderr}

        # 5. R2: the run is structurally valid (evidence present, post_execute gate + quality
        # check clean), so author verdict.json deterministically from the IR predicates +
        # diagnostics.json. A per-test predicate failure (physics_fail / structural_violation)
        # fails the execute substep WITHOUT spawning the judge leaf (the R2 cost lever) —
        # classify_failure reads verdict.json#failure_class to route it. An all-clean verdict
        # (self_verdict ∈ {pass, xfail}) leaves the execute substep passing; the judge then
        # authors semantic_review.json only.
        verdict_doc = self._author_execute_verdict(refs, ir, run_diag)
        if verdict_doc.get("self_verdict") == "fail":
            # Persist the failing predicate(s) as `failure_excerpt`, symmetric with the structural
            # branch above: a dev `--resume` after the `fail_closed` threads it into the reopened
            # Generate as repair findings (`_derive_dev_validate_execute_resume_directive`).
            # `failure_category` is deliberately NOT written — it keys
            # VALIDATE_EXECUTE_FAILURE_ROUTING, and classify_failure's no-verdict branch (B1) would
            # then read this run as a structural gate failure. The resume deriver takes the category
            # from the routing reason's suffix instead.
            block = _verdict_failure_report(verdict_doc)
            trial_meta["status"] = "fail"
            trial_meta["failure_excerpt"] = _execute_failure_excerpt(block)
            (node_dir / "trial_meta.json").write_text(
                json.dumps(trial_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            stderr += "\n" + block
        return {"returncode": 0, "stdout": stdout, "stderr": stderr}

    def _author_execute_verdict(self, refs: NodeRefs, ir: dict[str, Any],
                                run_diag: dict[str, Any]) -> dict[str, Any]:
        """R2: author verdict.json from ``io_contract.test_predicates`` + the runner's
        diagnostics.json (``run_diag``). Returns the authored doc. A missing / malformed
        predicate DSL (which the Compile-stage gate forbids) is authored as a
        ``structural_violation`` verdict; classify_failure's execute branch then routes it to the
        escalate diagnostician (prod) / fail_closed (dev) — the diagnostician can reopen Compile
        for the IR defect — rather than crashing execute."""
        from tools.verdict_evaluator import evaluate_verdict, PredicateError

        io_contract = (ir.get("io_contract") or {}) if isinstance(ir, dict) else {}
        predicates = io_contract.get("test_predicates") if isinstance(io_contract, dict) else None
        try:
            if not isinstance(predicates, list) or not predicates:
                raise PredicateError(
                    "io_contract.test_predicates missing/empty (Compile must author it)")
            doc = evaluate_verdict(predicates, run_diag,
                                   run_id=refs.run_id, node_key=refs.node_key)
        except Exception as exc:  # noqa: BLE001 - any evaluation failure is an IR/contract defect
            # Catch broadly (not just PredicateError): a malformed IR must always route via the
            # deterministic structural_violation path (escalate/fail_closed), never crash execute
            # into a blunt transport fail_closed. evaluate_verdict provably raises only
            # PredicateError today; catching Exception keeps that guarantee robust to evaluator
            # evolution (e.g. a future op that could raise TypeError/ZeroDivisionError).
            doc = {
                "node_key": refs.node_key,
                "run_id": refs.run_id,
                "self_verdict": "fail",
                "failure_class": "structural_violation",
                "per_test": [],
                "predicate_error": f"{type(exc).__name__}: {exc}"[:400],
            }
        self._write_run_node_meta(refs, "verdict.json", doc)
        return doc

    def run_substep(self, refs: NodeRefs, phase: str, substep: str | None,
                    repair: dict[str, str] | None = None,
                    resolved_dependencies: tuple[dict[str, str], ...] = (),
                    dependency_surface: tuple[dict[str, Any], ...] = ()) -> SubstepOutcome:
        # Certify the codex hooks feature BEFORE record_launch: this can fail closed
        # (SandboxEnforcementError) when the feature is uncertified, and doing it here —
        # ahead of allocating an arid / recording a durable launch — avoids orphaning a
        # recorded launch (phantom `child_running` active run) on that fail-closed path.
        # Memoized per orchestration (no-op after the first); spawn_leaf also calls it as a
        # safety net for the record-launch-less diagnostician leaf.
        self._ensure_codex_feature_cache()
        # Z2 pure-function producer (M-C): `generate.generate` on a claude M3c node under
        # executor=pure runs as a host-mediated pure function with its OWN spawn/validate/
        # repair/finalize/write loop (empty write authority; the host writes the bundle
        # artifacts after the child window closes). It does not share the generic leaf loop
        # below (no allowed_output_paths, no determine_substep_status-before-finalize).
        if self._pure_leaf_substep(refs, phase, substep):
            if substep == "verify":
                # Z2 pure reviewer (M-D): its own spawn/validate/repair/finalize loop, host-authors
                # source_meta.json from the returned verdict after the child window closes.
                return self._run_pure_verify_substep(
                    refs, phase, substep, resolved_dependencies)
            return self._run_pure_generate_substep(
                refs, phase, substep, repair, resolved_dependencies)
        # Resolve the warm-resume decision BEFORE building the request so the slim-vs-full
        # prompt choice (build_launch_request) matches what record_launch persists and what
        # spawn_leaf sends below. None => cold launch (full prompt). Deterministic substeps
        # run in-process (no leaf to resume), so skip the resolver entirely — it keeps the
        # session-transcript glob and the `resume_session_unavailable` emit side-effect-free
        # for them even if a reuse repair ever reaches one.
        deterministic = self._is_deterministic_substep(phase, substep)
        resume_session_id = (None if deterministic
                             else self._resolve_reuse_resume(repair, phase, substep))
        # Slim repair turn is always used when a warm resume actually fires (build_launch_request
        # further requires a findings excerpt to be present, so in practice slim is scoped to the
        # deterministic-gate reopens — lint/static/compile_static — which carry one; a warm reuse
        # without findings, e.g. a cross-phase code repair, still re-sends the full prompt).
        warm_resume = resume_session_id is not None
        # R5: resolve a certified sibling exemplar for the authoring leaf only (generate.generate),
        # and NOT on a warm-resume slim repair (the resumed leaf already has it). build_launch_request
        # attaches it solely for generate.generate; other substeps ignore the value.
        exemplar = (self._resolve_exemplar(refs)
                    if (phase == "generate" and substep == "generate"
                        and not deterministic and not warm_resume) else None)
        # Bounded transient-transport retry (see _RETRYABLE_LEAF_INFRA_TAGS): a leaf whose
        # connection died mid-response is re-launched in place rather than fail-closing the whole
        # run for a human to `--resume` hours later. The loop is closed INSIDE run_substep on
        # purpose — run_phase's `outcomes` list is positionally aligned with SUBSTEPS[phase]
        # (_producer_arid / _judge_attempt_count index into it), so an extra outcome per retry
        # would corrupt it. Everything attempt-invariant (codex cache, warm-resume target,
        # exemplar) is resolved ABOVE the loop; only the launch itself repeats.
        attempt = 0
        usage_waits = 0
        while True:
            child_arid = self.new_agent_run_id()
            request = build_launch_request(
                refs, step=phase, substep=substep,
                orchestration_id=self.orchestration_id,
                orchestration_agent_run_id=self.orchestration_agent_run_id,
                child_agent_run_id=child_arid,
                agent_model=self.agent_model, workflow_mode=self.workflow_mode,
                case_ids=self.read_case_ids(refs) if phase == "validate" else (),
                evidence_artifacts=self._read_evidence_artifacts(refs) if phase == "validate"
                else ("state_snapshots",),
                # build's allowed_output_paths binary path = the imposed canonical exe name.
                exe_name=(self._resolve_exe_name(refs) if phase == "build" else None),
                # leaf generate: src/Makefile is conductor-authored, so drop it from the leaf's
                # allowed_output_paths (it must not author it).
                makefile_host_authored=(
                    phase == "generate" and self._conductor_authors_makefile(refs)),
                # leaf generate: on an M3c node the runner is conductor-rendered, so the leaf
                # authors <spec_id>_checks.f90 instead of <spec_id>_runner.f90
                # (build_launch_request swaps it).
                runner_host_authored=(
                    phase == "generate" and self._conductor_authors_runner(refs)),
                repair=repair,
                resolved_dependencies=resolved_dependencies,
                dependency_surface=dependency_surface,
                exemplar=exemplar,
                warm_resume=warm_resume,
            )
            rec = self.record_launch(child_arid, request)
            # Capture the launch instant so a producer substep only passes on outputs
            # (re)written during this child window, not stale files from a prior attempt.
            # Re-taken per attempt: a half-written artifact left by the leaf that died is older
            # than the retry's window, so it cannot fake the retry's pass.
            launched_at = time.time()
            if deterministic:
                # Non-LLM step: run the body in-process and play the child-return ourselves
                # (no `claude -p` leaf). record_launch above + record-child-return here +
                # finalize_child below keep the executor a normal step/substep agent_run_id,
                # so the integrity validators pass unchanged.
                proc = self._run_deterministic_substep(refs, phase, substep, child_arid, request)
                self._persist_leaf_output(child_arid, proc, prefix="deterministic")
                token = self.read_parent_return_token(child_arid)
                self.runtime([
                    "record-child-return", *self._oid_args(),
                    "--agent-run-id", child_arid, "--return-token", token,
                ])
            else:
                # resume_session_id was resolved before build_launch_request (above) so the
                # slim-vs-full prompt selection is consistent with what is actually sent here.
                # A retry keeps the SAME resume target: the producer session is idempotent to
                # fork, and a cold retry would silently drop the slim turn's findings excerpt
                # (build_launch_request only sends it when warm_resume is True).
                proc = self.spawn_leaf(
                    rec["launch_prompt_text"], self._child_env(child_arid),
                    session_id=child_arid, resume_session_id=resume_session_id,
                    child_arid=child_arid)
                # Persist the leaf's verbatim stdout/stderr durably (every run, pass or
                # fail) so the LLM's actual response — including an infra failure message
                # such as a token-limit abort — is never lost. These conductor-side writes
                # land in the child's bookkeeping dir (not its allowed_output_paths) and are
                # not hook-guarded, so they don't trip the output-manifest guard. Each attempt
                # has its own arid, so a retried substep keeps the dead attempt's log too.
                self._persist_leaf_output(child_arid, proc)
                token = self.read_parent_return_token(child_arid)
                # G3 split: the `--stage pre_judge` gate that used to run here inline after the
                # judge leaf is now the deterministic `post_judge` substep
                # (Conductor._post_judge_inproc), so the judge leaf is a pure LLM semantic pass and
                # run_substep no longer runs any gate for it.
            status, output_refs = self.determine_substep_status(
                refs, phase, substep, request["allowed_output_paths"], min_mtime=launched_at)
            # A nonzero leaf exit (crash / transport failure) fails the substep even if
            # the expected artifacts happen to exist (e.g. stale outputs from a prior
            # attempt) — the process return code gates artifact-based success.
            # EVERY non-pass status must carry a result_summary: a failed payload has no
            # output_refs, so without one _validate_agent_summary_text rejects the
            # auto-generated agent.summary.txt and finalize-child crashes. A nonzero exit
            # uses the leaf's stderr tail; a returncode-0 content failure (verify/judge
            # fail, missing deliverable) uses a generic tag — the detailed diagnostics
            # live in the canonical artifacts (ir_meta/verdict.json) that classify_failure
            # reads for routing.
            result_summary: str | None = None
            infra_error: tuple[str, str] | None = None
            if proc.returncode != 0:
                status = "fail"
                result_summary = self._leaf_failure_summary(proc)
                infra_error = _classify_leaf_infra_error(proc.stderr or "", proc.stdout or "")
            elif status != "pass":
                result_summary = f"substep_fail: {phase}" + (f".{substep}" if substep else "")
            reply = f"status: {status}\noutput_refs: {len(output_refs)}\nleaf rc={proc.returncode}"
            if result_summary:
                reply += f"\nresult_summary: {result_summary}"
            # Terminalize the attempt FIRST, and only then tombstone it. Both orderings have a
            # cost and this one is the survivable one:
            #   - `finalize_child` closes the child's write window: `record-agent-run` re-walks the
            #     live workspace and diffs it against the baseline `record-launch` took, and
            #     ANY path outside the child's write_roots is an unauthorized write. The tombstone
            #     writes `<orch_root>/reopen/{superseded_runs.json,reopen_log.jsonl}`, which is NOT
            #     runtime-ignored (unlike launches/ agents/ violations/), so tombstoning inside the
            #     window would attribute the conductor's own two writes to the dying leaf: the
            #     attempt is rejected as an unauthorized write, finalize-child exits nonzero, and
            #     the retry this function exists to perform never launches at all. (The three
            #     pre-existing tombstone call sites all sit outside any open child window, which is
            #     why they never hit this.)
            #   - tombstoning a leaf that ALSO made a genuine unauthorized write would hide it from
            #     `_derive_unauthorized_write_resume_directive`, which skips superseded candidates
            #     — leaving the operator in the `resume_reopen_no_valid_trigger` dead end.
            # The residual: a host crash BETWEEN the two writes leaves a terminal, un-vouched,
            # un-tombstoned arid, which a resume cannot repair on its own (it needs a manual
            # `add-superseded-runs`). That window is the same one the three pre-existing tombstone
            # sites carry, and it is two subprocess calls wide.
            self.finalize_child(
                child_arid, token, reply,
                self._agent_run_json(refs, phase, substep, child_arid, status,
                                     output_refs, result_summary))
            retryable = (
                not deterministic
                and proc.returncode != 0
                and infra_error is not None
                and infra_error[0] in _RETRYABLE_LEAF_INFRA_TAGS
                and attempt < MAX_LEAF_TRANSIENT_RETRIES)
            if not retryable:
                # --wait-usage-reset (opt-in): a usage limit is normally terminal (fail_closed for
                # a manual post-reset --resume). When the operator opted in AND the leaf's terminal
                # line carried a resolvable reset (a machine `|<epoch>` or a TZ-anchored human reset),
                # wait it out in place and re-launch — a same-run, substep-granular resume instead of
                # a next-day fresh run. Bounded by MAX_USAGE_LIMIT_WAITS, a budget DISTINCT from the
                # transient-retry budget above; a usage limit is never a transient-retry tag, so this
                # cannot compound with it.
                if (not deterministic and infra_error is not None
                        and infra_error[0] == "llm_usage_limit"):
                    # allow_envelope=False, not a `_pure_leaf_substep` call: a pure substep never
                    # reaches this loop (the dispatch above returns in BOTH branches), so the
                    # predicate is provably False here — evaluating it would only re-read the
                    # node's IR on the failure path and imply this loop can carry a pure leaf.
                    # An agentic leaf is launched without `--output-format json`, so a JSON line on
                    # ITS stdout is model-written and must never be unwrapped.
                    plan = self._usage_reset_wait_plan(
                        proc, usage_waits, node_key=refs.node_key, step=phase,
                        substep=substep, dead_agent_run_id=child_arid,
                        evidence=infra_error[1], allow_envelope=False)
                    if plan is not None:
                        self._wait_for_usage_reset(
                            node_key=refs.node_key, step=phase, substep=substep,
                            dead_agent_run_id=child_arid, wait_seconds=plan.wait_seconds,
                            reset_epoch=plan.reset_epoch, reset_source=plan.reset_source,
                            window=plan.window, wait_attempt=usage_waits + 1)
                        usage_waits += 1
                        continue
                # `attempts` counts EVERY launch (transient retries + usage waits + this one), so a
                # fail_closed after a wait still reports the honest launch count in `[attempts=N]`.
                return SubstepOutcome(child_arid, status, output_refs, proc.returncode,
                                      infra_error, launched_at, attempt + usage_waits + 1)
            tag = infra_error[0]
            max_attempts = MAX_LEAF_TRANSIENT_RETRIES + 1
            # A dead attempt is never vouched by a step_result (only the surviving attempt's arid
            # goes into substep_agent_run_ids), and an un-vouched TERMINAL arid is an orphan the
            # completion check rejects at the end of an otherwise-passing run. The FINAL attempt of
            # an exhausted budget is tombstoned instead by run_phase's transport branch — between
            # the two, every arid this loop mints is covered. Idempotent (a set union), so a
            # re-tombstone is harmless.
            self._add_superseded_run_ids(
                [child_arid],
                reason=(f"leaf_transient_retry_orphan: {tag}; "
                        f"attempt={attempt + 1}/{max_attempts}"))
            delays = _LEAF_RETRY_BACKOFF_SECONDS.get(tag, _DEFAULT_LEAF_RETRY_BACKOFF)
            delay = delays[min(attempt, len(delays) - 1)]
            self.emit("leaf_transient_retry", node_key=refs.node_key, step=phase,
                      substep=substep, tag=tag, attempt=attempt + 1,
                      max_attempts=max_attempts, backoff_seconds=delay,
                      dead_agent_run_id=child_arid, evidence=infra_error[1])
            self._sleep_backoff(delay)
            attempt += 1

    def _sleep_backoff(self, seconds: float) -> None:
        """Wait out a transient LLM-infrastructure failure before re-launching the leaf.

        The conductor's ONE and ONLY sleep, isolated in a method so tests can replace it (they
        assert the schedule without waiting it out) and so a future reader can see at a glance
        that the loop does not otherwise block."""
        time.sleep(seconds)

    def _run_usage_probe(self) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
        """`(rows, meta)` from a HOST-side `claude --output-format json -p /usage` — the PRIMARY
        reset source for `--wait-usage-reset` (see `_parse_usage_probe_rows`). `rows` is None when
        the probe produced nothing usable, and `meta` then names the outcome; on success `meta`
        carries only the timing + excerpt and the caller decides the outcome from the rows.

        Run by the CONDUCTOR, not a leaf: no untrusted prompt is involved, so it needs no bwrap
        (same trust model as the preflight backend probes in `orchestration_runtime`) and its output
        needs none of the abort-shape anti-forgery clauses the stdout scrape carries. It spends 0
        tokens — `/usage` is a local slash command that answers at `num_turns: 0`.

        The argv base is `leaf_command`'s (`--llm-command` wrapper if configured, else the bare
        backend), so the probe interrogates the executable the LEAF actually uses rather than a
        hardcoded `claude` — the same reasoning as `_ensure_codex_feature_cache`.

        The `result` is trusted ONLY when the envelope proves it came from the BUILT-IN `/usage`
        slash command, not from a model turn. `--output-format json -p /usage` on the real CLI
        answers at `num_turns == 0` (a local command, 0 tokens); an older or `--llm-command`-wrapped
        binary that does not recognise `/usage` would instead run it as an ordinary PROMPT, and the
        model's reply — attacker-uncontrolled but still model-authored, and free to contain
        window-shaped text — would arrive at `num_turns >= 1` (or with the field absent). Requiring
        `num_turns == 0` keeps that model output from being read as trusted usage data and arming a
        multi-hour wait; a response that fails it declines to the scrape, where the abort-shape
        clauses independently decide. This is the probe's equivalent of the scrape's forgery guard:
        the scrape distrusts a leaf's stdout, and here the conductor distrusts anything the probe's
        own model produced.

        NEVER raises: a timeout, a missing binary, a nonzero exit, unparseable output, an envelope
        that is itself an error, or one that consumed a model turn all return `(None, meta)` and the
        caller falls back to the scrape, i.e. to exactly today's behavior. An `is_error` envelope is
        not an exception either — it is the very field evidence the open question needs, so its raw
        text is kept in `excerpt` and reported as `probe_unparseable`."""
        started = time.monotonic()

        def _meta(outcome: str | None, excerpt: str = "") -> dict[str, Any]:
            meta: dict[str, Any] = {
                "duration_ms": int((time.monotonic() - started) * 1000),
                # Same backslashreplace round-trip the decline excerpt uses, so `emit`'s
                # `json.dumps(..., ensure_ascii=False)` can always encode what it is handed.
                "excerpt": (" ".join((excerpt or "").split())[:_USAGE_PROBE_EXCERPT_MAX_CHARS]
                            .encode("utf-8", "backslashreplace").decode("utf-8")),
            }
            if outcome is not None:
                meta["outcome"] = outcome
            return meta

        if self.backend != "claude":
            return None, _meta("backend_unsupported")
        base = shlex.split(self.llm_command) if self.llm_command.strip() else [self.backend]
        argv = [*(base or [self.backend]), "--output-format", "json", "-p", "/usage"]
        try:
            # `env=self.env`, matching every other conductor subprocess: the probe must query the
            # SAME account/endpoint context the leaf runs under, or its reset instant is for the
            # wrong quota. `self.env` is the leaf base env (auth, PATH, workflow mode); a bare
            # `os.environ` would diverge the moment a per-run endpoint/account override lands there
            # and not in the process environment — and this is the trusted PRIMARY source, so a
            # confidently-wrong instant here arms a multi-hour wait ahead of the scrape.
            proc = subprocess.run(argv, cwd=self.repo_root, env=self.env, text=True,
                                  capture_output=True, check=False,
                                  timeout=USAGE_PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return None, _meta("probe_timeout")
        except Exception as exc:      # missing binary, decode failure, OS error
            return None, _meta("probe_error", f"{type(exc).__name__}: {exc}")
        if proc.returncode != 0:
            return None, _meta("probe_error", proc.stderr or proc.stdout or "")
        try:
            doc = json.loads(proc.stdout or "")
        except Exception:
            return None, _meta("probe_unparseable", proc.stdout or "")
        result = doc.get("result") if isinstance(doc, dict) else None
        # Trust the `result` only as the BUILT-IN `/usage` command's output: `is_error` false, a
        # string result, AND `num_turns == 0` (no model turn was consumed). A binary that ran
        # `/usage` as a prompt yields model-authored text at `num_turns >= 1` — reject it so it is
        # never parsed into trusted rows. The raw envelope is kept as the excerpt for diagnosis.
        if (not isinstance(doc, dict) or doc.get("is_error") is True
                or doc.get("num_turns") != 0 or not isinstance(result, str)):
            return None, _meta("probe_unparseable", proc.stdout or "")
        rows = _parse_usage_probe_rows(result, time.time())
        if not rows:
            # Answered, but named no window this parser recognises — a wording change, or (the open
            # question) whatever `/usage` says once the quota is gone. The excerpt is the record.
            return None, _meta("probe_unparseable", result)
        return rows, _meta(None, result)

    def _usage_reset_wait_plan(self, proc: ProcResult, waits_done: int, *,
                               node_key: str, step: str, substep: str | None,
                               dead_agent_run_id: str, evidence: str,
                               allow_envelope: bool) -> UsageResetWaitPlan | None:
        """The `UsageResetWaitPlan` for a usage-limit-killed leaf that should be waited out and
        re-launched in place, or None to keep the current fail_closed behavior.

        Called only for an `llm_usage_limit`-tagged death (the call-site guard). Two reset sources,
        tried in order (the probe resolves its rows against its own `time.time()`; the SCRAPE path
        and the `remaining` math share the single `now` read here, so the scrape and the wait
        arithmetic see one instant):

        1. PROBE (`reset_source="probe"`) — the host asks the CLI (`_run_usage_probe`), and the row
           whose window the dead leaf's own abort line NAMES arms the wait, provided that window is
           actually exhausted (`_probe_reset_for_evidence`). Host-authored, dated, and window-named.
        2. SCRAPE — the dead leaf's terminal usage-limit line: the MACHINE epoch
           (`_parse_usage_reset_epoch`, `reset_source="scrape_machine"`) then the HUMAN TZ-anchored
           form (`_parse_usage_reset_human`, `reset_source="scrape_human"`), which is what the real
           CLI emits and therefore what armed the wait before the probe existed.

        The probe is PRIMARY and the scrape stays the fallback, so every probe failure — including
        the unverified case where `/usage` itself is refused once the quota is gone — degrades to
        exactly the previous behavior rather than to no wait at all. Every probe attempt emits
        `leaf_usage_limit_probe` with its raw outcome, which is how the next real incident answers
        that open question without a purpose-built experiment.

        None (fall back to fail_closed) whenever ANY precondition misses — the flag is off; the
        per-substep wait budget is spent; neither source resolved an instant (the probe declined AND
        the terminal line carries neither a machine epoch nor a resolvable TZ-anchored human reset —
        a human reset with no IANA TZ / no time-of-day is not guessed at, and a usage limit sharing
        the leaf's untrusted stdout with any other output does not arm the wait, see
        `_sole_content_usage_limit_line`); or the reset lies further out than
        MAX_USAGE_LIMIT_WAIT_SECONDS. That cap applies to BOTH sources unchanged: a weekly reset days
        out is still not something to sit on, the difference being that a probe-sourced decline now
        names the window it declined instead of inferring one. Every decline EXCEPT the flag-off
        short-circuit emits `leaf_usage_limit_wait_declined` with a reason, so a run that opted in
        but did not wait is greppable — the invisibility of this decline is exactly what masked the
        machine-vs-human envelope mismatch.

        The wait sleeps slightly PAST the reset (USAGE_LIMIT_WAIT_MARGIN_SECONDS) so the re-launch's
        preflight live-probe finds the window actually open; a reset already in the past (a
        minutes-stale human message) waits only that margin."""
        if not self.wait_usage_reset:
            return None

        # The line the wait is actually DECIDED from — the classifier's own tagged line, except for
        # the enveloped shape where that line is raw JSON clipped at 160 chars (it truncates
        # mid-`result` and hides the wording). Computed once and used for BOTH the decline excerpt
        # and the probe's window match, so the operator-facing evidence and the window the code
        # matched on are the same text. The override is narrow in both uses: stdout must be the
        # stream the resolver actually consulted (stderr named no usage limit) AND the text must
        # pass the shape check — otherwise it would quote, and match on, a `result` the decision was
        # never made from.
        inner = None
        if allow_envelope and _stream_terminal_usage_limit_line(proc.stderr or "") is None:
            inner = _sole_content_usage_limit_line(proc.stdout or "", allow_envelope=True)
        decision_line = " ".join(inner.split())[:160] if inner else evidence

        def _decline(reason: str, *, window: str | None = None,
                     reset_source: str | None = None) -> None:
            # The arid and the offending line are what the NEXT unrecognised envelope will be
            # diagnosed from: without them the operator gets a bare `no_reset_time` and has to
            # guess which `agents/<arid>/dialogs/` to open — and this decline being uninformative
            # is what let the stderr-only bug survive two rounds of investigation. `decision_line`
            # is the CLASSIFIER's own line (`infra_error[1]`), i.e. the line the `llm_usage_limit`
            # tag came from — not a stream tail, which on a leaf with noisy stderr would quote
            # something the decision was never made from — with the enveloped-shape override applied
            # above. Same field the sibling `leaf_transient_retry` emits.
            # `json.loads` turns an escaped lone surrogate (`\ud800`) in the leaf's `result` into a
            # REAL surrogate, which `emit`'s `json.dumps(..., ensure_ascii=False)` then cannot encode
            # on write — turning this fail_closed decline into a conductor crash. Same
            # backslashreplace round-trip the other leaf-derived excerpts use. (The classifier line
            # comes from already-decoded process output and cannot carry one, but sanitizing both
            # branches keeps the emit unconditionally safe.)
            self.emit("leaf_usage_limit_wait_declined", node_key=node_key, step=step,
                      substep=substep, reason=reason, dead_agent_run_id=dead_agent_run_id,
                      window=window, reset_source=reset_source,
                      evidence=decision_line.encode("utf-8", "backslashreplace").decode("utf-8"))

        if waits_done >= MAX_USAGE_LIMIT_WAITS:
            # Before the probe on purpose: a spent budget cannot wait whatever `/usage` answers, so
            # probing here would spend a subprocess (and its timeout) on a decision already made.
            _decline("budget_spent")
            return None

        rows, probe_meta = self._run_usage_probe()
        probe_epoch: int | None = None
        window: str | None = None
        probe_outcome = probe_meta.get("outcome")
        if rows is not None:
            probe_outcome, probe_epoch, window = _probe_reset_for_evidence(decision_line, rows)
        # Emitted for EVERY attempt, resolved or not: this event is the field evidence that answers
        # whether `/usage` still responds once the quota is exhausted — the one open question the
        # design could not settle offline. `windows` carries the parsed rows so a decline can be
        # re-judged after the fact; `excerpt` carries the raw text when there were none.
        self.emit("leaf_usage_limit_probe", node_key=node_key, step=step, substep=substep,
                  outcome=probe_outcome, windows=rows or [], matched_window=window,
                  reset_epoch=probe_epoch, duration_ms=probe_meta.get("duration_ms"),
                  excerpt=probe_meta.get("excerpt", ""), dead_agent_run_id=dead_agent_run_id)

        now = time.time()
        if probe_epoch is not None:
            reset_epoch: int | None = probe_epoch
            reset_source = "probe"
        else:
            window = None       # the scrape resolves no window name; do not carry the probe's
            reset_epoch = _parse_usage_reset_epoch(proc.stderr or "", proc.stdout or "",
                                                   allow_envelope=allow_envelope)
            reset_source = "scrape_machine"
            if reset_epoch is None:
                reset_epoch = _parse_usage_reset_human(proc.stderr or "", now, proc.stdout or "",
                                                       allow_envelope=allow_envelope)
                reset_source = "scrape_human"
        if reset_epoch is None:
            _decline("no_reset_time")
            return None
        remaining = reset_epoch - now
        if remaining > MAX_USAGE_LIMIT_WAIT_SECONDS:
            _decline("over_6h_cap", window=window, reset_source=reset_source)
            return None
        wait_seconds = max(0.0, remaining) + USAGE_LIMIT_WAIT_MARGIN_SECONDS
        return UsageResetWaitPlan(wait_seconds, reset_epoch, reset_source, window)

    def _wait_for_usage_reset(self, *, node_key: str, step: str, substep: str | None,
                              dead_agent_run_id: str, wait_seconds: float, reset_epoch: int,
                              reset_source: str, window: str | None,
                              wait_attempt: int) -> None:
        """Tombstone the usage-limit-killed attempt, announce the wait, and sleep out the reset
        before the caller re-launches the substep.

        The dead attempt is finalized (terminalized) by the caller BEFORE this runs, so the
        tombstone lands OUTSIDE the child FS-diff window (same ordering invariant the transient
        retry and the pure loops rely on). Its orphan arid — terminalized but never vouched by a
        step_result — would otherwise fail the completion check on the surviving pass, so it is
        superseded here; idempotent (a set union) with the pure loop's later per-attempt tombstone.
        The lone `_sleep_backoff` is reused so tests stub one sleep and this loop stays the
        conductor's only block."""
        self._add_superseded_run_ids(
            [dead_agent_run_id],
            reason=f"leaf_usage_limit_wait_orphan: attempt={wait_attempt}")
        self.emit("leaf_usage_limit_wait", node_key=node_key, step=step,
                  substep=substep, reset_epoch=reset_epoch, wait_seconds=wait_seconds,
                  reset_source=reset_source, window=window,
                  wait_attempt=wait_attempt, dead_agent_run_id=dead_agent_run_id)
        self._sleep_backoff(wait_seconds)

    def _persist_leaf_output(self, child_arid: str, proc: ProcResult,
                             prefix: str = "leaf") -> None:
        """Write the leaf process stdout/stderr to the child's dialogs dir."""
        dialogs = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                   / "agents" / child_arid / "dialogs")
        dialogs.mkdir(parents=True, exist_ok=True)
        (dialogs / f"{prefix}.stdout.log").write_text(proc.stdout or "", encoding="utf-8")
        (dialogs / f"{prefix}.stderr.log").write_text(proc.stderr or "", encoding="utf-8")

    @staticmethod
    def _leaf_failure_summary(proc: ProcResult) -> str:
        """A terse one-line reason from a failed leaf's output tail (stderr first,
        else stdout), bounded so the reply/summary stays well under the budget.

        An LLM-infrastructure marker (usage limit, rate limit, overload) found in EITHER stream is
        PREPENDED to that tail, never substituted for it: the tail is the leaf's real error and
        must survive, while a marker that landed on stdout would otherwise be buried under a noisy
        stderr. A misfiring classifier can then only add noise, never destroy evidence."""
        infra = _classify_leaf_infra_error(proc.stderr or "", proc.stdout or "")
        tail = " ".join((proc.stderr.strip() or proc.stdout.strip() or "")[-400:].split())
        parts = [f"leaf_exit={proc.returncode}"]
        if infra is not None:
            parts.append(f"{infra[0]}: {infra[1]}")
        if tail:
            parts.append(tail)
        return "; ".join(parts)

    # -- phase + conduct ------------------------------------------------------

    def _completed_producer_arid(self, node_key: str, phase: str,
                                 executor_arid: str | None) -> str | None:
        """The producing substep arid of an already-completed phase, read from its
        checkpointed step_result (recovers a repair target when resume skips it)."""
        if not executor_arid:
            return None
        sr = _read_json(
            self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
            / "steps" / node_key_safe(node_key) / phase / executor_arid / "step_result.json")
        if not isinstance(sr, dict):
            return None
        subs = sr.get("substep_agent_run_ids")
        if isinstance(subs, list) and subs:
            return subs[0]
        return sr.get("executor_agent_run_id")

    def _ensure_fresh_producer_id(self, refs: NodeRefs, phase: str) -> None:
        """If a producing phase's output already exists (a prior attempt or a
        cross-phase reopen re-run), allocate a fresh producer id so the re-run
        writes to a new location instead of overwriting prior artifacts (which also
        trips create-form guarded writes). No-op on the first run of a phase."""
        date = _today()
        if phase == "compile":
            if (self.repo_root / refs.ir_ref).exists():
                safe, slug = node_key_safe(refs.node_key), _slug_of(refs.spec_id)
                seq = _next_seq(self.repo_root / "workspace" / "ir" / safe, f"{slug}_{date}")
                refs.ir_id = f"{slug}_{date}_{seq}"
                self.reserve_root(refs.node_key, "compile", refs.ir_id,
                                  self.orchestration_agent_run_id)
        elif phase == "generate":
            if (self.repo_root / refs.source_dir()).exists():
                seq = _next_seq(self.repo_root / refs.pipeline_ref / "source", f"src_{date}")
                refs.source_id = f"src_{date}_{seq}"
        elif phase == "build":
            if (self.repo_root / refs.binary_dir()).exists():
                seq = _next_seq(self.repo_root / refs.pipeline_ref / "binary", f"bin_{date}")
                refs.binary_id = f"bin_{date}_{seq}"
                refs.source_binary_id = refs.binary_id
        elif phase == "validate":
            if (self.repo_root / refs.pipeline_ref / "runs" / str(refs.run_id)).exists():
                seq = _next_seq(self.repo_root / refs.pipeline_ref / "runs", f"run_{date}")
                refs.run_id = f"run_{date}_{seq}"

    def _repair_payload(self, decision: RouteDecision, target_arid: str | None,
                        findings: str | None = None) -> dict[str, str]:
        payload = {
            "issue_severity": "major",
            "repair_strategy": decision.repair_strategy or "restart",
            "repair_target_agent_run_id": target_arid or "none",
            "repair_reason": decision.reason or "route_repair",
        }
        # The failing gate's findings excerpt, threaded to the (warm) repair leaf so it can fix
        # the exact reported lines instead of re-discovering them. Only carried for the reasons
        # _read_repair_findings recognizes (the deterministic gates + a structural
        # validate.execute failure); empty otherwise.
        if findings and findings.strip():
            payload["repair_findings"] = findings.strip()
        return payload

    def _read_repair_findings(self, refs: NodeRefs, reason: str | None,
                              phase: str | None = None) -> str | None:
        """The failing artifact's finding text to inject into the (warm/slim) repair, selected
        by the route reason:
          `gate_*`           -> source/gate_meta.json#failure_excerpt (unioned checker findings)
          `compile_static_*` -> ir/compile_static_meta.json#failure_excerpt
          `verify_*`         -> the phase's verify meta #last_fail_reason
                                (compile -> ir/ir_meta.json, generate -> source/source_meta.json)
          `validate_execute_<category>` (category in VALIDATE_EXECUTE_FAILURE_ROUTING)
                             -> runs/<run_id>/trial_meta.json#failure_excerpt
        Read at the conduct reopen point where `refs` still names the FAILED artifact (rotation
        to the fresh id happens later, inside run_phase -> _ensure_fresh_producer_id). Returns
        None when unavailable so the repair simply falls back to the full prompt."""
        r = (reason or "")
        field = "failure_excerpt"
        # compile_static_ is checked before gate_ for clarity; the two share no prefix, so order
        # is not load-bearing.
        if r.startswith("compile_static_"):
            meta_path = self.repo_root / refs.ir_ref / "compile_static_meta.json"
        elif r.startswith("gate_"):
            # The Generate.gate union verdict: gate_meta.json#failure_excerpt already composes
            # the per-checker sections ([syntax]/[lint]/[static]) in canonical order.
            meta_path = self.repo_root / refs.source_dir() / "gate_meta.json"
        elif r.startswith(GENERATE_BUNDLE_REASON_PREFIX):
            # Z2 pure producer: the exhausted bundle repair's terminal category/excerpt.
            meta_path = self.repo_root / refs.source_dir() / "bundle_meta.json"
        elif r.startswith("verify_"):
            # The verify substep records its finding in the phase's meta `last_fail_reason`.
            field = "last_fail_reason"
            meta_path = (self.repo_root / refs.ir_ref / "ir_meta.json" if phase == "compile"
                         else self.repo_root / refs.source_dir() / "source_meta.json")
        elif (r.startswith(VALIDATE_EXECUTE_REASON_PREFIX)
              and r[len(VALIDATE_EXECUTE_REASON_PREFIX):] in VALIDATE_EXECUTE_FAILURE_ROUTING):
            # A structural validate.execute failure (B1). Matched on the CATEGORY suffix, not the
            # prefix: the cold-restart `validate_execute_fail` and the per-test predicate reasons
            # share the prefix and must NOT pick up an excerpt.
            meta_path = self.repo_root / refs.run_node_dir() / "trial_meta.json"
        else:
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        excerpt = meta.get(field) if isinstance(meta, dict) else None
        return excerpt if isinstance(excerpt, str) and excerpt.strip() else None

    # -- Validate.judge conductor-owned pre_judge gate (G3) --------------------
    # The purely-structural `--stage pre_judge` gate (orchestration-record integrity +
    # the cross-pipeline dependency DAG) used to run INSIDE the LLM judge leaf as its final
    # step. It is hoisted to the conductor as two deterministic substeps wrapping the judge,
    # mirroring the G1/G2 deterministic-gate hoists one and two phases up:
    #   - pre_judge  (`_pre_judge_inproc`, index 0): before running execute or spawning a COLD
    #     judge, fail fast when a --with-deps dependency closure is not built+validated in its
    #     own pipeline (via `_judge_pre_spawn_dag_block`). A failure is fail_closed.
    #   - post_judge (`_post_judge_inproc`, index 3): after the judge returns its verdict, run
    #     `--stage pre_judge` and record `post_judge_meta.json` with a severity `disposition`;
    #     a recoverable (leaf/judge-authored) violation warm-resumes the judge, an integrity
    #     violation is fail_closed.
    # The judge leaf itself invokes no validator gate (ALLOWED_VALIDATE_PIPELINE_STAGES for
    # all three of pre_judge/judge/post_judge == frozenset()), so it is a pure LLM semantic pass.

    def _judge_pre_spawn_dag_block(self, refs: NodeRefs) -> str | None:
        """Pre-spawn Validate.judge dependency-DAG readiness (multi-node closures only).

        The `--with-deps` model builds+validates each dependency node in its OWN separate
        pipeline, then runs the dependent. Before spawning a cold judge (and before even
        running execute), verify every closure node has its own fully built+validated
        pipeline. Returns a human-readable excerpt when some closure node is not ready
        (caller fails fast); None for a ready closure OR a single-node run (empty closure,
        zero overhead — the common case).

        Provably a STRICT SUBSET of the post-gate: it derives the closure from the SAME
        source and normalization the post-gate's pre_judge DAG check uses
        (the conductor-authored sidecar `dependency_graph.json`'s `all_nodes` -> normalized
        `<kind>/<spec_id>` tokens via `_dependency_expected_node_keys`) and consults the SAME
        cross-pipeline predicate
        (`_closure_node_validated_in_own_pipeline`), so anything blocked here would also
        fail pre_judge — it never fails a run the post-gate would pass; it only saves the
        wasted execute+judge cost when the closure is genuinely incomplete. Reading
        `all_nodes` directly (not `_dependency_closure_nodes`) also skips that helper's L6
        diamond guard, which is a Build/Model-B staging concern irrelevant to DAG readiness
        (and would otherwise mis-raise for a c/cpp/mixed node at validate time)."""
        from tools.validate_pipeline_semantics import (
            _closure_node_validated_in_own_pipeline,
            _dependency_expected_node_keys, _normalize_node_key_token,
            _read_dependency_graph_sidecar)
        # The closure (`all_nodes`) is read from the conductor-authored sidecar
        # <ir_ref>/dependency_graph.json, not the IR (the derived graph moved there). When the
        # sidecar is absent (a resumed pre-sidecar node whose compile predates this change, or
        # a corrupt/missing graph), fall back to the IR `dependency` block so a node that still
        # declares `direct_deps` is NOT waved through the readiness gate with an empty closure:
        # `_dependency_expected_node_keys` derives the closure from `direct_deps` when
        # `all_nodes` is absent, keeping this fail-closed. A genuine leaf (empty direct_deps and
        # a leaf `all_nodes=[self]`) still yields an empty closure -> None (zero overhead).
        graph = _read_dependency_graph_sidecar(self.repo_root, refs.ir_ref)
        if not isinstance(graph, dict):
            ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
            graph = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
            if not isinstance(graph, dict):
                return None
        # Exclude self: its own pipeline is the one under validation now, not a
        # separately-completed dependency.
        self_token = _normalize_node_key_token(refs.node_key)
        closure = {t for t in _dependency_expected_node_keys(graph) if t != self_token}
        if not closure:
            return None
        missing = sorted(t for t in closure
                         if not _closure_node_validated_in_own_pipeline(self.repo_root, t))
        if not missing:
            return None
        return ("dependency closure not built+validated in its own pipeline; missing node "
                f"workflows {missing} (run the dependency closure first, e.g. via "
                "run_workflow.py --with-deps)")

    def _write_run_node_meta(self, refs: NodeRefs, filename: str,
                             meta: dict[str, Any]) -> None:
        """Author a conductor-owned meta JSON under the run-node dir (the same host-written
        area as trial_meta / aggregate_verdict; not a leaf deliverable)."""
        path = self.repo_root / refs.run_node_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _pre_judge_inproc(self, refs: NodeRefs, child_arid: str,
                          cap_token: str) -> dict[str, str]:
        """Deterministic Validate.pre_judge: the pre-spawn dependency-DAG readiness gate,
        promoted from a run_phase pre-loop branch to a recorded substep at index 0. Runs
        BEFORE execute so a --with-deps closure that is not built+validated in its own
        pipeline fails fast (no execute, no cold judge spawned). Authors pre_judge_meta.json;
        a not-ready closure is a CONTENT failure (status=fail, rc 0) that classify_failure
        routes to fail_closed (a non-physics integrity blocker — never warm-resumed, since no
        judge has run). A single-node run (empty closure) passes with zero overhead."""
        block = self._judge_pre_spawn_dag_block(refs)
        status = "fail" if block is not None else "pass"
        self._write_run_node_meta(refs, "pre_judge_meta.json", {
            "run_id": refs.run_id,
            "node_key": refs.node_key,
            "pipeline_id": refs.pipeline_id,
            "status": status,
            "validation_stage": "pre_judge",
            "failure_category": "pre_judge_dag_incomplete" if block else None,
            "failure_excerpt": block,
        })
        if block is not None:
            self.emit("judge_pre_spawn_blocked", node_key=refs.node_key, detail=block[:200])
        # Content failure returns rc 0 so run_phase routes it via classify_failure ->
        # fail_closed; determine_substep_status reads pre_judge_meta.status.
        return {"returncode": 0, "stdout": "",
                "stderr": ("[pre_judge dag incomplete]\n" + block) if block else ""}

    def _author_derived_validate_artifacts(self, refs: NodeRefs) -> None:
        """G6: deterministically author the derived Validate artifacts the judge leaf used
        to write — `aggregate_verdict.json` / `summary.json` / `validate_meta.json` — from
        the judge's `verdict.json#per_test` plus the dependency set's `aggregate_verdict`s.
        These are 100% deterministically derivable, so the conductor authors them
        correct-by-construction (closing the previously un-gated aggregate/`blocked`-DAG
        composition hole) instead of the LLM. Called at the TOP of `_post_judge_inproc`,
        before the `--stage pre_judge` gate re-validates `summary.counts` vs
        `verdict.per_test` (`_validate_tests_verdict_summary_consistency`). Idempotent on a
        warm-resume re-run (re-derived from the execute-authored `verdict.json`). As of R2 the
        judge authors only `semantic_review.json`; `verdict.json` is host-authored at execute."""
        from tools.orchestration_runtime import _resolve_dependency_facts

        node_dir = self.repo_root / refs.run_node_dir()
        verdict = _read_json(node_dir / "verdict.json") or {}
        per_test = verdict.get("per_test")
        per_test = per_test if isinstance(per_test, list) else []

        # counts over per_test statuses (status/outcome, normalized) — must equal the
        # `summary.counts` the `--stage pre_judge` gate cross-checks. `blocked` is a legal
        # per-test TEST_OUTCOME value (validate_pipeline_semantics.TEST_OUTCOME_VALUES), so it
        # is counted and non-certifying here too (else an all-`blocked` verdict would slip
        # through as pass — the gate excludes `blocked` from both sides when the key is absent).
        outcome_keys = ("pass", "fail", "xfail", "skipped", "blocked")
        counts = {k: 0 for k in outcome_keys}
        for item in per_test:
            if not isinstance(item, dict):
                continue
            st = item.get("status")
            if st is None:
                st = item.get("outcome")
            st = str(st or "").strip().lower()
            if st in counts:
                counts[st] += 1
        total = sum(counts.values())

        # self_verdict reduce: fail if any entry fails OR is blocked (a per-test `blocked` is
        # not certifiable); else xfail if every non-skipped entry is xfail; else pass
        # (skipped is non-failing).
        if counts["fail"] > 0 or counts["blocked"] > 0:
            self_verdict = "fail"
        elif counts["xfail"] > 0 and counts["pass"] == 0:
            self_verdict = "xfail"
        else:
            self_verdict = "pass"

        # Direct-dependency `blocked` rule. The blocking decision uses the SAME readiness
        # predicate the pre_judge substep and the `--stage pre_judge` gate use
        # (`_closure_node_validated_in_own_pipeline` — a dep is ready iff any binary-bound
        # verdict in its own pipeline is pass/xfail), so the derived `aggregate_verdict` can
        # never contradict a readiness gate that already passed (a dep that regressed to a
        # newer `fail` verdict but retains an older bound pass is still ready — pre_judge
        # admits it, so we must not blindly block on its LATEST verdict). `_resolve_dependency_
        # facts` (orientation-only, latest bound verdict) supplies each dep's display verdict +
        # pipeline/run refs; it is NEVER the blocking signal.
        from tools.validate_pipeline_semantics import (
            _closure_node_validated_in_own_pipeline,
            _dependency_expected_node_keys, _normalize_node_key_token,
            _read_dependency_graph_sidecar)
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        dep = (ir.get("dependency") or {}) if isinstance(ir, dict) else {}
        if not isinstance(dep, dict):
            dep = {}
        facts_by_token: dict[str, dict[str, Any]] = {}
        for fact in _resolve_dependency_facts(self.repo_root, refs.ir_ref):
            try:
                facts_by_token[_normalize_node_key_token(str(fact.get("node_key")))] = fact
            except Exception:
                continue
        dependency_nodes: list[dict[str, Any]] = []
        dep_counts = {"total": 0, "pass": 0, "xfail": 0, "fail": 0, "blocked": 0}
        blocking_direct_deps: list[str] = []
        fold_order = {"pass": 0, "xfail": 1, "fail": 2, "blocked": 3}
        fold_inv = {0: "pass", 1: "xfail", 2: "fail", 3: "blocked"}
        worst = fold_order.get(self_verdict, 0)
        direct_deps = dep.get("direct_deps") if isinstance(dep.get("direct_deps"), list) else []
        for entry in direct_deps:
            node_key = entry.get("node_key") if isinstance(entry, dict) else entry
            if not isinstance(node_key, str) or not node_key.strip():
                continue
            node_key = node_key.strip()
            try:
                token = _normalize_node_key_token(node_key)
                ready = _closure_node_validated_in_own_pipeline(self.repo_root, token)
            except Exception:
                ready = False
                token = None
            fact = facts_by_token.get(token, {}) if token else {}
            # Display verdict = the dep's latest bound aggregate (informational; may show a
            # regression), else pass/unknown per readiness.
            agg_ref = fact.get("aggregate_verdict_ref")
            display = ""
            if isinstance(agg_ref, str) and agg_ref.strip():
                dep_doc = _read_json(self.repo_root / agg_ref) or {}
                display = str(dep_doc.get("aggregate_verdict") or "").strip().lower()
            if not display:
                display = "pass" if ready else "unknown"
            dependency_nodes.append({
                "node_key": node_key,
                "aggregate_verdict": display,
                "ready": ready,
                "pipeline_ref": fact.get("pipeline_ref"),
                "run_id": fact.get("run_id"),
            })
            dep_counts["total"] += 1
            if not ready:
                blocking_direct_deps.append(node_key)
                dep_counts["blocked"] += 1
                worst = max(worst, fold_order["blocked"])
            else:
                # A ready dep is bound pass/xfail; fold its readiness-consistent contribution
                # (xfail only when its latest bound verdict is xfail, else pass — never
                # fail/blocked, which would contradict readiness).
                contrib = "xfail" if display == "xfail" else "pass"
                dep_counts[contrib] += 1
                worst = max(worst, fold_order[contrib])

        # aggregate_verdict: `blocked` when any immediate dep is not ready; else the transitive
        # fold (precedence blocked > fail > xfail > pass) over {self_verdict} + ready-dep
        # contributions. On the post_judge path (judge passed → self_verdict ∈ {pass,xfail} and
        # every dep ready) this is always ∈ {pass,xfail}.
        blocked = bool(blocking_direct_deps)
        blocked_reason: str | None = None
        if blocked:
            aggregate_verdict = "blocked"
            blocked_reason = ("immediate dependency not built+validated in its own pipeline: "
                              + ", ".join(blocking_direct_deps))
        else:
            aggregate_verdict = fold_inv[worst]

        # dependency_set: the transitive closure node tokens (self excluded), from the
        # conductor-authored dependency-graph sidecar (`all_nodes` lives there now, not the
        # IR), matching the readiness check's derivation.
        dependency_set: list[str] = []
        try:
            self_token = _normalize_node_key_token(refs.node_key)
            graph = _read_dependency_graph_sidecar(self.repo_root, refs.ir_ref) or {}
            dependency_set = sorted(
                t for t in _dependency_expected_node_keys(graph) if t != self_token)
        except Exception:
            dependency_set = []

        agg_doc: dict[str, Any] = {
            "aggregate_verdict": aggregate_verdict,
            "self_verdict": self_verdict,
            "blocked": blocked,
            "dependency_set": dependency_set,
            "dependency_nodes": dependency_nodes,
        }
        if blocked:
            agg_doc["blocked_reason"] = blocked_reason
            agg_doc["blocking_direct_deps"] = blocking_direct_deps
        self._write_run_node_meta(refs, "aggregate_verdict.json", agg_doc)

        failure_class = verdict.get("failure_class")
        summary_doc: dict[str, Any] = {
            "self_summary": {
                "verdict": self_verdict,
                "failure_class": failure_class,
                "total": total,
                "pass": counts["pass"],
                "xfail": counts["xfail"],
                "fail": counts["fail"],
                "skipped": counts["skipped"],
                "blocked": counts["blocked"],
            },
            "dependency_summary": {
                "total": dep_counts["total"],
                "pass": dep_counts["pass"],
                "xfail": dep_counts["xfail"],
                "fail": dep_counts["fail"],
                "blocked": dep_counts["blocked"],
            },
            "counts": {
                "pass": counts["pass"],
                "fail": counts["fail"],
                "xfail": counts["xfail"],
                "skipped": counts["skipped"],
                "blocked": counts["blocked"],
            },
        }
        qc = _read_json(node_dir / "quality_check.json")
        if isinstance(qc, dict):
            qc_checks = qc.get("checks") if isinstance(qc.get("checks"), dict) else {}
            summary_doc["quality_check"] = {
                "target_class": qc.get("target_class"),
                "status": qc.get("status"),
                "diagnostics_match": qc_checks.get("diagnostics_match"),
                "verdict_match": qc_checks.get("verdict_match"),
            }
        self._write_run_node_meta(refs, "summary.json", summary_doc)

        # validate_meta.json bookkeeping (not gate-validated; keys per phase_04 §"required
        # keys"). last_fail_reason reads the PRIOR post_judge_meta (present only on a
        # warm-resume re-run; None on the first pass).
        prior_post = _read_json(node_dir / "post_judge_meta.json") or {}
        last_fail_reason = prior_post.get("failure_excerpt") or None
        attempt_count = getattr(self, "_judge_attempt_count", {}).get(refs.node_key, 1)
        self._write_run_node_meta(refs, "validate_meta.json", {
            "run_id": refs.run_id,
            "node_key": refs.node_key,
            "pipeline_id": refs.pipeline_id,
            "attempt_count": attempt_count,
            "verification_status": "pass",
            "last_fail_reason": last_fail_reason,
            "debug_mode": (self.workflow_mode == "dev"),
            "context_isolated": True,
            "judge_command_ref": f"{refs.run_node_dir()}/semantic_review.json",
        })

    def _post_judge_inproc(self, refs: NodeRefs, child_arid: str,
                           cap_token: str) -> dict[str, str]:
        """Deterministic Validate.post_judge: FIRST author the derived artifacts
        (`_author_derived_validate_artifacts` — G6: aggregate_verdict/summary/validate_meta),
        then run `validate_pipeline_semantics --stage pre_judge` (the gate the judge leaf
        used to own, G3) AFTER the judge returns its verdict, then classify the violation
        severity into a `disposition`. Authors post_judge_meta.json
        {status, violations, disposition}.

        Scoped to this run (`--pipeline-root`/`--run-id`) so historically-broken sibling
        pipelines cannot fail an otherwise-conformant node. `--in-flight-agent-run-id` names
        BOTH the judge substep (recorded+returned) AND this post_judge substep's own arid:
        post_judge's agent_graph edge is written by record_launch but its agent_runs.jsonl
        row is appended only after this body returns, so its own edge is dangling RIGHT NOW
        and must be declared in-flight or `_validate_orchestration_hierarchy` flags it (the
        gate's in-flight filter accepts substep ∈ {judge, post_judge}). The judge arid is
        declared too (harmless: it is already recorded, so it is a no-op that documents the
        live judge region).

        disposition: recoverable (leaf/judge-authored conformance violation) -> warm_resume
        (run_phase warm-resumes the judge in place); unrecoverable (orchestration-record /
        cross-pipeline DAG integrity) -> fail_closed; unknown -> fail_closed (conservative;
        an escalate-LLM adjudicator is a deferred follow-up)."""
        # G6: the conductor authors the deterministically-derivable artifacts (aggregate_verdict
        # / summary / validate_meta) from the judge's verdict.json + the dependency set BEFORE
        # the gate, so `--stage pre_judge` re-validates the conductor's own summary.counts.
        self._author_derived_validate_artifacts(refs)
        judge_arid = getattr(self, "_pending_judge_arid", {}).get(refs.node_key, "")
        cmd = ["python3", "tools/validate_pipeline_semantics.py", "--stage", "pre_judge",
               "--orchestration-id", self.orchestration_id,
               "--pipeline-root", refs.pipeline_ref, "--run-id", str(refs.run_id),
               "--in-flight-agent-run-id", child_arid]
        if judge_arid:
            cmd += ["--in-flight-agent-run-id", judge_arid]
        try:
            gate = subprocess.run(
                cmd, cwd=self.repo_root, env=self.env, text=True,
                capture_output=True, check=False)
        except OSError as exc:
            self._write_run_node_meta(refs, "post_judge_meta.json", {
                "run_id": refs.run_id, "node_key": refs.node_key,
                "pipeline_id": refs.pipeline_id, "status": "fail",
                "validation_stage": "pre_judge",
                "failure_category": "post_judge_gate_error",
                "failure_excerpt": f"pre_judge gate subprocess failed to launch: {exc}"[:400],
                "violations": [], "disposition": "fail_closed",
            })
            return {"returncode": 0, "stdout": "",
                    "stderr": f"[post_judge gate launch fail] {exc}"}
        if gate.returncode == 0:
            self._write_run_node_meta(refs, "post_judge_meta.json", {
                "run_id": refs.run_id, "node_key": refs.node_key,
                "pipeline_id": refs.pipeline_id, "status": "pass",
                "validation_stage": "pre_judge", "failure_category": None,
                "failure_excerpt": None, "violations": [], "disposition": None,
            })
            return {"returncode": 0, "stdout": "", "stderr": ""}
        combined = gate.stdout + gate.stderr
        # The validator prints each violation as a `- {line}` bullet after a FAIL header.
        violations = [ln[2:] for ln in combined.splitlines() if ln.startswith("- ")]
        severity = classify_post_judge_violations(violations)
        # G5: an `unknown` violation (unclassifiable by the deterministic path-prefix rules) is
        # no longer a blind fail_closed — it routes to the unified escalate LLM. `recoverable`
        # (judge-authored) still warm-resumes deterministically; `unrecoverable` (integrity)
        # still fail_closes. run_phase turns the `escalate` disposition into an escalate
        # RouteDecision in prod (dev keeps fail_closed — no billed escalate leaf).
        disposition = {
            "recoverable": "warm_resume",
            "unrecoverable": "fail_closed",
            "unknown": "escalate",
        }[severity]
        self._write_run_node_meta(refs, "post_judge_meta.json", {
            "run_id": refs.run_id, "node_key": refs.node_key,
            "pipeline_id": refs.pipeline_id, "status": "fail",
            "validation_stage": "pre_judge",
            "failure_category": "pre_judge_violation",
            "failure_excerpt": "\n".join(combined.splitlines()[-50:]),
            "violations": violations, "disposition": disposition,
        })
        return {"returncode": 0, "stdout": "", "stderr": "[post_judge gate fail]\n" + combined}

    def _maybe_warm_resume_post_judge(
            self, refs: NodeRefs, outcomes: list["SubstepOutcome"],
            dep_facts: tuple[dict[str, str], ...]) -> list["SubstepOutcome"]:
        """Recover a RECOVERABLE post_judge conformance violation in place instead of
        terminalizing fail_closed. When the failed substep is `post_judge` with
        disposition=="warm_resume" (a leaf/judge-authored violation like a wrong
        semantic_review.review_method literal), warm-resume the judge — re-authoring its
        semantic_review.json with context intact via the slim findings-only prompt — then
        re-run the deterministic post_judge gate. Bounded by MAX_ATTEMPTS_PER_PHASE.

        Self-contained by design (does NOT go through conduct/reopen_phase): reopen_phase
        re-runs the whole phase from index 0, passes repair only to index 0, refuses a
        passing pipeline, and crashes for a validate trigger. This loop drives the warm-resume
        primitives (`_resolve_reuse_resume`, the slim prompt) directly on the judge substep,
        so the "repair only to index 0" rule is never consulted and the judge's index is
        irrelevant. An unrecoverable/unknown disposition, or a judge re-run that itself fails,
        falls through unchanged to run_phase's fail_closed posture."""
        # Trigger only when the LAST (failed) substep is post_judge with a warm_resume verdict.
        # These guards touch no filesystem so a passing phase returns before reading anything.
        if not outcomes or outcomes[-1].status == "pass":
            return outcomes
        if SUBSTEPS["validate"][len(outcomes) - 1] != "post_judge":
            return outcomes
        node_dir = self.repo_root / refs.run_node_dir()
        meta = _read_json(node_dir / "post_judge_meta.json") or {}
        if meta.get("disposition") != "warm_resume":
            return outcomes

        for attempt in range(MAX_ATTEMPTS_PER_PHASE):
            self.emit("post_judge_warm_resume", node_key=refs.node_key, attempt=attempt + 1)
            judge_arid = getattr(self, "_pending_judge_arid", {}).get(refs.node_key, "")
            findings = meta.get("failure_excerpt") or "post_judge conformance violation"
            # Tombstone the superseded judge + post_judge attempt (outcomes[-2] is always the
            # judge here: a post_judge failure implies the judge passed and both ran).
            self._add_superseded_run_ids(
                [outcomes[-2].agent_run_id, outcomes[-1].agent_run_id],
                reason="validate_post_judge_warm_resume_orphan")
            repair = {
                "issue_severity": "major",
                "repair_strategy": "reuse",
                "repair_target_agent_run_id": judge_arid,
                "repair_reason": "post_judge_conformance",
                "repair_findings": findings,
            }
            judge_oc = self.run_substep(refs, "validate", "judge", repair=repair,
                                        resolved_dependencies=dep_facts)
            outcomes[-2] = judge_oc
            if judge_oc.status != "pass":
                # The warm-resumed judge failed (a fresh non-pass verdict or a transport
                # error). Drop the stale post_judge so the failed judge is the terminal
                # outcome; run_phase's transport branch / classify_validate_judge takes over.
                return outcomes[:-1]
            self._pending_judge_arid[refs.node_key] = judge_oc.agent_run_id
            if not hasattr(self, "_judge_attempt_count"):
                self._judge_attempt_count = {}
            self._judge_attempt_count[refs.node_key] = (
                self._judge_attempt_count.get(refs.node_key, 0) + 1)
            post_oc = self.run_substep(refs, "validate", "post_judge",
                                       resolved_dependencies=dep_facts)
            outcomes[-1] = post_oc
            if post_oc.status == "pass":
                return outcomes  # recovered: 4 passing substeps -> phase pass
            meta = _read_json(node_dir / "post_judge_meta.json") or {}
            if meta.get("disposition") != "warm_resume":
                break  # became unrecoverable/unknown -> fail_closed
        return outcomes

    def _maybe_warm_resume_verify_meta(
            self, refs: NodeRefs, phase: str, outcomes: list["SubstepOutcome"],
            dep_facts: tuple[dict[str, str], ...]) -> list["SubstepOutcome"]:
        """Recover a verify leaf that authored a CONTRACT-VIOLATING stage meta by warm-resuming
        that same leaf to re-author it, instead of letting the violation persist.

        The violating meta (canonically: a `last_fail_reason` written as a structured incident
        dict rather than one plain string) is the unrepairable class from E2E #4 — a Generate
        reopen rotates a FRESH source dir and deletes nothing, so the bad meta stays readable
        forever and every later gate re-trips on it. The fix must therefore land while the
        AUTHORING leaf is still resumable: re-run that same verify substep with the contract
        findings as slim repair findings, and it rewrites its own meta with context intact.

        Self-contained by design, exactly like `_maybe_warm_resume_post_judge` (see its
        docstring): conduct/reopen_phase re-runs the whole phase from index 0, hands repair
        only to index 0 (the producer), and rotates a fresh producer dir — all three are wrong
        for a verify-authored meta defect. `verify` is the LAST substep of both compile and
        generate, so a recovered pass needs no downstream re-run.

        Bounded by MAX_ATTEMPTS_PER_PHASE. On budget exhaustion the outcome stays fail and
        classify_failure's meta-schema guard terminalizes it as `{phase}_fail_meta_schema`
        rather than routing a garbage meta through the severity table.

        The repair carries ONLY the violation clauses as findings — no instructions. The slim
        renderer wraps `repair_findings` in an UNTRUSTED-data fence that tells the leaf not to
        obey anything inside it, so a constraint smuggled in there is both ignored and
        self-contradictory. The constraint is imposed structurally instead: the repair narrows
        `allowed_output_paths` to the meta alone (see build_launch_request), which the leaf sees
        as its trusted deliverable list and the file-tool write guard holds it to.
        """
        # Guards touch no filesystem beyond the meta read, so a healthy phase returns fast.
        if not outcomes or outcomes[-1].status == "pass":
            return outcomes
        if SUBSTEPS[phase][len(outcomes) - 1] != "verify":
            return outcomes
        # Z2 pure reviewer (M-D): the pure `generate.verify` OWNS its in-conversation verdict
        # repair (bounded warm-resume of its own session inside `_run_pure_verify_substep`) and
        # the host authors source_meta.json from the returned verdict — there is no leaf-authored
        # meta to re-author here. A schema-exhausted pure verify is routed by classify_failure's
        # verdict table (a cold generate restart), not by this agentic meta warm-resume loop. Only
        # (generate, verify) is pure (compile.verify stays agentic), so this fires solely for the
        # generate phase on a pure-leaf node.
        if self._pure_leaf_substep(refs, phase, "verify"):
            return outcomes
        failed = outcomes[-1]
        # A leaf that died of an infra/transport error (usage limit, OOM) did not "author a bad
        # meta" — it authored nothing. Repairing here would overwrite outcomes[-1] and erase the
        # nonzero returncode that run_phase's transport branch fail_closes on, silently turning
        # a dead leaf into a certified phase.
        if failed.leaf_returncode != 0:
            return outcomes
        # Attribution: repair only a meta THIS verify leaf actually (re)wrote. A meta whose
        # mtime predates this substep's launch was left by the PRODUCER, and the verify failed
        # for some other reason — canonically the freshness clause ("an inspect-only verify that
        # writes nothing cannot terminate pass"). Handing such a leaf a "just fix the meta" turn
        # would let it satisfy the freshness gate without doing the verification it skipped.
        # Not this loop's class: classify_failure escalates it as `{phase}_fail_meta_schema`.
        if not self._stage_meta_authored_since(refs, phase, failed.launched_at):
            return outcomes
        findings = self._stage_meta_contract_findings(refs, phase)
        if not findings:
            return outcomes
        for attempt in range(MAX_ATTEMPTS_PER_PHASE):
            verify_arid = outcomes[-1].agent_run_id
            # Warm resume is the whole mechanism: the leaf fixes its own meta with its context
            # (and its semantic verdict) intact, from a findings-only slim turn. Without a
            # resumable session the launch silently degrades to a COLD full prompt, which
            # carries NO findings (the full template has no findings placeholder) — the leaf
            # would re-verify blind and escalate anyway. Re-checked every iteration, not just on
            # entry: each repair turn is a new session that may itself not be resumable.
            if not self._verify_session_resumable(verify_arid):
                self.emit("verify_meta_schema_no_warm_session", node_key=refs.node_key,
                          phase=phase, attempt=attempt + 1,
                          detail="; ".join(findings)[:200])
                return outcomes
            self.emit("verify_meta_schema_warm_resume", node_key=refs.node_key, phase=phase,
                      attempt=attempt + 1, detail="; ".join(findings)[:200])
            # Tombstone the superseded verify attempt so a later --resume can still reach pass
            # (same contract as the post_judge warm-resume).
            self._add_superseded_run_ids(
                [verify_arid], reason=f"{phase}_verify_meta_schema_warm_resume_orphan")
            repair = {
                "issue_severity": "major",
                "repair_strategy": "reuse",
                "repair_target_agent_run_id": verify_arid,
                "repair_reason": VERIFY_META_SCHEMA_REPAIR_REASON,
                "repair_findings": "\n".join(findings),
            }
            oc = self.run_substep(refs, phase, "verify", repair=repair,
                                  resolved_dependencies=dep_facts)
            outcomes[-1] = oc
            if oc.leaf_returncode != 0:
                return outcomes  # transport error -> run_phase's fail_closed posture
            findings = self._stage_meta_contract_findings(refs, phase)
            if not findings:
                # Schema repaired. A pass status now passes the phase; a legitimately recorded
                # fail carries a READABLE last_fail_reason into the normal severity gate.
                return outcomes
        return outcomes

    def run_phase(self, refs: NodeRefs, phase: str,
                  repair: dict[str, str] | None = None) -> PhaseOutcome:
        """Run one phase as a single attempt and write one terminal step_result.
        On a substep failure the phase's routing decision (cross-phase reopen,
        fail_closed, or escalate) is returned for conduct() to act on; in-place
        retry is intentionally not done here (its retry_decisions / effective-pass
        bookkeeping is error-prone) — a same-phase decision terminalizes via conduct.
        """
        node_key = refs.node_key
        if not hasattr(self, "_producer_arid"):
            self._producer_arid: dict[str, str] = {}
        completed = self.check_step_completed(node_key, phase)
        if completed is not None:
            # A resumed run skips this phase, but a later cross-phase repair may
            # still target its producer (repair_strategy=reuse). Recover the
            # producing substep arid from the checkpointed step_result so the
            # repair child has a prior run to diff against.
            producer = self._completed_producer_arid(
                node_key, phase, completed.get("agent_run_id"))
            if producer:
                self._producer_arid[phase] = producer
            return PhaseOutcome(phase, "pass", decision=RouteDecision("advance"),
                                skipped=True)
        # Item C: a transport-substep resume (armed by _consume_transport_resume_directive) preseats
        # the surviving producer as outcomes[0] and relaunches only the deterministic mids + verify.
        # Popped so it fires once; a normal run leaves it None. When set, the producer-id rotation is
        # SUPPRESSED (refs already point at the surviving artifact) — otherwise _ensure_fresh_producer_id
        # would allocate a new id and orphan the artifact we mean to reuse.
        preseat = getattr(self, "_substep_resume", {}).pop(phase, None)
        # Validate dependency-DAG readiness is checked HERE, before the generic launch gate.
        # This is load-bearing: workflow_launch_check (and EVERY substep's own record-launch,
        # including pre_judge's) is itself dependency-gated (`_dependency_ready`), so a
        # not-built+validated closure would raise `dependency_not_ready` as an uncaught
        # RuntimeError before the `pre_judge` substep could ever run. Fail fast with the clean
        # `validate_pre_judge_dag_incomplete` fail_closed (the historic pre-spawn behavior).
        # `_dependency_ready` only checks DIRECT deps, whereas `_judge_pre_spawn_dag_block`
        # checks the full `dependency.all_nodes` closure, so this also catches transitive gaps
        # the launch gate would miss. The deterministic `pre_judge` substep (index 0) re-runs
        # the same check on the ready path and records `pre_judge_meta.json` (its in-body fail
        # path is defensive — this pre-launch guard is the primary readiness check).
        if phase == "validate":
            block = self._judge_pre_spawn_dag_block(refs)
            if block is not None:
                self.emit("judge_pre_spawn_blocked", node_key=node_key, detail=block[:200])
                return PhaseOutcome(phase, "fail", decision=RouteDecision(
                    "fail_closed", reason="validate_pre_judge_dag_incomplete"))
        self.workflow_launch_check(node_key, phase, child_agent_role(phase))
        if preseat is None:
            self._ensure_fresh_producer_id(refs, phase)
        # Author/refresh the pipeline lineage.json host-side BEFORE the substeps run:
        # generate.gate's static (post_generate) checker requires it, and the sandboxed leaf cannot
        # write it (pipeline-root file; see _write_lineage). Pipeline phases only —
        # compile writes under workspace/ir/, not the pipeline root.
        dep_facts: tuple[dict[str, str], ...] = ()
        dep_surface: tuple[dict[str, Any], ...] = ()
        if phase in ("generate", "build", "validate"):
            dep_facts = tuple(self._write_lineage(refs))
        # The conductor authors src/Makefile deterministically (runtime-owned, like
        # lineage.json) for every make+fortran node BEFORE the substeps run: the generate leaf
        # must not author it, and generate.gate's static (post_generate) checker inspects it. The
        # template encodes the fixed runner->model use-graph and, for a dependency node, the
        # closure object rules (Model B); the dep sources are staged at build (see
        # _stage_dependency_sources). c/cpp/mixed keep LLM authoring (see _write_makefile).
        # A pure producer authors the bundle-DERIVED Makefile after the producer returns
        # (`_write_pure_bundle_artifacts`), so it compiles exactly the bundle's file set — skip
        # the IR-shaped pre-authoring here for a pure node (the runner glue below is still
        # host-rendered from the IR, independent of the bundle).
        if (phase == "generate" and self._conductor_authors_makefile(refs)
                and not self._pure_leaf_substep(refs, "generate", "generate")):
            self._write_makefile(refs)
        # R1/M3c-β: for a physics node with a harness dependency the conductor host-renders
        # src/<spec_id>_runner.f90 (glue over the certified harness plumbing + the leaf-authored
        # <spec_id>_checks.f90), BEFORE the substeps run — so, like the Makefile, the write is
        # outside the substep FS-diff window (no write-attribution regression) and re-renders on
        # each attempt after the source_id rotate (_ensure_fresh_producer_id, above). An
        # unresolvable/unbuilt harness, a harness-interface drift (signature pin), or an
        # unrenderable IR is a fail_closed precondition (operator --resume), NOT a Generate retry.
        if phase == "generate" and self._conductor_authors_runner(refs):
            try:
                self._write_runner(refs)
            except Exception as exc:  # RuntimeError/RenderError -> transport fail_closed
                self.emit("generate_runner_render_failed", node_key=node_key,
                          detail=str(exc)[:200])
                return PhaseOutcome(phase, "fail", decision=RouteDecision(
                    "fail_closed", reason="generate_runner_render_failed"))
        # Author the dependency-graph sidecar host-side at Compile start (before any
        # substep's record-launch baseline): the derived closure/topo graph is a pure
        # function of deps.yaml + spec_catalog.yaml, so the conductor writes
        # <ir_ref>/dependency_graph.json deterministically and the compile.generate leaf
        # authors only direct_deps. Mirrors the lineage/Makefile host-author blocks above.
        # cycle / unresolvable / version-conflict / catalog-corrupt are deps.yaml/catalog
        # structural breaks, not content the LLM can fix — fail_closed (same contract as
        # run_workflow._resolve_dependency_closure), NOT a compile.generate content retry.
        if phase == "compile":
            err = self._write_dependency_graph(refs)
            if err is not None:
                self.emit("compile_dependency_graph_failed", node_key=node_key,
                          detail=str(err)[:200])
                return PhaseOutcome(phase, "fail", decision=RouteDecision(
                    "fail_closed",
                    reason=f"compile_dependency_graph_{err['reason']}"))
            # Author the component-dep published-surface sidecar (L2) from the graph just written,
            # and thread it into the compile.generate launch so the leaf is SHOWN the real op-name
            # catalog. Best-effort: a resolution gap yields `unresolved` entries; the L3 gate is
            # inert where the surface is unresolved.
            dep_surface = tuple(self._write_dependency_surface(refs))

        outcomes: list[SubstepOutcome] = []
        if preseat is not None:
            # Seed the surviving run-1 producer as a synthetic pass at index 0 and start the loop at
            # index 1. The producer arid is superseded (transport-tombstoned) but is re-vouched by
            # this run's step_result — the completion check `continue`s superseded rows, and the
            # re-run mids/verify supply the fresh non-superseded rows the fresh-replacement rule needs.
            outcomes.append(SubstepOutcome(
                preseat["producer_arid"], "pass", [], 0, None, 0.0, 1))
            self.emit("substep_resumed", node_key=refs.node_key, phase=phase,
                      substep=SUBSTEPS[phase][0] or "step",
                      agent_run_id=preseat["producer_arid"])
        for i, substep in enumerate(SUBSTEPS[phase]):
            # A preseated producer occupies outcomes[0]; skip the already-satisfied indices so the
            # producer leaf is not re-spawned (repair=repair if i==0 is dead under preseat — index 0
            # is skipped and a transport resume carries no pending_repair).
            if i < len(outcomes):
                continue
            # Surface substep activity on the host stdout event stream so an
            # operator sees per-substep progress (the phase-level emits alone
            # leave a long gap during multi-substep phases like generate). The
            # build phase has no substep (SUBSTEPS["build"] == (None,)); use
            # the agent_role label "step" so the line still reads cleanly.
            substep_label = substep or "step"
            substep_started = time.monotonic()
            self.emit("substep_start", node_key=refs.node_key, phase=phase,
                      substep=substep_label, attempt=i + 1)
            oc = self.run_substep(refs, phase, substep, repair=repair if i == 0 else None,
                                  resolved_dependencies=dep_facts,
                                  dependency_surface=dep_surface)
            self.emit("substep_complete", node_key=refs.node_key, phase=phase,
                      substep=substep_label, result=oc.status,
                      agent_run_id=oc.agent_run_id,
                      elapsed_seconds=round(time.monotonic() - substep_started, 2))
            outcomes.append(oc)
            if phase == "validate" and substep == "judge":
                # post_judge (the next substep) runs `--stage pre_judge` and must declare the
                # judge's arid in-flight; stash it before post_judge is dispatched.
                if not hasattr(self, "_pending_judge_arid"):
                    self._pending_judge_arid: dict[str, str] = {}
                self._pending_judge_arid[refs.node_key] = oc.agent_run_id
                # G6: track judge attempts for validate_meta.attempt_count (best-effort; not
                # gate-validated). Incremented again per warm-resume judge re-run.
                if not hasattr(self, "_judge_attempt_count"):
                    self._judge_attempt_count: dict[str, int] = {}
                self._judge_attempt_count[refs.node_key] = (
                    self._judge_attempt_count.get(refs.node_key, 0) + 1)
            if oc.status != "pass":
                break
        # Warm-resume mini-loop: a RECOVERABLE post_judge conformance violation (e.g. a wrong
        # semantic_review.review_method literal) warm-resumes the judge in place and re-runs
        # the deterministic post_judge gate, instead of terminalizing fail_closed.
        if phase == "validate":
            outcomes = self._maybe_warm_resume_post_judge(refs, outcomes, dep_facts)
        # Warm-resume mini-loop: a verify leaf that authored a contract-violating stage meta
        # (e.g. last_fail_reason as a dict) re-authors it in place. The violating meta is
        # immutable once the phase moves on — a reopen rotates a fresh source dir and deletes
        # nothing — so it must be repaired while its authoring leaf is still resumable.
        if phase in ("compile", "generate"):
            outcomes = self._maybe_warm_resume_verify_meta(refs, phase, outcomes, dep_facts)
        self._producer_arid[phase] = outcomes[0].agent_run_id

        if phase in SUBSTEP_AWARE_PHASES:
            executor = self.orchestration_agent_run_id
            substep_arids = [oc.agent_run_id for oc in outcomes]
        else:  # build: the single step child is the executor
            executor = outcomes[0].agent_run_id
            substep_arids = []
        failed = [oc.agent_run_id for oc in outcomes if oc.status != "pass"]
        status = "pass" if not failed and len(outcomes) == len(SUBSTEPS[phase]) else "fail"

        result: dict[str, Any] = {
            "status": status,
            "required_outputs": phase_required_outputs(
                refs, phase,
                exe_name=(self._resolve_exe_name(refs) if phase == "build" else None),
                makefile_required=not (phase == "generate" and self._conductor_authors_makefile(refs)),
                runner_host_authored=(phase == "generate" and self._conductor_authors_runner(refs))),
            "executor_agent_run_id": executor,
            "substep_agent_run_ids": substep_arids,
            "failed_substeps": failed,
            "retry_decisions": None,
            "validation_stage": PHASE_VALIDATION_STAGE[phase],
        }
        # Every terminal Validate step_result (pass OR fail) must carry a launch_request_ref
        # so the pre_phase_complete judge hook can resolve the execution dir. Point it at the
        # JUDGE substep whenever the judge ran (index 2): on a pass the last substep is the
        # deterministic post_judge, and a post_judge launch_request_ref would make
        # _pre_phase_complete_judge_checks skip the semantic_review enforcement (it keys on
        # substep=="judge"). When the judge never ran (pre_judge/execute failure) fall back to
        # the last substep that ran — its request points at a non-judge substep, so the hook
        # correctly skips the semantic-review requirement.
        if phase == "validate" and outcomes:
            judge_idx = SUBSTEPS["validate"].index("judge")
            ref_oc = outcomes[judge_idx] if len(outcomes) > judge_idx else outcomes[-1]
            result["launch_request_ref"] = (
                f"workspace/orchestrations/{self.orchestration_id}"
                f"/launches/{ref_oc.agent_run_id}.request.json")
        # A nonzero leaf exit is an infra/transport failure (token limit, OOM, transport,
        # session limit) the decision tables cannot classify — route straight to fail_closed
        # so the operator can --resume. This is checked BEFORE write_step_result: a transport
        # failure leaves no canonical evidence (e.g. a judge that died with no
        # semantic_review.json), so writing the step_result would crash on the
        # post_phase_complete judge gate instead of cleanly failing closed. Skipping the write
        # leaves the attempt's already-terminalized agents without a step_result, so tombstone
        # them (add-superseded-runs) — otherwise a later --resume (which re-runs the phase fresh)
        # trips _validate_orchestration_completion_for_pass on the orphaned arids. Tombstone
        # EVERY outcome arid: substep agents for substep-aware phases, or the single step-role
        # agent for build (substep_arids is empty there, but outcomes[0] is recorded in
        # agent_runs.jsonl and would be flagged as a step orphan).
        transport = (next((oc for oc in outcomes if oc.leaf_returncode != 0), None)
                     if status != "pass" else None)
        if transport is not None:
            # Name the cause when the leaf's captured output identified one (an LLM usage limit is
            # the common case, and a bare `leaf_exit=1` sends the operator hunting for a bug that
            # is not there). The `leaf_transport_error` PREFIX is load-bearing: set_status maps the
            # fail_closed reason to a reason_code by prefix match, so the tag only ever appends.
            # The evidence is clipped so the whole reason survives set_status's reason_detail[:200].
            infra = transport.infra_error
            suffix = f" (tag: {infra[0]}; {infra[1][:110]})" if infra else ""
            # A retried-and-still-dead leaf reached here only after exhausting its transient
            # budget, i.e. the outage outlasted every backoff. Say so: `attempts=3` tells the
            # operator NOT to `--resume` immediately (the provider is still down), where a bare
            # transport error would invite an instant retry that dies the same way.
            if transport.attempts > 1:
                suffix += f" [attempts={transport.attempts}]"
            orphan_arids = [oc.agent_run_id for oc in outcomes]
            if orphan_arids:
                self._add_superseded_run_ids(
                    orphan_arids,
                    reason=(f"leaf_transport_error_orphan: "
                            f"leaf_exit={transport.leaf_returncode}{suffix}"))
            decision = RouteDecision(
                "fail_closed",
                reason=f"leaf_transport_error: leaf_exit={transport.leaf_returncode}{suffix}")
            return PhaseOutcome(phase, status, substep_arids, failed, decision)

        # G4: the deterministic validate gate substeps (pre_judge / post_judge) fail the phase
        # as non-physics INTEGRITY blockers, terminalized fail_closed WITHOUT a routeable
        # step_result (skip-write + tombstone, matching the transport branch shape). Gate on the
        # ACTUALLY-failed substep (index len-1), NOT a meta status on disk: a warm-resumed judge
        # that physics-fails leaves a STALE post_judge_meta from a superseded attempt, and must
        # route via classify_failure (judge physics), not fail_closed on the stale meta. Cases:
        #   - pre_judge (index 0): a --with-deps closure not built+validated. No judge ran, so
        #     this preserves the historic pre-spawn terminal behavior (no step_result written).
        #   - post_judge (index 3): the `--stage pre_judge` gate failed with a terminal
        #     disposition (an integrity violation, or a recoverable one whose warm-resume budget
        #     was exhausted). The judge passed physics; the pre_phase_complete hook forbids a
        #     `fail` step_result atop a `pass` semantic_review, so the write is skipped.
        #   - judge (index 2) with semantic_review.decision != "fail" (pass, or missing/empty):
        #     a judge deliverable inconsistency the hook cannot express — either verdict.json is
        #     malformed (per_test uses a wrong field name / non-certifying value) while decision
        #     stays `pass`, or the judge left a stray `fail`/`blocked` per_test entry yet reported
        #     decision `pass` (a routeable failure MUST carry decision=="fail"). Either way the
        #     pre_phase_complete hook forbids a `fail` step_result atop a non-`fail`
        #     semantic_review, so a naive write_step_result would raise a hard conductor_error
        #     (physics-pass here is per the leaf's own decision, which may be the very bug).
        #     Route it like the integrity blockers: skip-write + escalate (prod) / fail_closed
        #     (dev). A present decision=="fail" is a routeable physics/semantic failure and falls
        #     through to write_step_result + classify_failure unchanged (the hook allows fail+fail).
        if phase == "validate" and status != "pass" and outcomes:
            failed_sub = SUBSTEPS["validate"][len(outcomes) - 1]
            judge_conformance_block = False
            if failed_sub == "judge":
                judge_conformance_block = self._judge_semantic_decision(refs) != "fail"
            if failed_sub in ("pre_judge", "post_judge") or judge_conformance_block:
                if judge_conformance_block:
                    cat = "judge_conformance_violation"
                    # A judge-authored conformance violation is routed to the escalate
                    # diagnostician in prod (it reads verdict.json + semantic_review.json).
                    is_escalate = True
                else:
                    fname = ("pre_judge_meta.json" if failed_sub == "pre_judge"
                             else "post_judge_meta.json")
                    gate_meta = _read_json(self.repo_root / refs.run_node_dir() / fname) or {}
                    cat = gate_meta.get("failure_category") or (
                        "pre_judge_dag_incomplete" if failed_sub == "pre_judge"
                        else "pre_judge_violation")
                    # G5: a prod post_judge `unknown` (disposition="escalate") routes to the
                    # unified escalate LLM.
                    is_escalate = (failed_sub == "post_judge"
                                   and gate_meta.get("disposition") == "escalate")
                escalate_reason = ("validate_judge_conformance_violation"
                                   if judge_conformance_block else "validate_post_judge_unknown")
                # Return the escalate decision WITHOUT pre-tombstoning the orphan arids:
                # conduct() runs the diagnostician and, on a reopen directive, calls
                # reopen_phase(trigger=this failed arid) — which NO-OPs if the trigger is already
                # in superseded_runs.json (idempotency guard), so the trigger MUST stay live to
                # drive the rollback. reopen_phase supersedes the whole validate attempt (incl.
                # this trigger) as part of the upstream reopen; a fail_closed resolution is
                # terminal (no completion vouch runs). Skip-write posture is preserved (no
                # step_result written here).
                if is_escalate and self.workflow_mode != "dev":
                    decision = RouteDecision("escalate", reason=escalate_reason)
                    return PhaseOutcome(phase, status, substep_arids, failed, decision)
                # Terminal fail_closed cases (pre_judge / integrity / dev escalate / warm-resume
                # budget exhausted): skip-write + tombstone the orphan arids (they have no
                # step_result home, and no reopen will consume them as a trigger).
                orphan_arids = [oc.agent_run_id for oc in outcomes]
                if orphan_arids:
                    self._add_superseded_run_ids(
                        orphan_arids, reason=f"validate_gate_fail_orphan: {cat}")
                if is_escalate:  # dev fail-fast (no billed escalate leaf)
                    decision = RouteDecision("fail_closed", reason=escalate_reason)
                else:
                    reason = ("validate_pre_judge_dag_incomplete"
                              if cat == "pre_judge_dag_incomplete" else f"validate_{cat}")
                    decision = RouteDecision("fail_closed", reason=reason)
                return PhaseOutcome(phase, status, substep_arids, failed, decision)

        self.write_step_result(node_key, phase, executor, result)
        if status == "pass":
            decision = RouteDecision("advance")
        else:
            decision = self.classify_failure(refs, phase, outcomes)
        return PhaseOutcome(phase, status, substep_arids, failed, decision)

    def _gather_failure_context(self, refs: NodeRefs, phase: str) -> dict[str, Any]:
        """Collect the canonical status artifacts of a failed phase so the
        diagnostician reasons over their CONTENT (no filesystem read by the leaf,
        sidestepping the read-manifest guard for an unregistered reasoning agent).

        The FAILED phase's own artifacts lead the result: the prompt budgets the context per
        artifact in insertion order, so the evidence that explains this failure must not sit behind
        an unrelated 11k-character ir_meta."""
        candidates = {
            "verdict.json": f"{refs.run_node_dir()}/verdict.json",
            "semantic_review.json": f"{refs.run_node_dir()}/semantic_review.json",
            "aggregate_verdict.json": f"{refs.run_node_dir()}/aggregate_verdict.json",
            # G5: the deterministic validate gate metas — post_judge_meta carries the
            # `violations` / `failure_excerpt` / `disposition` that drive a post_judge
            # `unknown` escalate; pre_judge_meta carries the dependency-DAG readiness result.
            "post_judge_meta.json": f"{refs.run_node_dir()}/post_judge_meta.json",
            "pre_judge_meta.json": f"{refs.run_node_dir()}/pre_judge_meta.json",
            "binary_meta.json": f"{refs.binary_dir()}/binary_meta.json",
            "ir_meta.json": f"{refs.ir_ref}/ir_meta.json",
            "source_meta.json": f"{refs.source_dir()}/source_meta.json",
            # The Generate.gate union verdict: its failure_categories / composed excerpt are the
            # diagnostic material for a `gate_unknown_category:` escalate (an unclassifiable
            # per-checker category the deterministic table did not cover).
            "gate_meta.json": f"{refs.source_dir()}/gate_meta.json",
        }
        primary = _PHASE_PRIMARY_ARTIFACTS.get(phase, ())
        ordered = [name for name in primary if name in candidates]
        ordered += [name for name in candidates if name not in ordered]
        ctx: dict[str, Any] = {}
        for name in ordered:
            data = _read_json(self.repo_root / candidates[name])
            if data is not None:
                ctx[name] = data
        return ctx

    def escalate(self, refs: NodeRefs, phase: str, outcome: PhaseOutcome) -> RouteDecision:
        """One-shot LLM diagnostician for a failure the decision tables cannot
        classify. Embeds the failure-artifact content in the prompt, spawns a
        read-only reasoning leaf, and parses its final JSON routing directive.
        An unparsable/invalid directive is conservatively terminal (fail_closed)."""
        context = self._gather_failure_context(refs, phase)
        prompt = _diagnosis_prompt(refs.node_key, phase, outcome.failed_substeps,
                                   context, self.workflow_mode,
                                   persona=_load_escalate_persona(self.repo_root))
        try:
            # The diagnostician has no record-launch profile (no child_arid); under
            # bwrap-enforced mode build a dedicated read-only profile (repo ro, no
            # write_roots) so it runs sandboxed instead of fail-closing. A read-only
            # leaf has nothing to attribute, so the FS-diff is trivially empty.
            profile = self._readonly_sandbox_profile() if self._bwrap_enabled() else None
            proc = self.spawn_leaf(
                prompt, self._child_env(self.orchestration_agent_run_id), profile=profile)
        except (SandboxEnforcementError, OSError) as exc:
            # The host cannot launch the sandboxed read-only diagnostician — either the
            # profile is unbuildable (SandboxEnforcementError) or the bwrap/backend
            # binary is missing (OSError/FileNotFoundError from subprocess.run, e.g. if
            # the startup preflight was bypassed). The diagnostician is a best-effort
            # recovery leaf, so treat an un-launchable diagnosis as conservatively
            # terminal — same posture as an unparsable directive — rather than crashing
            # the conductor or launching unconfined.
            self.emit("diagnose_launch_failed", phase=phase, error=str(exc)[:200])
            return RouteDecision("fail_closed", reason=f"{phase}_diagnose_sandbox_unavailable")
        self._persist_leaf_output(self.orchestration_agent_run_id, proc,
                                  prefix=f"diagnose.{phase}")
        decision = _parse_directive(proc.stdout)
        if decision is None:
            return RouteDecision("fail_closed", reason=f"{phase}_diagnose_unparsable")
        # G5: normalize reuse-vs-discard from the graded severity so every escalate site
        # (generate/compile verify, judge, the post_judge unknown) shares one policy.
        return resolve_severity_directive(decision)

    def classify_failure(self, refs: NodeRefs, phase: str,
                         outcomes: list[SubstepOutcome]) -> RouteDecision:
        """Map a failed phase to a routing decision (M3 wires the full tables)."""
        if phase == "build":
            meta = _read_json(self.repo_root / refs.binary_dir() / "binary_meta.json") or {}
            return classify_build_failure(meta.get("failure_category"))
        if phase == "generate" and outcomes and outcomes[-1].status != "pass":
            # The substep that failed is the last one the run_phase loop ran (it breaks on
            # first failure), so it maps to SUBSTEPS["generate"][len(outcomes)-1]. A gate failure
            # routes via its deterministic union table (warm resume); generate.generate /
            # generate.verify fall through to the verify-severity gate below.
            failed_substep = SUBSTEPS["generate"][len(outcomes) - 1]
            if failed_substep == "generate" and self._pure_leaf_substep(refs, "generate", "generate"):
                # Z2 pure producer exhausted its bounded in-conversation repair budget. Route on
                # the terminal category recorded in bundle_meta.json: every bundle category is a
                # document defect a fresh (generate, generate) attempt with a warm reuse repair
                # can fix (its excerpt threaded via _read_repair_findings). A transport/unknown
                # category has no bundle_meta route -> cold restart.
                meta = _read_json(self.repo_root / refs.source_dir() / "bundle_meta.json") or {}
                category = str(meta.get("failure_category") or "")
                route = GENERATE_BUNDLE_FAILURE_ROUTING.get(category)
                if route:
                    target, strategy = route
                    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                                         reason=f"{GENERATE_BUNDLE_REASON_PREFIX}{category}")
                return RouteDecision("retry", target_phase="generate", repair_strategy="restart",
                                     reason="generate_bundle_fail")
            if failed_substep == "verify" and self._pure_leaf_substep(refs, "generate", "verify"):
                # Z2 pure reviewer (M-D). Two failure shapes reach here:
                #   (a) a schema-EXHAUSTED verdict (unparseable / truncated / schema-invalid past
                #       the in-conversation repair budget): verdict_meta.json carries the terminal
                #       category, source_meta.json was NOT written (proof-of-work). Route on the
                #       category -> a cold generate restart (fresh producer + fresh reviewer). See
                #       GENERATE_VERDICT_FAILURE_ROUTING for why reuse is not an option here.
                #   (b) a schema-VALID `fail` verdict (the reviewer legitimately rejected the
                #       source): verdict_meta.json has no routed category, source_meta.json WAS
                #       written with the verdict projection -> fall through to the verify-severity
                #       gate below (classify_verify_severity), identical to the agentic verify leaf.
                vmeta = _read_json(self.repo_root / refs.source_dir() / "verdict_meta.json") or {}
                category = str(vmeta.get("failure_category") or "")
                route = GENERATE_VERDICT_FAILURE_ROUTING.get(category)
                if route:
                    target, strategy = route
                    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                                         reason=f"{GENERATE_VERDICT_REASON_PREFIX}{category}")
                # No routed category -> a valid `fail` verdict; fall through to the severity gate.
            if failed_substep == "gate":
                meta = _read_json(self.repo_root / refs.source_dir() / "gate_meta.json") or {}
                cats = meta.get("failure_categories")
                return classify_gate_failure(cats if isinstance(cats, list) else [])
        if phase == "compile" and outcomes and outcomes[-1].status != "pass":
            # Mirror of the generate branch: SUBSTEPS["compile"] == ("generate","static","verify")
            # and run_phase breaks on first failure, so the failed substep is index len-1. A
            # compile.static failure routes via its deterministic table (warm resume to
            # compile.generate); compile.generate / compile.verify fall through to the
            # verify-severity gate below.
            failed_substep = SUBSTEPS["compile"][len(outcomes) - 1]
            if failed_substep == "static":
                meta = _read_json(self.repo_root / refs.ir_ref / "compile_static_meta.json") or {}
                return classify_compile_static_failure(meta.get("failure_category"))
        if phase == "validate" and outcomes:
            # SUBSTEPS["validate"] == ("pre_judge","execute","judge","post_judge") and run_phase
            # breaks on first failure (a recovered post_judge warm-resume passes and never
            # reaches here), so the failed substep is index len-1.
            failed_substep = SUBSTEPS["validate"][len(outcomes) - 1]
            if failed_substep == "pre_judge":
                # Deterministic DAG-readiness failure: a non-physics integrity blocker (a
                # --with-deps closure not built+validated in its own pipeline). No judge ran,
                # so a normal fail step_result is legal (the launch_request_ref points at the
                # pre_judge deterministic substep, so the judge completion hook is skipped).
                return RouteDecision("fail_closed", reason="validate_pre_judge_dag_incomplete")
            if failed_substep == "post_judge":
                # Defensive: a post_judge gate failure is terminalized fail_closed in run_phase
                # (the post_judge_meta.status==fail branch) BEFORE classify_failure. Reaching
                # here would mean a post_judge failure with no meta — treat as integrity blocker.
                return RouteDecision("fail_closed", reason="validate_post_judge_violation")
            if failed_substep == "execute":
                # R2: an execute failure now has two kinds, disambiguated by whether execute
                # authored a verdict.json with a failure_class:
                #   (a) a per-test PREDICATE failure (physics_fail / structural_violation):
                #       evidence was structurally valid, but a deterministic predicate over
                #       diagnostics.json failed, so the judge leaf was intentionally not
                #       spawned. Attribution (code / ir / spec) needs reasoning, so route to
                #       the escalate diagnostician in prod (it reads verdict.json#per_test.basis)
                #       and fail_closed in dev (F1 cross-phase rollback posture). This preserves
                #       the routing the judge's failure_class x attribution used to drive.
                verdict = _read_json(self.repo_root / refs.run_node_dir() / "verdict.json") or {}
                fclass = verdict.get("failure_class")
                if fclass in ("physics_fail", "structural_violation"):
                    # A predicate failure is a DIFFERENT class than a no-verdict runner failure,
                    # so it breaks any run of consecutive no-verdict failures — reset the C2
                    # counter so it counts only CONSECUTIVE no-verdict execute failures (else a
                    # physics fail sandwiched between two runner failures would trip the Compile
                    # reopen one attempt early).
                    if hasattr(self, "_validate_execute_fail_count"):
                        self._validate_execute_fail_count[refs.node_key] = 0
                    reason = f"{VALIDATE_EXECUTE_REASON_PREFIX}{fclass}"
                    # A `predicate_error` verdict is the missing/malformed `test_predicates` DSL
                    # (_author_execute_verdict's guard), i.e. a defect in the already-certified IR
                    # that Generate cannot author. Attribute it to the IR with the same `_ir`
                    # suffix the C2 backstop and the host-rendered-runner re-attribution use: the
                    # diagnostician gets the attribution in prod, and in dev the suffix keeps the
                    # reason out of the resume directive's category set, so `--resume` does not
                    # reopen (and rebuild) a Generate that provably cannot converge.
                    if fclass == "structural_violation" and verdict.get("predicate_error"):
                        reason += "_ir"
                    if self.workflow_mode == "dev":
                        return RouteDecision("fail_closed", reason=reason)
                    return RouteDecision("escalate", reason=reason)
                #   (b) a STRUCTURAL/runtime execute failure (no verdict.json): the runner
                # produced bad/missing primary evidence (a runtime error or a post_execute
                # structural violation), which is a code defect -> regenerate. Route to Generate
                # deterministically rather than escalating with no verdict.
                # Backstop (C2): a Generate restart regenerates the RUNNER, which cannot
                # fix an IR-rooted structural mismatch (the runner keeps emitting its
                # natural shape; the IR is the wrong side). Count execute (no-verdict)
                # failures per node; once a Generate restart has already failed to fix one
                # (threshold 2, still within MAX_ATTEMPTS_PER_PHASE=3), attribute the
                # defect to the IR and reopen Compile instead of looping Generate.
                #
                # The counter resets BOTH (a) when escalating to Compile here and (b) when
                # validate advances (conduct, on validate pass). (a) is essential: the
                # Compile reopen regenerates the IR (and downstream source), so the next
                # execute failure is against FRESH artifacts and must get its own
                # Generate-retry-first cycle rather than immediately re-escalating because
                # a stale count is still >= 2.
                if not hasattr(self, "_validate_execute_fail_count"):
                    self._validate_execute_fail_count: dict[str, int] = {}
                count = self._validate_execute_fail_count.get(refs.node_key, 0) + 1
                if count >= C2_EXECUTE_FAIL_ESCALATION_THRESHOLD:
                    self._validate_execute_fail_count[refs.node_key] = 0
                    return RouteDecision("reopen", target_phase="compile",
                                         reason="validate_execute_fail_ir")
                self._validate_execute_fail_count[refs.node_key] = count
                # B1: below the C2 threshold, split the no-verdict failure by the category
                # _execute_inproc recorded in trial_meta.json. A recognized structural category
                # is a code defect the failing gate DESCRIBED, so repair it warm (reuse) with
                # that description threaded through as findings (_read_repair_findings), the
                # same treatment the judge's ("structural_violation","code") already gets. A
                # runner runtime error writes no trial_meta, and an unknown category is not
                # understood well enough to guide a repair — both keep the cold restart.
                trial = _read_json(self.repo_root / refs.run_node_dir() / "trial_meta.json") or {}
                category = trial.get("failure_category") if trial.get("status") == "fail" else None
                route = VALIDATE_EXECUTE_FAILURE_ROUTING.get(str(category or ""))
                if route:
                    # On an M3c node the runner is host-rendered, so a category the table sends to
                    # Generate may be structurally unrepairable there; attribute it to the IR and
                    # reopen Compile (the `_ir` reason suffix, as the C2 backstop uses). The suffix
                    # also keeps it out of the reuse set that `_read_repair_findings` and the dev
                    # B2 resume directive key on, so neither threads findings into a Generate
                    # repair that could not apply them.
                    if (str(category) in HOST_RENDERED_RUNNER_UNREPAIRABLE
                            and self._conductor_authors_runner(refs)):
                        # Reset the C2 counter for the same reason the threshold branch above
                        # does: this Compile reopen regenerates the IR and everything downstream,
                        # so the next execute failure is against FRESH artifacts and must get its
                        # own Generate-retry-first cycle. Without the reset the stale count (1)
                        # would push the very next failure — typically a leaf-repairable value
                        # defect in the regenerated checks module — straight into the
                        # findings-less C2 reopen, skipping the warm repair this table exists for.
                        self._validate_execute_fail_count[refs.node_key] = 0
                        return RouteDecision(
                            "reopen", target_phase="compile",
                            reason=f"{VALIDATE_EXECUTE_REASON_PREFIX}{category}_ir")
                    target, strategy = route
                    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                                         reason=f"{VALIDATE_EXECUTE_REASON_PREFIX}{category}")
                return RouteDecision("retry", target_phase="generate", repair_strategy="restart",
                                     reason="validate_execute_fail")
            verdict = _read_json(self.repo_root / refs.run_node_dir() / "verdict.json") or {}
            review = _read_json(self.repo_root / refs.run_node_dir() / "semantic_review.json") or {}
            findings = review.get("findings") or []
            attribution = findings[0].get("attribution") if findings and isinstance(findings[0], dict) else None
            failure_class = verdict.get("failure_class")
            # R2: the judge substep fails on `semantic_review.decision == "fail"` even when the
            # host-authored (execute) per_test is clean (a fabrication / consistency finding on
            # passing tests). In that case `verdict.failure_class` is `pass`, and classify_validate_judge would
            # treat `pass` as `advance` — silently dropping the finding and terminalizing the failed
            # phase as a generic `fail`. Route it to the diagnostician instead (it reads
            # semantic_review + verdict and decides reuse/restart/reopen/fail_closed), matching how an
            # under-specified judge output (missing class/attribution) already escalates.
            if (str(review.get("decision") or "").strip().lower() == "fail"
                    and failure_class in (None, "", "pass")):
                return RouteDecision("escalate", reason="judge_semantic_review_fail")
            return classify_validate_judge(failure_class, attribution)
        # compile / generate: verify severity gate. A failed phase with no
        # recorded severity (e.g. the producing substep itself failed) is
        # unclassifiable -> escalate to the diagnostician rather than guessing.
        #
        # A meta that violates the stage-meta contract is read FIRST and never routed by
        # severity: its fields are not trustworthy inputs to a decision table (the same posture
        # as `{phase}_fail_unclassified`). Reaching here means the warm-resume mini-loop
        # already spent its budget trying to get the leaf to re-author it, so this is the
        # terminal edge of that class — and it also covers a malformed meta left by the
        # PRODUCING substep, which the mini-loop (verify-scoped) does not touch.
        if self._stage_meta_contract_findings(refs, phase):
            return RouteDecision("escalate", reason=f"{phase}_fail_meta_schema")
        meta_path = (refs.ir_ref + "/ir_meta.json") if phase == "compile" else (refs.source_dir() + "/source_meta.json")
        meta = _read_json(self.repo_root / meta_path) or {}
        sev = meta.get("last_fail_severity") or meta.get("issue_severity")
        if not sev or sev == "none":
            return RouteDecision("escalate", reason=f"{phase}_fail_unclassified")
        return classify_verify_severity(sev, self.workflow_mode)

    def _consume_resume_directive(self, refs: NodeRefs,
                                  phases: list[str]) -> dict[str, dict[str, str]]:
        """Act on a `resume_directive` left in orchestration_meta.json by the resume
        (`cmd_init --resume-from-checkpoint`), returning the `pending_repair` it seeds.

        Only the dev structural-validate.execute directive
        (`_derive_dev_validate_execute_resume_directive`) is honored. In dev such a failure is
        terminal — F1 fail_closes a structural GATE failure as `dev_phase_rollback` instead of
        retrying it, and a per-test `structural_violation` verdict fail_closes directly
        (`conductor_phase_fail_closed`) — so a plain `--resume` would skip the checkpointed
        Generate/Build and re-run the identical binary into the identical deterministic failure.
        Reopening Generate here — with the failure's own violation text as warm repair findings —
        is the operator-initiated equivalent of the `("generate","reuse")` route prod takes
        automatically (B1). F1 is unchanged: an in-run automatic rollback still fail_closes.

        The producer arid is recovered BEFORE `reopen_phase`, which drops the checkpoint entry
        it is read from. A reopen failure degrades to a plain resume (no repair seeded) rather
        than crashing the run; the phases then simply re-run cold from Generate.
        """
        from tools.orchestration_runtime import (
            DEV_VALIDATE_EXECUTE_RESUME_SOURCE, LEAF_TRANSPORT_RESUME_SOURCE)

        meta = _read_json(self.repo_root / "workspace" / "orchestrations"
                          / self.orchestration_id / "orchestration_meta.json") or {}
        directive = meta.get("resume_directive")
        if not isinstance(directive, dict):
            return {}
        source = directive.get("source")
        # Item C: a transport-substep resume arms preseat state that run_phase consumes; it seeds
        # NO pending_repair (a transport death has nothing to repair — the producer passed), so it
        # returns {} either way and never routes through reopen_phase.
        if source == LEAF_TRANSPORT_RESUME_SOURCE:
            self._consume_transport_resume_directive(refs, phases, directive)
            return {}
        if source != DEV_VALIDATE_EXECUTE_RESUME_SOURCE:
            return {}
        if str(directive.get("node_key") or "").strip() != refs.node_key:
            return {}
        if str(directive.get("reopen_from") or "").strip() != "generate" or "generate" not in phases:
            return {}
        trigger = str(directive.get("trigger_agent_run_id") or "").strip()
        if not trigger:
            return {}
        # A Generate that is NOT checkpointed-complete will be re-run by the plain resume
        # anyway, and reopening it would archive the in-progress attempt. Nothing to do.
        completed = self.check_step_completed(refs.node_key, "generate")
        if completed is None:
            return {}
        producer = self._completed_producer_arid(
            refs.node_key, "generate", completed.get("agent_run_id"))
        try:
            result = self.reopen_phase(refs.node_key, from_phase="generate", trigger_arid=trigger,
                                       reason="dev_resume_validate_execute_structural")
        except Exception as exc:  # noqa: BLE001 - degrade to a plain resume
            self.emit("resume_directive_reopen_failed", node_key=refs.node_key,
                      detail=str(exc)[:200])
            return {}
        # A `noop` means a prior reopen already consumed this trigger, so Generate was NOT
        # reopened and stays checkpointed — run_phase would skip it and silently drop the repair.
        # The deriver already rejects superseded triggers; this is the second guard.
        if str(result.get("status") or "").strip() == "noop":
            self.emit("resume_directive_reopen_noop", node_key=refs.node_key, trigger=trigger)
            return {}
        payload: dict[str, str] = {
            "issue_severity": "major",
            "repair_strategy": "reuse",
            "repair_target_agent_run_id": producer or "none",
            "repair_reason": "validate_execute_structural_resume",
        }
        findings = directive.get("repair_findings")
        if isinstance(findings, str) and findings.strip():
            payload["repair_findings"] = findings.strip()
        self.emit("resume_directive_consumed", node_key=refs.node_key, phase="generate",
                  failure_category=str(directive.get("failure_category") or ""),
                  findings=bool(payload.get("repair_findings")))
        return {"generate": payload}

    def _consume_transport_resume_directive(
            self, refs: NodeRefs, phases: list[str], directive: dict[str, Any]) -> None:
        """Arm substep-granular preseat for a compile/generate phase that fail_closed on a leaf
        transport error whose verify substep died (`_derive_leaf_transport_resume_directive`).
        `run_phase` then seeds outcomes[0] with the surviving producer and relaunches only the
        deterministic mids + verify — instead of re-paying the billed producer leaf.

        Every check is defensive: any miss emits `transport_resume_declined` and returns, leaving
        the plain full-phase resume untouched (a decline is exactly today's behavior — no new
        failure mode, and no ref is mutated until every precondition holds)."""
        def _decline(reason: str) -> None:
            self.emit("transport_resume_declined", node_key=refs.node_key, reason=reason)

        if str(directive.get("node_key") or "").strip() != refs.node_key:
            return _decline("node_key_mismatch")
        step = str(directive.get("step") or "").strip().lower()
        if step not in ("compile", "generate") or step not in phases:
            return _decline("step_out_of_scope")
        if str(directive.get("resume_substep") or "").strip().lower() != "verify":
            return _decline("not_verify_resume")
        producer_arid = str(directive.get("producer_agent_run_id") or "").strip()
        artifact_id = str(directive.get("producer_artifact_id") or "").strip()
        if not producer_arid or not artifact_id:
            return _decline("incomplete_directive")
        # A phase already checkpointed complete is skipped by run_phase — nothing to preseat.
        if self.check_step_completed(refs.node_key, step) is not None:
            return _decline("phase_already_complete")
        # The producer must be a real pass row of THIS orchestration.
        from tools.orchestration_runtime import _load_run_records, _orchestration_root
        runs = _load_run_records(_orchestration_root(self.repo_root, self.orchestration_id))
        prod = runs.get(producer_arid)
        if not (isinstance(prod, dict)
                and str(prod.get("status") or "").strip().lower() == "pass"):
            return _decline("producer_row_absent")
        # Confirm the surviving artifact still exists BEFORE mutating any ref (so a decline leaves
        # the plain-resume refs intact). compile → reserved ir dir + spec.ir.yaml; generate → the
        # deliverable the reused verify substep actually consumes.
        if step == "compile":
            deliverable = (self.repo_root / "workspace" / "ir" / refs.safe
                           / artifact_id / "spec.ir.yaml")
        else:
            src_root = self.repo_root / refs.source_dir(artifact_id)
            # A PURE `generate.verify` reviewer's input is the producer's `codegen_bundle.json`, and
            # an absent file is NOT caught downstream: `_build_pure_verify_context` supplies an EMPTY
            # `bundle_document`, and the re-run `post_generate` gate (`_validate_post_generate_bundle`)
            # SKIPS bundle re-validation when the file is missing. Reusing a source dir without it
            # would let the reviewer certify blind, so require it — the mid re-run of post_generate
            # then re-validates the whole bundle (incl. every `files[]` entry byte-for-byte), so this
            # one check backstops the reuse. An agentic node has no bundle, so require `src/` as before.
            deliverable = (src_root / "codegen_bundle.json"
                           if self._pure_leaf_substep(refs, "generate", "verify")
                           else src_root / "src")
        if not deliverable.exists():
            return _decline("artifact_dir_missing")
        # All checks passed: re-point refs at the surviving artifact (compile is normally a no-op —
        # the reservation already yields it; generate corrects the day-boundary source_id default)
        # and arm the preseat run_phase reads.
        if step == "compile":
            refs.ir_id = artifact_id
        else:
            refs.source_id = artifact_id
        if not hasattr(self, "_substep_resume"):
            self._substep_resume: dict[str, dict[str, str]] = {}
        self._substep_resume[step] = {
            "producer_arid": producer_arid, "artifact_id": artifact_id}
        self.emit("transport_substep_resume", node_key=refs.node_key, step=step,
                  resume_substep="verify", producer_arid=producer_arid, artifact_id=artifact_id)

    def conduct(self, refs: NodeRefs, until_phase: str) -> str:
        """Drive the phases, acting on each phase's cross-phase routing decision:
        reopen an upstream (already-passed) phase, fail_closed, or escalate. The
        per-phase attempt budget bounds the reopen loop."""
        phases = phases_through(until_phase)
        attempts: dict[str, int] = {p: 0 for p in phases}
        pending_repair: dict[str, dict[str, str]] = {}
        # A resume may carry a directive to reopen an already-passed phase and repair it with
        # the findings of the failure that terminalized the prior run (dev F1 deadlock break).
        pending_repair.update(self._consume_resume_directive(refs, phases))
        idx = 0
        while idx < len(phases):
            phase = phases[idx]
            self.emit("phase_start", node_key=refs.node_key, phase=phase,
                      attempt=attempts[phase] + 1)
            phase_started = time.monotonic()
            try:
                outcome = self.run_phase(refs, phase, repair=pending_repair.pop(phase, None))
            except SandboxEnforcementError as exc:
                # bwrap-enforced mode + a leaf with no usable profile: terminalize as
                # fail_closed (the sandbox-enforcement failure path) rather than letting
                # it bubble to run_workflow's generic conductor_error/fail handler.
                self.set_status("fail_closed", reason_code="sandbox_enforcement_violation",
                                reason_detail=str(exc)[:200])
                return "fail_closed"
            if outcome.skipped:
                # Already checkpointed complete (resume): no body ran, so an
                # elapsed time would be misleading — report it as skipped instead.
                self.emit("phase_complete", node_key=refs.node_key, phase=phase,
                          result="skipped")
            else:
                self.emit("phase_complete", node_key=refs.node_key, phase=phase,
                          result=outcome.status,
                          elapsed_seconds=round(time.monotonic() - phase_started, 2))
            if outcome.status == "pass":
                # validate advanced: a later, unrelated execute failure should start its
                # escalation count fresh (C2 backstop counter).
                if phase == "validate" and hasattr(self, "_validate_execute_fail_count"):
                    self._validate_execute_fail_count.pop(refs.node_key, None)
                idx += 1
                continue

            decision = outcome.decision or RouteDecision("escalate", reason="no_decision")
            if decision.action == "escalate":
                # escalate() runs resolve_severity_directive, so a retry/reopen directive always
                # arrives here with a concrete repair_strategy derived from its graded severity
                # (G5: minor/major -> reuse, major-override/critical -> restart, re_execute
                # passthrough). No conduct-side strategy normalization is needed — the severity
                # policy is the single source of truth. A same-phase (compile/generate) target
                # with a reuse/restart strategy fires the producer reopen below; a null / build /
                # validate target terminalizes as before.
                escalate_source = decision.reason
                decision = self.escalate(refs, phase, outcome)
                # G5: the validate post_judge / judge-conformance escalate returned WITHOUT
                # tombstoning its orphan arids (skip-write posture) so that a diagnostician
                # UPSTREAM REOPEN's trigger stays live (reopen_phase no-ops on an
                # already-superseded trigger). For EVERY
                # terminal, non-reopen resolution — fail_closed, an unparsable/sandbox directive,
                # a null/out-of-scope target that falls to the `target_idx >= idx` terminal
                # `fail`, or a budget-exhausted reopen — no reopen supersedes the attempt, so
                # tombstone the orphans here (matching the transport/integrity terminal branches)
                # to keep a later resume/pass completion vouch from tripping. Only an upstream
                # reopen that will actually fire supersedes them via reopen_phase, so exclude
                # exactly that case (mirroring conduct's own reopen conditions below: an in-scope
                # upstream target with budget remaining).
                if escalate_source in ("validate_post_judge_unknown",
                                       "validate_judge_conformance_violation") and outcome.substep_arids:
                    tgt = decision.target_phase
                    will_upstream_reopen = (
                        decision.action in ("retry", "reopen")
                        and tgt in phases
                        and phases.index(tgt) < idx
                        and attempts[tgt] + 1 <= MAX_ATTEMPTS_PER_PHASE)
                    if not will_upstream_reopen:
                        orphan_reason = (
                            "validate_judge_conformance_escalate_terminal_orphan"
                            if escalate_source == "validate_judge_conformance_violation"
                            else "validate_post_judge_escalate_terminal_orphan")
                        self._add_superseded_run_ids(
                            list(outcome.substep_arids), reason=orphan_reason)
            if decision.action == "fail_closed":
                reason = decision.reason or ""
                # Map to an allowlisted FAIL_CLOSED_REASON_CODES value (the runtime
                # rejects any other code for fail_closed); the specific routing reason is
                # preserved in reason_detail.
                if reason.startswith("leaf_transport_error"):
                    reason_code = "leaf_transport_error"
                elif "sandbox" in reason:
                    # diagnostician could not be sandboxed under mandatory bwrap
                    reason_code = "sandbox_enforcement_violation"
                else:
                    reason_code = "conductor_phase_fail_closed"
                self.set_status("fail_closed", reason_code=reason_code,
                                reason_detail=reason[:200])
                return "fail_closed"

            target = decision.target_phase or phase
            if target not in phases:
                self.set_status("fail", reason_code=f"{phase}_fail",
                                reason_detail=f"route_target_out_of_scope:{target}")
                return "fail"

            target_idx = phases.index(target)
            # F1: dev confines auto-retry to WITHIN a single phase (the run_phase substep
            # loop, e.g. generate.generate -> generate.verify -> regenerate). A cross-phase
            # backward rollback — the only routing that actually reopens an already-passed
            # upstream phase (target_idx < idx, the branch below) — fail_closes immediately so
            # the operator sees the structural issue on the first occurrence instead of burning
            # the whole attempt budget on a regeneration loop that cannot fix it (the
            # C1/C2/D2 "regenerating one side can't fix the other" pattern). This generalizes
            # the older dev verify/judge-severity gate (classify_verify_severity) to "any
            # cross-phase rollback regardless of failure classification". `target_idx < idx`
            # already covers every real reopen (they all target compile = upstream) and every
            # earlier-phase retry; a same-phase/forward (malformed) reopen is NOT a backward
            # rollback, so it falls through to the `target_idx >= idx` terminal-fail branch
            # below — same as prod — rather than being mislabeled a dev_phase_rollback. prod
            # keeps today's bounded cross-phase reopen/retry (the C2 backstop's compile reopen
            # stays live for prod and is a no-op here for dev).
            if self.workflow_mode == "dev" and target_idx < idx:
                self.set_status("fail_closed", reason_code="dev_phase_rollback",
                                reason_detail=(decision.reason or f"{phase}->{target}")[:200])
                return "fail_closed"

            attempts[target] += 1
            if attempts[target] > MAX_ATTEMPTS_PER_PHASE:
                self.set_status("fail_closed", reason_code="retry_budget_exhausted",
                                reason_detail=f"{target} exceeded {MAX_ATTEMPTS_PER_PHASE}")
                return "fail_closed"

            # Same-phase producer reopen: re-run the SAME phase's producer substep
            # (compile.generate / generate.generate) to fix a finding, instead of terminalizing.
            # This is the canonical recovery for any decision that targets the current phase with
            # a concrete repair_strategy — the deterministic gates (generate.gate,
            # compile.static) and verify-minor (all `reuse` -> warm), AND the escalate
            # diagnostician when it judges the right level is "this phase's own producer"
            # (`reuse` -> warm / `restart` -> cold, e.g. a major IR defect regenerated from
            # scratch). The producer id is rotated on re-run (_ensure_fresh_producer_id), avoiding
            # the error-prone in-place-retry bookkeeping the old design forbade. A same-phase
            # decision with NO repair_strategy (a malformed/unflagged retry) still terminalizes
            # via the `target_idx >= idx` branch below. Bounded by attempts[target]. The dev
            # rollback guard (target_idx < idx) does not fire — this is a within-phase reopen,
            # which dev keeps (F1 confines dev fail-fast to cross-phase rollbacks).
            if (target_idx == idx and target == phase
                    and phase in ("compile", "generate")
                    and decision.action in ("retry", "reopen")
                    and decision.repair_strategy in ("reuse", "restart")):
                trigger = outcome.failed_substeps[-1] if outcome.failed_substeps else None
                if trigger is None:
                    self.set_status("fail", reason_code=f"{phase}_fail",
                                    reason_detail="same_phase_reopen_no_trigger")
                    return "fail"
                # Read the findings excerpt BEFORE reopen_phase/rotation while refs still names
                # the failed artifact (its {gate,compile_static}_meta.json failure_excerpt, or the
                # verify meta last_fail_reason). None for a diagnostician reason -> the repair
                # falls back to the full prompt (a cold restart re-derives anyway).
                findings = self._read_repair_findings(refs, decision.reason, phase)
                self.reopen_phase(refs.node_key, from_phase=phase, trigger_arid=trigger,
                                  reason=decision.reason or "same_phase_reopen")
                pending_repair[phase] = self._repair_payload(
                    decision, self._producer_arid.get(phase, "none"), findings=findings)
                continue  # idx unchanged -> re-run the phase producer with the repair

            if target_idx >= idx:
                # same/downstream target reaching conduct means run_phase already
                # exhausted its in-place retries -> terminal.
                self.set_status("fail", reason_code=f"{phase}_fail",
                                reason_detail=(decision.reason or "")[:200])
                return "fail"

            # upstream target is checkpointed pass -> reopen it (and downstream).
            trigger = outcome.failed_substeps[-1] if outcome.failed_substeps else None
            if trigger is None:
                self.set_status("fail", reason_code=f"{phase}_fail",
                                reason_detail="reopen_no_trigger")
                return "fail"
            # As in the same-phase branch: read the findings excerpt BEFORE reopen_phase, while
            # refs still names the failed artifact (a validate.execute structural failure keeps
            # its excerpt in the failed run's trial_meta.json, and reopen rotates the run id).
            # Every other cross-phase reason yields None -> the repair falls back to the full
            # prompt, exactly as before.
            findings = self._read_repair_findings(refs, decision.reason, phase)
            self.reopen_phase(refs.node_key, from_phase=target, trigger_arid=trigger,
                              reason=decision.reason or f"{phase}_reopen")
            if decision.repair_strategy and decision.repair_strategy not in ("none", None):
                pending_repair[target] = self._repair_payload(
                    decision, self._producer_arid.get(target, "none"), findings=findings)
            idx = target_idx
        self.set_status("pass")
        return "pass"


# --- phase deliverables (step_result.required_outputs) -------------------------
#
# validation_stage and required_outputs per phase, grounded in real step_result.json
# (see test fixtures). required_outputs is the phase deliverable subset, NOT the full
# union of substep allowed_output_paths.

PHASE_VALIDATION_STAGE: dict[str, str] = {
    "compile": "compile",
    "generate": "post_generate",
    "build": "post_build",
    "validate": "pre_judge",
}


def phase_required_outputs(refs: NodeRefs, phase: str, exe_name: str | None = None,
                           *, makefile_required: bool = True,
                           runner_host_authored: bool = False) -> list[str]:
    if phase == "compile":
        return [f"{refs.ir_ref}/spec.ir.yaml", f"{refs.ir_ref}/ir_meta.json"]
    if phase == "generate":
        src = refs.source_dir()
        # lineage.json is authored host-side by the conductor (_write_lineage), not a leaf
        # output_ref, so it is NOT a step required_output (which must be covered by the
        # producer leaf's output_refs). post_generate still verifies it independently. For a
        # leaf node src/Makefile is likewise conductor-authored (_write_makefile), so it is
        # excluded when makefile_required is False (same rationale as lineage). On an M3c node
        # the runner is conductor-rendered (_write_runner) and the leaf authors _checks.f90
        # instead — swap it symmetrically (same rationale as the Makefile).
        make_entry = [f"{src}/src/Makefile"] if makefile_required else []
        runner_or_checks = (f"{src}/src/{refs.spec_id}_checks.f90" if runner_host_authored
                            else f"{src}/src/{refs.spec_id}_runner.f90")
        return [
            f"{src}/src/{refs.spec_id}_model.f90",
            runner_or_checks,
            *make_entry,
            f"{src}/source_meta.json",
        ]
    if phase == "build":
        bdir = refs.binary_dir()
        # The binary basename = the imposed canonical exe name (mirrors
        # build_launch_request's exe_name); the <spec_id>_runner fallback applies only
        # when no exe_name is threaded (non-build callers).
        return [
            f"{bdir}/bin/{exe_name or (refs.spec_id + '_runner')}",
            f"{bdir}/binary_meta.json",
            f"{refs.source_dir()}/src/command_log.jsonl",
        ]
    if phase == "validate":
        rundir = refs.run_node_dir()
        return [
            f"{rundir}/aggregate_verdict.json",
            f"{rundir}/verdict.json",
            f"{rundir}/summary.json",
            f"{rundir}/semantic_review.json",
            f"{rundir}/validate_meta.json",
        ]
    raise ValueError(f"unknown phase: {phase}")


# --- loop outcome types --------------------------------------------------------


@dataclass
class SubstepOutcome:
    agent_run_id: str
    status: str
    output_refs: list[str]
    # The leaf process exit code. Nonzero is an infra/transport failure (token
    # limit, OOM, transport) — not a content failure the decision tables can
    # classify — so run_phase routes it straight to fail_closed.
    leaf_returncode: int = 0
    # (tag, evidence_line) when the failed leaf's captured output named an LLM-infrastructure
    # cause. Carried STRUCTURALLY so the phase-level transport branch can name WHY the leaf died:
    # re-deriving it there from the already-truncated result_summary would silently lose the tag
    # whenever the marker sat past the truncation point.
    infra_error: tuple[str, str] | None = None
    # Wall-clock instant this substep's leaf was launched (== the `min_mtime` its
    # determine_substep_status freshness check used). Carried so a later attribution check can
    # ask "did THIS leaf author that artifact?" — an mtime older than this belongs to an
    # earlier substep, not to the one that just failed.
    launched_at: float = 0.0
    # How many times run_substep launched this substep, counting the surviving/last attempt
    # (1 = no retry). >1 means a transient LLM-infrastructure failure was retried in place; the
    # dead attempts are tombstoned and not carried here.
    attempts: int = 1


@dataclass
class PhaseOutcome:
    phase: str
    status: str
    substep_arids: list[str] = field(default_factory=list)
    failed_substeps: list[str] = field(default_factory=list)
    decision: RouteDecision | None = None
    # True when a --resume short-circuited the phase because it was already
    # checkpointed complete (no body re-run). Lets conduct() avoid reporting a
    # misleading ~0.0s elapsed time for a phase that did not actually execute.
    skipped: bool = False


# --- node resolution + id allocation + entrypoint ------------------------------


def _slug_of(spec_id: str) -> str:
    """Deterministic id slug: lower-case spec_id, non-alnum runs -> '-'. Matches
    the reserve-phase-root canonical-format regex (the specific value is free)."""
    return re.sub(r"[^a-z0-9]+", "-", spec_id.lower()).strip("-") or "node"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _next_seq(parent: Path, prefix: str) -> str:
    """Next 3-digit sequence for <prefix>_<NNN> directories under parent."""
    mx = 0
    if parent.is_dir():
        pat = re.compile(re.escape(prefix) + r"_(\d{3})$")
        for d in parent.iterdir():
            m = pat.match(d.name)
            if m:
                mx = max(mx, int(m.group(1)))
    return f"{mx + 1:03d}"


_SPEC_REF_FILE_NAMES = frozenset({"controlled_spec.md", "tests.md", "deps.yaml"})


def resolve_node(repo_root: Path, spec_ref: str) -> tuple[str, str]:
    """Resolve (node_key, spec_path) from spec_ref via spec_catalog.yaml.

    Accepts the same spec_ref forms as run_workflow: a spec directory OR a
    file-style ref (controlled_spec.md / tests.md / deps.yaml) under it — the
    latter is normalized to its parent directory before the catalog lookup.
    """
    ref = Path(spec_ref.strip().rstrip("/"))
    spec_dir = ref.parent if ref.name in _SPEC_REF_FILE_NAMES else ref
    spec_id = spec_dir.name
    # M3d spec-input gate: bound spec_id length before any phase runs. A spec_id
    # over MAX_SPEC_ID_LEN is a node-IDENTITY defect (the compile.static hoist
    # excludes it because a re-author cannot shorten a spec_id) that would otherwise
    # fail-close at conductor render time on a harness-backed node — a workflow-kill.
    # This is the canonical capture point (see runner_renderer.spec_id_length_violation).
    # Deliberately spec-input (pre-IR), so it is language/phase-agnostic: the 55-char
    # bound reflects the f2008 identifier limit of the ONLY current backend (fortran),
    # where every >55 spec_id is doomed regardless of phase. It also rejects a >55 spec
    # on a Compile-only run — acceptable while every backend is fortran. When a backend
    # with a different identifier limit is added, move the bound to a language-aware point.
    from tools.runner_renderer import spec_id_length_violation
    _sid_violation = spec_id_length_violation(spec_id)
    if _sid_violation:
        raise ValueError(
            f"spec-input rejected: {_sid_violation} (from spec_ref {spec_ref})")
    catalog = _read_yaml(repo_root / "spec" / "registry" / "spec_catalog.yaml") or {}
    for entry in catalog.get("specs") or []:
        if isinstance(entry, dict) and entry.get("spec_id") == spec_id:
            kind = entry["spec_kind"]
            version = entry["spec_version"]
            spec_path = str(Path(entry["controlled_spec_path"]).parent)
            return f"{kind}/{spec_id}@{version}", spec_path
    raise ValueError(
        f"spec_id not found in spec_catalog.yaml: {spec_id} (from spec_ref {spec_ref})")


def resume_node_refs(conductor: "Conductor", node_key: str, spec_path: str) -> NodeRefs:
    """Reconstruct NodeRefs from the RESUMED ORCHESTRATION's own records (NOT the
    global-latest workspace dirs, which could belong to a different/newer run).
    ir_id/pipeline_id come from this orchestration's reservations; source/binary/run
    come from its checkpoint's completed-step outputs (fresh ids are allocated for a
    producing phase that has not run yet, which the resumed run then creates)."""
    safe = node_key_safe(node_key)
    orch_dir = (conductor.repo_root / "workspace" / "orchestrations"
                / conductor.orchestration_id)
    res_dir = orch_dir / "reservations" / safe
    ir_id = (_read_json(res_dir / "compile.json") or {}).get("reserved_ir_id")
    pipeline_id = (_read_json(res_dir / "generate.json") or {}).get("reserved_ir_id")
    if not ir_id or not pipeline_id:
        # A resume re-resolves the node_key from the CURRENT catalog, so a spec_version bump
        # since the orchestration started makes it look for reservations under a node_key that
        # orchestration never held. That is not a corrupt run — the respec invalidated it — but
        # the bare "missing reservation" reads as one, so name the likely cause.
        reservations_root = orch_dir / "reservations"
        held = sorted(p.name for p in reservations_root.glob("*")) if (
            reservations_root.is_dir()) else []
        hint = ""
        if held and safe not in held:
            hint = (f"; this orchestration reserved {held} — if the spec_version was bumped, its "
                    f"node_key changed and the orchestration cannot be resumed: start a new run")
        raise ValueError(
            f"conductor resume: missing ir/pipeline reservation for {node_key} in "
            f"{conductor.orchestration_id}{hint}")

    source_id = binary_id = run_id = None
    checkpoint = _read_json(orch_dir / "orchestration_checkpoint.json") or {}
    for entry in checkpoint.get("completed_steps", []):
        if not isinstance(entry, dict) or entry.get("node_key") != node_key:
            continue
        for ref in entry.get("output_refs", []):
            if not isinstance(ref, str):
                continue
            if "/source/" in ref:
                source_id = ref.split("/source/")[1].split("/")[0]
            if "/binary/" in ref:
                binary_id = ref.split("/binary/")[1].split("/")[0]
            if "/runs/" in ref:
                run_id = ref.split("/runs/")[1].split("/")[0]
    date = _today()
    source_id = source_id or f"src_{date}_001"
    binary_id = binary_id or f"bin_{date}_001"
    run_id = run_id or f"run_{date}_001"
    return NodeRefs(
        node_key=node_key, spec_path=spec_path,
        ir_id=ir_id, pipeline_id=pipeline_id,
        source_id=source_id, binary_id=binary_id, run_id=run_id,
        source_binary_id=binary_id,
    )


def prepare_node(conductor: "Conductor", node_key: str, spec_path: str) -> NodeRefs:
    """Allocate canonical ids (ir/pipeline/source/binary/run) and reserve the
    ir_id + pipeline_id roots before the Compile phase runs."""
    safe = node_key_safe(node_key)
    slug = _slug_of(spec_id_of(node_key))
    date = _today()
    ir_id = f"{slug}_{date}_{_next_seq(conductor.repo_root / 'workspace' / 'ir' / safe, f'{slug}_{date}')}"
    pipeline_id = (
        f"{slug}_{date}_"
        f"{_next_seq(conductor.repo_root / 'workspace' / 'pipelines' / safe, f'{slug}_{date}')}"
    )
    refs = NodeRefs(
        node_key=node_key, spec_path=spec_path,
        ir_id=ir_id, pipeline_id=pipeline_id,
        source_id=f"src_{date}_001", binary_id=f"bin_{date}_001",
        run_id=f"run_{date}_001", source_binary_id=f"bin_{date}_001",
    )
    by = conductor.orchestration_agent_run_id
    conductor.reserve_root(node_key, "compile", ir_id, by)
    conductor.reserve_root(node_key, "generate", pipeline_id, by)
    return refs


def run_conductor(*, repo_root: Path | str, orchestration_id: str,
                  orchestration_agent_run_id: str, spec_ref: str,
                  source_dependency_ref: str, until_phase: str, backend: str,
                  agent_model: str, workflow_mode: str, env: dict[str, str],
                  llm_command: str = "", resume: bool = False,
                  wait_usage_reset: bool = False) -> str:
    """Conductor entrypoint used by run_workflow.py (the only orchestration driver).
    Resolves the node, allocates+reserves ids (or, on resume, reuses the checkpointed
    ids), and runs the deterministic phase loop. Returns the terminal orchestration
    status (pass | fail | fail_closed)."""
    root = Path(repo_root)
    node_key, spec_path = resolve_node(root, spec_ref)
    # An explicit --agent-model wins; otherwise fall back to the backend's unpinned
    # spec-side alias (claude -> settings alias / "opus"; codex -> "codex"). Never a
    # pinned version: the exact version is resolved post-run from the leaf transcript.
    from tools.orchestration_runtime import default_agent_model_for_backend
    resolved_agent_model = agent_model or default_agent_model_for_backend(backend)
    conductor = Conductor(
        repo_root=root, orchestration_id=orchestration_id,
        orchestration_agent_run_id=orchestration_agent_run_id,
        backend=backend, env=env,
        agent_model=resolved_agent_model, workflow_mode=workflow_mode,
        llm_command=llm_command, wait_usage_reset=wait_usage_reset,
    )
    refs = (resume_node_refs(conductor, node_key, spec_path) if resume
            else prepare_node(conductor, node_key, spec_path))
    return conductor.conduct(refs, until_phase.lower())

