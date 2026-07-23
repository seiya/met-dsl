#!/usr/bin/env python3
"""Validate workflow pipeline semantic anti-cheat rules under workspace/."""

from __future__ import annotations

import argparse
import hashlib
import json
import re

import yaml
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

try:
    from tools.meta_contracts import (
        STAGE_META_FILENAME_BY_STEP,
        required_meta_keys_for_step,
        stage_meta_type_violations,
    )
    # PURE_PROMPT_SENTINEL is IMPORTED (not copy-pasted like SLIM_REPAIR_PROMPT_SENTINEL): the
    # Z2 pure sentinel has a single source in tools/pure_leaf so orchestration_runtime, the
    # prompt templates, and this validator cannot drift (a parity test still pins template
    # line 0 against it). pure_leaf is stdlib-only, so importing it here introduces no cycle.
    from tools.pure_leaf import (
        PURE_CAPABILITY_MODE,
        PURE_PROMPT_CONTRACT_VERSION,
        PURE_PROMPT_SENTINEL,
        is_pure_request as _pure_leaf_is_pure_request,
    )
except ModuleNotFoundError:  # pragma: no cover - import bootstrap for direct CLI execution
    _THIS_FILE = Path(__file__).resolve()
    _REPO_ROOT = _THIS_FILE.parent.parent
    import sys

    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from tools.meta_contracts import (
        STAGE_META_FILENAME_BY_STEP,
        required_meta_keys_for_step,
        stage_meta_type_violations,
    )
    from tools.pure_leaf import (
        PURE_CAPABILITY_MODE,
        PURE_PROMPT_CONTRACT_VERSION,
        PURE_PROMPT_SENTINEL,
        is_pure_request as _pure_leaf_is_pure_request,
    )

PLACEHOLDER_TEXT_PATTERNS = (
    '"sample":"state_recorded"',
    '"dummy"',
    '"placeholder"',
)

SNAPSHOT_SCHEMA_FILE = "snapshot_schema.json"
FORBIDDEN_RUNNER_OUTPUTS = (
    "verdict.json",
    "aggregate_verdict.json",
    "summary.json",
    "trial_meta.json",
)
LLM_REVIEW_FILENAME = "semantic_review.json"
FORTRAN_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]*")
RAW_EVIDENCE_ARTIFACTS = {
    "metrics_basis.json",
    "execution_trace.json",
    "state_snapshots",
}
ALGORITHM_EXECUTION_MODES = {"sequence", "conditional", "iterative", "columnwise"}
ALGORITHM_STEP_KINDS = {
    "boundary_apply",
    "reconstruct",
    "flux_compute",
    "source_term",
    "time_integrate",
    "column_process",
    "pointwise_process",
    "iterative_solve",
    "filter",
    "reduction",
    "diagnostic",
}
RAW_EVIDENCE_ALIASES = {
    "metrics_basis.json": "metrics_basis.json",
    "raw/metrics_basis.json": "metrics_basis.json",
    "execution_trace.json": "execution_trace.json",
    "raw/execution_trace.json": "execution_trace.json",
    "state_snapshots": "state_snapshots",
    "raw/state_snapshots": "state_snapshots",
    "raw/state_snapshots/": "state_snapshots",
}
FORTRAN_KEYWORDS = {
    "if",
    "then",
    "else",
    "endif",
    "do",
    "enddo",
    "call",
    "subroutine",
    "module",
    "contains",
    "intent",
    "in",
    "out",
    "inout",
    "real",
    "integer",
    "logical",
    "character",
    "type",
    "public",
    "private",
    "use",
    "only",
    "true",
    "false",
}
QUALITY_CHECK_ALLOWED_COMMANDS = {"make", "ctest", "pytest"}
FORBIDDEN_QUALITY_CHECK_EXECUTABLES = {"python", "python3", "pypy", "bash", "sh", "zsh"}
MAKE_QUALITY_CHECK_REQUIRED_LANGUAGES = {"fortran", "c", "cpp", "mixed"}
TEST_ID_HEADING_PATTERN = re.compile(r"^###\s+\d+-\d+\.\s+`([^`]+)`\s*$")
# tests.md test-id declarations come in two forms: the problem-spec heading form
# (`### 6-1. `<id>``, matched above) and the component/profile bullet form
# (`- `test_id`: `<id>``). The bullet form captures the SECOND backtick group (the id),
# not the literal `test_id` label; anchoring on the `test_id`: key excludes sibling bullets
# like `- `pass_when`:` / `- `suite.pass_rule`:`.
TEST_ID_BULLET_PATTERN = re.compile(r"^-\s+`test_id`\s*:\s*`([^`]+)`")
TEST_OUTCOME_VALUES = {"pass", "fail", "xfail", "skipped", "blocked"}
# Bundled schema lives next to this validator; used as the canonical fallback
# when no target repo_root is in scope (tests, ad-hoc invocation) and as the
# default canonical reference for the validator's pinned rules.
_BUNDLED_SHAPE_EXPR_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "spec" / "schema" / "ir" / "shape_expr.schema.json"
)
# Active repo_root for schema resolution. Set by main() via --repo-root and by
# stage-level entry points (validate, validate_compile_stage, ...). When set, the
# target repo's spec/schema/... is preferred over the validator bundle, so a
# repo with diverged shape_expr rules is validated against ITS rules rather
# than the validator-installation's bundled copy.
_active_repo_root_for_schema: ContextVar["Path | None"] = ContextVar(
    "_active_repo_root_for_schema", default=None
)
# Strip outer brackets/parens to expose the comma-separated body. The
# inner grammar (what each dim token may look like) is owned entirely by
# the active schema's list-form regex — this split is just a syntactic
# extractor for downstream binding/equality logic.
_SHAPE_EXPR_DIM_SPLIT = re.compile(r"^[\[\(]\s*(.+?)\s*[\]\)]$")



@contextmanager
def _pinned_repo_root_for_schema(repo_root: "Path | None") -> Iterator[None]:
    """Scope `_active_repo_root_for_schema` to `repo_root` for the duration of
    the with-block, then reset to the previous value via the captured token.

    Use this at every public validator entrypoint that accepts a `repo_root`
    so schema resolution is bound to the caller's intent rather than ambient
    process-global state. Without scoped reset, consecutive in-process calls
    against different repo roots would leak the first repo's context into
    subsequent validations.
    """
    token = _active_repo_root_for_schema.set(repo_root)
    try:
        yield
    finally:
        _active_repo_root_for_schema.reset(token)


def _resolve_shape_expr_schema_path(repo_root: "Path | None" = None) -> Path:
    """Resolve the canonical shape_expr.schema.json for the active validation.

    Resolution rules (fail-closed when scope is in effect):
      - If `repo_root` is provided OR `_active_repo_root_for_schema` is set,
        the validation has a target repo in scope. Require the target's
        `<repo_root>/spec/schema/ir/shape_expr.schema.json`. If it is
        missing, raise `RuntimeError` instead of silently falling back to
        the validator bundle — the caller (CLI main) converts this into a
        structured `pipeline semantic validation: FAIL` violation. Falling
        back here would let partial deploys, bad rebases, or repo-specific
        rule changes validate against the wrong rule set.
      - If neither is set (library / ad-hoc / unit-test invocation with no
        target repo), the validator-bundled schema is used. This covers
        `_parse_shape_expr` calls that originate from tests or other tools
        importing the validator without a target repo.

    Explicit `repo_root` takes priority over the active context when both
    are set (caller intent wins).
    """
    chosen: Path | None = None
    if repo_root is not None:
        chosen = repo_root
    else:
        active = _active_repo_root_for_schema.get()
        if active is not None:
            chosen = active
    if chosen is not None:
        candidate = chosen / "spec" / "schema" / "ir" / "shape_expr.schema.json"
        if not candidate.is_file():
            raise RuntimeError(
                f"shape_expr schema not found at {candidate}. "
                "Canonical source: spec/schema/ir/shape_expr.schema.json. "
                "When a repo_root is in scope (--repo-root or active context), "
                "the target repo must ship this schema; the validator bundle is "
                "not used as a silent fallback."
            )
        return candidate
    return _BUNDLED_SHAPE_EXPR_SCHEMA_PATH


@lru_cache(maxsize=8)
def _load_shape_expr_patterns_by_mtime(
    schema_path_str: str,
    mtime_ns: int,  # noqa: ARG001 — used only as a cache-busting key
) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    """Internal mtime-keyed cache. The mtime arg invalidates the entry when
    the schema file content changes at the same path (rebases, branch
    switches, schema repairs) within a long-lived process. Without this,
    the loader would memoize a stale or previously-broken parse.
    """
    schema_path = Path(schema_path_str)
    try:
        schema_text = schema_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"shape_expr schema not found at {schema_path}. "
            "Canonical source: spec/schema/ir/shape_expr.schema.json"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"shape_expr schema {schema_path} is unreadable: {exc}. "
            "Canonical source: spec/schema/ir/shape_expr.schema.json"
        ) from exc
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"shape_expr schema {schema_path} is malformed JSON: {exc}. "
            "Canonical source: spec/schema/ir/shape_expr.schema.json"
        ) from exc
    # Structural validation: every malformed-but-valid-JSON case must produce
    # a RuntimeError so callers (CLI main + run_workflow startup guard) emit
    # structured failure output instead of an opaque AttributeError traceback.
    if not isinstance(schema, dict):
        raise RuntimeError(
            f"shape_expr schema {schema_path} top-level must be a JSON object "
            f"(got {type(schema).__name__})"
        )
    if "oneOf" not in schema:
        raise RuntimeError(
            f"shape_expr schema {schema_path} must declare a top-level 'oneOf' array"
        )
    branches = schema["oneOf"]
    if not isinstance(branches, list):
        raise RuntimeError(
            f"shape_expr schema {schema_path} 'oneOf' must be a JSON array "
            f"(got {type(branches).__name__})"
        )
    # Classify each branch by what shape category it expresses. Two paths:
    #
    #   1. Explicit metadata via `x-shape-form` (recommended): one of
    #      "scalar" | "list" | "tuple". This is grammar-agnostic — the
    #      schema author asserts the structural category and the loader
    #      trusts that assertion. Treats list and tuple as equivalent
    #      "list-form" categories internally (the parser strips outer
    #      delimiters and operates on the comma-separated body identically).
    #
    #   2. Probe-based fallback (when `x-shape-form` is absent): try a rich
    #      probe set so id-only schemas (`[nx]` / `(ny)`), digit-only
    #      schemas (`[1]`/`(1)`), and mixed schemas all classify correctly.
    #      A branch matches a probe set iff its regex `fullmatch`es ANY
    #      probe in the set; the broader probe set avoids hard-failing
    #      otherwise-valid repo-local grammars that exclude integer
    #      literals or identifier literals.
    _SCALAR_PROBES = ("scalar", "Scalar", "SCALAR")
    _LIST_PROBES = (
        "[1]", "[a]", "[A]", "[Nx]", "[1,2]", "[a,b]", "[Nx,Ny]",
        "(1)", "(a)", "(A)", "(Nx)", "(1,2)", "(a,b)", "(Nx,Ny)",
    )
    _ALLOWED_SHAPE_FORM_VALUES = {"scalar", "list", "tuple"}
    scalar_pats: list[re.Pattern[str]] = []
    list_form_pats: list[re.Pattern[str]] = []
    for branch_idx, branch in enumerate(branches):
        if not isinstance(branch, dict):
            raise RuntimeError(
                f"shape_expr schema {schema_path} 'oneOf'[{branch_idx}] must be a JSON object "
                f"(got {type(branch).__name__})"
            )
        pat = branch.get("pattern")
        if not isinstance(pat, str) or not pat:
            raise RuntimeError(
                f"shape_expr schema {schema_path} 'oneOf'[{branch_idx}].pattern must be a non-empty string"
            )
        try:
            compiled = re.compile(pat)
        except re.error as exc:
            raise RuntimeError(
                f"shape_expr schema {schema_path} contains invalid regex {pat!r}: {exc}"
            ) from exc
        explicit_form_raw = branch.get("x-shape-form")
        if explicit_form_raw is not None:
            if not isinstance(explicit_form_raw, str):
                raise RuntimeError(
                    f"shape_expr schema {schema_path} 'oneOf'[{branch_idx}].x-shape-form "
                    f"must be a string (got {type(explicit_form_raw).__name__})"
                )
            explicit_form = explicit_form_raw.strip().lower()
            if explicit_form not in _ALLOWED_SHAPE_FORM_VALUES:
                raise RuntimeError(
                    f"shape_expr schema {schema_path} 'oneOf'[{branch_idx}].x-shape-form "
                    f"must be one of {sorted(_ALLOWED_SHAPE_FORM_VALUES)} (got {explicit_form_raw!r})"
                )
            if explicit_form == "scalar":
                scalar_pats.append(compiled)
            else:
                list_form_pats.append(compiled)
            continue
        # Fallback: probe-based classification
        if any(compiled.fullmatch(probe) for probe in _SCALAR_PROBES):
            scalar_pats.append(compiled)
        elif any(compiled.fullmatch(probe) for probe in _LIST_PROBES):
            list_form_pats.append(compiled)
        else:
            raise RuntimeError(
                f"shape_expr schema {schema_path} oneOf branch with pattern {pat!r} "
                "matches no probe in the canonical set (scalar literal, or "
                "bracket/paren shape with integer or identifier dim tokens); "
                "set 'x-shape-form' to 'scalar' | 'list' | 'tuple' to disambiguate explicitly"
            )
    if not scalar_pats or not list_form_pats:
        raise RuntimeError(
            f"shape_expr schema {schema_path} must declare at least one scalar branch "
            "and at least one list/tuple-form branch"
        )
    return tuple(scalar_pats), tuple(list_form_pats)


def _load_shape_expr_patterns_cached(
    schema_path_str: str,
) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    """Public schema loader. Stats the file to derive mtime then delegates
    to the mtime-keyed cache so file-content changes at the same path are
    observed within a single process (rebases, repairs, branch switches).

    Stat errors are mapped to `RuntimeError` for caller compatibility — the
    CLI / run_workflow guards only catch `RuntimeError`.
    """
    schema_path = Path(schema_path_str)
    try:
        mtime_ns = schema_path.stat().st_mtime_ns
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"shape_expr schema not found at {schema_path}. "
            "Canonical source: spec/schema/ir/shape_expr.schema.json"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"shape_expr schema {schema_path} is unreadable: {exc}. "
            "Canonical source: spec/schema/ir/shape_expr.schema.json"
        ) from exc
    return _load_shape_expr_patterns_by_mtime(schema_path_str, mtime_ns)


# Expose `cache_clear` so existing test code (and operators that need to
# force a reload, e.g. after editing the bundled schema) keeps working.
_load_shape_expr_patterns_cached.cache_clear = (  # type: ignore[attr-defined]
    _load_shape_expr_patterns_by_mtime.cache_clear
)


def _get_shape_expr_patterns(
    repo_root: "Path | None" = None,
) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    schema_path = _resolve_shape_expr_schema_path(repo_root)
    return _load_shape_expr_patterns_cached(str(schema_path))
REQUIRED_WORKFLOW_STEPS = ("compile", "generate", "build", "validate")
SUBSTEP_WORKFLOW_STEPS = frozenset({"compile", "generate", "validate"})
AGENT_TERMINAL_STATUSES = {"pass", "fail", "blocked", "timeout", "cancel"}

# Generate-stage static lint (MCP run_linter); see docs/workflow/WORKFLOW_CORE.md and docs/workflow/phases/phase_02_generate.md
_LINT_PRESET_FOR_LANGUAGE: dict[str, str] = {
    "fortran": "fortitude",
    "cuda_fortran": "fortitude",
    "c": "cppcheck",
    "cpp": "cppcheck",
    "c++": "cppcheck",
    "cuda_c": "cppcheck",
    "mixed": "mixed",
    "python": "ruff",
}
_LINT_ALLOWED_PRESETS = frozenset({"fortitude", "cppcheck", "ruff", "mixed"})
_NODE_KEY_SAFE_PATTERN_LINEAGE = re.compile(
    r"^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$"
)
_SLUG_DATE_SEQ3_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*_(\d{8})_(\d{3})$")
_STAGE_DATE_SEQ3_PATTERNS: dict[str, re.Pattern[str]] = {
    "src": re.compile(r"^src_(\d{8})_(\d{3})$"),
    "bin": re.compile(r"^bin_(\d{8})_(\d{3})$"),
    "run": re.compile(r"^run_(\d{8})_(\d{3})$"),
}


def _extract_launch_response_agent_session_id(payload: dict[str, Any]) -> str | None:
    candidate_paths: tuple[tuple[str, ...], ...] = (
        ("agent_session_id",),
        ("agent_id",),
        ("session_id",),
        ("child_agent_id",),
        ("child_agent_session_id",),
        ("id",),
        ("agent", "id"),
        ("agent", "session_id"),
        ("child_agent", "id"),
        ("child_agent", "session_id"),
        ("data", "id"),
        ("data", "session_id"),
    )
    for path in candidate_paths:
        current: Any = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return None


def _is_sequential_agent_token(value: str) -> bool:
    token = value.strip()
    if not token:
        return False
    return bool(re.fullmatch(r"(?:ctx|session)_[0-9]+(?:_[0-9]+)*", token))


def _has_informative_agent_summary(text: str) -> bool:
    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(non_empty_lines) < 2:
        return False
    if not any(line.startswith("status: ") for line in non_empty_lines):
        return False
    if any(line == "output_refs:" for line in non_empty_lines):
        return True
    return any(
        line.startswith(prefix)
        for prefix in ("result_summary: ", "summary: ", "reason: ", "failure_reason: ")
        for line in non_empty_lines
    )


DETERMINISTIC_PROMPT_SENTINEL = "Conductor-executed deterministic step"

# Mirror of the warm-resume slim repair prompt's identifying strings in
# orchestration_runtime.py (SLIM_REPAIR_PROMPT_SENTINEL / SLIM_REPAIR_FINDINGS_HEADER).
# Copied literals (the validator intentionally does not import orchestration_runtime,
# matching the DETERMINISTIC_PROMPT_SENTINEL duplication); a cross-module equality test
# guards against drift.
SLIM_REPAIR_PROMPT_SENTINEL = "Warm-resume slim repair turn"
SLIM_REPAIR_FINDINGS_HEADER = "Findings to fix (from the lint/syntax/static gate or verify finding):"


def _is_slim_launch_prompt_text(launch_text: str) -> bool:
    """True when a recorded launch_prompt_ref body is shaped like a warm-resume slim repair turn.

    Detect by the sentinel's POSITION (first line), NOT a whole-body substring: the FULL
    substep template documents the slim mechanism in its always-rendered boilerplate (see
    tools/prompt_templates/substep_agent.txt), so the sentinel string appears inside every
    full substep prompt. The slim renderer (orchestration_runtime._render_slim_repair_launch_prompt)
    always emits the sentinel as the very first line, so anchoring on the prefix is exact.

    This is a NECESSARY but not SUFFICIENT signal for downgrading the required marker set:
    the authoritative signal is the structured launch request (see
    _launch_request_is_slim_repair). A record is treated as slim only when both agree."""
    return launch_text.lstrip().startswith(SLIM_REPAIR_PROMPT_SENTINEL)


def _launch_request_is_slim_repair(request_payload: dict) -> bool:
    """True when the structured launch REQUEST payload is a warm-resume slim repair.

    Mirror of orchestration_runtime._is_slim_repair_request (the renderer's own authoritative
    predicate; the validator intentionally does not import orchestration_runtime, matching the
    SLIM_REPAIR_PROMPT_SENTINEL duplication — a cross-module parity test guards against drift).
    Gating the reduced marker set on this — not on prompt text alone — prevents a non-slim
    launch record whose prompt was replaced with a slim-looking body from escaping the full
    skill / must-read / requirements markers (an inconsistent record would otherwise pass).

    A pure request is excluded up front (mirrors orchestration_runtime._is_slim_repair_request):
    a pure warm-resume repair satisfies the slim shape but has its own pure marker set, so pure
    and slim classify mutually exclusively here too."""
    if request_payload.get("deterministic"):
        return False
    if _pure_leaf_is_pure_request(request_payload):
        return False
    if not request_payload.get("warm_resume"):
        return False
    if str(request_payload.get("repair_strategy", "")).strip() != "reuse":
        return False
    return bool(str(request_payload.get("repair_findings", "")).strip())


def _is_pure_launch_prompt_text(launch_text: str) -> bool:
    """True when a recorded launch_prompt body is shaped like a Z2 pure-function leaf turn.

    Anchored on the sentinel's POSITION (first line), mirroring _is_slim_launch_prompt_text: the
    pure renderer (orchestration_runtime._render_pure_launch_prompt / _render_pure_repair_prompt)
    always emits PURE_PROMPT_SENTINEL as line 0. A NECESSARY but not SUFFICIENT signal — the
    authoritative signal is the structured request (_launch_request_is_pure); a record is treated
    as pure only when both agree, and a disagreement is itself a violation."""
    return launch_text.lstrip().startswith(PURE_PROMPT_SENTINEL)


def _launch_request_is_pure(request_payload: dict) -> bool:
    """True when the structured launch REQUEST is a Z2 pure-function leaf (`leaf_mode == "pure"`).

    Delegates to `pure_leaf.is_pure_request` — the SAME single detection source
    orchestration_runtime._is_pure_launch_request uses, so the producer and this validator cannot
    disagree about what "pure" is. Gating on this (not prompt text alone) prevents a non-pure
    record whose prompt was swapped for a pure-looking body from escaping the full markers, and
    vice versa."""
    return _pure_leaf_is_pure_request(request_payload)


def _required_launch_prompt_markers_for_role(
    role: str,
    deterministic: bool = False,
    slim: bool = False,
    pure: bool = False,
) -> list[str]:
    if pure:
        # Z2 pure-function leaf (launch OR repair): sentinel + identity + contract version. No
        # skill / must-read / requirements markers — the pure prompt has no skill section and
        # no leaf-authorable gate/write instructions. Mirror
        # orchestration_runtime._required_launch_prompt_markers (pure branch).
        return [
            PURE_PROMPT_SENTINEL,
            "Target node_key:", "Target step:", "Target substep:",
            "orchestration_id:", "agent_run_id:",
            "prompt_contract_version:",
        ]
    if slim:
        # Warm-resume slim repair turn: the conductor renders a reduced body (built
        # directly, not from the template) because the resumed producer leaf already holds
        # the SKILL / must-read / requirements sections. Mirror the renderer's reduced
        # marker set (orchestration_runtime._required_launch_prompt_markers, slim branch)
        # so this pipeline-semantic re-check does not false-reject a legitimate slim prompt.
        return [
            SLIM_REPAIR_PROMPT_SENTINEL,
            "Target node_key:", "Target step:", "Target substep:",
            "orchestration_id:", "agent_run_id:", "parent_agent_run_id:",
            "source_id:", "capability_doc_path:", "allowed_output_paths:",
            "output_manifest_path:", SLIM_REPAIR_FINDINGS_HEADER,
        ]
    if deterministic:
        # Build / Validate.execute run in-process (no leaf); their minimal launch prompt
        # carries no skill section or leaf instructions — only the sentinel + core ids
        # + node/step. See _render_deterministic_launch_prompt in orchestration_runtime.
        det = [
            DETERMINISTIC_PROMPT_SENTINEL,
            "Target node_key:", "Target step:",
            "orchestration_id:", "agent_run_id:", "parent_agent_run_id:",
            "ir_ref:", "pipeline_ref:",
        ]
        if role == "substep":
            det.append("Target substep:")
        return det
    markers = [
        "orchestration_id:",
        "agent_run_id:",
        "parent_agent_run_id:",
        "ir_ref:",
        "pipeline_ref:",
        "dependency_ref:",
        "skill_name:",
        "skill_ref:",
        "skill_must_read_refs:",
        "Required requirements:",
    ]
    if role == "substep":
        return [
            "You are a substep agent.",
            "Target node_key:",
            "Target step:",
            "Target substep:",
            *markers,
        ]
    if role == "step":
        return [
            "You are a step agent.",
            "Target node_key:",
            "Target step:",
            *markers,
        ]
    return []


# Backward compatibility: orchestrations launched before the English translation
# of the launch-prompt templates persisted Japanese template markers in their
# `launch_prompt_ref`. Map each current English marker to its legacy Japanese
# equivalent so `pre_judge` / `full` validation accepts both marker sets.
_LEGACY_LAUNCH_PROMPT_MARKERS: dict[str, str] = {
    "Required requirements:": "必須要件:",
    "You are a substep agent.": "あなたは substep agent である。",
    "You are a step agent.": "あなたは step agent である。",
    "Target node_key:": "対象 node_key:",
    "Target step:": "対象 step:",
    "Target substep:": "対象 substep:",
}


def _launch_prompt_marker_present(marker: str, launch_text: str) -> bool:
    """True if the current English marker or its legacy Japanese form is present."""
    if marker in launch_text:
        return True
    legacy = _LEGACY_LAUNCH_PROMPT_MARKERS.get(marker)
    return bool(legacy) and legacy in launch_text


def _normalize_workspace_root_token(workspace_root: str) -> str:
    token = workspace_root.strip().replace("\\", "/")
    token = token.lstrip("./")
    while "//" in token:
        token = token.replace("//", "/")
    return token.rstrip("/")


def _normalize_node_key_token(raw: str) -> str:
    token = raw.strip()
    if "/" not in token:
        return token
    kind, body = token.split("/", 1)
    spec_id = body.split("@", 1)[0].strip()
    if not kind.strip() or not spec_id:
        return token
    return f"{kind.strip()}/{spec_id}"


def _node_key_to_safe(node_key: str) -> str | None:
    token = node_key.strip()
    if "/" not in token or "@" not in token:
        return None
    spec_kind, tail = token.split("/", 1)
    spec_id, spec_version = tail.rsplit("@", 1)
    spec_kind = spec_kind.strip()
    spec_id = spec_id.strip()
    spec_version = spec_version.strip()
    if not spec_kind or not spec_id or not spec_version:
        return None
    return f"{spec_kind}__{spec_id}__{spec_version}"


def _node_safe_to_node_key(node_safe: str) -> str | None:
    """Restore `<spec_kind>__<spec_id>__<spec_version>` to `<spec_kind>/<spec_id>@<spec_version>`.

    The inverse of `_node_key_to_safe`. Because `spec_kind` / `spec_version` do not contain `__`,
    the value re-joining the leading token as `spec_kind`, the trailing token as `spec_version`,
    and the middle (re-joined with `__`) is `spec_id` (the `__` inside `spec_id` is also restored).
    """
    parts = node_safe.split("__")
    if len(parts) < 3:
        return None
    spec_kind, spec_version = parts[0], parts[-1]
    spec_id = "__".join(parts[1:-1])
    if not spec_kind or not spec_id or not spec_version:
        return None
    return f"{spec_kind}/{spec_id}@{spec_version}"


def _parse_slug_date_seq3_id(value: str) -> tuple[int, int] | None:
    match = _SLUG_DATE_SEQ3_PATTERN.fullmatch(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _parse_stage_attempt_id(value: str, prefix: str) -> tuple[int, int] | None:
    pattern = _STAGE_DATE_SEQ3_PATTERNS[prefix]
    match = pattern.fullmatch(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _agent_role(item: dict[str, Any]) -> str | None:
    for key in ("agent_role", "agent_type", "role"):
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    return None


def _split_fortran_names(raw: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for idx, ch in enumerate(raw):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == "," and depth == 0:
            parts.append(raw[start:idx])
            start = idx + 1
    parts.append(raw[start:])

    names: list[str] = []
    for token in parts:
        part = token.strip().lower()
        if not part:
            continue
        part = re.sub(r"\(.*\)", "", part).strip()
        if FORTRAN_IDENTIFIER_PATTERN.fullmatch(part):
            names.append(part)
    return names


def _is_literal_like_expr(expr: str) -> bool:
    lowered = expr.strip().lower()
    if not lowered:
        return False
    if lowered in {".true.", ".false.", "true", "false"}:
        return True
    return bool(re.fullmatch(r"[0-9dDeE\.\+\-\*\/\(\)\s,_]+", lowered))


def _validate_problem_model_literal_outputs(
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not execution.node_key.startswith("problem/"):
        return

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")

    for match in subroutine_pattern.finditer(lowered):
        sub_name = match.group(1)
        arg_names = set(_split_fortran_names(match.group(2)))
        body = match.group(3)

        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if not out_vars:
            continue

        assign_map: dict[str, list[str]] = {}
        for out_var in sorted(out_vars):
            exprs = re.findall(
                rf"\b{re.escape(out_var)}\s*=\s*([^\n!]+)",
                body,
            )
            if exprs:
                assign_map[out_var] = [expr.strip() for expr in exprs]

        if set(assign_map.keys()) != out_vars:
            continue

        all_literal = True
        input_dependent = False
        for out_var, exprs in assign_map.items():
            for expr in exprs:
                if not _is_literal_like_expr(expr):
                    all_literal = False
                    expr_ids = {
                        token
                        for token in FORTRAN_IDENTIFIER_PATTERN.findall(expr)
                        if token not in {"d", "e", "true", "false"}
                    }
                    if expr_ids & (arg_names - {out_var}):
                        input_dependent = True

        if all_literal and not input_dependent:
            violations.append(
                f"{model_file}: subroutine {sub_name} has literal-only assignments for all intent(out) vars"
            )


def _extract_identifiers(expr: str) -> set[str]:
    return {
        token
        for token in FORTRAN_IDENTIFIER_PATTERN.findall(expr.lower())
        if token not in FORTRAN_KEYWORDS
    }


def _assignment_records(body: str) -> list[tuple[str, set[str], int]]:
    records: list[tuple[str, set[str], int]] = []
    assign_pattern = re.compile(
        r"^\s*([a-z_][a-z0-9_]*(?:\s*\([^\n=]*\))?)\s*=\s*([^\n!]+)",
        re.MULTILINE,
    )
    for match in assign_pattern.finditer(body):
        lhs_expr = match.group(1)
        lhs_match = FORTRAN_IDENTIFIER_PATTERN.search(lhs_expr.lower())
        if lhs_match is None:
            continue
        lhs = lhs_match.group(0)
        rhs_ids = _extract_identifiers(match.group(2))
        records.append((lhs, rhs_ids, match.start()))
    return records


def _validate_problem_model_dependency_dataflow(
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    """`Generate.static` reachability lint: a `problem` subroutine that calls a dependency
    operation must let that operation's RESULT flow to an `intent(out)` dummy. The check is a
    backward ASSIGNMENT closure from the intent(out) vars: an assignment `lhs = f(rhs...)` makes
    every `rhs` a source of `lhs`, transitively; a dependency-call output not assigned (directly or
    through the chain) into any intent(out) is flagged (the inert-call / discarded-result defect).

    Scope note — this gate does NOT deterministically check the stronger
    ``semantic_dependency.required_sources`` reachability. That property (each intent(out)'s
    expression tree reaches the required physical inputs) is inherently flow-sensitive and
    argument-intent-dependent: whether a value passed to a `call` is read or written, and which of
    several writes to a reused scratch variable reaches a use, cannot be decided from this
    regex-level, flow-insensitive view without the callee interfaces. Every flow-insensitive
    approximation attempted here either false-rejected physically-correct code (a dependency result
    reaching intent(out) through a call chain) or failed open (a required source merely co-passed to
    an unrelated call, or fed to a call whose write is later overwritten). It is therefore left to
    ``Generate.verify`` G5, which reads ``controlled_spec.md`` and IS the semantic authority for
    "each intent(out) reaches the required_sources", backed by the runtime. Check 1 below (a
    dependency RESULT reaching intent(out)) is assignment-only, sound, and kept."""
    if not execution.node_key.startswith("problem/"):
        return
    if not dep_spec_ids:
        return

    dep_prefixes = tuple(f"{spec_id.lower()}__" for spec_id in dep_spec_ids)
    if not dep_prefixes:
        return

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")

    for sub_match in subroutine_pattern.finditer(lowered):
        sub_name = sub_match.group(1)
        arg_names = set(_split_fortran_names(sub_match.group(2)))
        body = sub_match.group(3)
        assignments = _assignment_records(body)

        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if not out_vars:
            continue

        dep_output_candidates: set[str] = set()
        for callee, args_raw, call_pos in _iter_fortran_calls(body):
            if not any(callee.startswith(prefix) for prefix in dep_prefixes):
                continue
            call_vars = _split_fortran_names(args_raw)
            for var in call_vars:
                if var in arg_names:
                    continue
                assigned_before_call = any(
                    lhs == var and pos < call_pos for lhs, _, pos in assignments
                )
                if not assigned_before_call:
                    dep_output_candidates.add(var)

        if not dep_output_candidates:
            continue

        # A dependency RESULT is consumed into output through ASSIGNMENTS (you assign the call's
        # output argument into your state / an intent(out)). Backward-close over assignment RHS only:
        # crossing calls here has no sound flow-insensitive form (see the function docstring), and
        # the assignment closure is the origin/main behavior with no false-positive history.
        dependency_sources = set(out_vars)
        changed = True
        while changed:
            changed = False
            for lhs, rhs_ids, _ in assignments:
                if lhs not in dependency_sources:
                    continue
                for src in rhs_ids:
                    if src not in dependency_sources:
                        dependency_sources.add(src)
                        changed = True

        if dep_output_candidates.isdisjoint(dependency_sources):
            violations.append(
                f"{model_file}: subroutine {sub_name} does not propagate dependency operation outputs "
                f"to intent(out) dataflow (candidates={sorted(dep_output_candidates)})"
            )


def _is_multidim_problem_node_key(node_key: str) -> bool:
    if not node_key.startswith("problem/"):
        return False
    spec_id = _spec_id_from_node_key(node_key)
    if spec_id is None:
        return False
    spec_id_l = spec_id.lower()
    return "2d" in spec_id_l or "3d" in spec_id_l


def _is_multidim_problem_node(execution: NodeExecution) -> bool:
    return _is_multidim_problem_node_key(execution.node_key)


def _validate_problem_metric_only_scalar_kernel(
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not _is_multidim_problem_node(execution):
        return
    spec_id = _spec_id_from_node_key(execution.node_key) or execution.node_key

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")
    intent_in_or_inout_array_pattern = re.compile(
        r"intent\s*\(\s*(?:in|inout)\s*\)\s*::\s*[^\n]*\([^)]+\)"
    )
    do_loop_pattern = re.compile(r"^\s*do\s+[a-z_][a-z0-9_]*\s*=", re.MULTILINE)
    forall_pattern = re.compile(r"^\s*forall\s*\(", re.MULTILINE)

    for match in subroutine_pattern.finditer(lowered):
        sub_name = match.group(1)
        body = match.group(3)
        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if len(out_vars) < 5:
            continue

        has_array_inputs = bool(intent_in_or_inout_array_pattern.search(body))
        has_loop = bool(do_loop_pattern.search(body) or forall_pattern.search(body))
        if has_array_inputs or has_loop:
            continue

        violations.append(
            f"{model_file}: subroutine {sub_name} is metric-only scalar kernel for {spec_id}; "
            "2d/3d problem model must not derive many intent(out) metrics without array inputs or update loops"
        )


def _extract_first_output_block(lowered: str, output_name: str) -> str | None:
    start = -1
    for marker in (f"/{output_name}", f"'{output_name}'", f'"{output_name}"'):
        start = lowered.find(marker)
        if start >= 0:
            break
    if start < 0:
        return None

    close_idx = lowered.find("close(", start)
    if close_idx < 0:
        return lowered[start:]
    return lowered[start:close_idx]


def _iter_fortran_calls(text: str) -> list[tuple[str, str, int]]:
    calls: list[tuple[str, str, int]] = []
    call_start_pattern = re.compile(r"\bcall\s+([a-z_][a-z0-9_]*)\s*\(")
    for match in call_start_pattern.finditer(text):
        name = match.group(1).lower()
        start = match.start()
        open_pos = match.end() - 1
        depth = 1
        idx = open_pos + 1
        while idx < len(text) and depth > 0:
            ch = text[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            idx += 1
        if depth == 0:
            args = text[open_pos + 1 : idx - 1]
        else:
            args = text[open_pos + 1 :]
        calls.append((name, args, start))
    return calls


def _makefile_logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw_line in text.splitlines():
        if raw_line.startswith("\t"):
            continue

        line_no_comment = raw_line.split("#", 1)[0].rstrip()
        if not line_no_comment.strip():
            continue

        chunk = line_no_comment.strip()
        if chunk.endswith("\\"):
            buffer += chunk[:-1].strip() + " "
            continue

        logical = (buffer + chunk).strip()
        buffer = ""
        if logical:
            lines.append(logical)

    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def _normalize_make_token(token: str) -> str | None:
    cleaned = token.strip().rstrip("\\")
    if not cleaned:
        return None
    if "%" in cleaned:
        return None

    cleaned = re.sub(r"\$\([^)]+\)", "", cleaned)
    cleaned = re.sub(r"\$\{[^}]+\}", "", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if cleaned.startswith("$"):
        return None

    name = Path(cleaned).name.lower()
    if not name or "$" in name:
        return None
    return name


_MAKE_ASSIGNMENT_PATTERN = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)\s*([:+?]?)=\s*(.*)$"
)
_MAKE_VAR_REF_PATTERN = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_make_vars(
    expr: str,
    var_map: dict[str, str],
    depth: int = 8,
    strip_unknown: bool = False,
    preserve: set[str] | None = None,
) -> str:
    """Substitute ``$(NAME)`` / ``${NAME}`` for known names, bounded by depth.

    By default unknown variables are left intact so `_normalize_make_token`
    strips them, preserving behavior for genuinely unresolved references at
    rule-expansion time. With ``strip_unknown=True`` (used for immediate ``:=``
    expansion) any still-unresolved reference collapses to empty, matching GNU
    make: a ``:=`` RHS that forward-references a not-yet-defined variable
    expands to empty *now* and must not be resolved by a later definition. The
    depth bound stops self/cyclic references from looping forever.

    ``preserve`` names are never substituted nor stripped: their ``$(NAME)``
    reference survives verbatim. This is used by the directory-prefix-aware
    Makefile analysis to keep the ``$(OBJDIR)`` sentinel intact (so a
    ``$(MODEL_OBJ) := $(OBJDIR)/foo.o`` definition expands to
    ``$(OBJDIR)/foo.o`` rather than collapsing the out-of-source prefix away).
    """
    preserve = preserve or set()

    for _ in range(depth):
        if "$" not in expr:
            break

        def _sub(match: "re.Match[str]") -> str:
            name = match.group(1) or match.group(2)
            if name in preserve:
                return match.group(0)
            return var_map[name] if name in var_map else match.group(0)

        expanded = _MAKE_VAR_REF_PATTERN.sub(_sub, expr)
        if expanded == expr:
            break
        expr = expanded
    if strip_unknown:

        def _strip(match: "re.Match[str]") -> str:
            name = match.group(1) or match.group(2)
            return match.group(0) if name in preserve else ""

        expr = _MAKE_VAR_REF_PATTERN.sub(_strip, expr)
    return expr


def _parse_makefile_rules(makefile_text: str) -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {}
    # Track simple variable definitions (`=` / `:=` / `?=` / `+=`) in file
    # order. GNU make expands a rule's targets and prerequisites immediately
    # when it reads the rule, so only definitions that appear *before* a rule
    # are visible to it; a forward reference expands to empty, which means the
    # prerequisite is genuinely absent (and `make -j` can race). Building the
    # map incrementally reproduces that: a not-yet-defined variable stays
    # unexpanded and `_normalize_make_token` drops it. `var_flavor` records
    # whether a variable is simply-expanded (`:=`) or recursively-expanded
    # (`=` / `?=`), which determines how `+=` treats the appended text.
    var_map: dict[str, str] = {}
    var_flavor: dict[str, str] = {}

    for line in _makefile_logical_lines(makefile_text):
        assign_match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if assign_match is not None:
            name, op, value = (
                assign_match.group(1),
                assign_match.group(2),
                assign_match.group(3).strip(),
            )
            if op == "?":
                # Conditional: only sets when undefined; defines a
                # recursively-expanded variable.
                if name not in var_map:
                    var_map[name] = value
                    var_flavor[name] = "recursive"
            elif op == ":":
                # Simply-expanded: RHS is expanded immediately at definition
                # time, so later redefinitions (or later first-definitions) of
                # referenced variables do not change this value. Unresolved
                # forward references collapse to empty, as make does now.
                var_map[name] = _expand_make_vars(
                    value, var_map, strip_unknown=True
                )
                var_flavor[name] = "simple"
            elif op == "+":
                if name not in var_map:
                    # No prior definition: `+=` acts like `=` (recursive).
                    var_map[name] = value
                    var_flavor[name] = "recursive"
                else:
                    # Appended text is expanded immediately for a
                    # simply-expanded variable, but kept raw (lazy) for a
                    # recursively-expanded one.
                    addition = (
                        _expand_make_vars(value, var_map, strip_unknown=True)
                        if var_flavor.get(name) == "simple"
                        else value
                    )
                    existing = var_map[name]
                    var_map[name] = (
                        (existing + " " + addition).strip() if existing else addition
                    )
            else:
                # Recursively-expanded (`=`): store raw, expand lazily at use.
                var_flavor[name] = "recursive"
                var_map[name] = value
            continue
        if ":" not in line:
            continue

        target_raw, prereq_raw = line.split(":", 1)
        target_raw = _expand_make_vars(target_raw, var_map)
        target_tokens = target_raw.split()
        if not target_tokens:
            continue

        prereq_expr = prereq_raw.split(";", 1)[0].replace("|", " ")
        prereq_expr = _expand_make_vars(prereq_expr, var_map)
        prereq_tokens = prereq_expr.split()
        prereqs = {
            norm
            for token in prereq_tokens
            if (norm := _normalize_make_token(token)) is not None
        }

        for target_token in target_tokens:
            target = _normalize_make_token(target_token)
            if target is None:
                continue
            rules.setdefault(target, set()).update(prereqs)
    return rules


def _apply_make_assignment(
    var_map: dict[str, str],
    var_flavor: dict[str, str],
    name: str,
    op: str,
    value: str,
    preserve: frozenset[str] | None = None,
) -> None:
    """Apply one variable assignment to ``var_map`` honoring make's flavors:
    `?=` (set if unset), `:=` (immediate expansion), `+=` (append, immediate for a
    simply-expanded var else lazy), `=` (recursive, stored raw). ``preserve`` names
    are kept as literal `$(NAME)` references through immediate expansion."""
    if op == "?":
        if name not in var_map:
            var_map[name] = value
            var_flavor[name] = "recursive"
    elif op == ":":
        var_map[name] = _expand_make_vars(
            value, var_map, strip_unknown=True, preserve=preserve
        )
        var_flavor[name] = "simple"
    elif op == "+":
        if name not in var_map:
            var_map[name] = value
            var_flavor[name] = "recursive"
        else:
            addition = (
                _expand_make_vars(value, var_map, strip_unknown=True, preserve=preserve)
                if var_flavor.get(name) == "simple"
                else value
            )
            existing = var_map[name]
            var_map[name] = (
                (existing + " " + addition).strip() if existing else addition
            )
    else:
        var_map[name] = value
        var_flavor[name] = "recursive"


# Make's built-in tool variables (`$(MAKE)`, `$(FC)`, …). They are predefined by
# make and usually never assigned in the Makefile, so they are absent from a map
# built only from explicit assignments. Preserving them through `:=`/`+=`
# expansion keeps the reference alive in an alias (`M := $(MAKE)` stores
# `$(MAKE)`, not the empty string), so a relink reached via that alias is still
# detected — and `$(MAKE)` matches `_RELINK_TOOL_PATTERN` literally.
_RELINK_BUILTIN_VARS = frozenset(
    {"MAKE", "FC", "CC", "CXX", "LD", "AR", "F90", "F95", "F77"}
)


def _makefile_full_var_map(makefile_text: str) -> dict[str, str]:
    """Variable map after reading the whole Makefile (definition order honored,
    `?=`/`:=`/`+=`/`=` flavors handled). Unlike the per-rule incremental map this
    resolves every reference (no preserved sentinels) so a variable-named target
    such as `$(BIN):` or `$(BINDIR)/$(BIN):` resolves to its concrete basename. The
    relink built-in tool variables are preserved through immediate expansion so an
    alias of `$(MAKE)`/`$(LD)`/… survives."""
    var_map: dict[str, str] = {}
    var_flavor: dict[str, str] = {}
    for line in _makefile_logical_lines(makefile_text):
        match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if match is None:
            continue
        _apply_make_assignment(
            var_map,
            var_flavor,
            match.group(1),
            match.group(2),
            match.group(3).strip(),
            preserve=_RELINK_BUILTIN_VARS,
        )
    return var_map


def _makefile_target_recipes(
    makefile_text: str, var_map: dict[str, str]
) -> dict[str, list[str]]:
    """Map of resolved target basename -> its recipe lines (tab-indented and
    inline `; …`). Target names are expanded with ``var_map`` so a variable-named
    rule (`$(BIN):`) is keyed by its concrete basename."""
    recipes: dict[str, list[str]] = {}
    current_targets: list[str] = []
    for raw_line in makefile_text.splitlines():
        if raw_line.startswith("\t"):
            for target in current_targets:
                recipes.setdefault(target, []).append(raw_line)
            continue
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#") or ":" not in raw_line:
            current_targets = []
            continue
        head, rest = raw_line.split(":", 1)
        if "=" in head:
            current_targets = []
            continue
        current_targets = [
            norm
            for tok in _expand_make_vars(head, var_map).split()
            if (norm := _normalize_make_token(tok)) is not None
        ]
        # An inline recipe (`target: prereqs ; recipe`) is part of the recipe and
        # must be classified too, not just tab-indented lines.
        inline_recipe = rest.partition(";")[2]
        if inline_recipe.strip():
            for target in current_targets:
                recipes.setdefault(target, []).append(inline_recipe)
    return recipes


def _makefile_relinking_recipe_targets(
    recipes: dict[str, list[str]], var_map: dict[str, str]
) -> set[str]:
    """Targets whose recipe relinks the binary — i.e. invokes a recursive make or
    a compiler/linker. A target that only runs the (already-built) binary, mkdirs,
    cleans up, etc. does not relink and is not included."""
    return {
        target
        for target, body in recipes.items()
        if any(_recipe_line_relinks(line, var_map) for line in body if line.strip())
    }


# Out-of-source directory sentinels parameterized in generated Makefiles. They
# default to "." for in-source `make` but are overridden at Build/Validate time
# (e.g. `OBJDIR=<per-run tmp>`), so a prerequisite's `$(OBJDIR)/` prefix is NOT
# cosmetic: it determines which concrete target make resolves under an override.
_MAKE_DIR_SENTINELS = frozenset({"OBJDIR", "BINDIR", "RUNDIR"})
_OBJDIR_REF_PATTERN = re.compile(r"\$[({]OBJDIR[)}]")


def _token_has_objdir_prefix(token: str) -> bool:
    """True if a (sentinel-preserved) token references `$(OBJDIR)` / `${OBJDIR}`."""
    return bool(_OBJDIR_REF_PATTERN.search(token))


def _parse_makefile_rules_objdir_aware(
    makefile_text: str,
) -> tuple[dict[str, bool], dict[str, set[tuple[str, bool]]]]:
    """Parse rules while preserving the `$(OBJDIR)` sentinel so the out-of-source
    directory prefix survives basename normalization.

    Mirrors `_parse_makefile_rules`' incremental variable tracking but (1) never
    records the directory sentinels (`OBJDIR`/`BINDIR`/`RUNDIR`) as defined
    variables and (2) preserves their `$(...)` references through `:=`/`+=`
    immediate expansion. Returns:

    - ``target_has_objdir``: object-rule target basename → whether the producing
      rule writes it under `$(OBJDIR)/` (OR-ed across rules).
    - ``prereqs_diraware``: target basename → set of
      ``(prereq_basename, prereq_has_objdir_prefix)`` for its prerequisites.
    """
    var_map: dict[str, str] = {}
    var_flavor: dict[str, str] = {}
    target_has_objdir: dict[str, bool] = {}
    prereqs_diraware: dict[str, set[tuple[str, bool]]] = {}

    for line in _makefile_logical_lines(makefile_text):
        assign_match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if assign_match is not None:
            name, op, value = (
                assign_match.group(1),
                assign_match.group(2),
                assign_match.group(3).strip(),
            )
            # Never record the directory sentinels; their `$(...)` refs must stay
            # literal so a `$(OBJDIR)/`-prefixed value is structurally detectable.
            if name in _MAKE_DIR_SENTINELS:
                continue
            if op == "?":
                if name not in var_map:
                    var_map[name] = value
                    var_flavor[name] = "recursive"
            elif op == ":":
                var_map[name] = _expand_make_vars(
                    value, var_map, strip_unknown=True, preserve=_MAKE_DIR_SENTINELS
                )
                var_flavor[name] = "simple"
            elif op == "+":
                if name not in var_map:
                    var_map[name] = value
                    var_flavor[name] = "recursive"
                else:
                    addition = (
                        _expand_make_vars(
                            value,
                            var_map,
                            strip_unknown=True,
                            preserve=_MAKE_DIR_SENTINELS,
                        )
                        if var_flavor.get(name) == "simple"
                        else value
                    )
                    existing = var_map[name]
                    var_map[name] = (
                        (existing + " " + addition).strip() if existing else addition
                    )
            else:
                var_flavor[name] = "recursive"
                var_map[name] = value
            continue
        if ":" not in line:
            continue

        target_raw, prereq_raw = line.split(":", 1)
        target_raw = _expand_make_vars(target_raw, var_map, preserve=_MAKE_DIR_SENTINELS)
        target_tokens = target_raw.split()
        if not target_tokens:
            continue

        prereq_expr = prereq_raw.split(";", 1)[0].replace("|", " ")
        prereq_expr = _expand_make_vars(prereq_expr, var_map, preserve=_MAKE_DIR_SENTINELS)
        prereq_pairs: set[tuple[str, bool]] = set()
        for token in prereq_expr.split():
            norm = _normalize_make_token(token)
            if norm is None:
                continue
            prereq_pairs.add((norm, _token_has_objdir_prefix(token)))

        for target_token in target_tokens:
            target = _normalize_make_token(target_token)
            if target is None:
                continue
            target_has_objdir[target] = target_has_objdir.get(
                target, False
            ) or _token_has_objdir_prefix(target_token)
            prereqs_diraware.setdefault(target, set()).update(prereq_pairs)
    return target_has_objdir, prereqs_diraware


def _local_fortran_module_map(src_files: list[Path]) -> dict[str, str]:
    module_map: dict[str, str] = {}
    pattern = re.compile(r"^\s*module\s+(?!procedure\b)([a-z_][a-z0-9_]*)\b", re.MULTILINE)
    for src_file in src_files:
        text = src_file.read_text(encoding="utf-8", errors="ignore").lower()
        stem = src_file.stem.lower()
        for match in pattern.finditer(text):
            module_name = match.group(1)
            module_map.setdefault(module_name, stem)
    return module_map


def _fortran_source_module_deps(src_files: list[Path]) -> dict[str, set[str]]:
    module_map = _local_fortran_module_map(src_files)
    use_pattern = re.compile(
        r"^\s*use(?:\s*,\s*(?:intrinsic|non_intrinsic)\s*::|\s*::|\s+)?\s*([a-z_][a-z0-9_]*)\b",
        re.MULTILINE,
    )
    deps_by_stem: dict[str, set[str]] = {}
    for src_file in src_files:
        text = src_file.read_text(encoding="utf-8", errors="ignore").lower()
        stem = src_file.stem.lower()
        deps: set[str] = set()
        for match in use_pattern.finditer(text):
            used_module = match.group(1)
            provider_stem = module_map.get(used_module)
            if provider_stem is None or provider_stem == stem:
                continue
            deps.add(provider_stem)
        deps_by_stem[stem] = deps
    return deps_by_stem


# BIN assignment forms. `?=` is overridable by the make environment (the only channel
# Validate.execute's run_quality_checks make_test has); `=`/`:=`/`+=` are not, so a
# make-env BIN override is silently ignored and `make test` desyncs from the binary Build
# produced with its command-line BIN override.
# Leading whitespace is spaces only: a make variable ASSIGNMENT cannot start with a tab
# (a tab-indented line is a recipe command, e.g. a shell `BIN=...` inside a target body),
# so excluding a leading tab avoids a false positive on recipe lines.
_MAKE_BIN_ASSIGN_RE = re.compile(r"^[ ]*BIN[ \t]*(\?=|:=|\+=|=)", re.MULTILINE)
_MAKE_BIN_REF_RE = re.compile(r"\$[({]BIN[)}]")


def _validate_makefile_bin_overridable(
    makefile_path: Path, makefile_text: str, violations: list[str]
) -> None:
    """Require `BIN ?= <name>` when the Makefile builds a binary via `$(BIN)`.

    The VALUE is not constrained (the conductor imposes `<spec_id>_runner`); only the
    overridable `?=` form is required so the execute make_test environment override
    applies. A Makefile that never references `$(BIN)` (degenerate in-source object-only)
    is exempt.
    """
    ops = _MAKE_BIN_ASSIGN_RE.findall(makefile_text)
    references_bin = bool(_MAKE_BIN_REF_RE.search(makefile_text))
    if not ops and not references_bin:
        return
    has_overridable = any(op == "?=" for op in ops)
    has_hard = any(op != "?=" for op in ops)
    if has_hard or not has_overridable:
        violations.append(
            f"{makefile_path}: BIN must be declared overridable as `BIN ?= <name>` "
            "(not `=`/`:=`/`+=`) so Build and Validate.execute can impose the canonical "
            "<spec_id>_runner binary name (Validate.execute's make_test overrides BIN only "
            "via the environment, which applies to `?=` assignments only)"
        )


def _validate_fortran_makefile_src_dir(src_dir: Path, violations: list[str]) -> None:
    if not src_dir.is_dir():
        return

    src_files = sorted(
        p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() == ".f90"
    )
    if not src_files:
        return

    deps_by_stem = _fortran_source_module_deps(src_files)
    required_object_deps = {
        stem: deps for stem, deps in deps_by_stem.items() if deps
    }

    makefile_path = src_dir / "Makefile"
    if not makefile_path.exists():
        # The module-dependency build contract requires a Makefile; the
        # directory-prefix check below has nothing to inspect without one.
        if required_object_deps:
            violations.append(
                f"{makefile_path}: missing for fortran module dependency build"
            )
        return

    makefile_text = makefile_path.read_text(encoding="utf-8", errors="ignore")

    # The execution binary basename is NOT pinned to a specific VALUE here, but BIN must
    # be declared OVERRIDABLE (`BIN ?= <name>`). Build and Validate.execute impose the
    # canonical `<spec_id>_runner` binary name on the SAME Makefile so they always agree:
    # Build passes `BIN=...` on the make command line (overrides any assignment), but
    # Validate.execute re-runs `make test` via run_quality_checks, which can only pass BIN
    # through the environment — and a make environment value overrides a `?=` assignment
    # only (not a plain `=`/`:=`/`+=`). A hard BIN assignment would therefore desync
    # `make test`'s `$(BINDIR)/$(BIN)` guard from the binary Build actually produced. The
    # default VALUE stays the generator's choice (any value; conductor overrides it), so
    # this is a structural `?=` requirement, not the removed `BIN must be <spec_id>_runner`
    # value gate. Mirrors the `OBJDIR/BINDIR/RUNDIR ?=` out-of-source parameterization.
    _validate_makefile_bin_overridable(makefile_path, makefile_text, violations)

    rules = _parse_makefile_rules(makefile_text)
    # Directory-prefix-aware view: detects a prerequisite whose `$(OBJDIR)/`
    # prefix structure disagrees with its producing object rule. The basename
    # `rules` view above normalizes the prefix away, so a bare `foo.o`
    # prerequisite passes there even though the only rule that produces it
    # targets `$(OBJDIR)/foo.o` — which breaks `make -j` under an out-of-source
    # OBJDIR override (no rule makes the bare target). See SKILL.md L42.
    target_has_objdir, prereqs_diraware = _parse_makefile_rules_objdir_aware(
        makefile_text
    )
    # Basename-level: every used-module dependency must be a prerequisite of
    # the consuming object rule (the `.mod` or the `.o`). Gated by
    # `required_object_deps` — only meaningful when sources have local `use`
    # dependencies on each other.
    for stem, deps in sorted(required_object_deps.items()):
        object_target = f"{stem}.o"
        prereqs = rules.get(object_target)
        if prereqs is None:
            violations.append(
                f"{makefile_path}: missing explicit object dependency rule ({object_target})"
            )
            continue

        for dep_stem in sorted(deps):
            dep_mod = f"{dep_stem}.mod"
            dep_obj = f"{dep_stem}.o"
            if dep_mod not in prereqs and dep_obj not in prereqs:
                violations.append(
                    f"{makefile_path}: {object_target} missing prerequisite for used module ({dep_mod} or {dep_obj})"
                )

    # Out-of-source correctness (directory-prefix consistency) runs
    # UNCONDITIONALLY — independent of `required_object_deps` and source count.
    # A bare object prerequisite on a link rule (e.g. `$(BINDIR)/app: main.o`
    # against `$(OBJDIR)/main.o:`) breaks the out-of-source Build even when no
    # source has a local `use` dependency, so this pass must not be gated by the
    # module-dependency early returns above.
    # Checked across
    # ALL rules — object rules AND the link/default rule. When an object (or its
    # paired `.mod`) is produced under `$(OBJDIR)/`, every rule that consumes it
    # must reference it with the same `$(OBJDIR)/` prefix. A bare basename
    # prerequisite has no producing rule once OBJDIR is overridden, so
    # `make -j` aborts with "No rule to make target" — the same prefix mismatch
    # whether the bare name appears on the runner object rule or the link rule.
    def _produced_under_objdir(prereq_basename: str) -> bool:
        # The object itself is produced under $(OBJDIR)/...
        if target_has_objdir.get(prereq_basename, False):
            return True
        # ...or it is a `.mod` whose sibling `.o` is produced under $(OBJDIR)/
        # (the .mod is typically a by-product of compiling that .o and may have
        # no explicit rule of its own).
        if prereq_basename.endswith(".mod"):
            sibling_obj = f"{prereq_basename[: -len('.mod')]}.o"
            return target_has_objdir.get(sibling_obj, False)
        return False

    for consumer_target in sorted(prereqs_diraware):
        bare = sorted(
            {
                prereq_basename
                for prereq_basename, has_objdir in prereqs_diraware[consumer_target]
                if not has_objdir and _produced_under_objdir(prereq_basename)
            }
        )
        if bare:
            violations.append(
                f"{makefile_path}: {consumer_target} prerequisite "
                f"({', '.join(bare)}) must carry the same $(OBJDIR)/ prefix as its "
                f"producing rule target; a bare basename has no rule under an "
                f"out-of-source OBJDIR override and breaks make -j (no rule to make target)"
            )


# A relinking command word: a recursive make or a compiler/linker/archiver, as
# the *first* word of a shell command. Anchored at the start of an extracted
# command word, so a tool name appearing inside an argument (e.g. an echo message)
# is never matched. The make-variable form allows an optional second `$` so a
# recipe-escaped `$$(MAKE)` / `$${MAKE}` is recognized too. `g++`/`c++`/`clang++`
# need no trailing word boundary (a `+` is not a word char). The command word's
# path basename is also tested so an absolute path (`/usr/bin/make`) is recognized.
# Build drivers (cmake/ninja/meson/libtool) are intentionally NOT matched: in a
# `build_system=make` Makefile they appear mostly in non-building utility modes
# (`cmake -E`, `ninja -t`, `meson test`, `libtool --mode=execute`), so a bare
# command-word match would be a false positive.
_RELINK_TOOL_PATTERN = re.compile(
    r"""^(?:
        \$\$?[({](?:MAKE|FC|CC|CXX|LD|AR|F90|F95|F77)[)}]
      | (?:make|gmake|mingw32-make|gfortran|gcc|clang|cc|ld|ar|nvcc|nvfortran|ifort|ifx|f90|f95|f77)\b
      | (?:g|c|clang)\+\+
    )""",
    re.VERBOSE,
)
# The phony test entrypoints are not themselves build targets, so a `check: test`
# alias (canonical) must not be read as a relink-triggering prerequisite.
_PHONY_TEST_TARGETS = frozenset({"test", "check"})
# Shell control words that introduce another command directly (no separator), so
# the *following* token is itself a command word (e.g. `then $(MAKE)`, `if ! make`).
_SHELL_CMD_PREFIX_KEYWORDS = frozenset(
    {"if", "elif", "then", "else", "while", "until", "do", "time", "!"}
)
# Command wrappers whose *next* token is the wrapped command (`ccache gfortran …`,
# `env FC=gfortran make …`): skip the wrapper and examine that command word. Only
# wrappers that take no argument before the command are included — wrappers that
# take their own options/args first (`timeout 60 make`, `nice -n10 make`,
# `sudo -u x make`, `xargs -n1 make`) would mis-identify the arg as the command,
# so they are intentionally omitted (a documented low-realism gap).
_SHELL_CMD_WRAPPER_PREFIXES = frozenset(
    {"ccache", "distcc", "sccache", "nohup", "env"}
)
# A leading `NAME=value` shell assignment precedes the actual command, so the
# following token is the command word (e.g. `FC=gfortran make …`).
_SHELL_ASSIGNMENT_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _shell_command_words(recipe: str) -> list[str]:
    """Extract the command word (first token) of each shell command in a recipe
    line, honoring quotes and make's `$(...)`/`${...}` syntax. Commands are
    delimited by unquoted `;` / `&` / `|`, and by `{` / `(` *group openers* (a `{`
    or `(` not immediately following `$`, so `${VAR}` / `$(VAR)` stay intact).
    Surrounding quotes are stripped from a quoted command word (`"$(MAKE)"` ->
    `$(MAKE)`), while separators/words inside an argument's quotes are ignored."""
    words: list[str] = []
    word: list[str] = []
    reading = False
    cmd_start = True
    quote: str | None = None
    prev = ""

    def emit() -> None:
        nonlocal reading, word
        if reading:
            words.append("".join(word))
            word = []
            reading = False

    for char in recipe:
        if quote is not None:
            if char == quote:
                quote = None
            elif reading:
                word.append(char)
            prev = char
            continue
        if char in "'\"":
            if cmd_start:
                reading = True  # a quoted command word; the quotes are dropped
            quote = char
            prev = char
            continue
        if char == "#" and (prev == "" or prev in " \t;&|({"):
            # An unquoted `#` at a word boundary starts a shell comment; the rest
            # of the line is not executed (`… $(BIN)  # build first; make all`).
            break
        if char in ";&|" or (char in "{(" and prev != "$"):
            emit()
            cmd_start = True
            prev = char
            continue
        if char in " \t":
            if reading:
                emit()
                # A shell control keyword (`then`/`do`/`!`/…), a command wrapper
                # (`ccache`/`env`/…), or a leading `NAME=value` assignment is
                # followed by another command, so the next token is also a command
                # word.
                cmd_start = (
                    words[-1] in _SHELL_CMD_PREFIX_KEYWORDS
                    or words[-1] in _SHELL_CMD_WRAPPER_PREFIXES
                    or bool(_SHELL_ASSIGNMENT_PREFIX.match(words[-1]))
                )
            prev = char
            continue
        # Ordinary char.
        if cmd_start and not reading and char in "@+-":
            prev = char  # skip leading make recipe prefixes (@ silent, - ignore, + force)
            continue
        if cmd_start:
            reading = True
            word.append(char)
        prev = char
    emit()
    return words


_SHELL_DASH_C_ARG = re.compile(
    r"""\b(?:sh|bash|dash|zsh|ksh)\s+-[A-Za-z]*c\s+   # -c, possibly with combined flags (-lc)
        ("(?:[^"]*)"|'(?:[^']*)'|\S+)""",
    re.VERBOSE,
)
_BACKTICK_SPAN = re.compile(r"`([^`]*)`")


def _command_word_relinks(command: str, var_map: dict[str, str] | None) -> bool:
    """True if a command word is a relink tool, after optionally resolving a
    make-variable alias (`$(LINK)` -> `gfortran`) via ``var_map``."""
    candidates = {command}
    if var_map:
        candidates.add(_expand_make_vars(command, var_map))
    for candidate in candidates:
        if _RELINK_TOOL_PATTERN.search(candidate) or _RELINK_TOOL_PATTERN.search(
            candidate.rsplit("/", 1)[-1]
        ):
            return True
    return False


def _nested_command_texts(text: str) -> list[str]:
    """Shell-command substrings nested inside a recipe line: make `$(shell …)`
    function bodies, shell `$$(…)` command substitutions, backtick substitutions,
    and the argument of `sh -c` / `bash -c`. Returned so the relink scan can
    recurse into them (a relink hidden in `$(shell $(MAKE) …)` etc.)."""
    bodies: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] == "$" and i + 1 < n:
            j = i + 1
            shell_subst = False
            if text[j] == "$":  # `$$(` -> shell command substitution
                j += 1
                shell_subst = True
            if j < n and text[j] == "(":
                depth = 1
                k = j + 1
                start = k
                while k < n and depth:
                    if text[k] == "(":
                        depth += 1
                    elif text[k] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                body = text[start:k]
                if shell_subst:
                    bodies.append(body)
                else:
                    shell_fn = re.match(r"shell\s+(.*)", body, re.S)
                    if shell_fn:
                        bodies.append(shell_fn.group(1))
                i = k + 1
                continue
        i += 1
    bodies.extend(m.group(1) for m in _BACKTICK_SPAN.finditer(text))
    for match in _SHELL_DASH_C_ARG.finditer(text):
        arg = match.group(1)
        if len(arg) >= 2 and arg[0] in "'\"" and arg[-1] == arg[0]:
            arg = arg[1:-1]
        bodies.append(arg)
    return bodies


def _text_relinks(text: str, var_map: dict[str, str] | None, depth: int = 0) -> bool:
    if depth > 6:  # bound pathological nesting
        return False
    if any(_command_word_relinks(cmd, var_map) for cmd in _shell_command_words(text)):
        return True
    return any(
        _text_relinks(body, var_map, depth + 1) for body in _nested_command_texts(text)
    )


def _recipe_line_relinks(recipe_line: str, var_map: dict[str, str] | None = None) -> bool:
    """True if a recipe line executes a relinking command (recursive make, a build
    driver, or a compiler/linker) as a command word — at the top level or nested
    in a `$(shell …)` / `$$(…)` / backtick substitution or a `sh -c` body. Command
    words are expanded with ``var_map`` so a make-variable alias resolves first."""
    return _text_relinks(recipe_line.lstrip("\t"), var_map)


def _validate_makefile_test_no_relink(
    src_dir: Path,
    violations: list[str],
    build_system: str | None = None,
    language: str | None = None,
) -> None:
    # The non-relinking `test`/`check` contract applies only to the make-based
    # quality-check toolchains (`Validate.execute` runs `make_test`/`make_check`
    # only then). Skip any other toolchain — a Makefile kept for local
    # convenience must not fail post_generate/post_build here.
    if build_system != "make" or language not in MAKE_QUALITY_CHECK_REQUIRED_LANGUAGES:
        return
    if not src_dir.is_dir():
        return
    makefile_path = src_dir / "Makefile"
    if not makefile_path.exists():
        return

    text = makefile_path.read_text(encoding="utf-8", errors="ignore")
    # Whole-file variable map: resolves variable-named targets/recipes and the
    # binary basename (`$(BIN)`). Targets/recipes are position-independent, so the
    # final map is correct for them; `test`/`check` *prerequisites* are resolved
    # with an incremental map below to honor make's read-time expansion order.
    full_var_map = _makefile_full_var_map(text)
    binary_basename = _normalize_make_token(_expand_make_vars("$(BIN)", full_var_map))
    recipes = _makefile_target_recipes(text, full_var_map)
    relinking_recipe_targets = _makefile_relinking_recipe_targets(recipes, full_var_map)

    # Incremental pass mirroring GNU make: a rule's prerequisites are expanded
    # immediately at read time, so only definitions seen *before* the rule are
    # visible (a forward reference expands to empty). Records each rule target's
    # resolved prerequisite basenames; `test`/`check` rules are captured for the
    # verdict.
    inc_var_map: dict[str, str] = {}
    inc_var_flavor: dict[str, str] = {}
    prereq_names_by_target: dict[str, set[str]] = {}
    test_check_rules: list[tuple[frozenset[str], set[str], str]] = []
    for line in _makefile_logical_lines(text):
        assign_match = _MAKE_ASSIGNMENT_PATTERN.match(line)
        if assign_match is not None:
            _apply_make_assignment(
                inc_var_map,
                inc_var_flavor,
                assign_match.group(1),
                assign_match.group(2),
                assign_match.group(3).strip(),
            )
            continue

        if ":" not in line:
            continue
        head, rest = line.split(":", 1)
        target_names = {
            norm
            for tok in _expand_make_vars(head, inc_var_map).split()
            if (norm := _normalize_make_token(tok)) is not None
        }
        if not target_names:
            continue

        # Right-hand side: prerequisites (normal + order-only, both built by make)
        # and an optional inline recipe (`target: prereqs ; recipe`).
        prereq_part, _, inline_recipe = rest.partition(";")
        prereq_names = {
            norm
            for tok in _expand_make_vars(
                prereq_part.replace("|", " "), inc_var_map
            ).split()
            if (norm := _normalize_make_token(tok)) is not None
        }
        for target in target_names:
            prereq_names_by_target.setdefault(target, set()).update(prereq_names)

        guarded_targets = target_names & _PHONY_TEST_TARGETS
        if guarded_targets:
            test_check_rules.append(
                (frozenset(guarded_targets), prereq_names, inline_recipe)
            )

    # A rule target relinks when made if its recipe builds (links) or it is the
    # binary itself, plus any target that transitively depends on such a target.
    # Computed as a fixpoint over the prerequisite graph; the phony test
    # entrypoints are excluded (a `check: test` alias is not a build prerequisite).
    relinking = set(relinking_recipe_targets)
    if binary_basename is not None:
        relinking.add(binary_basename)
    relinking -= _PHONY_TEST_TARGETS
    changed = True
    while changed:
        changed = False
        for target in set(prereq_names_by_target) - relinking - _PHONY_TEST_TARGETS:
            if prereq_names_by_target[target] & relinking:
                relinking.add(target)
                changed = True

    for guarded_targets, prereq_names, inline_recipe in test_check_rules:
        target_label = "/".join(sorted(guarded_targets))

        # Prerequisite relink: a prerequisite (normal or order-only) that is the
        # binary or a target that relinks when built.
        if prereq_names & relinking:
            violations.append(
                f"{makefile_path}: {target_label} target has a build prerequisite "
                f"that relinks the binary; the target must reference the existing "
                f"binary via a non-relinking recipe guard "
                f"'test -x $(BINDIR)/$(BIN) || {{ echo \"error: ...\" >&2; exit 1; }}' "
                f"with no build prerequisite, so Validate.execute does not write into "
                f"the read-only-bound binary/ (unauthorized_write_violation -> fail_closed)"
            )

        # Recipe relink: an inline (`; ...`) or tab-indented recipe line that
        # rebuilds the binary (recursive make or a compiler/linker invocation).
        recipe_lines: list[str] = []
        if inline_recipe.strip():
            recipe_lines.append(inline_recipe)
        for tgt in guarded_targets:
            recipe_lines.extend(recipes.get(tgt, []))
        for recipe_line in recipe_lines:
            if _recipe_line_relinks(recipe_line, full_var_map):
                violations.append(
                    f"{makefile_path}: {target_label} target recipe relinks the binary "
                    f"(recursive make or compiler/linker invocation); use a non-relinking "
                    f"fail-closed guard "
                    f"'test -x $(BINDIR)/$(BIN) || {{ echo \"error: ...\" >&2; exit 1; }}' "
                    f"so Validate.execute does not write into the read-only-bound "
                    f"binary/ (unauthorized_write_violation -> fail_closed)"
                )
                break


def _validate_makefile_test_invokes_cases(
    src_dir: Path,
    violations: list[str],
    build_system: str | None = None,
    language: str | None = None,
) -> None:
    """Flag a ``test``/``check`` target whose recipe runs the runner binary but
    does NOT forward ``--cases $(SPEC) $(CASES)``.

    ``Validate.execute`` runs the binary two ways and compares them for value
    equality (``quality_check.json``): ``run_program`` invokes it as
    ``--cases <spec.ir.yaml> <case_id>...`` and ``make test`` must invoke it the
    same way. The conductor injects ``SPEC``/``CASES`` via the make-test env so
    the canonical recipe ``$(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)`` is
    byte-identical to ``run_program``. Two recipes desync the two invocations and
    are flagged: (a) a bare run (no ``--cases``) — the runner aborts, the
    candidate emits no ``diagnostics.json`` (``verdict_available=false``); and
    (b) a run that hardcodes ``--cases <spec> <ids>`` instead of referencing the
    ``$(SPEC)``/``$(CASES)`` variables — the env override has no effect and make
    test runs a different spec/case set than ``run_program`` (wrong-evidence
    comparison). The conductor-authored fortran Makefile already satisfies this;
    the check guards the LLM-authored c/cpp/mixed path. Best-effort static parse —
    the runtime ``quality_check`` is the deterministic backstop. Scoped to the
    make-based quality-check toolchains (same as the no-relink check)."""
    if build_system != "make" or language not in MAKE_QUALITY_CHECK_REQUIRED_LANGUAGES:
        return
    if not src_dir.is_dir():
        return
    makefile_path = src_dir / "Makefile"
    if not makefile_path.exists():
        return

    text = makefile_path.read_text(encoding="utf-8", errors="ignore")
    full_var_map = _makefile_full_var_map(text)
    recipes = _makefile_target_recipes(text, full_var_map)
    binary_basename = _normalize_make_token(_expand_make_vars("$(BIN)", full_var_map))

    # `make test` also runs the recipes of `test`/`check`'s prerequisite targets, so
    # a recipe that delegates the run to a helper (`test: run-qc`, run in `run-qc`)
    # must be traced. Build/relink targets (the binary itself + any compile/link
    # recipe) are EXCLUDED from the trace so a `$(FC) … -o $(BINDIR)/$(BIN)` line is
    # not misread as a runner invocation (their no-build-prerequisite contract is the
    # separate `_validate_makefile_test_no_relink` gate's concern).
    rules = _parse_makefile_rules(text)
    build_targets = set(_makefile_relinking_recipe_targets(recipes, full_var_map))
    if binary_basename:
        build_targets.add(binary_basename)

    def _run_recipe_lines(entrypoint: str) -> list[str]:
        seen: set[str] = set()
        stack = [entrypoint]
        collected: list[str] = []
        while stack:
            t = stack.pop()
            if t in seen or t in build_targets:
                continue
            seen.add(t)
            collected.extend(recipes.get(t, []))
            stack.extend(rules.get(t, ()))
        return collected

    def _logical_recipe_lines(lines: list[str]) -> list[str]:
        # Fold trailing-`\` continuations so an invocation wrapped across physical
        # lines (`… $(BIN) \` / `  --cases …`) is scanned as one logical command.
        logical: list[str] = []
        buf = ""
        for raw in lines:
            chunk = raw.lstrip("\t")
            if chunk.rstrip().endswith("\\"):
                buf += chunk.rstrip()[:-1] + " "
                continue
            logical.append((buf + chunk).strip())
            buf = ""
        if buf.strip():
            logical.append(buf.strip())
        return logical

    def _segment_is_noise(seg: str) -> bool:
        # A segment that does not RUN the binary: the `test -x`/`[ -x ]` existence
        # guard or an `echo`/`printf` message (which may mention `$(BIN)` in its text,
        # e.g. the fail-closed guard's error string). Make recipe prefixes (`@`/`-`/
        # `+`) and a leading `{` (from `|| { echo … }`) are trimmed first.
        s = seg.strip().lower().lstrip("@-+{ \t")
        return (s.startswith("test ") or s.startswith("test\t") or s.startswith("[")
                or s.startswith("echo ") or s.startswith("echo\t") or s == "echo"
                or s.startswith("printf"))

    # Expand make variables (so a runner aliased via `RUNNER = $(BINDIR)/$(BIN)` is
    # still detected as a run) but PRESERVE `SPEC`/`CASES`: their `$(SPEC)`/`$(CASES)`
    # references must survive verbatim so the compliance check can confirm the recipe
    # forwards the env-injected values rather than hardcoding a spec/case list.
    for tgt in _PHONY_TEST_TARGETS:
        if tgt not in recipes and tgt not in rules:
            continue
        recipe_lines = _run_recipe_lines(tgt)
        if not recipe_lines:
            continue
        runs_binary = False
        noncompliant_run = False
        for line in _logical_recipe_lines(recipe_lines):
            expanded = _expand_make_vars(
                line, full_var_map, preserve={"SPEC", "CASES"})
            # Remove quote CHARACTERS (keep the content) so a shell-quoted forward
            # `--cases "$(SPEC)" "$(CASES)"` still exposes the `$(SPEC)`/`$(CASES)`
            # tokens, while a `;`/`|` inside a (now-unquoted) echo message that splits
            # a segment is harmless because echo segments are classified as noise.
            cleaned = expanded.replace('"', "").replace("'", "").replace("`", "").lower()
            # Split into shell command segments; compliance is checked on the SEGMENT
            # that runs the binary (not the whole line) so an echo mentioning `--cases`
            # elsewhere does not mask a bare run.
            for seg in re.split(r"&&|\|\||;|\|", cleaned):
                invokes = "$(bin)" in seg or "$(bindir)" in seg
                if not invokes and binary_basename:
                    invokes = re.search(
                        rf"\b{re.escape(binary_basename)}\b", seg) is not None
                if not invokes or _segment_is_noise(seg):
                    continue
                runs_binary = True
                # Compliant iff the run forwards the env-injected SPEC/CASES vars; a
                # bare run (no `--cases`) OR a hardcoded `--cases spec.ir.yaml c_old`
                # that ignores the env override both desync make test from run_program.
                forwards_spec = "$(spec)" in seg or "${spec}" in seg
                forwards_cases = "$(cases)" in seg or "${cases}" in seg
                if not ("--cases" in seg and forwards_spec and forwards_cases):
                    noncompliant_run = True
        if runs_binary and noncompliant_run:
            violations.append(
                f"{makefile_path}: {tgt} target does not invoke the runner as "
                "`$(BINDIR)/$(BIN) --cases $(SPEC) $(CASES)` — the recipe must forward "
                "the `$(SPEC)`/`$(CASES)` make variables (a bare run, or a hardcoded "
                "`--cases <spec> <ids>` that ignores them, desyncs make test from "
                "run_program: the runner requires `--cases`, and Validate.execute "
                "injects the authoritative SPEC/CASES via the env, which override the "
                "`?=` defaults kept for local use) "
                "(docs/workflow/RUNNER_OUTPUT_CONTRACT.md §5 / phase_04_validate.md §4-1)"
            )


# Edit descriptors may carry a leading repeat count (e.g. ``2l1`` / ``3f0.6``);
# the negative lookbehind excludes letters so multi-letter descriptors such as
# ``tl`` (tab-left) are not mistaken for an ``L`` (logical) descriptor.
_RUNNER_FORMAT_LOGICAL_DESC = re.compile(r"(?<![a-z])l\d*(?![a-z])")
# ``f0`` is the F descriptor with width 0; it may be preceded by a repeat count
# (``2f0.6``) or a ``P`` scale factor (``1pf0.6``). The lookbehind excludes every
# letter *except* ``p`` so a ``P`` scale factor is allowed while ``f0`` embedded
# in a word (e.g. ``leaf0``) is not matched.
_RUNNER_FORMAT_F0_DESC = re.compile(r"(?<![a-oq-z])f0(?:\.\d+)?")
# Statement recognizers (operate on lowercased logical lines). ``write`` must be a
# statement keyword followed by ``(`` (not a substring of an identifier such as
# ``write_flag`` / ``rewrite``). A FORMAT statement carries a leading label.
_RUNNER_WRITE_STMT = re.compile(r"(?<![a-z0-9_])write\s*\(")
_RUNNER_FORMAT_STMT = re.compile(r"^\s*(\d+)\s+format\s*\(")
_RUNNER_CHAR_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_RUNNER_KEYWORD_ITEM = re.compile(r"[a-z][a-z0-9_]*\s*=")
_RUNNER_FMT_KEYWORD = re.compile(r"fmt\s*=\s*(.*)", re.DOTALL)
_RUNNER_NAME_TOKEN = re.compile(r"[a-z][a-z0-9_]*\Z")
# Fortran scoping-unit boundaries. Statement labels, FORMAT statements, and local
# variables are scoped to their program unit, so resolution must not cross units.
_FORTRAN_UNIT_END = re.compile(
    r"^\s*end\s*$|^\s*end\s*(?:program|module|submodule|subroutine|function|blockdata)\b"
)
_FORTRAN_UNIT_OPEN = re.compile(
    r"^\s*(?:(?:pure|elemental|impure|recursive)\s+)*(?:program|subroutine|module|submodule)\b"
)
_FORTRAN_FUNCTION_OPEN = re.compile(r"(?<![a-z0-9_])function\s+[a-z][a-z0-9_]*\s*\(")


def _strip_fortran_inline_comment(line: str) -> str:
    """Drop a trailing ``!`` comment, honoring single/double quoted strings."""
    in_single = False
    in_double = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "!":
            return line[:i]
        i += 1
    return line


def _split_fortran_statements(line: str) -> list[str]:
    """Split a logical line on top-level ``;`` statement separators.

    Semicolons inside quotes or parentheses are ignored, so a line such as
    ``fmt = '(a,l1,a)'; write(u, fmt) x`` becomes two statements.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    for ch in line:
        if in_single:
            current.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            current.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
            current.append(ch)
        elif ch == '"':
            in_double = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == ";" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _iter_fortran_logical_lines(text: str) -> list[tuple[int, str]]:
    """Merge free-form continuation lines and split ``;``-separated statements.

    Returns ``(start_lineno, statement)`` pairs, where ``start_lineno`` is the
    physical line on which the statement began. Trailing ``&`` continuations are
    merged, inline ``!`` comments stripped, an optional leading ``&`` on a
    continuation line dropped, and a logical line carrying multiple ``;``-joined
    statements is expanded into one entry per statement (all sharing the line).
    """
    logical: list[tuple[int, str]] = []
    buffer = ""
    start_lineno: int | None = None

    def flush() -> None:
        if start_lineno is None:
            return
        for statement in _split_fortran_statements(buffer):
            if statement.strip():
                logical.append((start_lineno, statement))

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        code = _strip_fortran_inline_comment(raw_line).rstrip()
        continued = code.endswith("&")
        if continued:
            code = code[:-1]
        if buffer:
            stripped = code.lstrip()
            if stripped.startswith("&"):
                stripped = stripped[1:]
            buffer += stripped
        else:
            start_lineno = lineno
            buffer = code
        if continued:
            continue
        flush()
        buffer = ""
        start_lineno = None
    flush()
    return logical


def _extract_balanced_parens(text: str, open_index: int) -> str:
    """Return the substring inside the parentheses opening at ``open_index``.

    Parentheses appearing inside single/double quoted strings are ignored so a
    format literal such as ``'(a,l1,a)'`` does not prematurely close the group.
    """
    depth = 0
    in_single = False
    in_double = False
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i]
        i += 1
    return text[open_index + 1 :]


def _split_top_level_commas(text: str) -> list[str]:
    """Split on commas that are outside parentheses and quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    for ch in text:
        if in_single:
            current.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            current.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
            current.append(ch)
        elif ch == '"':
            in_double = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _fortran_literal_value(token: str) -> str:
    """Return the character value of a quoted Fortran literal token.

    Strips the outer quotes and collapses the doubled outer quote escape
    (``''`` -> ``'`` / ``""`` -> ``"``) so embedded string descriptors are left
    with single delimiters.
    """
    quote = token[0]
    inner = token[1:-1]
    return inner.replace(quote * 2, quote)


def _strip_format_char_literals(fmt_value: str) -> str:
    """Remove embedded character-string edit descriptors from a format value.

    A Fortran format may embed literal text via ``'...'`` or ``"..."`` (with the
    delimiter doubled to escape). Such text is *data*, not descriptor codes, so
    substrings like ``L1`` or ``F0`` inside it must not be scanned. Returns the
    format with all embedded character literals removed.
    """
    out: list[str] = []
    i = 0
    n = len(fmt_value)
    while i < n:
        ch = fmt_value[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if fmt_value[i] == quote:
                    if i + 1 < n and fmt_value[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _scan_runner_format_text(
    runner_file: Path, lineno: int, fmt_value: str, violations: list[str]
) -> None:
    # Blanks are insignificant in a Fortran format spec (outside character
    # literals, already stripped), so remove them before matching ``f 0.6`` etc.
    descriptor_stream = re.sub(r"\s+", "", _strip_format_char_literals(fmt_value))
    if _RUNNER_FORMAT_LOGICAL_DESC.search(descriptor_stream):
        violations.append(
            f"{runner_file}:{lineno}: runner write uses Fortran L edit descriptor "
            f"(emits T/F); JSON boolean must be literal true/false"
        )
    if _RUNNER_FORMAT_F0_DESC.search(descriptor_stream):
        violations.append(
            f"{runner_file}:{lineno}: runner write uses Fortran F0/F0.d descriptor in a "
            f"JSON numeric write; forbidden regardless of runtime fixup — use ES/EN "
            f"(e.g. ES24.16E3) or an explicit-width Fw.d with trim(adjustl()) instead"
        )


_RUNNER_UNIT_KEYWORD = re.compile(r"unit\s*=\s*(.*)", re.DOTALL)
# Unit designators that are never a JSON artifact (stdout / stderr); formatted
# writes to them are debug/log output and must not be scanned for JSON safety.
_NON_JSON_WRITE_UNITS = {"*", "output_unit", "error_unit"}


def _runner_write_unit(io_control: str) -> str | None:
    """Return the unit designator of a ``write`` control list (``*`` / name / id)."""
    items = [item.strip() for item in _split_top_level_commas(io_control)]
    for item in items:
        keyword = _RUNNER_UNIT_KEYWORD.match(item)
        if keyword:
            return keyword.group(1).strip()
    if items and not _RUNNER_KEYWORD_ITEM.match(items[0]):
        return items[0].strip()
    return None


def _runner_write_format_token(io_control: str) -> str | None:
    """Return the format spec token referenced by a ``write`` control list.

    Handles ``fmt=`` keyword form and the positional form (unit first, format
    second). Returns the raw token (a quoted literal, an integer label, or a
    name); ``None`` when there is no format (e.g. list-directed ``write(u, *)``
    is returned as ``*`` and filtered by the caller).
    """
    items = [item.strip() for item in _split_top_level_commas(io_control)]
    for item in items:
        keyword = _RUNNER_FMT_KEYWORD.match(item)
        if keyword:
            return keyword.group(1).strip()
    positional = [item for item in items if not _RUNNER_KEYWORD_ITEM.match(item)]
    if len(positional) >= 2:
        return positional[1].strip()
    return None


def _depth0_assignment_rhs(line: str, name: str) -> str | None:
    """Return the RHS of a top-level assignment ``name = rhs`` on ``line``.

    Quote- and paren-aware so an I/O keyword argument (``fmt=`` inside
    ``write(...)``), an equality test (``==``), and other relational operators
    (``/=`` / ``>=`` / ``<=``) are not mistaken for a variable assignment, and an
    array-element target (``a(i) = ...``) does not match a scalar ``name``.
    Returns ``None`` when the line is not such an assignment.
    """
    depth = 0
    in_single = False
    in_double = False
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "=" and depth == 0:
            if i + 1 < n and line[i + 1] == "=":
                i += 2
                continue
            if i > 0 and line[i - 1] in "<>/=":
                i += 1
                continue
            j = i - 1
            while j >= 0 and line[j] == " ":
                j -= 1
            end = j
            while j >= 0 and (line[j].isalnum() or line[j] == "_"):
                j -= 1
            lhs = line[j + 1 : end + 1]
            if lhs == name and not (j >= 0 and line[j] == ")"):
                # Stop at the next top-level comma so a sibling initializer in a
                # multi-name declaration (``:: a = '(...)', b = '(...)'``) is not
                # folded into this name's RHS.
                return _split_top_level_commas(line[i + 1 :])[0].strip()
        i += 1
    return None


def _split_top_level_concat(expr: str) -> list[str]:
    """Split ``expr`` on the Fortran ``//`` concatenation operator at top level.

    ``//`` inside quotes or parentheses is ignored, so only operands joined at the
    expression's top level are separated.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if in_single:
            current.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            current.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
            current.append(ch)
        elif ch == '"':
            in_double = True
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "/" and depth == 0 and i + 1 < n and expr[i + 1] == "/":
            parts.append("".join(current))
            current = []
            i += 2
            continue
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _resolve_format_expr(expr: str) -> str | None:
    """Resolve a format-spec expression to its constant value, or ``None``.

    Handles a single character literal and a constant concatenation of character
    literals (``'(a,' // 'l1,a)'``). Any non-literal operand (variable / function
    call) makes the expression unresolvable, returning ``None`` so a partial
    literal is never scanned. The resolved value must look like a Fortran format
    spec (parenthesized).
    """
    pieces: list[str] = []
    for operand in _split_top_level_concat(expr):
        operand = operand.strip()
        literal = _RUNNER_CHAR_LITERAL.fullmatch(operand)
        if not literal:
            return None
        pieces.append(_fortran_literal_value(operand))
    value = "".join(pieces)
    return value if value.lstrip().startswith("(") else None


def _assign_fortran_scopes(
    logical_lines: list[tuple[int, str]],
) -> tuple[list[int], dict[int, int | None]]:
    """Return a scope id per logical line plus a ``scope -> parent scope`` map.

    A ``program`` / ``subroutine`` / ``function`` / ``(sub)module`` header opens a
    new scope nested in the current one; the matching ``end`` (or ``end <kind>``)
    closes it. Construct ends (``end do`` / ``end if`` / ...) are not unit ends and
    do not close a scope. The parent map enables host-association lookups for
    names declared in an enclosing unit.
    """
    scopes: list[int] = []
    parents: dict[int, int | None] = {0: None}
    stack = [0]
    next_id = 1
    for _lineno, line in logical_lines:
        lowered = line.lower()
        if _FORTRAN_UNIT_END.match(lowered):
            scopes.append(stack[-1])
            if len(stack) > 1:
                stack.pop()
            continue
        opens_unit = bool(_FORTRAN_UNIT_OPEN.match(lowered)) and not re.match(
            r"^\s*module\s+procedure\b", lowered
        )
        if not opens_unit and _FORTRAN_FUNCTION_OPEN.search(lowered):
            opens_unit = True
        if opens_unit:
            next_id += 1
            parents[next_id] = stack[-1]
            stack.append(next_id)
            scopes.append(next_id)
        else:
            scopes.append(stack[-1])
    return scopes, parents


def _validate_runner_json_serialization(
    runner_file: Path,
    text: str,
    violations: list[str],
) -> None:
    """Flag JSON-incompatible Fortran edit descriptors in runner write statements.

    The ``runner`` emits only JSON artifacts (``diagnostics.json`` / ``perf.json``
    / ``raw/metrics_basis.json`` / ``raw/state_snapshots/*.json``). Two descriptor
    classes in a ``write`` format spec break standard JSON parsing and must never
    reach a JSON token:

      * ``L`` / ``L<n>`` logical edit descriptor emits the bare tokens ``T`` / ``F``
        instead of JSON ``true`` / ``false`` (booleans must be literal strings).
      * ``F0`` / ``F0.d`` numeric edit descriptor can emit leading-zero-less floats
        like ``.5`` that violate RFC 8259.

    The source is scanned as *logical* lines (free-form ``&`` continuations are
    merged, inline comments stripped). Only genuine ``write(...)`` statements are
    inspected — the keyword must be followed by ``(`` so an identifier such as
    ``write_flag`` on a ``read`` line is not mistaken for output. For each write
    the format spec it actually references is resolved and scanned, whether it is
    an inline literal (``write(u, '(...)')`` / ``write(u, fmt='(...)')``), an
    integer label bound to a ``FORMAT`` statement (``write(u, 100)`` +
    ``100 format(...)``), or a named character constant/variable
    (``write(u, fmt)`` + ``fmt = '(...)'``). Statement labels and local variables
    are resolved within the write's own Fortran scoping unit, so a label/name
    reused in another program unit is not confused. For a named format the most
    recent literal assignment at or before the write (its reaching definition) is
    scanned; a non-literal reassignment (``fmt=fmt`` / computed) does not clear a
    prior unsafe literal. ``read`` statements and read-only format definitions are
    never scanned, since logical/numeric input parsing legitimately uses these
    descriptors.
    """
    logical_lines = _iter_fortran_logical_lines(text)
    scopes, scope_parents = _assign_fortran_scopes(logical_lines)

    def scope_chain(scope: int) -> list[int]:
        chain: list[int] = []
        current: int | None = scope
        while current is not None:
            chain.append(current)
            current = scope_parents.get(current)
        return chain

    # First pass: collect per-scope label-bound formats and every write statement
    # with the format token it references; record which names are used so only
    # their assignments need tracking.
    # ``order`` is the logical-statement sequence index (monotonic across the
    # whole file), used for reaching analysis instead of the physical line number
    # because ``;``-separated statements share one physical line.
    label_formats: dict[tuple[int, str], str] = {}
    writes: list[tuple[int, int, int, str]] = []  # (scope, order, lineno, token)
    used_names: set[str] = set()
    for order, (scope, (lineno, line)) in enumerate(zip(scopes, logical_lines)):
        lowered = line.lower()
        label_match = _RUNNER_FORMAT_STMT.match(lowered)
        if label_match:
            label_formats[(scope, label_match.group(1))] = _extract_balanced_parens(
                lowered, label_match.end() - 1
            )
        for write_match in _RUNNER_WRITE_STMT.finditer(lowered):
            io_control = _extract_balanced_parens(lowered, write_match.end() - 1)
            unit = _runner_write_unit(io_control)
            if unit is not None and unit in _NON_JSON_WRITE_UNITS:
                continue
            token = _runner_write_format_token(io_control)
            if not token:
                continue
            writes.append((scope, order, lineno, token))
            if (
                token[0] not in "'\""
                and not token.isdigit()
                and _RUNNER_NAME_TOKEN.match(token)
            ):
                used_names.add(token)

    # Collect literal assignments per (scope, name) keyed by statement order. Only
    # resolvable format literals are recorded; a non-literal assignment is
    # intentionally not recorded so it neither resolves nor erases a prior literal.
    name_assignments: dict[tuple[int, str], list[tuple[int, str]]] = {}
    if used_names:
        for order, (scope, (lineno, line)) in enumerate(zip(scopes, logical_lines)):
            lowered = line.lower()
            for name in used_names:
                rhs = _depth0_assignment_rhs(lowered, name)
                if rhs is None:
                    continue
                value = _resolve_format_expr(rhs)
                if value is not None:
                    name_assignments.setdefault((scope, name), []).append(
                        (order, value)
                    )

    # Second pass: scan each write's resolved format spec. Labels are strictly
    # local to their unit; named formats follow host association (the write's
    # scope and its enclosing scopes) and use the latest assignment strictly
    # before the write in statement order.
    for scope, order, lineno, token in writes:
        if token[0] in "'\"":
            value = _resolve_format_expr(token)
            if value is not None:
                _scan_runner_format_text(runner_file, lineno, value, violations)
        elif token.isdigit():
            content = label_formats.get((scope, token))
            if content is not None:
                _scan_runner_format_text(runner_file, lineno, content, violations)
        elif _RUNNER_NAME_TOKEN.match(token):
            candidates: list[tuple[int, str]] = []
            for ancestor in scope_chain(scope):
                candidates.extend(name_assignments.get((ancestor, token), ()))
            reaching: str | None = None
            for assign_order, value in sorted(candidates):
                if assign_order < order:
                    reaching = value
                else:
                    break
            if reaching is not None:
                _scan_runner_format_text(runner_file, lineno, reaching, violations)


# A whole-path snapshot data filename embedded in a single string literal:
# ``state_snapshots/<name>.json``. The per-case contract requires the runner to
# BUILD the name from the case_id it receives on argv (e.g.
# ``'raw/state_snapshots/'//trim(case_id)//'.json'``), which keeps ``.json`` in a
# SEPARATE literal so this pattern does not match. A fixed/sequential literal
# (``snapshot_0001.json``, a combined file) does match. The character class
# excludes quotes so a match never crosses a string-literal boundary.
_RUNNER_SNAPSHOT_LITERAL = re.compile(r"state_snapshots/([^/'\"]+)\.json")


def _validate_runner_snapshot_filenames(
    runner_file: Path,
    text: str,
    violations: list[str],
    known_case_ids: set[str] | None = None,
) -> None:
    """Flag a hardcoded ``raw/state_snapshots/<name>.json`` filename in the runner.

    ``Validate.execute``'s deliverable gate requires exactly one
    ``raw/state_snapshots/<case_id>.json`` per ``case.test_case_set[].case_id``
    (``workflow_conductor.build_launch_request``), so the runner must build the
    snapshot path from the ``case_id`` it receives on argv (``--cases <spec>
    <case_id>...``) rather than emitting a fixed/sequential name. This best-effort
    static check catches the common failure: a whole-path string literal carrying
    ``state_snapshots/<name>.json`` with no per-case concatenation — e.g.
    ``snapshot_0001.json``. A correctly-built name (``trim(case_id)//'.json'``)
    keeps ``.json`` in a separate literal and is not flagged; the runtime
    deliverable gate is the deterministic backstop for constructions this static
    parse cannot resolve. ``snapshot_schema.json`` (conductor-authored metadata)
    is exempt. When ``known_case_ids`` is given, a hardcoded literal whose stem IS
    a declared case_id is NOT flagged — it satisfies the deliverable gate, so
    flagging it would be a false positive (case-insensitive: ``text`` is the
    already-lowercased runner source and case_ids are lowercase by convention).
    """
    case_id_stems = (
        {cid.lower() for cid in known_case_ids} if known_case_ids else set()
    )
    for lineno, line in _iter_fortran_logical_lines(text):
        if "state_snapshots/" not in line:
            continue
        # Scope to file-opening statements so a snapshot path written as JSON
        # *content* (not an output target) is never mistaken for a filename.
        if "file=" not in line and not re.search(r"\bopen\s*\(", line):
            continue
        for match in _RUNNER_SNAPSHOT_LITERAL.finditer(line):
            name = match.group(1)
            if name == "snapshot_schema" or name in case_id_stems:
                continue
            violations.append(
                f"{runner_file}:{lineno}: hardcoded snapshot filename "
                f"'state_snapshots/{name}.json' — write one "
                "raw/state_snapshots/<case_id>.json per case, building the name "
                "from the case_id received on argv (e.g. trim(case_id)//'.json'); "
                "a fixed/sequential name fails Validate.execute's per-case "
                "deliverable gate (phase_02_generate.md / phase_04_validate.md §43)"
            )


@dataclass
class NodeExecution:
    node_key: str
    node_dir: Path
    exec_dir: Path
    pipeline_dir: Path


@dataclass
class NodeLineage:
    node_key: str
    pipeline_dir: Path
    ir_ref: str | None
    dependency_ref: str | None


@dataclass
class SourceFingerprint:
    node_key: str
    pipeline_dir: Path
    digest: str


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # A UnicodeDecodeError is a ValueError, NOT a json.JSONDecodeError, so it escapes even the
        # callers that guard the decode error and report `invalid json` — the finding the leaf
        # repairs. Re-raise as the error they already handle. (Callers reading CONDUCTOR-authored
        # JSON — trial_meta.json, lineage.json — guard nothing; those files are host-authored, so no
        # leaf can put a bad byte in them.)
        raise json.JSONDecodeError(f"not valid UTF-8 ({exc})", "", 0) from exc
    return json.loads(text)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has_execution_artifacts(node_dir: Path) -> bool:
    markers = (
        node_dir / "diagnostics.json",
        node_dir / "perf.json",
        node_dir / "trial_meta.json",
        node_dir / "quality_check.json",
        node_dir / "raw" / "metrics_basis.json",
    )
    return any(path.exists() for path in markers)


def _subtree_has_any_file(root: Path) -> bool:
    """True if any regular file exists anywhere under ``root``.

    Used by the non-canonical run-directory scan. The only legitimate child under
    ``runs/<run_id>/`` is the pipeline's ``<node_key_safe>`` dir (skipped by the
    caller), so any file-bearing non-canonical child is a forbidden Validate
    output placement — regardless of filename. Detecting content generally
    (rather than a fixed marker set) covers every documented output: diagnostics
    / perf / verdict / summary / semantic_review / trial_meta / quality_check /
    validate_meta, raw evidence (metrics_basis, execution_trace, state_snapshots,
    snapshot_schema), stdout/stderr logs, and command_log — plus any future
    additions. Recursive so legacy nested ``<kind>/<spec>/`` layouts are caught.
    """
    for path in root.rglob("*"):
        if path.is_file():
            return True
    return False


def _node_executions(
    workspace_root: Path,
    pipeline_roots: list[Path] | None = None,
    run_ids: set[str] | None = None,
) -> list[NodeExecution]:
    result: list[NodeExecution] = []
    targets = _pipeline_targets(workspace_root, pipeline_roots)

    for pipeline_dir in targets:
        if not pipeline_dir.is_dir():
            continue
        # The canonical run node directory is the pipeline's own node_key_safe
        # (= pipeline_dir.parent.name). Derive node_key from it so a non-canonical
        # run subdir name (mismatched or unparseable) is never discovered as the
        # execution node; such dirs are reported by _validate_run_node_dir_names.
        expected_node_safe = pipeline_dir.parent.name
        node_key = _node_safe_to_node_key(expected_node_safe)
        if node_key is None:
            continue
        runs_root = pipeline_dir / "runs"
        if not runs_root.exists():
            continue
        for exec_dir in sorted(runs_root.iterdir()):
            if not exec_dir.is_dir():
                continue
            if run_ids is not None and exec_dir.name not in run_ids:
                continue
            for node_safe_dir in sorted(exec_dir.iterdir()):
                if not node_safe_dir.is_dir():
                    continue
                if not _has_execution_artifacts(node_safe_dir):
                    continue
                if node_safe_dir.name != expected_node_safe:
                    continue
                result.append(
                    NodeExecution(
                        node_key=node_key,
                        node_dir=node_safe_dir,
                        exec_dir=exec_dir,
                        pipeline_dir=pipeline_dir,
                    )
                )
    return result


def _validate_run_node_dir_names(
    workspace_root: Path,
    pipeline_roots: list[Path] | None,
    violations: list[str],
    run_ids: set[str] | None = None,
) -> None:
    """Every run-artifact-bearing ``runs/<run_id>/<child>`` directory must be
    named exactly the pipeline's ``node_key_safe`` (``pipeline_dir.parent.name``),
    and that parent must itself be a valid ``node_key_safe``.

    Reports mismatched-but-parseable names (e.g. a forged ``@version`` segment),
    unparseable run-child names, and malformed pipeline parents (where the
    canonical name itself is invalid, so the child could never be canonical).
    ``_node_executions`` only discovers the canonical dir, so without this scan a
    non-canonical artifact directory would be silently ignored and could pass
    validation whenever a canonical execution also exists. Content detection is
    general (``_subtree_has_any_file``) so every documented Validate output —
    including judge-only outputs, logs, command_log, validate_meta, and raw
    evidence — and legacy nested (``<kind>/<spec>/``) layouts are all covered.
    """
    for pipeline_dir in _pipeline_targets(workspace_root, pipeline_roots):
        if not pipeline_dir.is_dir():
            continue
        expected_node_safe = pipeline_dir.parent.name
        parent_is_valid = _node_safe_to_node_key(expected_node_safe) is not None
        runs_root = pipeline_dir / "runs"
        if not runs_root.exists():
            continue
        for exec_dir in sorted(runs_root.iterdir()):
            if not exec_dir.is_dir():
                continue
            if run_ids is not None and exec_dir.name not in run_ids:
                continue
            for child in sorted(exec_dir.iterdir()):
                if not child.is_dir():
                    continue
                # The canonical run node dir (parent valid + name match) is
                # validated by the per-execution checks; skip it (and its nested
                # raw/ artifacts) entirely.
                if parent_is_valid and child.name == expected_node_safe:
                    continue
                # Non-canonical child: any run artifact anywhere beneath it is a
                # forbidden non-canonical Validate output (including legacy nested
                # <kind>/<spec>/ layouts).
                if not _subtree_has_any_file(child):
                    continue
                if not parent_is_valid:
                    violations.append(
                        f"{child}: pipeline node_key_safe directory "
                        f"{expected_node_safe!r} is not a valid "
                        "'<spec_kind>__<spec_id>__<spec_version>'"
                    )
                else:
                    violations.append(
                        f"{child}: run node directory name must equal pipeline "
                        f"node_key_safe {expected_node_safe!r} (got {child.name!r})"
                    )


def _pipeline_targets(
    workspace_root: Path, pipeline_roots: list[Path] | None
) -> list[Path]:
    if pipeline_roots is None:
        pipelines_root = workspace_root / "pipelines"
        if not pipelines_root.exists():
            return []
        targets: list[Path] = []
        for node_safe_dir in sorted(pipelines_root.iterdir()):
            if not node_safe_dir.is_dir():
                continue
            for pipeline_dir in sorted(node_safe_dir.iterdir()):
                if pipeline_dir.is_dir():
                    targets.append(pipeline_dir)
        return targets
    deduped: list[Path] = []
    seen: set[Path] = set()
    for pipeline_dir in sorted(pipeline_roots):
        if pipeline_dir in seen:
            continue
        seen.add(pipeline_dir)
        deduped.append(pipeline_dir)
    return deduped


def _lineage_records(
    workspace_root: Path, pipeline_roots: list[Path] | None
) -> list[NodeLineage]:
    records: list[NodeLineage] = []
    for pipeline_dir in _pipeline_targets(workspace_root, pipeline_roots):
        lineage_path = pipeline_dir / "lineage.json"
        if not lineage_path.exists():
            continue
        try:
            lineage = _read_json(lineage_path)
        except json.JSONDecodeError:
            continue
        if not isinstance(lineage, dict):
            continue
        node_key = lineage.get("node_key")
        if not isinstance(node_key, str) or not node_key.strip():
            continue
        ir_ref = lineage.get("ir_ref")
        dep_ref = lineage.get("dependency_ref")
        records.append(
            NodeLineage(
                node_key=node_key.strip(),
                pipeline_dir=pipeline_dir,
                ir_ref=ir_ref if isinstance(ir_ref, str) else None,
                dependency_ref=dep_ref if isinstance(dep_ref, str) else None,
            )
        )
    return records


def _validate_pipeline_lineage_presence(
    executions: list[NodeExecution],
    violations: list[str],
) -> None:
    seen: set[Path] = set()
    for execution in executions:
        pipeline_dir = execution.pipeline_dir
        if pipeline_dir in seen:
            continue
        seen.add(pipeline_dir)
        lineage_path = pipeline_dir / "lineage.json"
        if not lineage_path.exists():
            violations.append(f"{lineage_path}: missing")
            continue
        try:
            lineage = _read_json(lineage_path)
        except json.JSONDecodeError:
            violations.append(f"{lineage_path}: invalid json")
            continue
        if not isinstance(lineage, dict):
            violations.append(f"{lineage_path}: must be json object")
            continue
        node_key = lineage.get("node_key")
        if not isinstance(node_key, str) or not node_key.strip():
            violations.append(f"{lineage_path}:node_key must be non-empty string")
        else:
            # Tie the declared node_key back to the pipeline's node_key_safe parent
            # directory so a forged/mistyped parent (e.g. a bumped @version) cannot
            # be validated against another node's IR. Downstream node matching
            # normalizes away @version, so this directory binding is the only place
            # the full versioned identity is enforced.
            expected_safe = _node_key_to_safe(node_key.strip())
            actual_safe = pipeline_dir.parent.name
            if expected_safe is None:
                violations.append(
                    f"{lineage_path}:node_key {node_key.strip()!r} must match "
                    "'<spec_kind>/<spec_id>@<spec_version>'"
                )
            elif expected_safe != actual_safe:
                violations.append(
                    f"{lineage_path}:node_key {node_key.strip()!r} (node_key_safe "
                    f"{expected_safe!r}) must match pipeline node_key_safe directory "
                    f"{actual_safe!r}"
                )
        raw_pid = lineage.get("pipeline_id")
        if not isinstance(raw_pid, str) or not raw_pid.strip():
            violations.append(f"{lineage_path}:pipeline_id must be non-empty string")
        else:
            pid = raw_pid.strip()
            if pid != pipeline_dir.name:
                violations.append(
                    f"{lineage_path}:pipeline_id {pid!r} must match directory name {pipeline_dir.name!r}"
                )
            node_safe_dir = pipeline_dir.parent.name
            if not _NODE_KEY_SAFE_PATTERN_LINEAGE.match(node_safe_dir):
                violations.append(
                    f"{pipeline_dir.parent}: invalid node_key_safe directory name for lineage check"
                )
            elif _parse_slug_date_seq3_id(pid) is None:
                violations.append(
                    f"{lineage_path}:pipeline_id must match <slug>_<YYYYMMDD>_<seq3>; got {pid!r}"
                )


def _validate_source_meta_json_files(
    pipeline_dir: Path,
    violations: list[str],
    *,
    in_scope_source_ids: set[str] | None = None,
) -> None:
    """Check every in-scope ``source/<id>/source_meta.json`` against the stage-meta contract.

    ``in_scope_source_ids`` scopes the sweep to the source directories the in-scope runs
    actually DECLARE (``trial_meta.source_source_id``), mirroring ``_execution_in_scope_src_dir``
    (see its docstring): a superseded attempt directory, left behind by an earlier Generate
    under the append-only contract, must not fail an otherwise-conformant run. That is not a
    cosmetic concern — a schema-violating meta in a superseded dir is UNREPAIRABLE: a Generate
    reopen rotates a FRESH source dir and deletes nothing, so every subsequent attempt re-reads
    the same immutable violation and the repair loop cannot converge (E2E #4).

    A superseded dir is therefore skipped ENTIRELY — not even its JSON is parsed. Anything the
    sweep could still flag there would reintroduce exactly the unrepairable class this scoping
    removes; the dir is debug provenance, not a certified deliverable.

    ``None`` (the default) keeps the historic pipeline-wide sweep: callers that cannot derive a
    declared-source scope must not silently get a weaker gate.
    """
    generate_root = pipeline_dir / "source"
    if not generate_root.exists() or not generate_root.is_dir():
        return
    for gen_dir in sorted(generate_root.iterdir()):
        if not gen_dir.is_dir():
            continue
        if in_scope_source_ids is not None and gen_dir.name not in in_scope_source_ids:
            continue
        meta_path = gen_dir / "source_meta.json"
        if not meta_path.exists():
            # An in-scope (declared) source MUST carry its meta: it is a required output of the
            # certified Generate. An undeclared/superseded dir is only ever an attempt artifact,
            # so a missing meta there says nothing.
            if in_scope_source_ids is not None:
                violations.append(f"{meta_path}: missing")
            continue
        try:
            data = _read_json(meta_path)
        except json.JSONDecodeError:
            violations.append(f"{meta_path}: invalid json")
            continue
        if not isinstance(data, dict):
            violations.append(f"{meta_path}: must be json object")
            continue
        for key in required_meta_keys_for_step("generate"):
            if key not in data:
                violations.append(f"{meta_path}: missing required key {key!r}")
        for clause in stage_meta_type_violations(data, step_token="generate"):
            violations.append(f"{meta_path}:{clause}")
        # NOTE: lint is no longer recorded in source_meta.lint_command_ref (the leaf does
        # not run run_linter); the conductor-run lint is certified by post_generate (which now
        # runs as the static check of the deterministic generate.gate substep, before verify)
        # against the host-authored lint evidence (_validate_generate_lint_command_logs).


def _validate_ir_meta_json(ir_dir: Path, violations: list[str]) -> None:
    meta_path = ir_dir / STAGE_META_FILENAME_BY_STEP["compile"]
    if not meta_path.exists():
        violations.append(f"{meta_path}: missing")
        return
    try:
        data = _read_json(meta_path)
    except json.JSONDecodeError:
        violations.append(f"{meta_path}: invalid json")
        return
    if not isinstance(data, dict):
        violations.append(f"{meta_path}: must be json object")
        return
    required_keys = required_meta_keys_for_step("compile")
    for key in required_keys:
        if key not in data:
            violations.append(f"{meta_path}: missing required key {key!r}")
    for clause in stage_meta_type_violations(data, step_token="compile"):
        violations.append(f"{meta_path}:{clause}")


_MCP_AUDIT_LOG_BASENAME: str = "command_log.jsonl"


def _canonical_mcp_log_refs_for_lint(meta_path: Path, repo_root: Path) -> set[str]:
    """Canonical command_log_ref placements for `source_meta.json` lint validation.

    Only one canonical placement: sibling under `<gen_dir>/src/`. A child agent
    that writes a forged command_log.jsonl elsewhere and points the
    `lint evidence run_linter[].command_log_ref` at it should be rejected.
    """
    parent = meta_path.parent
    canonical = parent / "src" / _MCP_AUDIT_LOG_BASENAME
    try:
        rel = canonical.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return set()
    return {rel}


def _iter_command_ref_entries(node: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "command_id" in node and (
            "command_log_ref" in node or "command_log_path" in node
        ):
            refs.append(node)
        for value in node.values():
            refs.extend(_iter_command_ref_entries(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_iter_command_ref_entries(item))
    return refs


def _validate_trial_meta(repo_root: Path, execution: NodeExecution, violations: list[str]) -> None:
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        violations.append(f"{trial_meta_path}: missing")
        return

    data = _read_json(trial_meta_path)

    process_trace_ref = data.get("process_trace_ref")
    if not isinstance(process_trace_ref, str) or not process_trace_ref.startswith("workspace/"):
        violations.append(
            f"{trial_meta_path}:process_trace_ref must start with workspace/"
        )
    else:
        trace_path = repo_root / process_trace_ref
        if not trace_path.exists():
            violations.append(f"{trial_meta_path}:process_trace_ref target not found ({process_trace_ref})")

    raw_refs = data.get("raw_artifact_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        violations.append(f"{trial_meta_path}:raw_artifact_refs must be non-empty list")
    else:
        for i, ref in enumerate(raw_refs):
            if not isinstance(ref, str) or not ref.startswith("workspace/"):
                violations.append(
                    f"{trial_meta_path}:raw_artifact_refs[{i}] must start with workspace/"
                )
                continue
            target = repo_root / ref
            if not target.exists():
                violations.append(
                    f"{trial_meta_path}:raw_artifact_refs[{i}] target not found ({ref})"
                )

    source_command_ref = data.get("source_command_ref")
    if source_command_ref is None:
        violations.append(f"{trial_meta_path}:source_command_ref missing")
        return

    # `source_source_id` is a hard requirement for execute trial_meta —
    # without it, validators cannot bind quality_check evidence to a specific
    # generation, and a writer could otherwise omit the field to silently
    # bypass `tool_name` / mandatory `run_program` checks. Legacy artifacts
    # under active validation must be re-recorded; post-migration writers
    # always emit the field.
    _src_gen_raw = data.get("source_source_id")
    if not isinstance(_src_gen_raw, str) or not _src_gen_raw.strip():
        violations.append(
            f"{trial_meta_path}:source_source_id is required (single "
            "trusted source for cross-phase quality_check provenance and "
            "the gate for strict source_command_ref validation)."
        )
    # `source_build_id` binds run_program evidence to the specific build whose
    # binary this execute used. Without it, a trial_meta could attribute its
    # results to one build while having actually executed a sibling build's
    # binary (mixed-build attribution).
    _src_build_raw = data.get("source_binary_id")
    _trial_source_build_id: str | None = None
    if not isinstance(_src_build_raw, str) or not _src_build_raw.strip():
        violations.append(
            f"{trial_meta_path}:source_build_id is required (binds "
            "run_program evidence to the specific build whose binary the "
            "execute run consumed)."
        )
    else:
        _trial_source_build_id = _src_build_raw.strip()
        # Verify the referenced binary directory exists with a bin/ subdirectory.
        _build_bin = (
            execution.pipeline_dir
            / "binary"
            / _trial_source_build_id
            / "bin"
        )
        if not _build_bin.is_dir():
            violations.append(
                f"{trial_meta_path}:source_build_id={_trial_source_build_id!r} "
                f"does not resolve to an existing build bin directory "
                f"({_build_bin!s})."
            )
            _trial_source_build_id = None
    declared_tool_names_in_entries: list[str] = []

    # source_command_ref entries record run_program/run_threads/etc. command
    # invocations; their log files use project-defined filenames (e.g.
    # `run_commands.jsonl`) and do NOT use the canonical MCP audit log basename.
    # The validator only checks command_id presence (no tool_name/ok inspection),
    # so the forge surface here is limited to "a record exists with this id" —
    # not meaningful evidence of successful MCP tool execution. Canonical
    # placement is enforced separately for the conductor lint evidence where the
    # validator inspects tool_name/ok and the forge becomes high-impact.
    for entry in _iter_command_ref_entries(source_command_ref):
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref") or entry.get("command_log_path")
        if not isinstance(command_id, str) or not command_id:
            violations.append(f"{trial_meta_path}:command_id invalid in source_command_ref")
            continue
        if not isinstance(log_ref, str):
            violations.append(f"{trial_meta_path}:command_log_ref/path invalid in source_command_ref")
            continue
        if not log_ref.startswith("workspace/"):
            violations.append(
                f"{trial_meta_path}:command_log_ref/path must start with workspace/ ({log_ref})"
            )
            continue
        log_path = repo_root / log_ref if log_ref.startswith("workspace/") else Path(log_ref)
        if not log_path.exists():
            violations.append(f"{trial_meta_path}:command log missing ({log_ref})")
            continue
        found_record: dict[str, Any] | None = None
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("command_id") == command_id:
                if isinstance(obj, dict):
                    found_record = obj
                break
        if found_record is None:
            violations.append(
                f"{trial_meta_path}:command_id {command_id} not found in {log_ref}"
            )
            continue
        # Defense against forged source_command_ref evidence: every entry must
        # declare its role via `tool_name` and the matched log record must
        # report the same tool. Execute trial_meta only documents tools
        # actually invoked by the execute role (run_program / run_quality_checks).
        # `compile_project` is a build-phase tool whose evidence belongs in
        # binary_meta.json, not execute trial_meta — accepting it here would
        # let a child claim build provenance through execute records without
        # any role-specific lineage validation.
        recognized_tool_names = {"run_program", "run_quality_checks"}
        declared_tool_name_raw = entry.get("tool_name")
        if (
            not isinstance(declared_tool_name_raw, str)
            or declared_tool_name_raw.strip() not in recognized_tool_names
        ):
            violations.append(
                f"{trial_meta_path}:source_command_ref entry with command_id "
                f"{command_id} must declare tool_name field in "
                f"{sorted(recognized_tool_names)!r} (got "
                f"{declared_tool_name_raw!r}). Each entry must commit to a "
                f"specific MCP tool role so downstream role-specific checks "
                f"cannot be silently skipped."
            )
            continue
        declared_tool_name = declared_tool_name_raw.strip()
        declared_tool_names_in_entries.append(declared_tool_name)
        record_tool_name = found_record.get("tool_name")
        if not isinstance(record_tool_name, str) or record_tool_name not in recognized_tool_names:
            violations.append(
                f"{trial_meta_path}:source_command_ref command_id {command_id} log "
                f"record must declare tool_name in {sorted(recognized_tool_names)!r} "
                f"(got {record_tool_name!r}). Records without a recognized MCP "
                f"tool_name cannot serve as tool-execution evidence."
            )
            continue
        if record_tool_name != declared_tool_name:
            violations.append(
                f"{trial_meta_path}:source_command_ref entry tool_name="
                f"{declared_tool_name!r} (command_id={command_id}) does not "
                f"match log record tool_name={record_tool_name!r}. The "
                f"declared role must match the resolved MCP record."
            )

    # Execute trial_meta MUST contain at least one run_program entry — this is
    # the actual execution evidence the trial is supposed to record. Without
    # this check, a forged trial_meta with only `compile_project` /
    # `run_quality_checks` entries (which validate via different code paths)
    # could pass `post_execute` while never proving the program actually ran.
    if "run_program" not in declared_tool_names_in_entries:
        violations.append(
            f"{trial_meta_path}:source_command_ref must include at least one "
            f"entry with tool_name='run_program' (declared roles: "
            f"{sorted(set(declared_tool_names_in_entries))!r}). Execute trial "
            f"metadata requires actual program-run evidence."
        )


def _validate_raw_evidence(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    state_snapshot_required = _state_snapshot_required(repo_root, execution)
    required_raw_evidence = _required_raw_evidence(repo_root, execution)
    (
        expected_state_variables,
        expected_time_variable,
        expected_time_shape_expr,
        required_snapshot_min_samples,
    ) = _state_snapshot_requirement_details(repo_root, execution)

    required = [
        execution.node_dir / "diagnostics.json",
        execution.node_dir / "perf.json",
        execution.node_dir / "quality_check.json",
    ]
    if "metrics_basis.json" in required_raw_evidence:
        required.append(execution.node_dir / "raw" / "metrics_basis.json")
    if "execution_trace.json" in required_raw_evidence:
        required.append(execution.node_dir / "raw" / "execution_trace.json")
    if state_snapshot_required or "state_snapshots" in required_raw_evidence:
        required.append(execution.node_dir / "raw" / "state_snapshots")
    for path in required:
        if not path.exists():
            violations.append(f"{path}: missing")

    snapshots_dir = execution.node_dir / "raw" / "state_snapshots"
    if snapshots_dir.exists() and snapshots_dir.is_dir():
        files = sorted(p for p in snapshots_dir.rglob("*") if p.is_file())
        if not files:
            violations.append(f"{snapshots_dir}: empty directory")
        else:
            schema_path = snapshots_dir / SNAPSHOT_SCHEMA_FILE
            snapshot_data_files = [p for p in files if p != schema_path]
            for snapshot in files:
                text = snapshot.read_text(encoding="utf-8", errors="ignore")
                compact = text.replace(" ", "").replace("\n", "")
                for patt in PLACEHOLDER_TEXT_PATTERNS:
                    if patt in compact:
                        violations.append(
                            f"{snapshot}: placeholder content detected ({patt})"
                        )

            if state_snapshot_required:
                if not schema_path.exists():
                    violations.append(
                        f"{schema_path}: missing for required state_snapshots evidence"
                    )
                else:
                    try:
                        schema_data = _read_json(schema_path)
                    except json.JSONDecodeError:
                        violations.append(
                            f"{schema_path}: invalid json"
                        )
                        schema_data = None

                    state_variables: list[str] = []
                    state_variable_shapes: dict[str, str] = {}
                    time_variable = ""
                    time_shape_expr = "scalar"
                    if isinstance(schema_data, dict):
                        raw_variables = schema_data.get("variables")
                        if isinstance(raw_variables, list):
                            for raw_variable in raw_variables:
                                if not isinstance(raw_variable, dict):
                                    continue
                                raw_name = raw_variable.get("name")
                                raw_shape_expr = raw_variable.get("shape_expr")
                                if (
                                    isinstance(raw_name, str)
                                    and raw_name.strip()
                                    and isinstance(raw_shape_expr, str)
                                    and raw_shape_expr.strip()
                                ):
                                    name = raw_name.strip()
                                    state_variables.append(name)
                                    state_variable_shapes[name] = _canonical_shape_expr(raw_shape_expr)

                        # Reject the legacy `state_variables: [name, ...]`
                        # shorthand: it left snapshot shape unconstrained,
                        # which let corrupted/wrong-rank payloads pass through
                        # to pre_judge undetected. The canonical form is
                        # `variables: [{name, shape_expr}, ...]` with explicit
                        # per-variable shape_expr.
                        if "state_variables" in schema_data:
                            violations.append(
                                f"{schema_path}: 'state_variables' shorthand is not supported; "
                                "use 'variables: [{name, shape_expr}, ...]' with explicit per-variable shape_expr"
                            )
                        raw_time_var = schema_data.get("time_variable")
                        if isinstance(raw_time_var, str) and raw_time_var.strip():
                            time_variable = raw_time_var.strip()
                        raw_time_shape = schema_data.get("time_shape_expr")
                        if isinstance(raw_time_shape, str) and raw_time_shape.strip():
                            time_shape_expr = _canonical_shape_expr(raw_time_shape)
                    else:
                        violations.append(
                            f"{schema_path}: must be json object"
                        )

                    if not state_variables:
                        violations.append(
                            f"{schema_path}: variables must be a non-empty list of "
                            "{name, shape_expr} objects"
                        )
                    if not time_variable:
                        violations.append(
                            f"{schema_path}: time_variable must be non-empty string"
                        )

                    if not snapshot_data_files:
                        violations.append(
                            f"{snapshots_dir}: snapshot data file missing"
                        )
                    elif state_variables and time_variable:
                        missing_state_by_file: dict[str, list[str]] = {}
                        missing_time = set()
                        # Scope each snapshot's required state variables to the
                        # raw variables its case's test actually declares
                        # (io_contract.test_evidence_requirements). An
                        # input-guard rejection case (e.g. n <= 0) produces no
                        # output state, so its snapshot legitimately carries
                        # only the rejected input; requiring the global union of
                        # declared variables in every snapshot would falsely
                        # fail it. Falls back to all declared variables when no
                        # per-test contract / case mapping is available.
                        contract = _io_contract_for_execution(repo_root, execution)
                        per_test_required = (
                            _contract_test_evidence_requirements(contract)
                            if isinstance(contract, dict)
                            else {}
                        )
                        case_to_test = _case_id_to_test_id(repo_root, execution)
                        case_to_tests = (
                            _case_id_to_test_ids(contract)
                            if isinstance(contract, dict)
                            else {}
                        )
                        # Declared `case.test_case_set[].case_id`, so a case the predicates
                        # do not range over can be told apart from an unknown snapshot: the
                        # former has an empty required set (the renderer emits an empty-state
                        # snapshot for it), the latter cannot be scoped at all.
                        declared_case_ids = _case_ids_for_execution(repo_root, execution)
                        for snapshot in snapshot_data_files:
                            if snapshot.suffix.lower() != ".json":
                                continue
                            try:
                                data = _read_json(snapshot)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(data, dict):
                                continue
                            keys = set(data.keys())
                            required_state_names = state_variables
                            if per_test_required:
                                # Identify what this snapshot's required raw
                                # variables are. Compile/runner output shape
                                # varies across runs (C-class IR
                                # nondeterminism): the snapshot may carry an
                                # explicit `test_id`, or only a `case_id`
                                # (mapped via case.test_case_set, which itself
                                # sometimes omits test_id), and is named
                                # `<case_or_test>[_NNNN].json`; case_id sometimes
                                # equals the test_id. Try each anchor in order of
                                # reliability and use the first that resolves to
                                # a declared per-test requirement; otherwise fall
                                # back to requiring every declared variable.
                                raw_case_id = data.get("case_id")
                                case_token = (
                                    raw_case_id.strip()
                                    if isinstance(raw_case_id, str)
                                    and raw_case_id.strip()
                                    else snapshot.stem
                                )
                                case_required = None
                                # (1) A per-TEST snapshot names its test outright.
                                raw_test_id = data.get("test_id")
                                if (
                                    isinstance(raw_test_id, str)
                                    and raw_test_id.strip() in per_test_required
                                ):
                                    case_required = per_test_required[raw_test_id.strip()]
                                # (2) A per-CASE snapshot (the contract's `<case_id>.json`,
                                # and everything a host-rendered runner writes) carries no
                                # test_id. Its required set is the UNION over every test
                                # ranging over the case — precisely what
                                # `runner_renderer._per_case_vars` emitted. Without this
                                # anchor an IR whose `case.test_case_set[]` omits `test_id`
                                # (never a required field) falls through to "every declared
                                # variable" and false-rejects a conformant per-case snapshot,
                                # contradicting the `state_snapshots` bullet of
                                # phase_04_validate.md ("the post_execute semantic gate scopes
                                # required variables per the snapshot's case"). A case targeted by
                                # several tests has no single test_id, so only the union is
                                # well-defined. Anchor (1) must stay ahead of this one: a per-test
                                # snapshot names its test and scopes to that test alone.
                                if case_required is None:
                                    union_tests = [
                                        t
                                        for t in case_to_tests.get(case_token, [])
                                        if t in per_test_required
                                    ]
                                    if union_tests:
                                        case_required = set().union(
                                            *(per_test_required[t] for t in union_tests)
                                        )
                                # (3) Legacy single-test anchors: an IR that does declare
                                # `case.test_case_set[].test_id`, or a snapshot whose stem
                                # is itself the test_id.
                                if case_required is None:
                                    for candidate in (
                                        case_to_test.get(case_token),
                                        case_token,
                                    ):
                                        if candidate and candidate in per_test_required:
                                            case_required = per_test_required[candidate]
                                            break
                                # (4) A DECLARED case that no predicate ranges over: its union is
                                # EMPTY, and `_per_case_vars` renders it as an empty-state
                                # snapshot. Demanding every declared variable of it would be the
                                # same false-reject the union anchor exists to remove. An
                                # untargeted case is schema-valid — `validate_predicate_schema`
                                # checks each `target_cases` entry IS a declared case, never that
                                # every declared case is targeted. Ordered last so a legacy
                                # per-case mapping still wins; and gated on the case being
                                # DECLARED, so an unknown snapshot token still gets the strict set.
                                if (case_required is None and case_to_tests
                                        and case_token in declared_case_ids):
                                    case_required = set()
                                if case_required is not None:
                                    required_state_names = [
                                        name
                                        for name in state_variables
                                        if name in case_required
                                    ]
                            missing_state = sorted(name for name in required_state_names if name not in keys)
                            if missing_state:
                                missing_state_by_file[snapshot.name] = missing_state
                            if time_variable not in keys:
                                missing_time.add(snapshot.name)

                            for name, shape_expr in state_variable_shapes.items():
                                if name not in data:
                                    continue
                                value_shape = _infer_json_shape(data.get(name))
                                if value_shape is None:
                                    violations.append(
                                        f"{snapshot}:{name} has unsupported or ragged shape"
                                    )
                                    continue
                                if not _shape_matches_expr(shape_expr, value_shape):
                                    violations.append(
                                        f"{snapshot}:{name} shape {value_shape} does not match declared shape_expr {shape_expr}"
                                    )

                            if time_variable in data:
                                time_shape = _infer_json_shape(data.get(time_variable))
                                if time_shape is None:
                                    violations.append(
                                        f"{snapshot}:{time_variable} has unsupported or ragged shape"
                                    )
                                elif not _shape_matches_expr(time_shape_expr, time_shape):
                                    violations.append(
                                        f"{snapshot}:{time_variable} shape {time_shape} does not match declared time_shape_expr {time_shape_expr}"
                                    )
                        if missing_state_by_file:
                            violations.append(
                                f"{snapshots_dir}: declared state_variables missing in snapshot files ({missing_state_by_file})"
                            )
                        if missing_time:
                            violations.append(
                                f"{snapshots_dir}: declared time_variable missing in snapshots ({time_variable}, files={sorted(missing_time)})"
                            )

                    if expected_state_variables:
                        missing_required = set(expected_state_variables.keys()) - set(state_variables)
                        if missing_required:
                            violations.append(
                                f"{schema_path}: missing required state_variables from io_contract ({sorted(missing_required)})"
                            )
                        for name, expected_shape in expected_state_variables.items():
                            declared_shape = state_variable_shapes.get(name)
                            if declared_shape is None:
                                continue
                            if _canonical_shape_expr(expected_shape) != _canonical_shape_expr(declared_shape):
                                violations.append(
                                    f"{schema_path}: variable {name} shape_expr must match io_contract ({expected_shape})"
                                )

                    if expected_time_variable and expected_time_variable != time_variable:
                        violations.append(
                            f"{schema_path}: time_variable must match io_contract ({expected_time_variable})"
                        )
                    if expected_time_variable and _canonical_shape_expr(expected_time_shape_expr) != _canonical_shape_expr(time_shape_expr):
                        violations.append(
                            f"{schema_path}: time_shape_expr must match io_contract ({expected_time_shape_expr})"
                        )

                if len(snapshot_data_files) < required_snapshot_min_samples:
                    violations.append(
                        f"{snapshots_dir}: snapshot data files must be >= {required_snapshot_min_samples}"
                    )

    diagnostics_path = execution.node_dir / "diagnostics.json"
    metrics_basis_path = execution.node_dir / "raw" / "metrics_basis.json"
    if diagnostics_path.exists() and metrics_basis_path.exists():
        try:
            diagnostics = _read_json(diagnostics_path)
        except json.JSONDecodeError:
            violations.append(f"{diagnostics_path}: invalid json")
            diagnostics = None
        try:
            metrics_basis = _read_json(metrics_basis_path)
        except json.JSONDecodeError:
            violations.append(f"{metrics_basis_path}: invalid json")
            metrics_basis = None
        if (
            diagnostics is not None
            and metrics_basis is not None
            and _canonical_json(diagnostics) == _canonical_json(metrics_basis)
        ):
            violations.append(
                f"{metrics_basis_path}: must not be identical copy of diagnostics.json"
            )
        if isinstance(metrics_basis, dict):
            _validate_metrics_basis_per_test(repo_root, execution, metrics_basis, violations)

    _validate_diagnostics_contract_output(repo_root, execution, violations)

    quality_path = execution.node_dir / "quality_check.json"
    if quality_path.exists():
        try:
            quality = _read_json(quality_path)
        except json.JSONDecodeError:
            violations.append(f"{quality_path}: invalid json")
            return
        checks = quality.get("checks", {})
        if not isinstance(checks, dict):
            violations.append(f"{quality_path}:checks must be object")
        else:
            if checks.get("verdict_available") is not True:
                violations.append(
                    f"{quality_path}:checks.verdict_available must be true"
                )
            if checks.get("diagnostics_match") is not True:
                violations.append(
                    f"{quality_path}:checks.diagnostics_match must be true"
                )
            if checks.get("verdict_match") is not True:
                violations.append(
                    f"{quality_path}:checks.verdict_match must be true"
                )
        if quality.get("status") != "pass":
            violations.append(f"{quality_path}:status must be pass")


def _validate_execution_json_outputs(execution: NodeExecution, violations: list[str]) -> None:
    diagnostics_path = execution.node_dir / "diagnostics.json"
    if diagnostics_path.exists():
        try:
            diagnostics = _read_json(diagnostics_path)
        except json.JSONDecodeError:
            violations.append(f"{diagnostics_path}: invalid json")
        else:
            if not isinstance(diagnostics, dict):
                violations.append(f"{diagnostics_path}: must be json object")

    perf_path = execution.node_dir / "perf.json"
    if not perf_path.exists():
        return

    try:
        perf = _read_json(perf_path)
    except json.JSONDecodeError:
        violations.append(f"{perf_path}: invalid json")
        return
    if not isinstance(perf, dict):
        violations.append(f"{perf_path}: must be json object")
        return

    for key in ("walltime_sec", "throughput_cells_per_sec", "parallelism"):
        if key not in perf:
            violations.append(f"{perf_path}: missing required field ({key})")

    for key in ("walltime_sec", "throughput_cells_per_sec"):
        if key not in perf:
            continue
        value = perf.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            violations.append(f"{perf_path}:{key} must be number")
        elif value < 0:
            violations.append(f"{perf_path}:{key} must be >= 0")

    parallelism = perf.get("parallelism")
    if parallelism is None:
        return
    if not isinstance(parallelism, dict):
        violations.append(f"{perf_path}:parallelism must be object")
        return
    for key in ("mpi_ranks", "threads_per_rank", "gpu_devices", "parallel_degree_total"):
        value = parallelism.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            violations.append(f"{perf_path}:parallelism.{key} must be integer")
        elif value < 0:
            violations.append(f"{perf_path}:parallelism.{key} must be >= 0")


def _execution_raw_source_source_id(execution: NodeExecution) -> str | None:
    """The ``source_source_id`` string declared by this execution's ``trial_meta.json``,
    stripped; ``None`` when the file/key is absent, unreadable, or blank."""
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        return None
    try:
        data = _read_json(trial_meta_path)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    source_source_id = data.get("source_source_id")
    if not isinstance(source_source_id, str) or not source_source_id.strip():
        return None
    return source_source_id.strip()


def _plain_source_dir_name(source_source_id: str | None) -> str | None:
    """``source_source_id`` if it is a single plain directory name, else ``None``.

    The id is used directly as a path component, so anything else — a separator, an absolute
    path, or a ``.``/``..`` traversal — is rejected: it would otherwise escape
    ``<pipeline>/source/`` and scope a check to an unintended (or out-of-pipeline) directory.
    """
    if source_source_id is None:
        return None
    id_parts = Path(source_source_id).parts
    if (
        "/" in source_source_id
        or "\\" in source_source_id
        or len(id_parts) != 1
        or id_parts[0] in (".", "..")
    ):
        return None
    return source_source_id


def _execution_declared_source_id(execution: NodeExecution) -> str | None:
    """The source directory NAME this execution declares it was produced from, or ``None``
    when absent/invalid. Pure: reports no violation (``_execution_in_scope_src_dir`` and
    ``_validate_trial_meta`` already own that reporting, and a second report of one defect
    would double-count it).
    """
    return _plain_source_dir_name(_execution_raw_source_source_id(execution))


def _execution_in_scope_src_dir(
    execution: NodeExecution, violations: list[str]
) -> Path | None:
    """Resolve the single ``source/<source_source_id>/src`` directory that this
    execution's ``trial_meta.json`` declares it was produced from.

    Structural SOURCE checks (model/runner content, Makefile module deps) must be
    scoped to the source the in-scope run actually used — not every ``source/*/src``
    sibling under the pipeline. Scanning all siblings lets historically-broken
    append-only sources (left by earlier Generate attempts, unremovable under the
    append-only contract) permanently fail an otherwise-conformant run; mirroring
    the existing ``--run-id`` run-scoping fixes this.

    Returns ``None`` when ``source_source_id`` is absent/invalid (already reported
    by ``_validate_trial_meta`` as a hard requirement, so no double-report and no
    silent pass) — callers then skip the structural source scan entirely rather
    than falling back to a pipeline-wide sweep.
    """
    raw_source_id = _execution_raw_source_source_id(execution)
    if raw_source_id is None:
        return None
    source_source_id = _plain_source_dir_name(raw_source_id)
    if source_source_id is None:
        # Malformed (separator / traversal). Flag it rather than silently skipping so a
        # forged/malformed trial_meta is caught.
        trial_meta_path = execution.node_dir / "trial_meta.json"
        violations.append(
            f"{trial_meta_path}:source_source_id={raw_source_id!r} must be a "
            "plain source directory name (no path separators or traversal)"
        )
        return None
    src_dir = execution.pipeline_dir / "source" / source_source_id / "src"
    if not src_dir.is_dir():
        violations.append(f"{src_dir}: declared source_source_id directory missing")
        return None
    return src_dir


def _validate_generate_outputs(
    repo_root: Path, execution: NodeExecution, src_dir: Path, violations: list[str]
) -> None:
    model_files, expected_model_name = _model_files_in_src_dir(src_dir, execution)
    if not model_files:
        violations.append(
            _model_source_not_found_violation(src_dir, expected_model_name)
        )
        return

    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)

    for model_file in model_files:
        text = model_file.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()

        if re.search(r"index\s*\(\s*case_id", lowered) and re.search(
            r"metrics\s*\(\s*\d+\s*\)", lowered
        ):
            violations.append(
                f"{model_file}: hardcoded case_id -> metrics assignment pattern detected"
            )

        assignments = re.findall(
            r"metrics\s*\(\s*\d+\s*\)\s*=\s*([^\n!]+)",
            lowered,
            flags=re.MULTILINE,
        )
        literal_like = 0
        for rhs in assignments:
            if re.search(r"[-+]?\d+(?:\.\d+)?(?:d|e)?[-+]?\d*", rhs):
                literal_like += 1
        if len(assignments) >= 6 and literal_like >= 6:
            violations.append(
                f"{model_file}: many literal metric assignments detected ({literal_like}/{len(assignments)})"
            )

        _validate_problem_model_literal_outputs(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

        _validate_problem_model_dependency_dataflow(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            dep_spec_ids=dep_spec_ids,
            violations=violations,
        )
        _validate_problem_metric_only_scalar_kernel(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

    _validate_fortran_makefile_src_dir(src_dir, violations)
    _build_system, _language = _impl_toolchain_from_pipeline_dir(
        repo_root, execution.pipeline_dir
    )
    _validate_makefile_test_no_relink(
        src_dir, violations, build_system=_build_system, language=_language
    )
    _validate_makefile_test_invokes_cases(
        src_dir, violations, build_system=_build_system, language=_language
    )


# Fortran 2008 (and the earlier standards the generated code targets) limit a
# name (identifier) to 63 characters. Used by the module-name check below to fail an
# over-limit `<spec_id>_model` as a spec-level problem (the spec_id is too long) at
# post_generate. General over-limit identifiers in the generated source are caught by
# the real compiler front-end in the deterministic `generate.gate` substep
# (gfortran -fsyntax-only via MCP run_syntax_check), which replaced the retired
# post_generate text heuristics (identifier length / `implicit none` spec-list /
# non-constant STOP code) that could only mimic gfortran one observed failure at a time.
_FORTRAN_NAME_LIMIT = 63


# R1/M3c-β: the fixed public ABI of a physics node's `<spec_id>_checks.f90`
# (see docs/workflow/CHECKS_MODULE_CONTRACT.md). Imported, NOT restated: `runner_renderer` owns
# this set — it renders the runner that consumes it, and selects the per-node subset it imports
# FROM it. A second copy here would be a second authority for one fact, which is exactly how the
# Z2 bundle gate came to require the imported subset while this gate required all ten.
from tools.runner_renderer import CHECKS_PUBLIC_NAMES as _CHECKS_PUBLIC_NAMES  # noqa: E402


def _infra_direct_dep_node_keys(ir: dict[str, Any]) -> list[str]:
    """The ``infrastructure/...`` direct-dependency node_keys of an IR, accepting BOTH the
    dict form (``{node_key: ...}``) AND the bare-string form (``"infrastructure/..."``) —
    the same dual shape the conductor's ``_infra_direct_deps`` and the rest of the dep
    machinery (``_component_dep_spec_ids`` / ``_dep_node_key_tokens``) accept. Keeping the M3c
    predicate's parse identical across the conductor / Generate.static / Compile consumers is
    load-bearing: a shape one side counts and another drops would host-render the runner while
    the other side skips the checks gate (fail-open) — see docs/design/deterministic_followups."""
    dep = ir.get("dependency") if isinstance(ir.get("dependency"), dict) else {}
    out: list[str] = []
    for d in (dep.get("direct_deps") or []) if isinstance(dep, dict) else []:
        nk = d.get("node_key") if isinstance(d, dict) else (d if isinstance(d, str) else None)
        if isinstance(nk, str) and nk.strip() and nk.split("/", 1)[0].strip() == "infrastructure":
            out.append(nk.strip())
    return out


def _ir_is_m3c_physics(ir: dict[str, Any]) -> bool:
    """True iff the IR dict describes an M3c physics node: make+fortran, ``spec_kind`` !=
    ``infrastructure``, with exactly one ``infrastructure`` (runner-harness) direct
    dependency. On such a node the runner is host-rendered and the leaf authors
    ``<spec_id>_checks.f90`` — mirrors ``workflow_conductor._conductor_authors_runner``."""
    if not isinstance(ir, dict):
        return False
    meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
    if str(meta.get("spec_kind") or "").strip() == "infrastructure":
        return False
    impl = ir.get("impl_defaults") if isinstance(ir.get("impl_defaults"), dict) else {}
    tc = impl.get("toolchain") if isinstance(impl.get("toolchain"), dict) else {}
    if str(tc.get("build_system") or "make").lower() != "make":
        return False
    if str(tc.get("language") or "fortran").lower() != "fortran":
        return False
    return len(_infra_direct_dep_node_keys(ir)) == 1


def _execution_is_m3c(repo_root: Path, execution: NodeExecution) -> bool:
    """True iff the node is an M3c physics node (see ``_ir_is_m3c_physics``)."""
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return False
    ir_path = ir_dir / "spec.ir.yaml"
    if not ir_path.is_file():
        return False
    try:
        ir = _read_yaml(ir_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return False
    return _ir_is_m3c_physics(ir) if isinstance(ir, dict) else False


def _validate_checks_source_files(
    execution: NodeExecution, src_dir: Path, model_files: list[Path], violations: list[str]
) -> None:
    """R1/M3c-β deterministic gate: an M3c physics node's leaf-authored
    ``<spec_id>_checks.f90`` must satisfy the fixed-ABI contract
    (docs/workflow/CHECKS_MODULE_CONTRACT.md). Checks: the file exists; it declares
    ``module <spec_id>_checks``; it publishes all ten ABI names; NEITHER the checks NOR
    the model source ``use``s the harness (the physics sources never depend on it — the
    host-rendered runner is the sole harness caller); the checks module does no file I/O
    (``open(``); and it writes no forbidden judge-artifact filename. A violation routes
    back to Generate.generate to re-author the checks source."""
    spec_id = _spec_id_from_node_key(execution.node_key)
    if spec_id is None:
        return
    checks_path = src_dir / f"{spec_id}_checks.f90"
    if not checks_path.is_file():
        violations.append(
            f"{checks_path}: an M3c physics node must author {spec_id}_checks.f90 (the "
            "fixed-ABI checks module the host-rendered runner drives) — see "
            "docs/workflow/CHECKS_MODULE_CONTRACT.md")
        return
    text = checks_path.read_text(encoding="utf-8", errors="ignore")
    logical = _fortran_logical_lines(text)

    if not any(re.match(rf"(?i)^\s*module\s+{re.escape(spec_id)}_checks\b", ln)
               for ln in logical):
        violations.append(
            f"{checks_path}: must declare `module {spec_id}_checks` (the fixed ABI module)")

    published, _, _ = checks_module_abi_facts(text, spec_id)
    missing = [n for n in _CHECKS_PUBLIC_NAMES if n not in published]
    if missing:
        violations.append(
            f"{checks_path}: checks module must publish the fixed ABI names "
            f"{list(_CHECKS_PUBLIC_NAMES)}; missing {missing}")

    _validate_checks_source_harness_isolation(execution, src_dir, model_files, violations)


def _fortran_statements(text: str) -> list[str]:
    """Fortran source as one STATEMENT per entry: comments stripped, `&` continuations joined,
    and `;`-joined statements split apart.

    Every rule written against "a line" is really written against a statement, so this is what
    such a rule must iterate. `_fortran_logical_lines` alone does only the first two steps, and
    the omission is invisible until someone writes `use a; use b` — at which point an anchored
    `^\\s*use\\b` rule silently sees one statement and misses the other. Both halves of the M3c
    checks gate go through here so they cannot drift apart on that."""
    return [stmt for line in _fortran_logical_lines(text)
            for stmt in _split_fortran_statements(line)]


def checks_module_abi_facts(text: str, spec_id: str) -> tuple[set[str], set[str], set[str]]:
    """`(published, defined_subroutines, defined_procs)` for `module <spec_id>_checks` in `text`,
    lowercased.

    THE single parser for the checks-module ABI surface, shared by the deterministic
    `Generate.static` gate (`_validate_checks_source_files`, which reads the staged file) and the
    Z2 bundle acceptance gate (`codegen_bundle.m3c_checks_abi_violation`, which reads the
    producer's in-memory bundle before anything is written). They MUST agree: a second
    implementation is how the bundle gate came to accept output that `Generate.static` then
    rejected, reopening the phase — the drift this function exists to make impossible.

    `published` is what `use <spec_id>_checks, only:` can resolve: Fortran's module default is
    PUBLIC, so a name is published iff it is defined and not `private ::`'d, unless a bare
    `private` statement flips the default, in which case it must be `public ::`'d.

    `defined_procs` are the module-level procedure definitions written HERE, and
    `defined_subroutines` the subset of those spelled `subroutine`. Callers must read them as
    positive evidence only, never as "everything callable": a name can be published and callable
    without appearing in either — `use`-associated from another module, declared through an
    `interface` / generic block, or implemented in a submodule. So `n in defined_procs and n not
    in defined_subroutines` proves n is a FUNCTION here, while `n not in defined_procs` proves
    nothing at all (and rejecting on it fails a legal module).

    Iterates `_fortran_statements`, not raw lines: left unsplit, `public :: a; public :: b` read
    as a single `public` statement whose list was `a; public :: b`, losing `a` (whose token was
    `a;`) and inventing a name `public` — legal Fortran (gfortran rc=0) reported unpublished by
    BOTH gates."""
    logical = _fortran_statements(text)
    public_ids: set[str] = set()
    private_ids: set[str] = set()
    defined_procs: set[str] = set()
    defined_subroutines: set[str] = set()
    module_default_private = False  # a bare module-level `private` flips the default
    type_depth = 0  # a bare `private` inside a derived-type def is a component attr, not the module default
    in_interface = False  # a subroutine/function header inside an interface block is a proto, not a def
    proc_depth = 0  # nesting of procedure defs; only depth-0 (module-level) procs are published
    in_target_module = False  # only defs INSIDE `module <spec_id>_checks` are its published ABI
    target_module = f"{spec_id}_checks".lower()
    # A subroutine/function definition header. `function` may carry a type-spec prefix; the
    # `end <proc>` case is handled before this so the leading-token alternation can't match it.
    # The type-spec `[^!]*` is greedy, so it is matched against a STRING-MASKED copy of the line
    # (below): unmasked it ran from a declaration's type keyword into a string literal —
    # `character(len=*), parameter :: note = 'run subroutine case_setup first'` registered a
    # phantom definition and suppressed every later `public ::`. Masking rather than excluding
    # quotes fixes that WITHOUT rejecting a legal quote inside the type-spec itself
    # (`character(kind=kind('a')) function metric_compute()`), which the exclusion turned into a
    # published-but-undefined function the gate then accepted — a fail-open both gate authors
    # missed until a Codex review, since the runner `call`s it and Generate.syntax fails later.
    proc_start = re.compile(
        r"(?i)^\s*(?:(?:module|pure|impure|elemental|recursive|non_recursive)\s+)*"
        r"(?:(?:integer|real|double\s+precision|complex|logical|character|type|class)\b"
        r"[^!]*\s+)?"
        r"(subroutine|function)\s+([A-Za-z]\w*)")
    for ln in logical:
        s = ln.strip()
        # Enter/leave the TARGET checks module. A `module <name>` statement (not `module
        # subroutine/function/procedure`, which carry more tokens) opens a module; only
        # definitions/accessibility INSIDE `module <spec_id>_checks` are importable via
        # `use <spec_id>_checks` — procs after `end module` or in a second module are not.
        mo = re.match(r"(?i)^\s*module\s+([A-Za-z]\w*)\s*$", s)
        if mo:
            in_target_module = mo.group(1).lower() == target_module
            proc_depth, type_depth, in_interface = 0, 0, False
            continue
        if re.match(r"(?i)^\s*end\s*module\b", s):
            in_target_module = False
            proc_depth, type_depth, in_interface = 0, 0, False
            continue
        # An `interface` / `abstract interface` block declares procedure PROTOTYPES, not
        # definitions — its subroutine/function headers must not count as `defined_procs`
        # (else a module could publish an ABI name it only prototypes but never defines).
        if not in_interface and re.match(r"(?i)^\s*(?:abstract\s+)?interface\b", s):
            in_interface = True
            continue
        if in_interface:
            if re.match(r"(?i)^\s*end\s*interface\b", s):
                in_interface = False
            continue
        # Track derived-type nesting so a component-level bare `private` isn't mistaken
        # for the module default. `type :: name` / `type name` / `type, attrs :: name`
        # open a def; `type(...)` decls and `type is (...)` guards do not.
        if type_depth == 0 and re.match(r"(?i)^\s*type\b", s) \
                and not re.match(r"(?i)^\s*type\s*\(", s) \
                and not re.match(r"(?i)^\s*type\s+is\b", s):
            type_depth = 1
            continue
        if type_depth > 0:
            if re.match(r"(?i)^\s*end\s*type\b", s):
                type_depth = 0
            continue
        # `end subroutine/function/procedure` closes a proc scope. A bare `end` closes the
        # innermost program unit: a procedure when we are inside one, else the module itself.
        # (`end module` is handled above; construct ends `end do`/`end if`/… fall through.)
        if re.match(r"(?i)^\s*end\s*(?:subroutine|function|procedure)\b", s):
            if proc_depth > 0:
                proc_depth -= 1
            continue
        if re.match(r"(?i)^\s*end\s*$", s):
            if proc_depth > 0:
                proc_depth -= 1
            else:
                in_target_module = False  # bare `end` at unit level closes the (target) module
            continue
        # A subroutine/function definition. Only a MODULE-LEVEL (depth-0) definition INSIDE the
        # target checks module is a published entity the runner can `use ... only:`; a nested
        # internal procedure, or one in another module / after `end module`, is not. Matched on a
        # string-masked copy so the greedy type-spec neither crosses INTO a string literal nor is
        # blocked by a legal quote WITHIN the type-spec.
        pm2 = proc_start.match(_mask_fortran_string_contents(s))
        if pm2:
            if in_target_module and proc_depth == 0:
                defined_procs.add(pm2.group(2).lower())
                if pm2.group(1).lower() == "subroutine":
                    defined_subroutines.add(pm2.group(2).lower())
            proc_depth += 1
            continue
        # `public` / `private` statements only publish/hide the target module's own entities,
        # and only in its specification part (depth 0, not inside a procedure body).
        if proc_depth > 0 or not in_target_module:
            continue
        m = re.match(r"(?i)^\s*public\b\s*(::)?\s*(.*)$", s)
        if m:
            for tok in re.split(r"[,\s]+", m.group(2).strip()):
                if re.fullmatch(r"[A-Za-z]\w*", tok):
                    public_ids.add(tok.lower())
            continue
        pm = re.match(r"(?i)^\s*private\b\s*(::)?\s*(.*)$", s)
        if pm:
            body = pm.group(2).strip()
            if not body:  # a bare module-level `private` makes the default accessibility private
                module_default_private = True
            else:
                for tok in re.split(r"[,\s]+", body):
                    if re.fullmatch(r"[A-Za-z]\w*", tok):
                        private_ids.add(tok.lower())
            continue
    # Fortran module default accessibility is PUBLIC unless a bare `private` statement flips
    # it. Under the (default) public module, a name is published iff it is DEFINED and not
    # explicitly `private ::`'d; under a bare-`private` module, iff it is `public ::`'d.
    if module_default_private:
        published = public_ids - private_ids
    else:
        published = (public_ids | defined_procs) - private_ids
    return published, defined_subroutines, defined_procs


def _validate_checks_source_harness_isolation(
    execution: NodeExecution, src_dir: Path, model_files: list[Path], violations: list[str]
) -> None:
    """The rest of the M3c checks-source gate (see `_validate_checks_source_files`)."""
    spec_id = _spec_id_from_node_key(execution.node_key)
    if spec_id is None:
        return
    checks_path = src_dir / f"{spec_id}_checks.f90"
    if not checks_path.is_file():
        return
    text = checks_path.read_text(encoding="utf-8", errors="ignore")
    logical = _fortran_logical_lines(text)

    # Neither the checks nor the model source may `use` the harness module. Tolerate the
    # optional `, <attr>` (e.g. `, intrinsic`) and `::` forms — `use harness_x`,
    # `use :: harness_x`, and `use, non_intrinsic :: harness_x` must all be caught. The scan is
    # per STATEMENT, not per line: the regex is anchored, so a harness `use` written as the second
    # statement of a `;`-joined line (`use, intrinsic :: iso_fortran_env, only: dp => real64;
    # use harness_fortran_cpu_model, only: ...`, legal and rc=0) would otherwise be invisible —
    # a fail-OPEN on the isolation invariant that nothing downstream catches, since the harness is
    # staged for `Generate.syntax` and the bundle contract has no isolation layer.
    use_harness_re = re.compile(r"(?i)^\s*use\b\s*(?:,\s*\w+\s*)?(?:::\s*)?harness_")
    for f in [checks_path, *model_files]:
        ftext = f.read_text(encoding="utf-8", errors="ignore")
        if any(use_harness_re.match(stmt.strip())
               for stmt in _fortran_statements(ftext)):
            violations.append(
                f"{f}: a physics source must not `use` the harness module — the physics "
                "node never depends on the harness at the source level (the host-rendered "
                "runner is the sole `use harness_*` site)")

    # The checks module does no file I/O (emission is the harness/runner's exclusive job). Scan
    # string-masked statements: an `open(` call is code, so a quoted `'open('` in a message string
    # is not one — matching it would fail-close a legal module. (The forbidden-filename scan below
    # is deliberately the opposite: it inspects string CONTENT, so it runs on the raw text.)
    if any(re.search(r"(?i)\bopen\s*\(", _mask_fortran_string_contents(ln)) for ln in logical):
        violations.append(
            f"{checks_path}: checks module must not do file I/O (`open(`) — emission is the "
            "harness's job; the checks module only computes state/checks/metrics")

    lowered = text.lower()
    for output_name in FORBIDDEN_RUNNER_OUTPUTS:
        if output_name in lowered:
            violations.append(
                f"{checks_path}: forbidden judge-artifact filename detected ({output_name})")


def _validate_generate_outputs_for_generation(
    repo_root: Path,
    execution: NodeExecution,
    source_id: str,
    violations: list[str],
) -> None:
    gen_dir = execution.pipeline_dir / "source" / source_id
    if not gen_dir.is_dir():
        violations.append(
            f"{gen_dir}: missing generate directory for source_id={source_id!r}"
        )
        return
    src_dir = gen_dir / "src"
    if not src_dir.is_dir():
        violations.append(f"{src_dir}: missing src directory")
        return

    model_files, expected_model_name = _model_files_in_src_dir(src_dir, execution)
    if not model_files:
        violations.append(
            _model_source_not_found_violation(src_dir, expected_model_name)
        )
        return

    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)

    for model_file in model_files:
        text = model_file.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()

        if re.search(r"index\s*\(\s*case_id", lowered) and re.search(
            r"metrics\s*\(\s*\d+\s*\)", lowered
        ):
            violations.append(
                f"{model_file}: hardcoded case_id -> metrics assignment pattern detected"
            )

        assignments = re.findall(
            r"metrics\s*\(\s*\d+\s*\)\s*=\s*([^\n!]+)",
            lowered,
            flags=re.MULTILINE,
        )
        literal_like = 0
        for rhs in assignments:
            if re.search(r"[-+]?\d+(?:\.\d+)?(?:d|e)?[-+]?\d*", rhs):
                literal_like += 1
        if len(assignments) >= 6 and literal_like >= 6:
            violations.append(
                f"{model_file}: many literal metric assignments detected ({literal_like}/{len(assignments)})"
            )

        _validate_problem_model_literal_outputs(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

        _validate_problem_model_dependency_dataflow(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            dep_spec_ids=dep_spec_ids,
            violations=violations,
        )
        _validate_problem_metric_only_scalar_kernel(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

    _validate_fortran_makefile_src_dir(src_dir, violations)
    _build_system, _language = _impl_toolchain_from_pipeline_dir(
        repo_root, execution.pipeline_dir
    )
    _validate_makefile_test_no_relink(
        src_dir, violations, build_system=_build_system, language=_language
    )
    _validate_makefile_test_invokes_cases(
        src_dir, violations, build_system=_build_system, language=_language
    )
    # The cheap deterministic runner backstops (name / forbidden-output / json-serialization
    # / snapshot-filename) run against every runner — leaf-authored (infra self-test / legacy)
    # or host-rendered (M3c). (The two LLM-fabrication heuristics — constant-heavy diagnostics
    # and non-physical case_path input — were removed in M3d: they were unreliable
    # `problem/`-scoped guesses with a known false-positive history, and once the physics
    # nodes host-render there is little leaf-authored physics-runner surface left for them to
    # police. NOTE they are NOT a full no-op: the legacy leaf-authored-runner path stays live —
    # the `infrastructure` harness self-test, any non-`(fortran, make)` node, and any node
    # without an infra dep (the catalog currently holds no such physics node, since the
    # advection_diffusion family opted into the harness, but nothing pins that). Removing the
    # heuristics accepts that fabrication in those runners is caught by the LLM verify/judge +
    # these deterministic backstops, not by the two deleted heuristics.)
    runner_files = sorted(src_dir.glob("*_runner.f90"))
    _validate_runner_source_files(
        execution, runner_files, violations,
        known_case_ids=_case_ids_for_execution(repo_root, execution),
    )
    # R1/M3c-β: the leaf-authored checks module (fixed ABI) is gated only on an M3c node.
    is_m3c = _execution_is_m3c(repo_root, execution)
    if is_m3c:
        _validate_checks_source_files(execution, src_dir, model_files, violations)

    if dep_spec_ids:
        _validate_dependency_operation_on_model_files(
            model_files, dep_spec_ids, violations
        )

    # R1/M3c-α: an infrastructure node's generated model must publish every §5.1 canonical
    # signature verbatim (no-op for physics nodes, whose interface is derived post-hoc).
    _validate_infrastructure_generated_signatures(
        repo_root, execution, model_files, violations
    )
    # L1b: a component node's generated model must publish EXACTLY its IR public_api op NAMES
    # (inert on a legacy IR that carries no public_api pin; no-op for non-component nodes).
    _validate_component_generated_surface(
        repo_root, execution, model_files, violations
    )


# `subroutine` declaration opener mirroring orchestration_runtime._FORTRAN_SUBROUTINE_RE (the
# published-surface scanner the resolver uses): optional pure/impure/elemental/recursive/module
# prefixes, then `subroutine <name>`. `^\s*` anchors at the (comment-stripped, continuation-
# joined) logical-line start, so `end subroutine` / `call` lines never match. Kept in lock-step
# with the runtime regex by the cross-scanner parity test (ComponentGeneratedSurfaceTests).
_COMPONENT_PUBLISHED_SUB_RE = re.compile(
    r"^\s*(?:(?:pure|impure|elemental|recursive|module)\s+)*"
    r"subroutine\s+(?P<name>[A-Za-z]\w*)",
    re.IGNORECASE,
)


def _list_component_published_subroutines(text: str, spec_id: str) -> list[str]:
    """Distinct, first-appearance-ordered ``subroutine`` names in ``text`` whose name begins
    (case-insensitive) with ``<spec_id>__`` — the component's published operation surface. The
    validator may NOT import ``orchestration_runtime`` (module-boundary rule), so this is a
    self-contained mirror of that module's ``_list_prefixed_subroutines``; the cross-scanner
    parity test pins the two implementations to the same result over the domain a code generator
    emits — declarations on one logical line and continuations broken at a TOKEN boundary (the
    ``subroutine __op( &`` argument-list wrap). The two scanners' shared-helper continuation joins
    differ in whitespace (``_iter_fortran_logical_lines`` here joins with no separator,
    ``orchestration_runtime._fortran_logical_lines`` with a space), so a pathological split
    THROUGH the ``subroutine`` keyword or through the name identifier (``pure&``/``dep__fo&\\no``)
    resolves differently — but no generator emits that, so it is out of the pinned domain. Uses
    this file's ``_iter_fortran_logical_lines`` (comment-strip + continuation-join). NEVER raises."""
    try:
        prefix = f"{spec_id}__".lower()
        out: list[str] = []
        seen: set[str] = set()
        for _lineno, stmt in _iter_fortran_logical_lines(text):
            m = _COMPONENT_PUBLISHED_SUB_RE.match(stmt)
            if m is None:
                continue
            name = m.group("name")
            if not name.lower().startswith(prefix):
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out
    except Exception:
        return []


def _validate_component_generated_surface(
    repo_root: Path,
    execution: NodeExecution,
    model_files: list[Path],
    violations: list[str],
) -> None:
    """L1b deterministic generated-source gate (``component`` nodes with a pinned public_api):
    the generated model source must publish EXACTLY the ``<spec_id>__`` public subroutine set the
    IR's ``public_api.published_operations`` names — no more, no less.

    This closes the generation half of the L1 name pin. Compile pins the published op NAMES into
    the component IR (``_validate_component_public_api``); this gate proves the generated ``.f90``
    realizes exactly those names, so a component's published surface cannot drift between the IR a
    consumer's Compile is shown (via the sidecar) and the source Build links. A mismatch routes
    back to ``Generate.generate``.

    Inert (skip) when the IR carries NO ``public_api`` (a legacy pre-L1 certified IR — nothing to
    pin against; L4 guards that resume path). No-op for a non-component node, an unresolvable IR,
    or a missing model (already flagged upstream)."""
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return
    ir_path = ir_dir / "spec.ir.yaml"
    if not ir_path.is_file():
        return
    try:
        ir = _read_yaml(ir_path)
    except yaml.YAMLError:
        return
    if not isinstance(ir, dict):
        return
    meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
    if meta.get("spec_kind") != "component":
        return
    public_api = ir.get("public_api")
    if not isinstance(public_api, dict) or "published_operations" not in public_api:
        return  # legacy IR without the L1 pin — inert (L4 guards the resume path)

    ops_raw = public_api.get("published_operations")
    published = {
        entry["operation_id"].strip()
        for entry in (ops_raw if isinstance(ops_raw, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("operation_id"), str)
        and entry["operation_id"].strip()
    }
    spec_id = _spec_id_from_node_key(execution.node_key)
    if not spec_id or not model_files:
        return  # spec_id/model resolution already handled upstream

    combined = "\n".join(
        f.read_text(encoding="utf-8", errors="ignore") for f in model_files)
    generated = _list_component_published_subroutines(combined, spec_id)
    gen_cf = {g.casefold() for g in generated}
    pub_cf = {p.casefold() for p in published}
    target = model_files[0]
    for missing in sorted(p for p in published if p.casefold() not in gen_cf):
        violations.append(
            f"{target}: generated model source does not publish component public_api operation "
            f"'{missing}' — declare `subroutine {missing}(...)` (the IR public_api pins it as a "
            "published operation)")
    for extra in sorted(g for g in generated if g.casefold() not in pub_cf):
        violations.append(
            f"{target}: generated model source publishes `{spec_id}__` subroutine '{extra}' that "
            "is NOT in the IR public_api.published_operations — a component's published surface "
            "must match its IR public_api exactly (rename an internal helper without the "
            f"`{spec_id}__` prefix, or add it to the published operations)")


def _read_dependency_graph_sidecar(repo_root: Path, ir_ref: str | None) -> dict[str, Any] | None:
    """Load the conductor-authored dependency-graph sidecar ``<ir_ref>/dependency_graph.json``.

    The derived dependency graph — ``all_nodes`` (each with ``topo_level``) and
    ``transitive_deps`` (each with ``via``) — is host-authored here by
    ``workflow_conductor._write_dependency_graph`` at Compile phase start; it no
    longer lives in ``spec.ir.yaml.dependency`` (which keeps only the LLM-authored
    ``node_key`` + ``direct_deps``). Returns the parsed dict, or ``None`` when
    ``ir_ref`` is unusable, the sidecar is absent, or it is malformed. Callers that
    read ``all_nodes`` / ``transitive_deps`` merge this over the IR dependency block."""
    if not isinstance(ir_ref, str) or not ir_ref.startswith("workspace/"):
        return None
    path = repo_root / ir_ref / "dependency_graph.json"
    if not path.is_file():
        return None
    try:
        data = _read_json(path)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _dependency_doc_path(repo_root: Path, dep_path: Path, ir_ref: str | None) -> Path | None:
    """Resolve the dependency document from a dependency_ref.

    Compile contract: dependency_ref is a spec/.../deps.yaml *file*.
    Generate+ contract (ORCHESTRATION.md:151): dependency_ref is the IR/pipeline
    phase-root *directory*; the dependency block lives in <ir>/spec.ir.yaml.
    Legacy fixtures also pointed dependency_ref straight at spec.ir.yaml (a file).
    """
    if dep_path.is_file():
        return dep_path
    if dep_path.is_dir():
        candidate = dep_path / "spec.ir.yaml"
        if candidate.is_file():
            return candidate
        # pipeline_ref phase-root has no spec.ir.yaml; fall back to the IR dir.
        if isinstance(ir_ref, str) and ir_ref.startswith("workspace/"):
            ir_candidate = repo_root / ir_ref / "spec.ir.yaml"
            if ir_candidate.is_file():
                return ir_candidate
    return None


def _dependency_resolved_for_execution(repo_root: Path, execution: NodeExecution) -> dict[str, Any] | None:
    lineage_path = execution.pipeline_dir / "lineage.json"
    if not lineage_path.exists():
        return None

    lineage = _read_json(lineage_path)
    dependency_ref = lineage.get("dependency_ref")
    if not isinstance(dependency_ref, str) or not dependency_ref.startswith("workspace/"):
        return None

    dep_path = repo_root / dependency_ref
    if not dep_path.exists():
        return None
    dep_doc_path = _dependency_doc_path(repo_root, dep_path, lineage.get("ir_ref"))
    if dep_doc_path is None:
        return None
    # spec.ir.yaml is YAML; legacy dependency.resolved.yaml was also YAML.
    # Older fixtures sometimes wrote JSON, so fall back to the JSON loader.
    # OSError guards against a stray directory ever crashing the validator.
    try:
        dep_data = _read_yaml(dep_doc_path)
    except (json.JSONDecodeError, yaml.YAMLError, OSError):
        try:
            dep_data = _read_json(dep_doc_path)
        except (json.JSONDecodeError, OSError):
            return None
    if not isinstance(dep_data, dict):
        return None
    # New IR nests the dependency block under spec.ir.yaml.dependency. Unwrap
    # it so callers continue to see the legacy flat keys.
    base = dep_data
    if isinstance(dep_data.get("dependency"), dict):
        nested = dep_data["dependency"]
        if "direct_deps" in nested or "transitive_deps" in nested or "node_key" in nested:
            base = nested
    # Merge the conductor-authored derived graph: all_nodes / transitive_deps now
    # live in <ir_ref>/dependency_graph.json (host-authored), NOT the IR. The IR
    # supplies node_key + direct_deps; the sidecar supplies the derived closure the
    # DAG-completeness check (_dependency_expected_node_keys) reads. When no sidecar
    # is present the base block is returned unchanged (defensive).
    graph = _read_dependency_graph_sidecar(repo_root, lineage.get("ir_ref"))
    if isinstance(graph, dict):
        merged = dict(base)
        for key in ("all_nodes", "transitive_deps"):
            if isinstance(graph.get(key), list):
                merged[key] = graph[key]
        return merged
    return base


def _ir_dir_from_pipeline_dir(repo_root: Path, pipeline_dir: Path) -> Path | None:
    lineage_path = pipeline_dir / "lineage.json"
    if not lineage_path.exists():
        return None

    try:
        lineage = _read_json(lineage_path)
    except json.JSONDecodeError:
        # A malformed lineage.json is reported as a violation by the dedicated
        # lineage validators; here we only resolve the IR dir best-effort and
        # must not crash the stage.
        return None
    if not isinstance(lineage, dict):
        return None
    ir_ref = lineage.get("ir_ref")
    if not isinstance(ir_ref, str) or not ir_ref.startswith("workspace/"):
        return None

    ir_dir = repo_root / ir_ref
    if not ir_dir.exists() or not ir_dir.is_dir():
        return None
    return ir_dir


def _ir_dir_for_execution(repo_root: Path, execution: NodeExecution) -> Path | None:
    return _ir_dir_from_pipeline_dir(repo_root, execution.pipeline_dir)


def _impl_toolchain_from_pipeline_dir(
    repo_root: Path, pipeline_dir: Path
) -> tuple[str | None, str | None]:
    """Resolve ``(build_system, language)`` from the pipeline's
    `spec.ir.yaml#impl_defaults.toolchain`, lowercased; either may be None when
    unresolvable. Used to gate make-only checks to the documented toolchain
    scope."""
    ir_dir = _ir_dir_from_pipeline_dir(repo_root, pipeline_dir)
    if ir_dir is None:
        return (None, None)
    contract_path = ir_dir / "spec.ir.yaml"
    if not contract_path.exists():
        return (None, None)
    try:
        data = _read_yaml(contract_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return (None, None)
    if not isinstance(data, dict):
        return (None, None)
    impl_defaults = data.get("impl_defaults")
    toolchain = (
        impl_defaults.get("toolchain")
        if isinstance(impl_defaults, dict)
        else data.get("toolchain")
    )
    if not isinstance(toolchain, dict):
        return (None, None)

    def _norm(value: Any) -> str | None:
        return value.strip().lower() if isinstance(value, str) and value.strip() else None

    return (_norm(toolchain.get("build_system")), _norm(toolchain.get("language")))
def _io_contract_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return None

    contract_path = ir_dir / "spec.ir.yaml"
    if not contract_path.exists():
        return None

    try:
        data = _read_yaml(contract_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        try:
            data = _read_json(contract_path)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    # Unwrap the io_contract section if the doc uses the new IR nesting:
    # spec.ir.yaml.io_contract holds inputs / outputs / raw_requirements /
    # test_evidence_requirements / semantic_dependency as siblings.  Tests and
    # callers that expect derived_contract.json's flat layout will see those
    # siblings at the top level after this transform.
    if (
        isinstance(data.get("io_contract"), dict)
        and (
            "inputs" in data["io_contract"]
            or "outputs" in data["io_contract"]
            or "raw_requirements" in data["io_contract"]
            or "test_evidence_requirements" in data["io_contract"]
        )
        and "raw_requirements" not in data
    ):
        section = data["io_contract"]
        flattened: dict[str, Any] = {
            "io_contract": {
                "inputs": section.get("inputs"),
                "outputs": section.get("outputs"),
            },
        }
        for key in (
            "raw_requirements",
            "semantic_dependency",
            "test_evidence_requirements",
            # `test_predicates[].target_cases` is the only case -> test mapping every IR
            # carries; the post_execute snapshot check scopes each per-case snapshot's
            # required raw variables through it (`_case_id_to_test_ids`).
            "test_predicates",
            "diagnostics_contract",
        ):
            if key in section:
                flattened[key] = section[key]
        return flattened
    return data


def _read_yaml(path: Path) -> Any:
    # Decode STRICTLY, and translate the two errors that would otherwise escape into the one the
    # callers already handle (`yaml.YAMLError` -> "invalid json" / a contract violation):
    #   - UnicodeDecodeError is not a YAMLError, so it reached the leaf as a traceback in place of
    #     its repair findings. Decoding leniently instead (errors="ignore") would be worse: it
    #     DELETES the offending bytes, so an IR whose invalid byte sits in a comment sanitizes into
    #     a clean document and CERTIFIES. An artifact that is not valid UTF-8 must fail, not be
    #     silently rewritten.
    #   - RecursionError (a pathologically nested document) is not a YAMLError either.
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise yaml.YAMLError(f"{path}: not valid UTF-8 ({exc})") from exc
    try:
        return yaml.safe_load(text)
    except RecursionError as exc:
        raise yaml.YAMLError(f"{path}: structure too deeply nested to parse") from exc


# A section of the IR document (`algorithm:`, `io_contract:`, ...) and the document itself are both
# plain dicts, so handing the whole document to a helper that looks its keys up at section level is
# invisible at the call site: the lookup misses, the helper returns None, and everything below it is
# dead. Three gates were silently dormant for exactly this reason (`_plan_dependency_node_key`,
# `_validate_problem_state_array_usage`, and the tests.md path resolution that fed
# `_validate_test_evidence_requirements` / `_validate_tests_verdict_summary_consistency`). The guard
# below discriminates the two shapes on keys that occur at document level only: `schema_version`, and
# the section's own name (no section of any of the 116 certified IRs contains either).
_IR_DOCUMENT_ONLY_KEYS = ("schema_version",)


def _require_ir_section(contract: dict[str, Any], section: str) -> dict[str, Any]:
    """Raise if the whole IR document is passed where the named section is expected."""
    markers = [key for key in (section, *_IR_DOCUMENT_ONLY_KEYS) if key in contract]
    if markers:
        raise ValueError(
            f"IR document passed where the `{section}` section was expected "
            f"(document-level keys present: {markers}); unwrap it with _ir_section() first"
        )
    return contract


def _ir_document_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    """The whole `spec.ir.yaml` document. Use _ir_section() to reach a section."""
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return None

    contract_path = ir_dir / "spec.ir.yaml"
    if not contract_path.exists():
        return None

    try:
        data = _read_yaml(contract_path)
    except (yaml.YAMLError, OSError):
        return None  # the contract-file gate reports a malformed IR at the same stage
    return data if isinstance(data, dict) else None


def _ir_section(document: dict[str, Any], section: str) -> dict[str, Any] | None:
    """The named top-level section of an IR document, or None when it is absent."""
    value = document.get(section)
    return value if isinstance(value, dict) else None


def _is_readable_file(path: Path) -> bool:
    """Total existence probe for an LLM-AUTHORED path.

    `Path.is_file()` is not total: it swallows only ENOENT / ENOTDIR / EBADF / ELOOP, so a ref that
    names an over-long path (ENAMETOOLONG) or one under an unsearchable directory (EACCES) RAISES
    out of the probe itself. Every ref in `meta.source_refs` is authored by the compile leaf, and
    the conductor turns a non-zero exit of this validator into the leaf's repair findings — so an
    escaping OSError would reach it as a traceback naming no IR field. Any such path is simply "not
    a readable file", which the callers already report as an unresolvable ref.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _tests_path_from_ir_document(repo_root: Path, document: dict[str, Any]) -> Path | None:
    """Resolve tests.md from `meta.source_refs.tests` — the only place a real IR carries it."""
    meta = _ir_section(document, "meta")
    if meta is None:
        return None
    source_refs = meta.get("source_refs")
    if not isinstance(source_refs, dict):
        return None

    tests_ref = source_refs.get("tests")
    if not isinstance(tests_ref, str) or not tests_ref.strip():
        return None

    tests_path = Path(tests_ref.strip())
    if not tests_path.is_absolute():
        tests_path = repo_root / tests_path
    return tests_path


def _ir_document_path_for_execution(
    repo_root: Path, execution: NodeExecution
) -> Path | None:
    """Path of the whole `spec.ir.yaml`. There is no section-scoped path: a name that promises one
    is what invited the document/section confusion this module now guards against."""
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return None
    return ir_dir / "spec.ir.yaml"


def _impl_contract_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return None

    contract_path = ir_dir / "spec.ir.yaml"
    if not contract_path.exists():
        return None

    try:
        data = _read_yaml(contract_path)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    # spec.ir.yaml nests impl-defaults under `impl_defaults:` (was a flat
    # impl.resolved.yaml in the legacy layout).
    impl_section = data.get("impl_defaults")
    if isinstance(impl_section, dict):
        return impl_section
    return data


def _resolve_logged_path(repo_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return repo_root / path


def _quality_check_preset_from_command(command: list[str]) -> str | None:
    normalized = [str(token).strip().lower() for token in command if str(token).strip()]
    if not normalized:
        return None
    executable = Path(normalized[0]).name.lower()
    if executable == "make":
        targets = set(normalized[1:])
        if "test" in targets:
            return "make_test"
        if "check" in targets:
            return "make_check"
        return None
    if executable == "ctest":
        return "ctest"
    if executable == "pytest":
        return "pytest"
    return None


def _generate_src_dirs(pipeline_dir: Path) -> list[Path]:
    generate_root = pipeline_dir / "source"
    if not generate_root.exists():
        return []
    return sorted(
        gen_dir / "src"
        for gen_dir in generate_root.iterdir()
        if gen_dir.is_dir() and (gen_dir / "src").exists()
    )


def _path_is_same_or_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _make_targets(makefile_path: Path) -> set[str]:
    targets: set[str] = set()
    for raw_line in makefile_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or raw_line.startswith("\t"):
            continue
        if ":" not in raw_line:
            continue
        head, _ = raw_line.split(":", 1)
        if "=" in head:
            continue
        for token in head.split():
            token_l = token.strip().lower()
            if token_l:
                targets.add(token_l)
    return targets


def _algorithm_state_contract(contract: dict[str, Any]) -> dict[str, Any] | None:
    """The state contract, read from the `algorithm:` SECTION (never the whole document)."""
    _require_ir_section(contract, "algorithm")

    state_contract = contract.get("state_contract")
    if isinstance(state_contract, dict):
        return state_contract

    update_semantics = contract.get("update_semantics")
    if isinstance(update_semantics, dict) and any(
        key in update_semantics
        for key in (
            "state_variables",
            "required_update_paths",
            "diagnostics_from_state",
            "fallback_policy",
        )
    ):
        return update_semantics

    state_variables = contract.get("state_variables")
    required_update_paths = contract.get("required_update_paths")
    diagnostics_from_state = contract.get("diagnostics_from_state")
    fallback_policy = contract.get("fallback_policy")
    if any(
        value is not None
        for value in (
            state_variables,
            required_update_paths,
            diagnostics_from_state,
            fallback_policy,
        )
    ):
        return {
            "state_variables": state_variables,
            "required_update_paths": required_update_paths,
            "diagnostics_from_state": diagnostics_from_state,
            "fallback_policy": fallback_policy,
        }
    return None


def _tests_path_for_execution(repo_root: Path, execution: NodeExecution) -> Path | None:
    document = _ir_document_for_execution(repo_root, execution)
    if not isinstance(document, dict):
        return None
    return _tests_path_from_ir_document(repo_root, document)


def _parse_test_ids_from_tests_md(tests_path: Path) -> list[str]:
    test_ids: list[str] = []
    seen: set[str] = set()
    try:
        text = tests_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        # The callers probe with `_is_readable_file` first, so this covers what a probe cannot: a
        # file readable at stat time but not at read time (EACCES on the file itself, a vanished
        # file). An empty id list is what the callers already treat as "no canonical set".
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = TEST_ID_HEADING_PATTERN.match(line) or TEST_ID_BULLET_PATTERN.match(line)
        if not match:
            continue
        test_id = match.group(1).strip()
        if not test_id or test_id in seen:
            continue
        seen.add(test_id)
        test_ids.append(test_id)
    return test_ids


# Matches a top-level numbered controlled_spec heading `## <n>. Title`. The `(?:\s|$)`
# after the dot ensures a decimal subsection like `## 5.1 Foo` is NOT read as section 5.
_CONTROLLED_SPEC_SECTION_HEADING = re.compile(r"^##\s+(\d+)\.(?:\s|$)")


def _extract_controlled_spec_section(text: str, section_num: str) -> str | None:
    """Return the body of a ``## <n>.`` controlled_spec section (the lines between that
    heading and the next ``## <m>.`` heading), or ``None`` when the section is absent.

    controlled_spec sections are numbered ``## 0.`` .. ``## 8.``; only these top-level
    numbered headings delimit a section (a ``### `` subsection does not)."""
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        match = _CONTROLLED_SPEC_SECTION_HEADING.match(line.strip())
        if match and match.group(1) == section_num:
            start = i
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if _CONTROLLED_SPEC_SECTION_HEADING.match(line.strip()):
            break
        body.append(line)
    return "\n".join(body)


def _parse_public_api_from_controlled_spec(
    controlled_spec_path: Path, spec_id: str
) -> tuple[set[str], set[str]]:
    """Extract the exact published ``operation_id`` set and derived-type set an
    ``infrastructure`` node declares in its controlled_spec §5 ('Public API and
    compatibility' — the authoritative "the published operation_ids are exactly: ..."
    contract). An infrastructure node's whole purpose is to publish a reusable surface
    that consuming physics nodes link against, so this parser feeds the deterministic
    ``--stage compile`` gate that pins the IR's ``public_api`` to §5.

    Returns ``(operation_ids, type_ids)``. Every ``<spec_id>__X`` identifier appearing inside
    a backtick span in §5 is a published symbol; it is classified as a TYPE (not an operation)
    when its span is a ``type(<spec_id>__X)`` reference, or the lead-in words that introduce it
    (since the last sentence break / previous backtick) contain the phrase "derived type" —
    so "derived type `<id>__X`", plural/hyphenated "derived types" / "derived-type", and
    ``type(<id>__X)`` all resolve to a type, and everything else to an operation. The phrase
    "derived type" (not bare "type") is required precisely so an op whose lead-in merely
    contains the substring "type" — "prototype", "typedef", "return type", a "type-generic"
    parenthetical leaking from the previous op — is NOT misread as a type (a false Compile
    rejection the LLM could not repair, since the drift would be in the spec prose). Matching
    the identifier ANYWHERE in the span (not the whole token) means an op written with its
    signature — ```<id>__op(args)``` — is still captured, so a §5 authored in the §3 signature
    style does not silently drop ops (which would be a false-accept: the gate would miss the
    very drift it exists to catch). Short forms (```__box```) and unprefixed prose
    (```h_named```, ```values(:)```) never match the ``<spec_id>__`` anchor. A "derived type(s)"
    phrase carries across a comma-separated backtick run — "the derived types `A`, `B`"
    classifies BOTH as types — but only over pure list separators (``,`` / ``and`` / ``or`` /
    ``/`` / whitespace), never across intervening prose, so a following op in the same sentence
    is not swept in. A published type introduced WITHOUT "derived type" / ``type(...)`` degrades
    to op-classified — a fail-closed set mismatch (a visible Compile fail), never a silent
    accept."""
    try:
        text = controlled_spec_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        # Readable at stat time, not at read time. The caller reports an empty §5 parse as a
        # fail-closed violation, which is the finding the leaf needs; a raise here would not be.
        return (set(), set())
    section = _extract_controlled_spec_section(text, "5")
    if section is None:
        return (set(), set())
    # §5.1's canonical interface block (a fenced code block) lives inside §5's body; its
    # ``<spec_id>__X`` identifiers are NOT backtick-wrapped prose and must not be mined as
    # published-surface tokens (the fence is parsed separately by
    # _parse_canonical_interface_from_controlled_spec). Strip fenced blocks first.
    section = _strip_fenced_blocks(section)
    ident_re = re.compile(re.escape(spec_id) + r"__[A-Za-z0-9_]+")
    separator_re = re.compile(r"[\s,]*(?:and|or|/)?[\s,]*", re.IGNORECASE)
    all_tokens: set[str] = set()
    type_tokens: set[str] = set()
    prev_end = 0
    prev_is_type = False
    for match in re.finditer(r"`([^`]*)`", section):
        span = match.group(1)
        idents = set(ident_re.findall(span))
        between = section[prev_end : match.start()]
        prev_end = match.end()
        if not idents:
            prev_is_type = False  # a non-identifier span breaks any list run
            continue
        all_tokens |= idents
        lead_in = re.split(r"[.`]", section[: match.start()])[-1].lower()
        is_type = bool(
            re.search(r"type\s*\(", span.lower())
            or re.search(r"derived[\s-]types?\b", lead_in)
            # a derived-type run continues over a pure list separator (no intervening prose)
            or (prev_is_type and separator_re.fullmatch(between))
        )
        if is_type:
            type_tokens |= idents
        prev_is_type = is_type
    return (all_tokens - type_tokens, type_tokens)


# --- R1/M3c-α: canonical interface block (§5.1) parsing + Fortran normalization ---
#
# §5.1 gives the exact published surface as a fenced Fortran interface block. Two deterministic
# gates consume it: the ``--stage compile`` gate cross-checks its symbol set against §5, and the
# ``Generate.static`` gate pins the generated model source against each signature's interface
# lines. Both compare after a normalization that erases every non-semantic difference — inline
# comments, ``&`` continuations, case, and whitespace — so a signature authored one way in §5.1
# and formatted another way in the generated source still matches (and a genuine argument-name /
# type / rank / intent drift still fails).

_FENCED_BLOCK_RE = re.compile(r"(?ms)^```[^\n]*\n(.*?)^```[^\n]*$")
_IFACE_PROC_START = re.compile(
    r"^\s*(?:pure\s+|elemental\s+|recursive\s+)*(subroutine|function)\s+([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
# ``end\s*`` (space optional) accepts the legal no-space free-form keywords endsubroutine /
# endfunction / endtype as well as the spaced forms.
_IFACE_PROC_END = re.compile(r"^\s*end\s*(?:subroutine|function)\b", re.IGNORECASE)
# A type DEFINITION header: ``type :: name`` or ``type, attrs :: name`` — never a component
# declaration ``type(...) :: x`` (a ``(`` immediately follows ``type`` there, so ``::`` at the
# end is preceded by the parenthesized kind, not a bare/attributed ``type``).
_IFACE_TYPE_START = re.compile(
    r"^\s*type\s*(?:,\s*[^:()]*?)?::\s*([A-Za-z0-9_]+)\s*$", re.IGNORECASE
)
_IFACE_TYPE_END = re.compile(r"^\s*end\s*type\b", re.IGNORECASE)


def _strip_fenced_blocks(text: str) -> str:
    """Remove fenced code blocks (```...```) from Markdown text."""
    return _FENCED_BLOCK_RE.sub("", text)


def _mask_fortran_string_contents(line: str) -> str:
    """Replace the CONTENTS of every quoted string with spaces, keeping the quote delimiters and
    every character's position.

    For matching a statement's KEYWORD structure only: a string literal can hold text that looks
    like Fortran (`'run subroutine x first'`), and a real type-spec can hold a quote inside its
    parens (`character(kind=kind('a')) function f()`). Masking the contents removes the phantom
    keyword without disturbing the parens/`::`/`,` a header match keys on. Do NOT feed a masked
    line to a rule that inspects string CONTENT (the forbidden-filename scan deliberately catches
    a quoted `verdict.json`)."""
    out: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote is not None:
            out.append(ch if ch == quote else " ")
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _strip_fortran_comment(line: str) -> str:
    """Drop a trailing ``!`` comment, honoring single/double-quoted strings (so a ``!`` inside a
    string literal is not treated as a comment)."""
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "!":
            return line[:i]
    return line


def _fortran_logical_lines(text: str) -> list[str]:
    """Split Fortran source/interface text into logical lines: comments stripped and ``&``
    continuation lines joined (a leading ``&`` on a continued line is consumed). Whitespace and
    case are preserved here (normalization happens per-line in ``_normalize_fortran_line``).

    Free-form Fortran permits blank and full-line-comment lines *between* a ``&``-terminated line
    and its continuation — they are ignored, not statement terminators — so while a continuation is
    open such lines are skipped rather than flushing a truncated logical line. This matters: the
    §5.1 ``write_perf`` header exceeds the 132-column free-form limit and must be wrapped, so a
    legally-formatted source with a comment inside that wrap must still join to one logical line."""
    logical: list[str] = []
    buf: str | None = None  # None: not continuing; str: accumulated continuation (trailing & removed)
    for raw in text.splitlines():
        piece = _strip_fortran_comment(raw)
        if buf is not None:
            # Mid-continuation: a blank / pure-comment line does not terminate the statement.
            if not piece.strip():
                continue
            stripped = piece.lstrip()
            if stripped.startswith("&"):
                stripped = stripped[1:]
            combined = buf + stripped
        else:
            combined = piece
        rstripped = combined.rstrip()
        if rstripped.endswith("&"):
            buf = rstripped[:-1]
            continue
        buf = None
        logical.append(combined)
    if buf is not None:
        logical.append(buf)
    return logical


def _normalize_fortran_line(logical_line: str) -> str:
    """Canonical form of a (comment-stripped, continuation-joined) logical line: lower-cased with
    ALL whitespace removed, so formatting/alignment differences do not defeat an equality test."""
    return re.sub(r"\s+", "", logical_line).lower()


_END_STMT_RE = re.compile(r"^\s*end\s*(type|subroutine|function)\b", re.IGNORECASE)


def _canonicalize_end_line(line: str) -> str:
    """Reduce a closing ``end [type|subroutine|function] [name]`` to just ``end <kind>`` — the
    optional trailing construct name is legal to omit (bare ``end type`` is the common style) and
    is redundant with the header, so a stanza that drops it must still compare equal."""
    m = _END_STMT_RE.match(line)
    return f"end {m.group(1).lower()}" if m else line


def _parse_interface_stanzas(
    block_body: str,
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Parse a §5.1 canonical Fortran interface block into per-symbol *stanzas*.

    Returns ``(op_stanzas, type_stanzas, errors)``. Each stanza value is the ordered list of that
    symbol's interface logical lines (comment-stripped, continuation-joined, but NOT yet
    whitespace-normalized — kept readable for gate messages):
    - a procedure stanza is its ``subroutine``/``function`` header + every dummy-argument /
      ``result`` declaration up to (but excluding) the ``end`` line;
    - a type stanza is its ``type :: name`` header + component declarations + the ``end type``
      line (inclusive, so the closing name is pinned too).
    Lines outside any stanza (the public ``parameter`` declarations, comments, blanks) are
    ignored. An unterminated stanza OR a duplicate symbol name is reported in ``errors``
    (fail-closed at the caller — a duplicate must never silently overwrite, which would let a
    malformed first copy hide behind a correct second)."""
    lines = _fortran_logical_lines(block_body)
    op_stanzas: dict[str, list[str]] = {}
    type_stanzas: dict[str, list[str]] = {}
    errors: list[str] = []
    seen: set[str] = set()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        m_type = _IFACE_TYPE_START.match(line)
        m_proc = _IFACE_PROC_START.match(line)
        if m_type:
            name = m_type.group(1)
            stanza = [line]
            i += 1
            closed = False
            while i < n:
                cur = lines[i].strip()
                # A following stanza header terminates this one so it cannot swallow the next
                # symbol — but a derived type MUST close with `end type` (a bare `end` does NOT
                # close a type in Fortran), so reaching a header first leaves `closed` False and
                # the stanza is reported unterminated (fail-closed). Reprocess the header on the
                # outer loop (do not advance).
                if cur and (_IFACE_TYPE_START.match(cur) or _IFACE_PROC_START.match(cur)):
                    break
                if cur:
                    stanza.append(cur)
                if cur and _IFACE_TYPE_END.match(cur):
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                errors.append(f"unterminated derived-type definition '{name}'")
            if name in seen:
                errors.append(f"duplicate signature for symbol '{name}'")
            seen.add(name)
            type_stanzas[name] = stanza
        elif m_proc:
            name = m_proc.group(2)
            stanza = [line]
            i += 1
            closed = False
            while i < n:
                cur = lines[i].strip()
                if cur and _IFACE_PROC_END.match(cur):
                    closed = True
                    i += 1
                    break
                # A bare `end` (legal for a module procedure) is not matched above; terminate on the
                # next stanza header so it cannot swallow the following symbol (reprocess it).
                if cur and (_IFACE_PROC_START.match(cur) or _IFACE_TYPE_START.match(cur)):
                    closed = True
                    break
                if cur:
                    stanza.append(cur)
                i += 1
            if not closed:
                errors.append(f"unterminated procedure interface '{name}'")
            if name in seen:
                errors.append(f"duplicate signature for symbol '{name}'")
            seen.add(name)
            op_stanzas[name] = stanza
        else:
            i += 1
    return op_stanzas, type_stanzas, errors


_SUBSECTION_51_HEADING = re.compile(r"^###\s+5\.1(?:[.\s]|$)")


def _extract_subsection_51(section5_body: str) -> str | None:
    """Return the body of the ``### 5.1`` subsection within a §5 body, or ``None`` when absent.
    Scoping the fence search to this subsection means an unrelated illustrative code fence
    elsewhere in §5 prose does not brick certification (it would otherwise look like a second
    interface block)."""
    lines = section5_body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if _SUBSECTION_51_HEADING.match(line.strip()):
            start = i
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        if re.match(r"^#{1,6}\s", line.strip()):  # any following heading ends the subsection
            break
        body.append(line)
    return "\n".join(body)


def _section51_fence_body(controlled_spec_path: Path) -> tuple[str | None, str | None]:
    """Return ``(fence_body, error)`` for the §5.1 canonical interface fence (the sole fenced block
    inside the ``### 5.1`` subsection). Fail-closed on a missing subsection, a missing fence, or
    multiple fences within §5.1."""
    try:
        text = controlled_spec_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return (None, f"{controlled_spec_path}: controlled_spec unreadable")
    section = _extract_controlled_spec_section(text, "5")
    if section is None:
        return (None, "controlled_spec has no §5 section")
    subsection = _extract_subsection_51(section)
    if subsection is None:
        return (None, "§5.1 canonical interface subsection (### 5.1) is missing")
    blocks = _FENCED_BLOCK_RE.findall(subsection)
    if not blocks:
        return (None, "§5.1 canonical interface block (a fenced code block) is missing")
    if len(blocks) > 1:
        return (None, "§5.1 has multiple fenced code blocks (the interface block must be the only one)")
    return (blocks[0], None)


def _section51_module_parameters(controlled_spec_path: Path) -> list[dict]:
    """The §5.1 structured ``module_parameters`` entries (each ``{name, base?, value}``). These are
    part of the published ABI a consuming node sees. Returns the well-formed entries (both ``name``
    and ``value`` present); a missing subsection / fence / non-YAML block yields ``[]`` (fail-closed
    at the calling gate, which flags a §5.1 that cannot be parsed). Used both to render the Fortran
    declaration lines for the Generate.static source pin and to pin the IR's ``public_api.
    module_parameters`` at Compile."""
    from tools.lang_backend_fortran import load_structured_signatures

    body, err = _section51_fence_body(controlled_spec_path)
    if err or body is None:
        return []
    struct, perr = load_structured_signatures(body)
    if perr:
        return []
    return [
        mp
        for mp in struct.get("module_parameters", [])
        if isinstance(mp, dict) and mp.get("name") is not None and mp.get("value") is not None
    ]


def _section51_parameter_lines(controlled_spec_path: Path) -> list[str]:
    """The §5.1 module-level ``parameter`` declaration lines in the target language (e.g. ``integer,
    parameter :: dp = real64``). §5.1 carries the NEUTRAL value (``dp = float64``); the Fortran
    backend lowers it to the language the generated source is written in via the single
    ``render_module_parameter_to_fortran`` — so the Generate.static source pin deterministically
    demands the Fortran spelling (``dp = real64``) from a neutral §5.1 ``float64``.

    ``render_module_parameter_to_fortran`` validates each parameter and can raise
    ``SignatureParseError`` on a malformed / stale-token §5.1 value; this helper lets it propagate,
    and the ONE gate that renders it (``_validate_infrastructure_generated_signatures``) both calls
    this only AFTER ``_parse_canonical_interface_from_controlled_spec`` — which renders the whole
    §5.1 struct first and short-circuits with a violation on any unrenderable parameter — and wraps
    this call in ``except SignatureParseError`` as defense-in-depth, so a malformed §5.1 fails closed
    with a clear violation, never an uncaught gate crash."""
    from tools.lang_backend_fortran import render_module_parameter_to_fortran

    return [
        render_module_parameter_to_fortran(mp)
        for mp in _section51_module_parameters(controlled_spec_path)
    ]


def _parse_canonical_interface_from_controlled_spec(
    controlled_spec_path: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]], str | None]:
    """Extract and parse an infrastructure node's §5.1 canonical interface block.

    §5.1 is a language-neutral *structured* signature block (Objective B). The Fortran-language
    backend loads it and renders each published symbol back to a canonical Fortran stanza, so the
    downstream gates compare in the same Fortran currency the generated ``.f90`` is written in
    (``_parse_interface_stanzas`` on the rendered block reproduces the exact stanza shape).

    Returns ``(op_stanzas, type_stanzas, error)``. ``error`` is non-``None`` when the block is
    missing, duplicated, not valid structured YAML, or renders to zero signatures — every such case
    is fail-closed at the gate (a spec that fails to pin its own surface cannot certify)."""
    from tools.lang_backend_fortran import (
        SignatureParseError,
        load_structured_signatures,
        render_signatures_to_fortran,
    )

    body, err = _section51_fence_body(controlled_spec_path)
    if err or body is None:
        return ({}, {}, err)
    struct, perr = load_structured_signatures(body)
    if perr:
        return ({}, {}, perr)
    try:
        rendered = render_signatures_to_fortran(struct)
    except SignatureParseError as exc:
        return ({}, {}, f"§5.1 structured block could not render to Fortran: {exc}")
    op_stanzas, type_stanzas, errors = _parse_interface_stanzas(rendered)
    if errors:
        return (op_stanzas, type_stanzas, "; ".join(errors))
    if not op_stanzas and not type_stanzas:
        return ({}, {}, "§5.1 canonical interface block parsed 0 signatures")
    return (op_stanzas, type_stanzas, None)


def _contract_test_evidence_requirements(
    contract: dict[str, Any],
) -> dict[str, set[str]]:
    raw_reqs = contract.get("test_evidence_requirements")
    if not isinstance(raw_reqs, list):
        return {}

    result: dict[str, set[str]] = {}
    for item in raw_reqs:
        if not isinstance(item, dict):
            continue
        raw_test_id = item.get("test_id")
        raw_variables = item.get("required_raw_variables")
        if (
            not isinstance(raw_test_id, str)
            or not raw_test_id.strip()
            or not isinstance(raw_variables, list)
        ):
            continue
        variables = {
            token.strip()
            for token in raw_variables
            if isinstance(token, str) and token.strip()
        }
        if variables:
            result[raw_test_id.strip()] = variables
    return result


def _case_id_to_test_id(
    repo_root: Path, execution: NodeExecution
) -> dict[str, str]:
    """Map each IR case_id to its tests.md test_id (case.test_case_set).

    Used to scope per-snapshot evidence: a snapshot file named for a case need
    only carry the raw variables that case's test declares in
    io_contract.test_evidence_requirements (e.g. an input-guard rejection case
    that produces no output state), not the global union of declared variables.
    """
    data = _ir_document_for_execution(repo_root, execution)
    if not isinstance(data, dict):
        return {}
    case_section = data.get("case")
    if not isinstance(case_section, dict):
        return {}
    test_case_set = case_section.get("test_case_set")
    if not isinstance(test_case_set, list):
        return {}
    mapping: dict[str, str] = {}
    for item in test_case_set:
        if not isinstance(item, dict):
            continue
        case_id = item.get("case_id")
        test_id = item.get("test_id")
        if (
            isinstance(case_id, str)
            and case_id.strip()
            and isinstance(test_id, str)
            and test_id.strip()
        ):
            mapping[case_id.strip()] = test_id.strip()
    return mapping


def _case_id_to_test_ids(contract: dict[str, Any]) -> dict[str, list[str]]:
    """Map each case_id to every test_id ranging over it, from
    ``io_contract.test_predicates[].target_cases``.

    This is the anchor a host-rendered runner is built from: `runner_renderer._per_case_vars`
    emits, per case, the union of `required_raw_variables` over exactly these tests. Reading
    the same field here makes the post_execute snapshot check a mirror of what the renderer
    wrote, rather than an independent guess (the `_validate_harness_render_preconditions`
    discipline). It is also the only case -> test mapping every IR carries: `case.test_case_set[]`
    is not required to declare a `test_id` (`phase_01_compile.md`), so `_case_id_to_test_id`
    returns {} for an IR that omits it, and a case targeted by several tests has no single id
    at all. Empty dict when the IR declares no predicates.
    """
    predicates = contract.get("test_predicates")
    if not isinstance(predicates, list):
        # `_io_contract_for_execution` hoists the key out of the nested `io_contract`
        # section; an un-flattened doc still nests it.
        nested = contract.get("io_contract")
        predicates = nested.get("test_predicates") if isinstance(nested, dict) else None
    if not isinstance(predicates, list):
        return {}
    mapping: dict[str, list[str]] = {}
    for item in predicates:
        if not isinstance(item, dict):
            continue
        test_id = item.get("test_id")
        if not (isinstance(test_id, str) and test_id.strip()):
            continue
        for case_id in item.get("target_cases") or []:
            if isinstance(case_id, str) and case_id.strip():
                bucket = mapping.setdefault(case_id.strip(), [])
                if test_id.strip() not in bucket:
                    bucket.append(test_id.strip())
    return mapping


def _test_id_to_case_ids(contract: dict[str, Any]) -> dict[str, list[str]]:
    """Map each test_id to every case_id its predicate ranges over, from
    ``io_contract.test_predicates[].target_cases`` — the reverse of ``_case_id_to_test_ids``,
    reading the very same field.

    This is the row set of a test's metrics-basis evidence: the host-rendered runner emits one
    ``h_mb_entry`` per ``(test_id, case_id)`` pair over exactly this product
    (``runner_renderer.render_runner``), so the post_execute completeness matrix
    (``_validate_metrics_basis_per_test``) mirrors the renderer rather than guessing. Empty dict
    when the IR declares no predicates.
    """
    predicates = contract.get("test_predicates")
    if not isinstance(predicates, list):
        # `_io_contract_for_execution` hoists the key out of the nested `io_contract`
        # section; an un-flattened doc still nests it.
        nested = contract.get("io_contract")
        predicates = nested.get("test_predicates") if isinstance(nested, dict) else None
    if not isinstance(predicates, list):
        return {}
    mapping: dict[str, list[str]] = {}
    for item in predicates:
        if not isinstance(item, dict):
            continue
        test_id = item.get("test_id")
        if not (isinstance(test_id, str) and test_id.strip()):
            continue
        bucket = mapping.setdefault(test_id.strip(), [])
        for case_id in item.get("target_cases") or []:
            if isinstance(case_id, str) and case_id.strip() and case_id.strip() not in bucket:
                bucket.append(case_id.strip())
    return mapping


def _case_ids_for_execution(repo_root: Path, execution: NodeExecution) -> set[str]:
    """All declared ``case.test_case_set[].case_id`` for the execution's IR.

    Used by the post_generate snapshot-filename check to avoid a false positive on
    a hardcoded ``raw/state_snapshots/<case_id>.json`` literal that legitimately
    matches a declared case (it satisfies the deliverable gate). Unlike
    ``_case_id_to_test_id`` this includes cases whose ``test_id`` is null.
    """
    data = _ir_document_for_execution(repo_root, execution)
    if not isinstance(data, dict):
        return set()
    case_section = data.get("case")
    if not isinstance(case_section, dict):
        return set()
    test_case_set = case_section.get("test_case_set")
    if not isinstance(test_case_set, list):
        return set()
    return {
        item["case_id"].strip()
        for item in test_case_set
        if isinstance(item, dict)
        and isinstance(item.get("case_id"), str)
        and item["case_id"].strip()
    }


def _metrics_basis_entries(
    metrics_basis: dict[str, Any],
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str], str | None]:
    """Index a ``raw/metrics_basis.json`` document by its ``(test_id, case_id)`` entry key.

    R3-core: a test's primary evidence is the evidence of EVERY case its predicate ranges over,
    so ``test_id`` alone is not a key. Every entry carries a non-empty ``case_id`` as a direct
    sibling of ``test_id`` (harness controlled_spec §2), and ``(test_id, case_id)`` is unique.

    Returns ``(entries, problems, form)`` where ``form`` names the container actually parsed
    (``"per_test"`` / ``"tests"``, or ``None`` when neither did). The ``tests`` object form is
    keyed by test_id and therefore cannot hold two entries for one test — it is deprecated for
    that reason; ``_validate_metrics_basis_per_test`` turns a multi-target test written that way
    into an actionable violation rather than an opaque "missing evidence".
    """
    raw_entries = metrics_basis.get("per_test")
    form: str | None = "per_test"
    if raw_entries is None:
        raw_entries = metrics_basis.get("tests")
        form = "tests"

    entries: dict[tuple[str, str], dict[str, Any]] = {}
    problems: list[str] = []

    def _entry_case_id(item: dict[str, Any], loc: str, test_id: str) -> str | None:
        raw_case_id = item.get("case_id")
        if not isinstance(raw_case_id, str) or not raw_case_id.strip():
            problems.append(
                f"{loc} (test_id {test_id}) must carry a non-empty `case_id` as a direct "
                "sibling of `test_id` — metrics-basis evidence is keyed by (test_id, case_id), "
                "one entry per case the test's predicate targets"
            )
            return None
        return raw_case_id.strip()

    if isinstance(raw_entries, list):
        for idx, item in enumerate(raw_entries):
            if not isinstance(item, dict):
                problems.append(f"per_test[{idx}] must be object")
                continue
            raw_test_id = item.get("test_id")
            if not isinstance(raw_test_id, str) or not raw_test_id.strip():
                problems.append(f"per_test[{idx}].test_id must be non-empty string")
                continue
            test_id = raw_test_id.strip()
            case_id = _entry_case_id(item, f"per_test[{idx}]", test_id)
            if case_id is None:
                continue
            if (test_id, case_id) in entries:
                problems.append(
                    f"per_test has duplicated (test_id, case_id) (({test_id}, {case_id}))"
                )
                continue
            entries[(test_id, case_id)] = item
        return entries, problems, form

    if isinstance(raw_entries, dict):
        for raw_test_id, item in raw_entries.items():
            if not isinstance(raw_test_id, str) or not raw_test_id.strip():
                problems.append("tests keys must be non-empty strings")
                continue
            if not isinstance(item, dict):
                problems.append(f"tests[{raw_test_id!r}] must be object")
                continue
            test_id = raw_test_id.strip()
            case_id = _entry_case_id(item, f"tests[{raw_test_id!r}]", test_id)
            if case_id is None:
                continue
            # JSON object keys are unique as WRITTEN, but this reader strips them — so
            # `"test_a"` and `" test_a "` are two distinct keys that name one entry. Without
            # this check the later one silently overwrites the earlier, and a malformed row
            # (say, one missing a required variable) simply disappears. Same rule as the
            # `per_test` list branch: one entry per (test_id, case_id).
            if (test_id, case_id) in entries:
                problems.append(
                    f"tests has duplicated (test_id, case_id) (({test_id}, {case_id})) — two "
                    "keys normalize to the same test_id"
                )
                continue
            entries[(test_id, case_id)] = item
        return entries, problems, form

    problems.append("must contain per_test list or tests object")
    return entries, problems, None


_METRICS_BASIS_NESTED_VARIABLE_FIELDS = ("raw_variables", "variables", "evidence")

_METRICS_BASIS_BOOKKEEPING_KEYS = frozenset(
    {
        "test_id",
        "case_id",
        "case_ids",
        "cases",
        "status",
        "summary",
        "notes",
        "meta",
        "artifacts",
    }
)


def _metrics_basis_variable_keys(entry: dict[str, Any]) -> set[str]:
    for field_name in _METRICS_BASIS_NESTED_VARIABLE_FIELDS:
        raw_value = entry.get(field_name)
        if isinstance(raw_value, dict):
            return {
                key.strip()
                for key in raw_value
                if isinstance(key, str) and key.strip()
            }

    return {
        key.strip()
        for key in entry
        if isinstance(key, str)
        and key.strip()
        and key not in _METRICS_BASIS_BOOKKEEPING_KEYS
    }


def _metrics_basis_unrecognized_wrapper(
    entry: dict[str, Any],
    missing_variables: list[str],
    required_variables: set[str],
) -> tuple[str, list[str]] | None:
    """Name the wrapper key hiding `missing_variables` one level below `test_id`.

    Returns the wrapper and the missing variables it actually holds, so the
    caller can scope its claim: with two wrappers hiding one variable each,
    naming only the winner and asserting it holds *the* missing variables
    would be false.

    Diagnostic only: the result feeds repair guidance, never a verdict, so the
    keys accepted by `_metrics_basis_variable_keys` stay exactly as they were.
    Ties are broken by coverage then lexically so the message is deterministic.

    Key recognition mirrors `_metrics_basis_variable_keys` exactly: bookkeeping
    and nesting fields are matched against the raw key, because that reader only
    honours them unpadded. A padded `" raw_variables "` is therefore a wrapper
    to both, and a contract variable is never named as the thing wrapping it.

    The raw key is what gets reported, so padding stays visible in the message;
    ordering uses the stripped form, which is the identity the reader compares.
    """
    wanted = set(missing_variables)
    best: tuple[int, str, str, tuple[str, ...]] | None = None
    for raw_key, value in entry.items():
        if not isinstance(raw_key, str) or not isinstance(value, dict):
            continue
        if raw_key in _METRICS_BASIS_BOOKKEEPING_KEYS:
            continue
        if raw_key in _METRICS_BASIS_NESTED_VARIABLE_FIELDS:
            continue
        key = raw_key.strip()
        if not key or key in required_variables:
            continue
        covered = tuple(
            sorted(
                {
                    nested.strip()
                    for nested in value
                    if isinstance(nested, str) and nested.strip()
                }
                & wanted
            )
        )
        if not covered:
            continue
        candidate = (len(covered), key, raw_key, covered)
        if best is None or (
            candidate[0] > best[0]
            or (candidate[0] == best[0] and candidate[1:3] < best[1:3])
        ):
            best = candidate
    return None if best is None else (best[2], list(best[3]))


def _impl_language_from_plan_dir(repo_root: Path, ir_dir: Path) -> str | None:
    impl_path = ir_dir / "spec.ir.yaml"
    if not impl_path.exists():
        return None
    try:
        data = _read_yaml(impl_path)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    # New IR nests toolchain under impl_defaults.  Fall back to a flat
    # `toolchain:` key when the doc still uses the legacy layout (eg. tests
    # that hand-construct only the impl section).
    impl_defaults = data.get("impl_defaults")
    if isinstance(impl_defaults, dict):
        toolchain = impl_defaults.get("toolchain")
    else:
        toolchain = data.get("toolchain")
    if not isinstance(toolchain, dict):
        return None
    raw = toolchain.get("language")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().lower()


def _infer_run_linter_preset_from_command(command: list[Any]) -> str | None:
    if not command:
        return None
    head = str(command[0]).strip().lower()
    exe = Path(head).name
    if exe == "fortitude":
        return "fortitude"
    if exe == "cppcheck":
        return "cppcheck"
    if exe == "ruff":
        return "ruff"
    return None


def _verify_mcp_command_log_record(
    repo_root: Path,
    meta_path: Path,
    label: str,
    command_id: str,
    log_ref: str,
    expected_tool: str,
    violations: list[str],
) -> list[Any] | None:
    """Shared forgery-detection for a host-authored evidence entry that cites an MCP
    ``command_log.jsonl`` record (used by both the lint and syntax certifications). The log
    ref must be the canonical placement, the record must exist, be ``expected_tool``, have
    ``ok=true``, and carry a non-empty ``command`` argv. Returns the logged ``command`` list
    on success (for the caller's tool-specific final check — preset for lint, argv[0]
    compiler for syntax), or ``None`` after appending exactly one violation. ``label``
    prefixes each message (e.g. ``lint evidence run_linter[0]`` /
    ``syntax evidence stages[0]``). Callers validate ``command_id`` / ``log_ref`` presence
    before calling."""
    canonical_refs = _canonical_mcp_log_refs_for_lint(meta_path, repo_root)
    log_ref_norm = log_ref.rstrip("/")
    if canonical_refs and log_ref_norm not in canonical_refs:
        violations.append(
            f"{meta_path}: {label}.command_log_ref must be the canonical MCP audit log "
            f"placement (expected one of {sorted(canonical_refs)!r}, got {log_ref_norm!r}). "
            "Non-canonical placements are rejected to prevent forged tool-execution evidence."
        )
        return None
    matched = _find_command_log_record(repo_root, command_id, log_ref)
    if matched is None:
        violations.append(
            f"{meta_path}: {label}: command log not found for command_id={command_id!r}"
        )
        return None
    if matched.get("tool_name") != expected_tool:
        violations.append(
            f"{meta_path}: {label}: command_id={command_id!r} tool_name must be {expected_tool}"
        )
        return None
    if matched.get("ok") is not True:
        violations.append(
            f"{meta_path}: {label}: command_id={command_id!r} {expected_tool} did not "
            "succeed (ok must be true)"
        )
        return None
    command = matched.get("command")
    if not isinstance(command, list) or not command:
        violations.append(f"{meta_path}: {label}: command log missing command")
        return None
    return command


def _validate_generate_lint_command_logs(
    repo_root: Path,
    meta_path: Path,
    data: dict[str, Any],
    impl_language: str | None,
    violations: list[str],
) -> None:
    """Certify the conductor-run static lint for Generate against its host-authored,
    leaf-non-writable evidence (`<pipeline_root>/lint_evidence/<source_id>.json`).

    Static lint is no longer run by the leaf (it is the deterministic `generate.gate`
    substep run in-process by the conductor — Conductor._gate_lint_check). The evidence
    certificate cannot be forged by the leaf (the pipeline root is read-only inside the
    sandbox), so this validates against it rather than the former leaf-written
    `source_meta.lint_command_ref` (which is now ignored)."""
    # meta_path = <pipeline_root>/source/<source_id>/source_meta.json
    source_id = meta_path.parent.name
    pipeline_root = meta_path.parents[2]
    from tools.hooks.lint_evidence import lint_evidence_path, read_lint_evidence

    # post_generate now runs in the deterministic `generate.gate` substep, which executes
    # BEFORE `generate.verify` sets verification_status=pass — but the conductor already wrote
    # the lint evidence in `generate.gate`. Certify whenever that evidence exists (the
    # static-stage flow) OR the leaf is claiming pass (legacy/back-compat). Skip only when
    # neither holds (e.g. a manual or pre-lint invocation on an un-certified source), matching
    # the prior "only certify a pass" behavior so unrelated callers are unaffected.
    status = data.get("verification_status")
    verified_pass = isinstance(status, str) and status.strip().lower() == "pass"
    try:
        evidence_present = lint_evidence_path(
            pipeline_root=pipeline_root, source_id=source_id).exists()
    except ValueError:
        evidence_present = False
    if not verified_pass and not evidence_present:
        return

    if not impl_language:
        violations.append(
            f"{meta_path}: cannot validate static lint without spec.ir.yaml toolchain.language"
        )
        return

    expected = _LINT_PRESET_FOR_LANGUAGE.get(impl_language)
    if expected is None:
        violations.append(
            f"{meta_path}: toolchain.language={impl_language!r} has no static lint mapping"
        )
        return

    try:
        evidence = read_lint_evidence(pipeline_root=pipeline_root, source_id=source_id)
    except ValueError as exc:
        violations.append(
            f"{meta_path}: malformed conductor lint evidence "
            f"({pipeline_root.name}/lint_evidence/{source_id}.json): {exc}"
        )
        return
    if evidence is None:
        violations.append(
            f"{meta_path}: missing conductor lint evidence "
            f"(expected {pipeline_root.name}/lint_evidence/{source_id}.json) when "
            "verification_status=pass; lint is run by the conductor (generate.gate)"
        )
        return
    if evidence.get("ok") is not True:
        violations.append(
            f"{meta_path}: conductor lint evidence reports lint did not succeed "
            "(ok must be true)"
        )
        return
    ev_preset = str(evidence.get("preset") or "").strip().lower()
    if ev_preset != expected:
        violations.append(
            f"{meta_path}: conductor lint evidence preset must be {expected!r} for "
            f"toolchain.language={impl_language} (got {ev_preset!r})"
        )
        return
    run_entries = evidence.get("run_linter")
    if not isinstance(run_entries, list) or not run_entries:
        violations.append(
            f"{meta_path}: conductor lint evidence run_linter must be a non-empty array"
        )
        return

    if expected == "mixed":
        if len(run_entries) != 2:
            violations.append(
                f"{meta_path}: toolchain.language=mixed requires exactly two run_linter entries "
                f"(found {len(run_entries)})"
            )
        presets_found: set[str] = set()
        for entry in run_entries:
            if not isinstance(entry, dict):
                violations.append(
                    f"{meta_path}: lint evidence run_linter entries must be objects"
                )
                continue
            p = entry.get("preset")
            if isinstance(p, str) and p.strip():
                presets_found.add(p.strip().lower())
        if presets_found != {"fortitude", "cppcheck"}:
            violations.append(
                f"{meta_path}: toolchain.language=mixed requires run_linter entries with "
                f"preset fortitude and cppcheck (found {sorted(presets_found)})"
            )
    else:
        if len(run_entries) != 1:
            violations.append(
                f"{meta_path}: lint evidence run_linter must have exactly one entry "
                f"for toolchain.language={impl_language}"
            )
        else:
            entry = run_entries[0]
            if not isinstance(entry, dict):
                violations.append(
                    f"{meta_path}: lint evidence run_linter[0] must be object"
                )
            else:
                p = entry.get("preset")
                if not isinstance(p, str) or p.strip().lower() != expected:
                    violations.append(
                        f"{meta_path}: lint preset must be {expected!r} for "
                        f"toolchain.language={impl_language} (got {p!r})"
                    )

    for idx, entry in enumerate(run_entries):
        if not isinstance(entry, dict):
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}] must be object"
            )
            continue
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref")
        preset_decl = entry.get("preset")
        if not isinstance(command_id, str) or not command_id.strip():
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}].command_id invalid"
            )
            continue
        if not isinstance(log_ref, str) or not log_ref.strip():
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}].command_log_ref invalid"
            )
            continue
        if not isinstance(preset_decl, str) or not preset_decl.strip():
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}].preset invalid"
            )
            continue
        preset_decl_l = preset_decl.strip().lower()
        if preset_decl_l not in _LINT_ALLOWED_PRESETS:
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}].preset must be one of "
                f"{sorted(_LINT_ALLOWED_PRESETS)}"
            )
            continue
        if preset_decl_l == "mixed":
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}].preset must not be "
                "'mixed'; record separate fortitude and cppcheck entries"
            )
            continue

        command = _verify_mcp_command_log_record(
            repo_root, meta_path, f"lint evidence run_linter[{idx}]",
            command_id.strip(), log_ref.strip(), "run_linter", violations)
        if command is None:
            continue
        inferred = _infer_run_linter_preset_from_command(command)
        if inferred != preset_decl_l:
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}]: logged command does not match "
                f"preset {preset_decl_l!r} (inferred {inferred!r})"
            )


def _validate_generate_syntax_command_logs(
    repo_root: Path,
    meta_path: Path,
    data: dict[str, Any],
    impl_language: str | None,
    violations: list[str],
) -> None:
    """Certify the conductor-run compiler syntax gate for Generate against its
    host-authored, leaf-non-writable evidence
    (`<pipeline_root>/syntax_evidence/<source_id>.json`).

    The syntax gate is the deterministic `generate.gate` substep run in-process by the
    conductor (Conductor._gate_syntax_check -> MCP run_syntax_check). Mirrors
    `_validate_generate_lint_command_logs`: the certificate cannot be forged by the leaf
    (the pipeline root is read-only inside the sandbox). Required only for
    toolchain.language=fortran (the only language with a syntax-check adapter); the
    MANDATORY stage is gfortran and must have passed. Optional additional stages (the
    METDSL_SYNTAX_COMPILERS target-compiler stages) may be recorded as `skipped` when
    their compiler is not installed; a recorded `fail` stage always fails certification."""
    source_id = meta_path.parent.name
    pipeline_root = meta_path.parents[2]
    from tools.hooks.syntax_evidence import read_syntax_evidence, syntax_evidence_path

    # fortran is the only language the gate runs for; other languages pass through the
    # generate.gate syntax check without evidence, so there is nothing to certify.
    if not impl_language or impl_language.strip().lower() != "fortran":
        return

    # Same trigger rule as the lint certification: certify whenever the conductor-run
    # evidence exists (the static-stage flow) OR the leaf is claiming pass; skip only when
    # neither holds (e.g. a manual or pre-syntax invocation on an un-certified source).
    status = data.get("verification_status")
    verified_pass = isinstance(status, str) and status.strip().lower() == "pass"
    try:
        evidence_present = syntax_evidence_path(
            pipeline_root=pipeline_root, source_id=source_id).exists()
    except ValueError:
        evidence_present = False
    if not verified_pass and not evidence_present:
        return

    try:
        evidence = read_syntax_evidence(pipeline_root=pipeline_root, source_id=source_id)
    except ValueError as exc:
        violations.append(
            f"{meta_path}: malformed conductor syntax evidence "
            f"({pipeline_root.name}/syntax_evidence/{source_id}.json): {exc}"
        )
        return
    if evidence is None:
        violations.append(
            f"{meta_path}: missing conductor syntax evidence "
            f"(expected {pipeline_root.name}/syntax_evidence/{source_id}.json) when "
            "verification_status=pass; the syntax gate is run by the conductor "
            "(generate.gate)"
        )
        return
    if evidence.get("ok") is not True:
        violations.append(
            f"{meta_path}: conductor syntax evidence reports the syntax gate did not "
            "succeed (ok must be true)"
        )
        return
    stages = evidence.get("stages")
    if not isinstance(stages, list) or not stages:
        violations.append(
            f"{meta_path}: conductor syntax evidence stages must be a non-empty array"
        )
        return

    gfortran_passed = False
    for idx, entry in enumerate(stages):
        if not isinstance(entry, dict):
            violations.append(
                f"{meta_path}: syntax evidence stages[{idx}] must be object"
            )
            continue
        compiler = str(entry.get("compiler") or "").strip().lower()
        stage_status = str(entry.get("status") or "").strip().lower()
        if stage_status == "fail":
            violations.append(
                f"{meta_path}: syntax evidence stages[{idx}] ({compiler}) recorded a "
                "failed syntax check"
            )
            continue
        if stage_status == "skipped":
            if compiler == "gfortran":
                violations.append(
                    f"{meta_path}: syntax evidence stages[{idx}]: the mandatory gfortran "
                    "stage must not be skipped"
                )
            continue
        if stage_status != "pass":
            violations.append(
                f"{meta_path}: syntax evidence stages[{idx}].status invalid "
                f"(got {stage_status!r})"
            )
            continue

        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref")
        if not isinstance(command_id, str) or not command_id.strip():
            violations.append(
                f"{meta_path}: syntax evidence stages[{idx}].command_id invalid"
            )
            continue
        if not isinstance(log_ref, str) or not log_ref.strip():
            violations.append(
                f"{meta_path}: syntax evidence stages[{idx}].command_log_ref invalid"
            )
            continue

        # Same canonical placement as the lint records: <gen_dir>/src/command_log.jsonl.
        command = _verify_mcp_command_log_record(
            repo_root, meta_path, f"syntax evidence stages[{idx}]",
            command_id.strip(), log_ref.strip(), "run_syntax_check", violations)
        if command is None:
            continue
        exe_basename = Path(str(command[0])).name.strip().lower()
        if exe_basename != compiler:
            violations.append(
                f"{meta_path}: syntax evidence stages[{idx}]: logged command does not "
                f"match compiler {compiler!r} (argv[0] is {command[0]!r})"
            )
            continue
        if compiler == "gfortran":
            gfortran_passed = True

    if not gfortran_passed:
        violations.append(
            f"{meta_path}: syntax evidence must record a passing gfortran stage "
            "(the mandatory syntax gate for toolchain.language=fortran)"
        )


def _find_command_log_record(
    repo_root: Path, command_id: str, log_ref: str
) -> dict[str, Any] | None:
    log_path = repo_root / log_ref if log_ref.startswith("workspace/") else Path(log_ref)
    if not log_path.exists():
        return None

    # Leaf-writable artifact (it is in the generate/verify `allowed_output_paths`): decode
    # leniently so a stray byte is a missing/!matching command record — a finding — not a traceback.
    for raw_line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("command_id") == command_id:
            return obj if isinstance(obj, dict) else None
    return None


def _parse_shape_expr(expr: str) -> tuple[bool, list[str], str]:
    """Validate shape_expr against spec/schema/ir/shape_expr.schema.json.

    Allowed forms (canonical source: spec/schema/ir/shape_expr.schema.json):
      - "scalar" (case-insensitive)
      - "[d1, d2, ...]" with non-empty dim tokens
      - "(d1, d2, ...)" with non-empty dim tokens

    Function-call notation such as "vector(3)" / "matrix(M,N)" / "tensor" is rejected.
    """
    token = expr.strip()
    if not token:
        return False, [], "shape_expr must be non-empty"
    scalar_patterns, list_form_patterns = _get_shape_expr_patterns()
    for pat in scalar_patterns:
        if pat.fullmatch(token):
            return True, [], ""
    matched = False
    for pat in list_form_patterns:
        if pat.fullmatch(token):
            matched = True
            break
    if not matched:
        return (
            False,
            [],
            "shape_expr must be scalar or [dim1,dim2,...] or (dim1,dim2,...). "
            "See spec/schema/ir/shape_expr.schema.json for canonical forms; "
            "function-call notations such as vector(N), matrix(M,N), tensor are forbidden.",
        )
    body_match = _SHAPE_EXPR_DIM_SPLIT.fullmatch(token)
    if body_match is None:  # pragma: no cover - schema pattern guarantees match
        return False, [], "shape_expr internal parse error"
    dims: list[str] = []
    # Per-token grammar is owned by the schema's regex (the list-form
    # branch already encodes which dim-token forms are accepted). The
    # parser only extracts components and rejects empty splits as a
    # safety net — it does NOT re-validate token shape, because doing so
    # would shadow the schema and create a drift hazard if a repo-local
    # schema legitimately evolves the accepted dim-token grammar.
    for raw_dim in body_match.group(1).split(","):
        dim = raw_dim.strip()
        if not dim:
            return False, [], "shape_expr has empty dimension token"
        dims.append(dim)
    return True, dims, ""


def _canonical_shape_expr(expr: str) -> str:
    """Normalize a shape_expr string for cross-artifact equality comparison.

    Identifier-style dim tokens are case-SENSITIVE: `Nx` and `nx` are
    distinct identifiers (matches Python identifier semantics, which the
    schema's regex allows). Only the literal scalar form is normalized to
    lowercase `"scalar"`, since the schema explicitly defines that form
    case-insensitively (`^[Ss][Cc][Aa][Ll][Aa][Rr]$`). Whitespace inside
    list/paren forms is collapsed.
    """
    ok, dims, _ = _parse_shape_expr(expr)
    if not ok:
        return expr.strip()
    if not dims:
        return "scalar"
    return "[" + ",".join(dim for dim in dims) + "]"


def _infer_json_shape(value: Any) -> list[int] | None:
    if isinstance(value, (int, float, str, bool)) or value is None:
        return []
    if isinstance(value, list):
        if not value:
            return [0]
        first_shape = _infer_json_shape(value[0])
        if first_shape is None:
            return None
        for item in value[1:]:
            shape = _infer_json_shape(item)
            if shape is None or shape != first_shape:
                return None
        return [len(value), *first_shape]
    return None


def _shape_matches_expr(shape_expr: str, actual_shape: list[int]) -> bool:
    """Match a parsed shape_expr against an observed shape list.

    Token semantics:
      - integer literal (e.g. "3"): must equal `actual_dim` exactly.
      - identifier-style symbol (e.g. "nx"): binds to `actual_dim` on first
        occurrence; subsequent occurrences of the SAME identifier in the same
        shape_expr must observe the same value (so `[n,n]` requires a square
        shape, `[nx,ny]` accepts any rectangular shape, `[nx,nx]` requires
        equal extents).
      - The legacy "*" wildcard token is no longer producible by `_parse_shape_expr`
        because the active schema's list-form regex rejects it, so this matcher
        does not special-case it.
    """
    ok, dims, _ = _parse_shape_expr(shape_expr)
    if not ok:
        return False
    if not dims:
        return actual_shape == []
    if len(dims) != len(actual_shape):
        return False
    bindings: dict[str, int] = {}
    for expected_dim, actual_dim in zip(dims, actual_shape):
        # Case-SENSITIVE token: `Nx` and `nx` are treated as distinct
        # identifiers (matches Python identifier semantics that the schema
        # regex accepts). Lowercasing here would silently merge these into
        # a single binding and over-constrain shape contracts.
        token = expected_dim.strip()
        if token.isdigit():
            if int(token) != actual_dim:
                return False
            continue
        # Symbolic identifier — bind on first occurrence, enforce equality on
        # subsequent occurrences. Negative actual extents are never valid.
        if actual_dim < 0:
            return False
        prev = bindings.get(token)
        if prev is None:
            bindings[token] = actual_dim
        elif prev != actual_dim:
            return False
    return True


def _state_snapshot_requirement_details(
    repo_root: Path, execution: NodeExecution
) -> tuple[dict[str, str], str, str, int]:
    required_variables: dict[str, str] = {}
    required_time_variable = ""
    required_time_shape_expr = "scalar"
    min_samples = 1

    raw_requirements = _raw_requirements_for_execution(repo_root, execution)
    if not isinstance(raw_requirements, dict):
        return required_variables, required_time_variable, required_time_shape_expr, min_samples

    required_evidence = raw_requirements.get("required_evidence")
    if not isinstance(required_evidence, list):
        return required_variables, required_time_variable, required_time_shape_expr, min_samples

    for item in required_evidence:
        if not isinstance(item, dict):
            continue
        raw_artifact = item.get("artifact")
        if not isinstance(raw_artifact, str):
            continue
        artifact = _normalize_raw_evidence_artifact(raw_artifact)
        if artifact != "state_snapshots":
            continue
        if isinstance(item.get("required"), bool) and not item["required"]:
            return required_variables, required_time_variable, required_time_shape_expr, min_samples

        raw_min_samples = item.get("min_samples")
        if isinstance(raw_min_samples, int) and raw_min_samples >= 1:
            min_samples = raw_min_samples

        schema = item.get("schema")
        if isinstance(schema, dict):
            raw_variables = schema.get("variables")
            if isinstance(raw_variables, list):
                for variable in raw_variables:
                    if not isinstance(variable, dict):
                        continue
                    raw_name = variable.get("name")
                    raw_shape_expr = variable.get("shape_expr")
                    if (
                        isinstance(raw_name, str)
                        and raw_name.strip()
                        and isinstance(raw_shape_expr, str)
                        and raw_shape_expr.strip()
                    ):
                        required_variables[raw_name.strip()] = _canonical_shape_expr(raw_shape_expr)

            # Legacy `state_variables` shorthand is no longer recognized.
            # `_validate_io_contract_schema` separately rejects schemas
            # that omit `variables`, so missing required-evidence shapes fail
            # closed there with a clear violation.
            raw_time_var = schema.get("time_variable")
            if isinstance(raw_time_var, str) and raw_time_var.strip():
                required_time_variable = raw_time_var.strip()
            raw_time_shape = schema.get("time_shape_expr")
            if isinstance(raw_time_shape, str) and raw_time_shape.strip():
                required_time_shape_expr = _canonical_shape_expr(raw_time_shape)

        return required_variables, required_time_variable, required_time_shape_expr, min_samples

    return required_variables, required_time_variable, required_time_shape_expr, min_samples


def _normalize_raw_evidence_artifact(token: str) -> str | None:
    normalized = token.strip().lower().replace("\\", "/")
    return RAW_EVIDENCE_ALIASES.get(normalized)


def _raw_requirements_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    contract = _io_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return None

    raw_requirements = contract.get("raw_requirements")
    if not isinstance(raw_requirements, dict):
        return None
    return raw_requirements


def _required_raw_evidence(
    repo_root: Path, execution: NodeExecution
) -> set[str]:
    # execution_trace.json is IR-driven: it is required only when the IR's
    # raw_requirements.required_evidence explicitly declares it
    # (artifact=execution_trace, required=true). Per phase_04_validate.md:42 the
    # IR is the canonical source for raw-evidence and a fixed minimal set must
    # not be imposed uniformly on every spec, so it is intentionally absent from
    # this default. metrics_basis.json stays as the baseline raw evidence.
    required: set[str] = {"metrics_basis.json"}
    raw_requirements = _raw_requirements_for_execution(repo_root, execution)
    if not isinstance(raw_requirements, dict):
        return required

    required_evidence = raw_requirements.get("required_evidence")
    if isinstance(required_evidence, list):
        for item in required_evidence:
            if not isinstance(item, dict):
                continue
            raw_artifact = item.get("artifact")
            if not isinstance(raw_artifact, str):
                continue
            artifact = _normalize_raw_evidence_artifact(raw_artifact)
            if artifact is None:
                continue
            item_required = item.get("required")
            if item_required is False:
                required.discard(artifact)
            else:
                required.add(artifact)
        return required

    if raw_requirements.get("state_snapshot_required") is True:
        required.add("state_snapshots")
    elif raw_requirements.get("state_snapshot_required") is False:
        required.discard("state_snapshots")
    return required


def _state_snapshot_required(repo_root: Path, execution: NodeExecution) -> bool:
    default_required = False
    raw_requirements = _raw_requirements_for_execution(repo_root, execution)
    if not isinstance(raw_requirements, dict):
        return default_required

    required_evidence = raw_requirements.get("required_evidence")
    if isinstance(required_evidence, list):
        for item in required_evidence:
            if not isinstance(item, dict):
                continue
            raw_artifact = item.get("artifact")
            if not isinstance(raw_artifact, str):
                continue
            artifact = _normalize_raw_evidence_artifact(raw_artifact)
            if artifact != "state_snapshots":
                continue
            item_required = item.get("required")
            if isinstance(item_required, bool):
                return item_required
            return True

    value = raw_requirements.get("state_snapshot_required")
    if isinstance(value, bool):
        return value
    return default_required


def _validate_io_contract_file(
    repo_root: Path, contract_path: Path, violations: list[str]
) -> None:
    try:
        contract = _read_yaml(contract_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        try:
            contract = _read_json(contract_path)
        except json.JSONDecodeError:
            violations.append(f"{contract_path}: invalid json")
            return

    if not isinstance(contract, dict):
        violations.append(f"{contract_path}: must be json object")
        return

    # tests.md is referenced from `meta.source_refs.tests`, which the io_contract flattening below
    # does not carry — resolve it from the document while we still hold it.
    tests_path = _tests_path_from_ir_document(repo_root, contract)

    # New IR: spec.ir.yaml has the io_contract section nested under
    # `io_contract:` and contains inputs / outputs / raw_requirements /
    # test_evidence_requirements / semantic_dependency as siblings.  The
    # original validator was written against derived_contract.json's flat
    # structure where `io_contract`, `raw_requirements`, etc. were all
    # top-level keys.  When we see the new nested form, lift the io_contract
    # section to act as the contract dict, but keep `io_contract.inputs`
    # path semantics by wrapping inputs/outputs in a synthetic `io_contract`
    # key.
    if (
        isinstance(contract.get("io_contract"), dict)
        and ("inputs" in contract["io_contract"] or "outputs" in contract["io_contract"])
        and "raw_requirements" not in contract  # not legacy flat form
    ):
        io_section = contract["io_contract"]
        new_contract: dict[str, Any] = {
            "io_contract": {
                "inputs": io_section.get("inputs"),
                "outputs": io_section.get("outputs"),
            },
        }
        for key in (
            "raw_requirements",
            "semantic_dependency",
            "test_evidence_requirements",
            "diagnostics_contract",
        ):
            if key in io_section:
                new_contract[key] = io_section[key]
        contract = new_contract

    output_items: list[tuple[int, dict[str, Any]]] = []
    io_contract = contract.get("io_contract")
    if not isinstance(io_contract, dict):
        violations.append(f"{contract_path}:io_contract must be object")
    else:
        inputs = io_contract.get("inputs")
        if not isinstance(inputs, list):
            violations.append(f"{contract_path}:io_contract.inputs must be list")
        else:
            for idx, item in enumerate(inputs):
                if not isinstance(item, dict):
                    violations.append(
                        f"{contract_path}:io_contract.inputs[{idx}] must be object"
                    )
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    violations.append(
                        f"{contract_path}:io_contract.inputs[{idx}].name must be non-empty string"
                    )
                evidence_ref = item.get("evidence_ref")
                if not isinstance(evidence_ref, str) or not evidence_ref.strip():
                    violations.append(
                        f"{contract_path}:io_contract.inputs[{idx}].evidence_ref must be non-empty string"
                    )
                shape_expr = item.get("shape_expr")
                if shape_expr is not None and (
                    not isinstance(shape_expr, str) or not shape_expr.strip()
                ):
                    violations.append(
                        f"{contract_path}:io_contract.inputs[{idx}].shape_expr must be non-empty string when present"
                    )
                elif isinstance(shape_expr, str):
                    ok_shape, _, shape_err = _parse_shape_expr(shape_expr)
                    if not ok_shape:
                        violations.append(
                            f"{contract_path}:io_contract.inputs[{idx}].shape_expr invalid ({shape_err})"
                        )

        outputs = io_contract.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            violations.append(f"{contract_path}:io_contract.outputs must be non-empty list")
        else:
            for idx, item in enumerate(outputs):
                if not isinstance(item, dict):
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}] must be object"
                    )
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}].name must be non-empty string"
                    )
                evidence_ref = item.get("evidence_ref")
                if not isinstance(evidence_ref, str) or not evidence_ref.strip():
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}].evidence_ref must be non-empty string"
                    )
                shape_expr = item.get("shape_expr")
                if shape_expr is not None and (
                    not isinstance(shape_expr, str) or not shape_expr.strip()
                ):
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}].shape_expr must be non-empty string when present"
                    )
                elif isinstance(shape_expr, str):
                    ok_shape, _, shape_err = _parse_shape_expr(shape_expr)
                    if not ok_shape:
                        violations.append(
                            f"{contract_path}:io_contract.outputs[{idx}].shape_expr invalid ({shape_err})"
                        )
                output_items.append((idx, item))

    raw_requirements = contract.get("raw_requirements")
    if not isinstance(raw_requirements, dict):
        violations.append(f"{contract_path}:raw_requirements must be object")
        return

    required_evidence = raw_requirements.get("required_evidence")
    if not isinstance(required_evidence, list) or not required_evidence:
        violations.append(
            f"{contract_path}:raw_requirements.required_evidence must be non-empty list"
        )
        return

    snapshot_required = False
    snapshot_variables: dict[str, str] = {}
    snapshot_time_variable = ""
    snapshot_time_shape_expr = "scalar"

    for idx, item in enumerate(required_evidence):
        if not isinstance(item, dict):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}] must be object"
            )
            continue
        raw_artifact = item.get("artifact")
        if not isinstance(raw_artifact, str) or not raw_artifact.strip():
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].artifact must be non-empty string"
            )
            continue

        artifact = _normalize_raw_evidence_artifact(raw_artifact)
        if artifact is None:
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].artifact {raw_artifact!r} "
                f"must be one of {sorted(RAW_EVIDENCE_ARTIFACTS)}"
            )
            continue

        required_value = item.get("required")
        if required_value is not None and not isinstance(required_value, bool):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].required must be bool when present"
            )

        min_samples = item.get("min_samples")
        if min_samples is not None and (
            not isinstance(min_samples, int) or min_samples < 1
        ):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].min_samples must be integer >= 1 when present"
            )

        if artifact != "state_snapshots":
            continue

        if item.get("required") is not False:
            snapshot_required = True

        schema = item.get("schema")
        if schema is None:
            continue
        if not isinstance(schema, dict):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema must be object"
            )
            continue

        raw_variables = schema.get("variables")
        if raw_variables is None and item.get("required") is not False:
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables must be non-empty list when state_snapshots is required"
            )
        elif raw_variables is not None:
            if not isinstance(raw_variables, list) or not raw_variables:
                violations.append(
                    f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables must be non-empty list when present"
                )
            else:
                for var_idx, variable in enumerate(raw_variables):
                    if not isinstance(variable, dict):
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}] must be object"
                        )
                        continue
                    raw_name = variable.get("name")
                    raw_shape_expr = variable.get("shape_expr")
                    if not isinstance(raw_name, str) or not raw_name.strip():
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}].name must be non-empty string"
                        )
                        continue
                    if not isinstance(raw_shape_expr, str) or not raw_shape_expr.strip():
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}].shape_expr must be non-empty string"
                        )
                        continue
                    ok_shape, _, shape_err = _parse_shape_expr(raw_shape_expr)
                    if not ok_shape:
                        violations.append(
                            f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.variables[{var_idx}].shape_expr invalid ({shape_err})"
                        )
                        continue
                    snapshot_variables[raw_name.strip()] = _canonical_shape_expr(raw_shape_expr)

        if "state_variables" in schema:
            # Legacy shorthand: rejected uniformly. `schema.variables` (the
            # canonical form with explicit per-variable shape_expr) is the
            # only accepted source of state-snapshot shape contracts.
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema "
                "must not use 'state_variables' shorthand; use 'variables: [{name, shape_expr}, ...]' instead"
            )
        raw_time_var = schema.get("time_variable")
        if raw_time_var is not None and (
            not isinstance(raw_time_var, str) or not raw_time_var.strip()
        ):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.time_variable must be non-empty string when present"
            )
        elif isinstance(raw_time_var, str) and raw_time_var.strip():
            snapshot_time_variable = raw_time_var.strip()

        raw_time_shape = schema.get("time_shape_expr")
        if raw_time_shape is not None and (
            not isinstance(raw_time_shape, str) or not raw_time_shape.strip()
        ):
            violations.append(
                f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.time_shape_expr must be non-empty string when present"
            )
        elif isinstance(raw_time_shape, str) and raw_time_shape.strip():
            ok_shape, _, shape_err = _parse_shape_expr(raw_time_shape)
            if not ok_shape:
                violations.append(
                    f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.time_shape_expr invalid ({shape_err})"
                )
            else:
                snapshot_time_shape_expr = _canonical_shape_expr(raw_time_shape)
                # The per-snapshot time index (e.g. snapshot_index) is canonically a
                # SCALAR loop counter — the runner always emits it as a scalar. A `[1]`
                # mis-declaration here makes post_execute fail ("shape [] does not match
                # declared time_shape_expr [1]"); reject it at the source (compile) so the
                # IR regenerates to scalar instead of looping execute. See C1 in
                # docs/design/deterministic_followups.md.
                if snapshot_time_shape_expr != "scalar":
                    violations.append(
                        f"{contract_path}:raw_requirements.required_evidence[{idx}].schema.time_shape_expr "
                        f"for the per-snapshot time index must be \"scalar\" (got {raw_time_shape!r})"
                    )

    if snapshot_required and not snapshot_variables:
        violations.append(
            f"{contract_path}:state_snapshots schema must declare variables with shape_expr when required"
        )
    if snapshot_required and not snapshot_time_variable:
        violations.append(
            f"{contract_path}:state_snapshots schema must declare time_variable when required"
        )
    snapshot_reference_variables = set(snapshot_variables)
    if snapshot_time_variable:
        snapshot_reference_variables.add(snapshot_time_variable)

    for idx, item in output_items:
        evidence_ref = item.get("evidence_ref")
        has_snapshot_ref = isinstance(evidence_ref, str) and "state_snapshots" in evidence_ref

        raw_variables = item.get("raw_variables")
        if has_snapshot_ref:
            if not isinstance(raw_variables, list) or not raw_variables:
                violations.append(
                    f"{contract_path}:io_contract.outputs[{idx}].raw_variables must be non-empty list when evidence_ref references state_snapshots"
                )
                continue
        elif snapshot_required:
            if not isinstance(raw_variables, list) or not raw_variables:
                violations.append(
                    f"{contract_path}:io_contract.outputs[{idx}].raw_variables must be non-empty list when evidence_ref is non-snapshot and state_snapshots is required"
                )
                continue
            
        if not isinstance(raw_variables, list):
            continue

        referenced_shapes: set[str] = set()
        unknown_variables: list[str] = []
        for var_idx, token in enumerate(raw_variables):
            if not isinstance(token, str) or not token.strip():
                violations.append(
                    f"{contract_path}:io_contract.outputs[{idx}].raw_variables[{var_idx}] must be non-empty string"
                )
                continue
            name = token.strip()
            if name == snapshot_time_variable:
                referenced_shapes.add(snapshot_time_shape_expr)
            elif name in snapshot_variables:
                referenced_shapes.add(snapshot_variables[name])
            else:
                unknown_variables.append(name)
        if unknown_variables:
            violations.append(
                f"{contract_path}:io_contract.outputs[{idx}].raw_variables must reference declared state_snapshots variables/time_variable ({sorted(set(unknown_variables))})"
            )
            continue
        if has_snapshot_ref and referenced_shapes:
            shape_expr = item.get("shape_expr")
            if isinstance(shape_expr, str) and shape_expr.strip() and len(referenced_shapes) == 1:
                declared_shape = _canonical_shape_expr(shape_expr)
                expected_shape = next(iter(referenced_shapes))
                if declared_shape != expected_shape:
                    violations.append(
                        f"{contract_path}:io_contract.outputs[{idx}].shape_expr must match referenced state_snapshots schema shape ({expected_shape})"
                    )
            elif isinstance(shape_expr, str) and shape_expr.strip() and len(referenced_shapes) > 1:
                violations.append(
                    f"{contract_path}:io_contract.outputs[{idx}].shape_expr must resolve to a single referenced state_snapshots variable/time_variable shape"
                )

    if "numerical_kernel_contract" in contract:
        violations.append(
            f"{contract_path}:numerical_kernel_contract must not appear in spec.ir.yaml; move generation contract to spec.ir.yaml"
        )

    _validate_test_evidence_requirements(
        tests_path=tests_path,
        contract_path=contract_path,
        contract=contract,
        snapshot_reference_variables=snapshot_reference_variables,
        snapshot_required=snapshot_required,
        violations=violations,
    )

    _validate_diagnostics_contract(contract_path, contract, violations)


_DIAGNOSTICS_CONTRACT_ABSENT = object()


def _raw_diagnostics_contract_value(contract: dict[str, Any]) -> Any:
    """Return the raw diagnostics_contract value (any type), or the sentinel
    ``_DIAGNOSTICS_CONTRACT_ABSENT`` when the key is not present at all.

    Accepts both the lifted form (top-level diagnostics_contract) and the raw
    nested form (io_contract.diagnostics_contract). Unlike
    _diagnostics_contract_section, this preserves the distinction between
    "absent" and "present but malformed (non-object)" so the structural
    validator can flag the latter instead of silently skipping it.
    """
    if "diagnostics_contract" in contract:
        return contract["diagnostics_contract"]
    io_contract = contract.get("io_contract")
    if isinstance(io_contract, dict) and "diagnostics_contract" in io_contract:
        return io_contract["diagnostics_contract"]
    return _DIAGNOSTICS_CONTRACT_ABSENT


def _diagnostics_contract_section(contract: dict[str, Any]) -> dict[str, Any] | None:
    """Return the diagnostics_contract dict from a (lifted) io_contract, or None.

    Accepts both the lifted form (where _validate_io_contract_file hoists
    diagnostics_contract to the top level) and the raw nested form
    (spec.ir.yaml.io_contract.diagnostics_contract). A present-but-malformed
    (non-object) value normalizes to None here; the structural validator
    (_validate_diagnostics_contract) is responsible for flagging that case.
    """
    section = _raw_diagnostics_contract_value(contract)
    return section if isinstance(section, dict) else None


def _diagnostics_contract_check_ids(contract: Any) -> list[str]:
    """Return the declared checks[].id list (empty when absent/malformed).

    Tolerates a non-dict `contract` (returns `[]`) so callers reading an IR that `_read_yaml`
    may have produced as a non-mapping need not repeat an `isinstance` guard."""
    if not isinstance(contract, dict):
        return []
    section = _diagnostics_contract_section(contract)
    if section is None:
        return []
    checks = section.get("checks")
    ids: list[str] = []
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict):
                cid = item.get("id")
                if isinstance(cid, str) and cid.strip():
                    ids.append(cid.strip())
    return ids


def _diagnostics_contract_verdict_fields(contract: dict[str, Any]) -> list[str]:
    """Return verdict.fields when verdict.required is true, else empty list."""
    section = _diagnostics_contract_section(contract)
    if section is None:
        return []
    verdict = section.get("verdict")
    if not isinstance(verdict, dict) or verdict.get("required") is not True:
        return []
    fields = verdict.get("fields")
    out: list[str] = []
    if isinstance(fields, list):
        for field in fields:
            if isinstance(field, str) and field.strip():
                out.append(field.strip())
    return out


def _validate_diagnostics_contract(
    contract_path: Path, contract: dict[str, Any], violations: list[str]
) -> None:
    """Structural validation of io_contract.diagnostics_contract when present.

    Presence is optional (a node whose tests.md has no §3 diagnostics contract
    omits it); coverage against tests.md §3 is the LLM Compile.verify
    responsibility (hybrid verification). Here we only enforce well-formedness:
    checks must be a non-empty list of {id: <non-empty string>}, and when a
    verdict block is present it must carry required: bool and (when
    required=true) a non-empty fields list of strings.
    """
    raw = _raw_diagnostics_contract_value(contract)
    if raw is _DIAGNOSTICS_CONTRACT_ABSENT:
        return
    if not isinstance(raw, dict):
        violations.append(
            f"{contract_path}:io_contract.diagnostics_contract must be object when present"
        )
        return
    section = raw

    checks = section.get("checks")
    if not isinstance(checks, list) or not checks:
        violations.append(
            f"{contract_path}:io_contract.diagnostics_contract.checks must be non-empty list when diagnostics_contract is present"
        )
    else:
        seen_ids: set[str] = set()
        for idx, item in enumerate(checks):
            if not isinstance(item, dict):
                violations.append(
                    f"{contract_path}:io_contract.diagnostics_contract.checks[{idx}] must be object"
                )
                continue
            cid = item.get("id")
            if not isinstance(cid, str) or not cid.strip():
                violations.append(
                    f"{contract_path}:io_contract.diagnostics_contract.checks[{idx}].id must be non-empty string"
                )
                continue
            if cid.strip() in seen_ids:
                violations.append(
                    f"{contract_path}:io_contract.diagnostics_contract.checks[{idx}].id duplicate ({cid.strip()})"
                )
            seen_ids.add(cid.strip())

    verdict = section.get("verdict")
    if verdict is not None:
        if not isinstance(verdict, dict):
            violations.append(
                f"{contract_path}:io_contract.diagnostics_contract.verdict must be object when present"
            )
        else:
            required_value = verdict.get("required")
            if not isinstance(required_value, bool):
                violations.append(
                    f"{contract_path}:io_contract.diagnostics_contract.verdict.required must be bool"
                )
            fields = verdict.get("fields")
            if required_value is True:
                if not isinstance(fields, list) or not fields:
                    violations.append(
                        f"{contract_path}:io_contract.diagnostics_contract.verdict.fields must be non-empty list when verdict.required is true"
                    )
                else:
                    for f_idx, field in enumerate(fields):
                        if not isinstance(field, str) or not field.strip():
                            violations.append(
                                f"{contract_path}:io_contract.diagnostics_contract.verdict.fields[{f_idx}] must be non-empty string"
                            )


def _validate_diagnostics_contract_output(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    """At post_execute/pre_judge, verify diagnostics.json satisfies the IR's
    io_contract.diagnostics_contract (the tests.md §3 contract encoded in the IR).

    When the contract declares checks[].id, diagnostics.json must carry a top-level
    `checks` object holding each id. When verdict.required is true, diagnostics.json
    must carry a top-level `verdict` object holding the declared fields. This catches
    the structural_violation earlier than the LLM judge. A node without a
    diagnostics_contract is unaffected.
    """
    contract = _io_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return
    check_ids = _diagnostics_contract_check_ids(contract)
    verdict_fields = _diagnostics_contract_verdict_fields(contract)
    if not check_ids and not verdict_fields:
        return

    diagnostics_path = execution.node_dir / "diagnostics.json"
    if not diagnostics_path.exists():
        # presence of diagnostics.json itself is already required by _validate_raw_evidence
        return
    try:
        diagnostics = _read_json(diagnostics_path)
    except json.JSONDecodeError:
        # invalid json already reported elsewhere
        return
    if not isinstance(diagnostics, dict):
        violations.append(f"{diagnostics_path}: must be json object")
        return

    if check_ids:
        checks = diagnostics.get("checks")
        if not isinstance(checks, dict):
            violations.append(
                f"{diagnostics_path}:checks must be an object holding the io_contract.diagnostics_contract checks ({sorted(check_ids)})"
            )
        else:
            missing = [cid for cid in check_ids if cid not in checks]
            if missing:
                violations.append(
                    f"{diagnostics_path}:checks missing io_contract.diagnostics_contract ids ({sorted(missing)})"
                )

    if verdict_fields:
        verdict = diagnostics.get("verdict")
        if not isinstance(verdict, dict):
            violations.append(
                f"{diagnostics_path}:verdict must be an object holding io_contract.diagnostics_contract.verdict.fields ({sorted(verdict_fields)})"
            )
        else:
            missing_fields = [f for f in verdict_fields if f not in verdict]
            if missing_fields:
                violations.append(
                    f"{diagnostics_path}:verdict missing io_contract.diagnostics_contract.verdict.fields ({sorted(missing_fields)})"
                )


def _validate_io_contract_schema(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    contract_path = _ir_document_path_for_execution(repo_root, execution)
    if contract_path is None:
        violations.append(
            f"{execution.pipeline_dir / 'lineage.json'}: ir_ref missing; cannot resolve spec.ir.yaml"
        )
        return
    if not contract_path.exists():
        violations.append(f"{contract_path}: missing")
        return
    _validate_io_contract_file(repo_root, contract_path, violations)


def _extract_spec_var_names(derived_path: Path) -> set[str] | None:
    """Return spec-traceable variable names from spec.ir.yaml for provenance checks.

    Collects names from io_contract.inputs/outputs AND raw_requirements evidence schema
    variables — the union of these two sets constitutes the full set of variables that
    can be legitimately traced back to the external spec or evidence artifacts.

    Returns None when the source is unreliable (parse error or non-dict items found in
    io_contract), which signals the caller to skip provenance checking rather than
    hard-fail with an incomplete symbol set.
    """
    try:
        data = _read_yaml(derived_path)
    except (json.JSONDecodeError, yaml.YAMLError, OSError):
        try:
            data = _read_json(derived_path)
        except (json.JSONDecodeError, OSError):
            return None
    if not isinstance(data, dict):
        return None
    # Unwrap the new IR layout where io_contract.{inputs,outputs} live inside
    # the spec.ir.yaml `io_contract:` section.  When already in legacy flat
    # form we'll see `inputs`/`outputs` at the root of `io_contract`.
    ic = data.get("io_contract")
    if isinstance(ic, dict) and isinstance(ic.get("io_contract"), dict):
        # Already double-nested: drill in once more
        ic = ic["io_contract"]
    elif isinstance(ic, dict) and "inputs" not in ic and "outputs" not in ic:
        # io_contract section but inputs/outputs not visible at this level —
        # try the section itself (the new IR sometimes stores inputs/outputs
        # as siblings of raw_requirements at the same level)
        pass
    if not isinstance(ic, dict):
        return None
    names: set[str] = set()
    for key in ("inputs", "outputs"):
        items = ic.get(key)
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                # Non-dict item means the schema is malformed; skip provenance to avoid
                # false failures (the io_contract validator will flag this separately).
                return None
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                return None
            names.add(name.strip())
    # Also collect variables declared in the state_snapshots evidence schema. These are
    # spec-internal state variables (e.g. h, hu, hv) that are externally evidenced even
    # though they don't appear in the component's public io_contract interface. Only
    # state_snapshots entries are considered because derived-contract validation enforces
    # schema.variables structure exclusively for that artifact type; schemas on other
    # artifact entries are unchecked and could inject arbitrary names.
    # raw_requirements may live at the root (legacy derived_contract.json) or
    # nested under spec.ir.yaml.io_contract.raw_requirements (new IR).
    rr = data.get("raw_requirements")
    if not isinstance(rr, dict):
        io_section = data.get("io_contract")
        if isinstance(io_section, dict):
            rr = io_section.get("raw_requirements")
    if isinstance(rr, dict):
        evidence_list = rr.get("required_evidence")
        if isinstance(evidence_list, list):
            for ev in evidence_list:
                if not isinstance(ev, dict):
                    continue
                if ev.get("artifact") != "state_snapshots":
                    continue
                schema = ev.get("schema")
                if not isinstance(schema, dict):
                    continue
                variables = schema.get("variables")
                if not isinstance(variables, list):
                    continue
                for var in variables:
                    if not isinstance(var, dict):
                        continue
                    n = var.get("name")
                    if isinstance(n, str) and n.strip():
                        names.add(n.strip())
    return names


def _validate_algorithm_contract_file(
    repo_root: Path,
    contract_path: Path,
    violations: list[str],
    *,
    multidim_node_key: str | None,
    direct_spec_vars: set[str] | None = None,
) -> None:
    try:
        contract = _read_yaml(contract_path)
    except yaml.YAMLError:
        violations.append(f"{contract_path}: invalid yaml")
        return

    if not isinstance(contract, dict):
        violations.append(f"{contract_path}: must be mapping")
        return

    # Unified IR: spec.ir.yaml has `algorithm:` key at the top.  Read the
    # algorithm section if present; otherwise fall back to a flat document
    # (still legal for tests that write the algorithm block at the root).
    # No leaf-authored shape may reach `_require_ir_section`'s raise below: the conductor turns a
    # non-zero exit of this gate into a `compile_static_violation` and hands the leaf the last 50
    # lines as its repair findings, so a traceback would arrive as an unrepairable finding. Every
    # shape an IR can take is reported as a violation here instead.
    section = contract.get("algorithm")
    if isinstance(section, dict):
        # `_require_ir_section` tells a section from the document by key, so a section that happens
        # to carry a document-level key would trip its raise. Drop those keys for this read instead
        # of rejecting the IR: unknown keys are tolerated everywhere else in the IR, and a gate must
        # not enforce a rule (`algorithm:` may not carry `schema_version`) that no canonical
        # document states to the compile author.
        markers = {"algorithm", *_IR_DOCUMENT_ONLY_KEYS} & set(section)
        contract = (
            {k: v for k, v in section.items() if k not in markers} if markers else section
        )
    elif "algorithm" in contract or "schema_version" in contract:
        violations.append(f"{contract_path}:algorithm section missing or not a mapping")
        return

    algorithm_id = contract.get("algorithm_id")
    if not isinstance(algorithm_id, str) or not algorithm_id.strip():
        violations.append(f"{contract_path}:algorithm_id must be non-empty string")

    execution_mode = contract.get("execution_mode")
    if execution_mode not in ALGORITHM_EXECUTION_MODES:
        violations.append(
            f"{contract_path}:execution_mode must be one of {sorted(ALGORITHM_EXECUTION_MODES)}"
        )

    steps = contract.get("steps")
    step_ids: set[str] = set()
    if not isinstance(steps, list) or not steps:
        violations.append(f"{contract_path}:steps must be non-empty list")
    else:
        for idx, item in enumerate(steps):
            if not isinstance(item, dict):
                violations.append(f"{contract_path}:steps[{idx}] must be object")
                continue
            step_id = item.get("step_id")
            if not isinstance(step_id, str) or not step_id.strip():
                violations.append(f"{contract_path}:steps[{idx}].step_id must be non-empty string")
            else:
                step_ids.add(step_id.strip())

            step_kind = item.get("step_kind")
            if step_kind not in ALGORITHM_STEP_KINDS:
                violations.append(
                    f"{contract_path}:steps[{idx}].step_kind must be one of {sorted(ALGORITHM_STEP_KINDS)}"
                )

            operation_ref = item.get("operation_ref")
            if not isinstance(operation_ref, str) or not operation_ref.strip():
                violations.append(
                    f"{contract_path}:steps[{idx}].operation_ref must be non-empty string"
                )

            for field_name in ("inputs", "outputs"):
                raw_value = item.get(field_name)
                if (
                    not isinstance(raw_value, list)
                    or not raw_value
                    or not all(isinstance(token, str) and token.strip() for token in raw_value)
                ):
                    violations.append(
                        f"{contract_path}:steps[{idx}].{field_name} must be non-empty string list"
                    )

    ordering = contract.get("ordering")
    if not isinstance(ordering, list):
        violations.append(f"{contract_path}:ordering must be list")
    elif len(step_ids) > 1 and not ordering:
        violations.append(f"{contract_path}:ordering must be non-empty when steps has multiple entries")
    else:
        for idx, item in enumerate(ordering):
            if isinstance(item, str):
                token = item.strip()
                if not token:
                    violations.append(f"{contract_path}:ordering[{idx}] must be non-empty string when scalar")
                elif step_ids and token not in step_ids:
                    violations.append(f"{contract_path}:ordering[{idx}] must reference known step_id")
                continue
            if not isinstance(item, dict):
                violations.append(f"{contract_path}:ordering[{idx}] must be string or object")
                continue
            before = item.get("before")
            after = item.get("after")
            if not isinstance(before, str) or not before.strip():
                violations.append(f"{contract_path}:ordering[{idx}].before must be non-empty string")
            elif step_ids and before.strip() not in step_ids:
                violations.append(f"{contract_path}:ordering[{idx}].before must reference known step_id")
            if not isinstance(after, str) or not after.strip():
                violations.append(f"{contract_path}:ordering[{idx}].after must be non-empty string")
            elif step_ids and after.strip() not in step_ids:
                violations.append(f"{contract_path}:ordering[{idx}].after must reference known step_id")

    control_condition = contract.get("control_condition")
    if not isinstance(control_condition, (str, list, dict)):
        violations.append(f"{contract_path}:control_condition must be string, list, or object")
    elif execution_mode == "conditional":
        is_empty = (
            (isinstance(control_condition, str) and not control_condition.strip())
            or (isinstance(control_condition, list) and not control_condition)
            or (isinstance(control_condition, dict) and not control_condition)
        )
        if is_empty:
            violations.append(f"{contract_path}:control_condition must be non-empty when execution_mode=conditional")

    iteration_contract = contract.get("iteration_contract")
    if not isinstance(iteration_contract, dict):
        violations.append(f"{contract_path}:iteration_contract must be object")
    elif execution_mode == "iterative" and not iteration_contract:
        violations.append(f"{contract_path}:iteration_contract must be non-empty when execution_mode=iterative")

    update_semantics = contract.get("update_semantics")
    if not isinstance(update_semantics, dict):
        violations.append(f"{contract_path}:update_semantics must be object")

    temporaries = contract.get("temporaries")
    if not isinstance(temporaries, list):
        violations.append(f"{contract_path}:temporaries must be list")
    else:
        for idx, item in enumerate(temporaries):
            if isinstance(item, str):
                if not item.strip():
                    violations.append(f"{contract_path}:temporaries[{idx}] must be non-empty string when scalar")
                continue
            if not isinstance(item, dict):
                violations.append(f"{contract_path}:temporaries[{idx}] must be string or object")
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                violations.append(f"{contract_path}:temporaries[{idx}].name must be non-empty string")
            if "shape_expr" not in item:
                violations.append(
                    f"{contract_path}:temporaries[{idx}].shape_expr is required for object-form entries "
                    "(canonical source: spec/schema/ir/shape_expr.schema.json)"
                )
            else:
                shape_expr = item.get("shape_expr")
                if (
                    not isinstance(shape_expr, str)
                    or not shape_expr.strip()
                    or not _parse_shape_expr(shape_expr)[0]
                ):
                    violations.append(f"{contract_path}:temporaries[{idx}].shape_expr invalid")

    derived_field_rules = contract.get("derived_field_rules")
    if not isinstance(derived_field_rules, list):
        violations.append(f"{contract_path}:derived_field_rules must be list")

    # Provenance check: every token in steps[*].inputs/outputs must be traceable to
    # direct spec I/O (from spec.ir.yaml), temporaries, or derived_field_rules.
    # Only performed when direct_spec_vars is provided (plan-stage with spec.ir.yaml).
    if direct_spec_vars is not None and isinstance(steps, list):
        tmp_names: set[str] = set()
        if isinstance(temporaries, list):
            for item in temporaries:
                if isinstance(item, str) and item.strip():
                    tmp_names.add(item.strip())
                elif isinstance(item, dict):
                    n = item.get("name")
                    if isinstance(n, str) and n.strip():
                        tmp_names.add(n.strip())
        dfr_names: set[str] = set()
        if isinstance(derived_field_rules, list):
            for item in derived_field_rules:
                if isinstance(item, dict):
                    n = item.get("name")
                    if isinstance(n, str) and n.strip():
                        dfr_names.add(n.strip())
        allowed_tokens = direct_spec_vars | tmp_names | dfr_names
        for step_idx, item in enumerate(steps):
            if not isinstance(item, dict):
                continue
            for field_name in ("inputs", "outputs"):
                raw_value = item.get(field_name)
                if not isinstance(raw_value, list):
                    continue
                for tok_idx, token in enumerate(raw_value):
                    if not isinstance(token, str):
                        continue
                    stripped = token.strip()
                    if stripped and stripped not in allowed_tokens:
                        violations.append(
                            f"{contract_path}:steps[{step_idx}].{field_name}[{tok_idx}]"
                            f" token '{stripped}' is not traceable to direct spec I/O,"
                            f" temporaries, or derived_field_rules (undefined binding)"
                        )

    invariants = contract.get("invariants")
    if not isinstance(invariants, list):
        violations.append(f"{contract_path}:invariants must be list")
    elif not all(isinstance(item, str) and item.strip() for item in invariants):
        violations.append(f"{contract_path}:invariants must be non-empty string list")

    splitting_policy = contract.get("splitting_policy")
    if not isinstance(splitting_policy, dict):
        violations.append(f"{contract_path}:splitting_policy must be object")
    elif not isinstance(splitting_policy.get("kind"), str) or not splitting_policy.get("kind", "").strip():
        violations.append(f"{contract_path}:splitting_policy.kind must be non-empty string")

    if execution_mode == "columnwise":
        has_column_step = False
        if isinstance(steps, list):
            has_column_step = any(
                isinstance(item, dict) and item.get("step_kind") == "column_process"
                for item in steps
            )
        if not has_column_step:
            violations.append(
                f"{contract_path}:execution_mode=columnwise requires at least one column_process step"
            )

    if multidim_node_key and _is_multidim_problem_node_key(multidim_node_key):
        state_contract = _algorithm_state_contract(contract)
        if not isinstance(state_contract, dict):
            violations.append(
                f"{contract_path}:state_contract must be object for multidimensional problem node"
            )
            return

        state_variables = state_contract.get("state_variables")
        declared_state_names: set[str] = set()
        if not isinstance(state_variables, list) or not state_variables:
            violations.append(
                f"{contract_path}:state_contract.state_variables must be non-empty list"
            )
        else:
            for idx, item in enumerate(state_variables):
                if not isinstance(item, dict):
                    violations.append(
                        f"{contract_path}:state_contract.state_variables[{idx}] must be object"
                    )
                    continue
                name = item.get("name")
                shape_expr = item.get("shape_expr")
                if not isinstance(name, str) or not name.strip():
                    violations.append(
                        f"{contract_path}:state_contract.state_variables[{idx}].name must be non-empty string"
                    )
                else:
                    declared_state_names.add(name.strip())
                if not isinstance(shape_expr, str) or not shape_expr.strip():
                    violations.append(
                        f"{contract_path}:state_contract.state_variables[{idx}].shape_expr must be non-empty string"
                    )
                elif not _parse_shape_expr(shape_expr)[0]:
                    violations.append(
                        f"{contract_path}:state_contract.state_variables[{idx}].shape_expr invalid"
                    )

        update_paths = state_contract.get("required_update_paths")
        # `not update_paths` is load-bearing: `all()` over an empty list is True, so without it an
        # empty `required_update_paths: []` passed a check whose own message says "non-empty" — a
        # multidimensional problem that declares it updates nothing. The hole was unreachable while
        # the gate was dormant (see `_plan_dependency_node_key`); it is live now.
        if (
            not isinstance(update_paths, list)
            or not update_paths
            or not all(isinstance(token, str) and token.strip() for token in update_paths)
        ):
            violations.append(
                f"{contract_path}:state_contract.required_update_paths must be non-empty string list"
            )
        elif declared_state_names:
            # Each token NAMES a state variable — the shape rule alone would accept a typo, or a
            # diagnostic/temporary, as an update target: an update contract nothing can fulfil,
            # discovered only when Generate cannot resolve the name. Skipped when the declared set
            # is itself invalid, so the cause is reported once rather than cascading.
            unknown = [
                token.strip() for token in update_paths
                if token.strip() not in declared_state_names
            ]
            if unknown:
                violations.append(
                    f"{contract_path}:state_contract.required_update_paths must name declared "
                    f"state_variables ({sorted(set(unknown))})"
                )

        diagnostics_from_state = state_contract.get("diagnostics_from_state")
        if diagnostics_from_state is not True:
            violations.append(
                f"{contract_path}:state_contract.diagnostics_from_state must be true"
            )

        fallback_policy = state_contract.get("fallback_policy")
        if fallback_policy != "fail_closed":
            violations.append(
                f"{contract_path}:state_contract.fallback_policy must be fail_closed"
            )


def _validate_algorithm_contract_schema(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    contract_path = _ir_document_path_for_execution(repo_root, execution)
    if contract_path is None:
        violations.append(
            f"{execution.pipeline_dir / 'lineage.json'}: ir_ref missing; cannot resolve spec.ir.yaml"
        )
        return
    if not contract_path.exists():
        violations.append(f"{contract_path}: missing")
        return
    _validate_algorithm_contract_file(
        repo_root,
        contract_path,
        violations,
        multidim_node_key=execution.node_key,
    )


def _validate_test_evidence_requirements(
    tests_path: Path | None,
    contract_path: Path,
    contract: dict[str, Any],
    snapshot_reference_variables: set[str],
    snapshot_required: bool,
    violations: list[str],
) -> None:
    # The ref is LLM-authored: it may name a directory, an over-long path, or an unreadable one.
    # `_validate_ir_source_refs_tests` reports such a ref as the violation it is; this gate must not
    # read (or probe) it in a way that raises before the leaf gets that finding.
    if tests_path is None or not _is_readable_file(tests_path):
        return

    test_ids = _parse_test_ids_from_tests_md(tests_path)
    if not test_ids:
        return

    raw_reqs = contract.get("test_evidence_requirements")
    if not isinstance(raw_reqs, list) or not raw_reqs:
        violations.append(f"{contract_path}:test_evidence_requirements must be non-empty list")
        return

    seen_test_ids: set[str] = set()
    mapped_test_ids: set[str] = set()
    for idx, item in enumerate(raw_reqs):
        if not isinstance(item, dict):
            violations.append(
                f"{contract_path}:test_evidence_requirements[{idx}] must be object"
            )
            continue
        raw_test_id = item.get("test_id")
        if not isinstance(raw_test_id, str) or not raw_test_id.strip():
            violations.append(
                f"{contract_path}:test_evidence_requirements[{idx}].test_id must be non-empty string"
            )
            continue
        test_id = raw_test_id.strip()
        if test_id in seen_test_ids:
            violations.append(
                f"{contract_path}:test_evidence_requirements has duplicated test_id ({test_id})"
            )
            continue
        seen_test_ids.add(test_id)
        mapped_test_ids.add(test_id)

        raw_variables = item.get("required_raw_variables")
        if not isinstance(raw_variables, list) or not raw_variables:
            violations.append(
                f"{contract_path}:test_evidence_requirements[{idx}].required_raw_variables must be non-empty list"
            )
            continue
        for var_idx, token in enumerate(raw_variables):
            if not isinstance(token, str) or not token.strip():
                violations.append(
                    f"{contract_path}:test_evidence_requirements[{idx}].required_raw_variables[{var_idx}] must be non-empty string"
                )
                continue
            name = token.strip()
            if snapshot_required and name not in snapshot_reference_variables:
                violations.append(
                    f"{contract_path}:test_evidence_requirements[{idx}].required_raw_variables[{var_idx}] must reference declared state_snapshots variable/time_variable ({name})"
                )

    expected = set(test_ids)
    missing = sorted(expected - mapped_test_ids)
    extra = sorted(mapped_test_ids - expected)
    if missing:
        violations.append(
            f"{contract_path}:test_evidence_requirements missing tests from tests.md ({missing})"
        )
    if extra:
        violations.append(
            f"{contract_path}:test_evidence_requirements has unknown test_id ({extra})"
        )


def _validate_metrics_basis_per_test(
    repo_root: Path,
    execution: NodeExecution,
    metrics_basis: dict[str, Any],
    violations: list[str],
) -> None:
    contract = _io_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return

    raw_requirements = _raw_requirements_for_execution(repo_root, execution)
    if not isinstance(raw_requirements, dict):
        return

    required_evidence = raw_requirements.get("required_evidence")
    metrics_basis_required = False
    if isinstance(required_evidence, list):
        for item in required_evidence:
            if not isinstance(item, dict):
                continue
            raw_artifact = item.get("artifact")
            if not isinstance(raw_artifact, str):
                continue
            if _normalize_raw_evidence_artifact(raw_artifact) != "metrics_basis.json":
                continue
            metrics_basis_required = item.get("required") is not False
            break
    if not metrics_basis_required:
        return

    test_requirements = _contract_test_evidence_requirements(contract)
    if not test_requirements:
        return

    metrics_basis_path = execution.node_dir / "raw" / "metrics_basis.json"
    entries, problems, form = _metrics_basis_entries(metrics_basis)
    for problem in problems:
        violations.append(f"{metrics_basis_path}: {problem}")
    if problems:
        return

    # The expected evidence is the test x target_case MATRIX: one entry per case each test's
    # predicate ranges over. `test_predicates[].target_cases` is the anchor the host-rendered
    # runner emits from (`runner_renderer._target_cases`), so both sides read one field.
    #
    # The row SET is `test_requirements`, whose source (`_contract_test_evidence_requirements`)
    # drops a test declaring an EMPTY `required_raw_variables` while the renderer's
    # `_test_evidence` keeps it — so such a test would render a row this matrix calls "unknown".
    # That IR never reaches here: `_validate_test_evidence_requirements` rejects an empty
    # `required_raw_variables` outright. Should that ever be relaxed, the two filters must be
    # reconciled rather than left to disagree.
    test_to_cases = _test_id_to_case_ids(contract)
    expected_keys: set[tuple[str, str]] = set()
    untargeted_tests: list[str] = []
    multi_target_tests: list[str] = []
    for test_id in test_requirements:
        target_cases = test_to_cases.get(test_id) or []
        if not target_cases:
            untargeted_tests.append(test_id)
            continue
        if len(target_cases) > 1:
            multi_target_tests.append(test_id)
        for case_id in target_cases:
            expected_keys.add((test_id, case_id))
    if untargeted_tests:
        violations.append(
            f"{metrics_basis_path}: test_id {sorted(untargeted_tests)} declare "
            "required_raw_variables but no io_contract.test_predicates[].target_cases — the "
            "expected (test_id, case_id) evidence rows cannot be derived"
        )
        return
    if form == "tests" and multi_target_tests:
        # A `tests` object is keyed by test_id, so it physically cannot hold the several rows a
        # multi-target test owes. Say so instead of reporting the rows as merely "missing".
        violations.append(
            f"{metrics_basis_path}: the deprecated `tests` object form is keyed by test_id and "
            f"cannot express the multiple (test_id, case_id) rows owed by {sorted(multi_target_tests)}; "
            "emit a `per_test` LIST with one entry per (test_id, case_id)"
        )
        return

    actual_keys = set(entries)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if missing:
        violations.append(
            f"{metrics_basis_path}: missing per-test evidence for (test_id, case_id) ({missing})"
        )
    if extra:
        violations.append(
            f"{metrics_basis_path}: has unknown per-test evidence (test_id, case_id) ({extra})"
        )

    for (test_id, case_id), entry in sorted(entries.items()):
        required_variables = test_requirements.get(test_id)
        if required_variables is None or not isinstance(entry, dict):
            continue
        variable_keys = _metrics_basis_variable_keys(entry)
        missing_variables = sorted(required_variables - variable_keys)
        if missing_variables:
            message = (
                f"{metrics_basis_path}: test_id {test_id} case_id {case_id} "
                f"missing required_raw_variables ({missing_variables})"
            )
            found = _metrics_basis_unrecognized_wrapper(
                entry, missing_variables, required_variables
            )
            if found is not None:
                wrapper, covered = found
                # Naming the whole missing set would lie when a second wrapper
                # holds the rest; only claim what this wrapper actually hides.
                subject = (
                    "the missing variables"
                    if covered == missing_variables
                    else f"the missing variables {covered}"
                )
                message += (
                    f" — {subject} are nested under the unrecognized wrapper key '{wrapper}'; "
                    "emit each required variable as a direct sibling key of test_id "
                    f'(e.g. {{"test_id": ..., "{covered[0]}": ...}}); '
                    f"do not wrap them under '{wrapper}'"
                )
            violations.append(message)


def _component_dep_spec_ids(repo_root: Path, execution: NodeExecution) -> list[str]:
    dep_data = _dependency_resolved_for_execution(repo_root, execution)
    if dep_data is None:
        return []

    direct_deps = dep_data.get("direct_deps")
    if not isinstance(direct_deps, list):
        return []

    result: list[str] = []
    for item in direct_deps:
        dep_token: str | None = None
        if isinstance(item, str):
            dep_token = item
        elif isinstance(item, dict):
            node_key = item.get("node_key")
            if isinstance(node_key, str):
                dep_token = node_key

        if not isinstance(dep_token, str):
            continue
        # Expected format: component/<spec_id>@<spec_version>
        if not dep_token.startswith("component/"):
            continue
        body = dep_token[len("component/") :]
        spec_id = body.split("@", 1)[0].strip()
        if spec_id:
            result.append(spec_id)
    return sorted(set(result))


def _dep_node_key_tokens(node: Any) -> list[str]:
    tokens: list[str] = []
    if isinstance(node, str):
        token = node.strip()
        if token:
            tokens.append(_normalize_node_key_token(token))
    elif isinstance(node, dict):
        raw = node.get("node_key")
        if isinstance(raw, str):
            token = raw.strip()
            if token:
                tokens.append(_normalize_node_key_token(token))
    return tokens


def _dependency_expected_node_keys(dep_data: dict[str, Any]) -> set[str]:
    expected: set[str] = set()

    all_nodes = dep_data.get("all_nodes")
    has_all_nodes = isinstance(all_nodes, list)
    if has_all_nodes:
        for item in all_nodes:
            expected.update(_dep_node_key_tokens(item))

    node_key = dep_data.get("node_key")
    if isinstance(node_key, str) and node_key.strip():
        expected.update(_dep_node_key_tokens(node_key))

    # Fall back to the directly-read deps when `all_nodes` is ABSENT — keyed on the presence
    # of `all_nodes`, NOT on `expected` being empty. When `all_nodes` is present it IS the
    # authoritative complete closure (the conductor-authored dependency_graph.json sidecar,
    # merged in by the callers), so it is trusted exactly — even a bare `[self]` for a leaf.
    # But when `all_nodes` is missing (a sidecar that is absent/corrupt, or a pre-sidecar IR),
    # the IR block carries only `node_key` + `direct_deps`; without this a node WITH real
    # dependencies would collapse to a self-only set (node_key makes `expected` non-empty and
    # the old `if not expected` guard skipped the fallback), silently bypassing the
    # DAG-completeness / pre-spawn readiness gate. Requiring at least the direct deps keeps
    # that degenerate state fail-closed.
    if not has_all_nodes:
        for field in ("direct_deps", "transitive_deps"):
            deps = dep_data.get(field)
            if not isinstance(deps, list):
                continue
            for item in deps:
                expected.update(_dep_node_key_tokens(item))

    return expected


def _dependency_run_token(dep_data: dict[str, Any]) -> str | None:
    resolved_at = dep_data.get("resolved_at")
    if isinstance(resolved_at, str) and resolved_at.strip():
        return resolved_at.strip()
    return None


_NODE_KEY_TOKEN_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _closure_node_validated_in_own_pipeline(repo_root: Path, normalized_token: str) -> bool:
    """True iff a closure node (a normalized ``<kind>/<spec_id>`` token, version-agnostic to
    match the DAG check's tokens) has its OWN fully-validated pipeline elsewhere in the
    workspace — i.e. some ``workspace/pipelines/<kind>__<spec_id>__*/<pipe>`` that carries a
    ``binary/*/binary_meta.json`` with ``verification_status == pass`` AND a
    ``runs/**/aggregate_verdict.json`` (``pass``/``xfail``) whose sibling ``trial_meta.json`` binds
    it (``source_binary_id``) to that SAME passing binary. The binary↔verdict binding prevents
    combining a passing binary from one attempt with an unrelated verdict from another (the
    cross-run mixing the readiness gate rejects via ``bound_to_binary_id``; Codex round 24).

    Rationale: the ``--with-deps`` orchestration model runs each dependency node as a SEPARATE
    orchestration/pipeline, then runs the dependent. The dependent's validation scope
    (``--pipeline-root`` / ``--run-id``) therefore does NOT contain the dependency's pipeline, so
    the DAG-completeness check below would wrongly flag a genuinely-completed dependency as a
    missing node workflow / plan / pipeline. A dependency that has its own VALIDATED pipeline is a
    completed workflow — accept it cross-pipeline. Only the ``resolved_at``-token-less
    ("validation scope") branch consults this; the per-token branch keeps its strict, single-scope
    semantics.

    Requiring the full built+validated chain (not a bare ``binary_meta`` field) is deliberate:
    it means only a node that genuinely completed its own ``compile→build→validate`` excuses the
    DAG requirement, so a stray/forged single-key ``binary_meta.json`` or a half-built leftover
    cannot. Freshness/version binding of the SPECIFIC resolved dependency is enforced separately
    at launch by the dependency-readiness gate (``orchestration_runtime._verify_dependency_
    readiness``); this check only certifies DAG completeness (the node ran), at the same
    version-agnostic granularity the surrounding DAG comparison already uses.
    """
    token = normalized_token.strip()
    if "/" not in token:
        return False
    kind, _, spec_id = token.partition("/")
    kind = kind.strip()
    spec_id = spec_id.strip()
    if not (kind and spec_id
            and _NODE_KEY_TOKEN_PART_RE.match(kind)
            and _NODE_KEY_TOKEN_PART_RE.match(spec_id)):
        return False
    pipelines_root = repo_root / "workspace" / "pipelines"
    if not pipelines_root.is_dir():
        return False

    def _passing_binary_ids(pipe: Path) -> set[str]:
        ids: set[str] = set()
        for meta_path in pipe.glob("binary/*/binary_meta.json"):
            if not meta_path.is_file():
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (isinstance(data, dict)
                    and str(data.get("verification_status", "")).strip().lower() == "pass"):
                ids.add(meta_path.parent.name)  # binary_id == the binary/<id>/ dir name
        return ids

    def _has_verdict_bound_to(pipe: Path, passing_binary_ids: set[str]) -> bool:
        # A pass/xfail aggregate_verdict counts ONLY when its sibling trial_meta.json binds it
        # (source_binary_id) to a PASSING binary in this pipeline. This prevents combining a
        # passing binary from one attempt with an unrelated verdict from another (the cross-run
        # mixing the readiness gate rejects via bound_to_binary_id; Codex round 24).
        for v_path in pipe.glob("runs/*/*/aggregate_verdict.json"):
            if not v_path.is_file():
                continue
            try:
                vdata = json.loads(v_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not (isinstance(vdata, dict)
                    and str(vdata.get("aggregate_verdict", "")).strip().lower() in ("pass", "xfail")):
                continue
            tm_path = v_path.parent / "trial_meta.json"
            try:
                tmdata = json.loads(tm_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            source_binary_id = tmdata.get("source_binary_id") if isinstance(tmdata, dict) else None
            if isinstance(source_binary_id, str) and source_binary_id in passing_binary_ids:
                return True
        return False

    for safe_dir in pipelines_root.glob(f"{kind}__{spec_id}__*"):
        if not safe_dir.is_dir():
            continue
        for pipe in safe_dir.iterdir():
            if not pipe.is_dir():
                continue
            passing_binary_ids = _passing_binary_ids(pipe)
            if passing_binary_ids and _has_verdict_bound_to(pipe, passing_binary_ids):
                return True
    return False


def _spec_id_from_node_key(node_key: str) -> str | None:
    if "/" not in node_key:
        return None
    body = node_key.split("/", 1)[1]
    spec_id = body.split("@", 1)[0].strip()
    return spec_id or None


def _model_files_in_src_dir(
    src_dir: Path, execution: NodeExecution
) -> tuple[list[Path], str | None]:
    spec_id = _spec_id_from_node_key(execution.node_key)
    if spec_id is None:
        return sorted(p for p in src_dir.glob("*_model.f90") if p.is_file()), None
    expected_name = f"{spec_id}_model.f90"
    candidate = src_dir / expected_name
    if candidate.exists():
        return [candidate], expected_name
    return [], expected_name


def _model_source_not_found_violation(
    src_dir: Path, expected_model_name: str | None
) -> str:
    """Build the violation message for an absent node model source.

    When ``expected_model_name`` is None the spec_id could not be derived, so the
    name is unknown and the message stays generic. Otherwise, distinguish the
    real causes: (a) the required literal module name ``<spec_id>_model`` exceeds
    the f2008 identifier limit, so no valid literal name exists and renaming
    cannot fix it; (b) no ``*_model.f90`` was emitted at all; (c) a model source
    exists but under a non-literal (abbreviated/derived) name. Case (c) is the
    common Generate mistake — the literal ``<spec_id>_model.f90`` is required (a
    depending node resolves it via ``use <spec_id>_model``) — so the message names
    the offending file and instructs a rename rather than the misleading
    "not found", which reads as if no file was written.
    """
    if expected_model_name is None:
        return f"{src_dir}: model source not found"
    # The module identifier is the expected name without the ``.f90`` suffix.
    expected_module = expected_model_name[: -len(".f90")]
    if len(expected_module) > _FORTRAN_NAME_LIMIT:
        # No legal literal name exists: <spec_id>_model is itself over the f2008
        # limit. Renaming the abbreviated file would only trade one violation for
        # another, so this is a spec-level problem (the spec_id is too long) and
        # must stop there rather than be "fixed" at Generate's discretion.
        return (
            f"{src_dir}: required model module name {expected_module} "
            f"({len(expected_module)} chars) exceeds the f2008 "
            f"{_FORTRAN_NAME_LIMIT}-char identifier limit; the spec_id is too "
            "long for a literal <spec_id>_model name — stop as a spec-level "
            "problem (do not abbreviate at Generate's discretion)"
        )
    present = sorted(
        p.name for p in src_dir.glob("*_model.f90") if p.is_file()
    )
    if present:
        return (
            f"{src_dir}: model source {', '.join(present)} present but must be "
            f"named {expected_model_name} (literal spec_id prefix required; "
            "abbreviated/derived prefix rejected) — rename to match"
        )
    return f"{src_dir}: node model source not found ({expected_model_name})"


def _validate_dependency_operation_on_model_files(
    model_files: list[Path],
    dep_spec_ids: list[str],
    violations: list[str],
) -> None:
    for model_file in model_files:
        text = model_file.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()

        for spec_id in dep_spec_ids:
            spec_id_l = spec_id.lower()
            op_prefix = re.escape(spec_id_l + "__")
            module_name = re.escape(spec_id_l + "_model")

            if not re.search(rf"\buse\s+{module_name}\b", lowered):
                violations.append(
                    f"{model_file}: missing dependency module use ({spec_id}_model)"
                )

            if re.search(rf"\bsubroutine\s+{op_prefix}[a-z0-9_]*\b", lowered):
                violations.append(
                    f"{model_file}: dependency operation redefinition detected ({spec_id}__*)"
                )

            if not re.search(rf"\bcall\s+{op_prefix}[a-z0-9_]*\b", lowered):
                violations.append(
                    f"{model_file}: missing dependency operation call ({spec_id}__*)"
                )


def _validate_dependency_operation_usage(
    repo_root: Path, execution: NodeExecution, src_dir: Path, violations: list[str]
) -> None:
    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)
    if not dep_spec_ids:
        return

    model_files, _expected_model_name = _model_files_in_src_dir(src_dir, execution)
    if not model_files:
        # The absent / mis-named model source is already reported by
        # _validate_generate_outputs (which always runs on this same src_dir just
        # before this check), via _model_source_not_found_violation. Re-reporting
        # here would emit a duplicate — and, for the abbreviated-name case, the
        # stale "node model source not found" wording alongside the clearer
        # rename instruction. Stay silent and let the generate-outputs check own
        # the diagnostic.
        return

    _validate_dependency_operation_on_model_files(
        model_files, dep_spec_ids, violations
    )


def _validate_runner_source_files(
    execution: NodeExecution,
    runner_files: list[Path],
    violations: list[str],
    known_case_ids: set[str] | None = None,
) -> None:
    # B2 (cosmetic): the runner source is found by `*_runner.f90` glob, which is
    # looser than generate's write-authorization (allowed_output_paths pins exactly
    # `<spec_id>_runner.f90`, so a leaf writing any other name already fails as an
    # unauthorized_write). Assert the basename matches so the validator's expectation
    # is explicit and consistent with the authorization — mirrors the model side
    # (_model_files_in_src_dir). No functional change: the authorization enforces it.
    spec_id = _spec_id_from_node_key(execution.node_key)
    if spec_id is not None:
        expected_runner_name = f"{spec_id}_runner.f90"
        for runner_file in runner_files:
            if runner_file.name != expected_runner_name:
                violations.append(
                    f"{runner_file}: runner source must be named {expected_runner_name} "
                    "(literal spec_id prefix required; matches generate write-authorization) "
                    "— rename to match"
                )
    for runner_file in runner_files:
        text = runner_file.read_text(encoding="utf-8", errors="ignore").lower()
        for output_name in FORBIDDEN_RUNNER_OUTPUTS:
            if output_name in text:
                violations.append(
                    f"{runner_file}: forbidden runner output write detected ({output_name})"
                )
        _validate_runner_json_serialization(
            runner_file=runner_file,
            text=text,
            violations=violations,
        )
        _validate_runner_snapshot_filenames(
            runner_file=runner_file,
            text=text,
            violations=violations,
            known_case_ids=known_case_ids,
        )


def _validate_runner_outputs(
    execution: NodeExecution, src_dir: Path, violations: list[str],
    known_case_ids: set[str] | None = None,
) -> None:
    runner_files = sorted(src_dir.glob("*_runner.f90"))
    if not runner_files:
        return
    _validate_runner_source_files(
        execution, runner_files, violations, known_case_ids=known_case_ids,
    )


def _canonical_log_ref_for_run_program(
    trial_meta_path: Path, repo_root: Path
) -> str | None:
    """Canonical command_log_ref placement for run_program records.

    Sibling of trial_meta inside the execute node directory.
    """
    canonical = trial_meta_path.parent / _MCP_AUDIT_LOG_BASENAME
    try:
        return canonical.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _canonical_log_ref_for_run_quality_checks(
    pipeline_dir: Path, repo_root: Path, source_source_id: str
) -> str | None:
    """Canonical command_log_ref placement for the trial's specific source.

    docs/workflow/phases/phase_04_validate.md mandates run_quality_checks
    against `project_dir=source/<source_id>/src/`. The canonical placement
    is bound strictly to the trial_meta's declared `source_source_id` —
    sibling or older sources under the same pipeline are NOT acceptable.
    This prevents a child from pointing trial_meta at a stale/unrelated
    source's audit log to forge quality-check evidence.
    """
    gen_id = source_source_id.strip()
    if not gen_id:
        return None
    canonical = pipeline_dir / "source" / gen_id / "src" / _MCP_AUDIT_LOG_BASENAME
    try:
        return canonical.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _validate_run_program_inputs(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        return

    data = _read_json(trial_meta_path)
    source_command_ref = data.get("source_command_ref")
    if source_command_ref is None:
        return

    canonical_run_program_ref = _canonical_log_ref_for_run_program(
        trial_meta_path, repo_root
    )
    # Bind run_program executable to trial_meta's source_build_id. The matched
    # record's `cwd` field (project_dir) or absolute argv[0] must resolve
    # under `<pipeline>/build/<source_build_id>/bin/`. Otherwise an execute
    # could attribute results to one build while running a sibling build's
    # binary (mixed-build attribution forge).
    _trial_source_build_id = data.get("source_binary_id")
    _build_bin_abs: Path | None = None
    if isinstance(_trial_source_build_id, str) and _trial_source_build_id.strip():
        _build_bin_abs = (
            execution.pipeline_dir
            / "binary"
            / _trial_source_build_id.strip()
            / "bin"
        ).resolve()

    for entry in _iter_command_ref_entries(source_command_ref):
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref") or entry.get("command_log_path")
        if not isinstance(command_id, str) or not isinstance(log_ref, str):
            continue

        matched = _find_command_log_record(
            repo_root=repo_root,
            command_id=command_id,
            log_ref=log_ref,
        )
        if matched is None:
            continue
        if matched.get("tool_name") != "run_program":
            continue

        # Reject failed MCP runs as evidence: a run_program record with
        # ok!=true means the program execution itself did not succeed, and
        # cannot serve as proof that the workload ran. Mirrors the
        # `run_linter` validator policy.
        if matched.get("ok") is not True:
            violations.append(
                f"{trial_meta_path}:run_program command_id={command_id} "
                f"ok must be true (got {matched.get('ok')!r}). Failed MCP "
                f"runs cannot serve as tool-execution evidence."
            )
            continue

        # Canonical-placement enforcement: a run_program record must be at
        # the canonical sibling-of-trial_meta location. Records claimed at
        # non-canonical paths (e.g. `raw/forged.jsonl` written by the agent
        # under raw/) are rejected to prevent forged tool-execution evidence.
        log_ref_norm = log_ref.strip().rstrip("/")
        if (
            canonical_run_program_ref is not None
            and log_ref_norm != canonical_run_program_ref
        ):
            violations.append(
                f"{trial_meta_path}:run_program command_id={command_id} "
                f"command_log_ref must be the canonical MCP audit log placement "
                f"({canonical_run_program_ref!r}, got {log_ref_norm!r}). "
                "Non-canonical placements are rejected to prevent forged "
                "tool-execution evidence."
            )
            continue

        command = matched.get("command")
        if not isinstance(command, list):
            continue

        has_case_resolved = any(
            isinstance(arg, str) and arg.endswith("spec.ir.yaml")
            for arg in command
        )
        if not has_case_resolved:
            violations.append(
                f"{trial_meta_path}:run_program command_id={command_id} must include spec.ir.yaml"
            )

        # Bind to source_build_id: the executed binary must live under the
        # declared build's bin/ directory. Resolve via the matched record's
        # `cwd` (project_dir) or argv[0] absolute path. Relative argv[0]
        # (e.g. `./simulate`) is resolved against `cwd`.
        if _build_bin_abs is not None and command:
            executable = command[0]
            cwd_val = matched.get("cwd")
            cwd_path: Path | None = None
            if isinstance(cwd_val, str) and cwd_val.strip():
                cwd_path = Path(cwd_val.strip())
            executable_resolved: Path | None = None
            if isinstance(executable, str) and executable.strip():
                exe_path = Path(executable.strip())
                if exe_path.is_absolute():
                    try:
                        executable_resolved = exe_path.resolve()
                    except (OSError, RuntimeError):
                        executable_resolved = None
                elif cwd_path is not None:
                    try:
                        executable_resolved = (cwd_path / exe_path).resolve()
                    except (OSError, RuntimeError):
                        executable_resolved = None
            ok_under_build_bin = False
            if executable_resolved is not None:
                try:
                    executable_resolved.relative_to(_build_bin_abs)
                    ok_under_build_bin = True
                except ValueError:
                    ok_under_build_bin = False
            if not ok_under_build_bin:
                violations.append(
                    f"{trial_meta_path}:run_program command_id={command_id} "
                    f"executable {executable!r} (cwd={cwd_val!r}) must "
                    f"resolve under source_build_id={_trial_source_build_id!r}"
                    f"'s bin directory ({_build_bin_abs!s}). Mixed-build "
                    f"attribution is not permitted."
                )


def _validate_quality_check_commands(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        return

    data = _read_json(trial_meta_path)
    source_command_ref = data.get("source_command_ref")
    if source_command_ref is None:
        return

    impl_contract = _impl_contract_for_execution(repo_root, execution)
    toolchain = impl_contract.get("toolchain") if isinstance(impl_contract, dict) else None
    language = None
    build_system = None
    if isinstance(toolchain, dict):
        raw_language = toolchain.get("language")
        raw_build_system = toolchain.get("build_system")
        if isinstance(raw_language, str) and raw_language.strip():
            language = raw_language.strip().lower()
        if isinstance(raw_build_system, str) and raw_build_system.strip():
            build_system = raw_build_system.strip().lower()

    generate_src_dirs = _generate_src_dirs(execution.pipeline_dir)
    # The cross-phase canonical placement is bound strictly to the trial's
    # `source_source_id` (single source of truth). Sibling/older
    # generations under the same pipeline are not acceptable evidence for
    # this execute run.
    source_source_id_raw = data.get("source_source_id")
    source_source_id: str | None = None
    if isinstance(source_source_id_raw, str) and source_source_id_raw.strip():
        source_source_id = source_source_id_raw.strip()
    canonical_qc_ref: str | None = None
    if source_source_id is not None:
        canonical_qc_ref = _canonical_log_ref_for_run_quality_checks(
            execution.pipeline_dir, repo_root, source_source_id
        )
        # Verify the declared generation actually exists (source_meta.json
        # present) and is in pass state. A forged source_source_id could
        # otherwise authorize an arbitrary path, and pointing at a failed or
        # superseded generation would credit stale evidence to this run.
        if source_source_id is not None and canonical_qc_ref is not None:
            gen_meta = (
                execution.pipeline_dir
                / "source"
                / source_source_id
                / "source_meta.json"
            )
            if not gen_meta.is_file():
                violations.append(
                    f"{trial_meta_path}:source_source_id={source_source_id!r} "
                    f"references a generation that does not exist on disk "
                    f"({gen_meta!s} missing)."
                )
                canonical_qc_ref = None
            else:
                try:
                    gen_meta_doc = _read_json(gen_meta)
                except (OSError, json.JSONDecodeError):
                    gen_meta_doc = None
                gen_status: str | None = None
                if isinstance(gen_meta_doc, dict):
                    raw_status = gen_meta_doc.get("verification_status")
                    if isinstance(raw_status, str):
                        gen_status = raw_status.strip().lower()
                if gen_status != "pass":
                    violations.append(
                        f"{trial_meta_path}:source_source_id={source_source_id!r} "
                        f"references a generation with verification_status="
                        f"{gen_status!r} (expected 'pass'). Stale or failed "
                        f"generations cannot serve as quality_check provenance."
                    )
                    canonical_qc_ref = None

    for entry in _iter_command_ref_entries(source_command_ref):
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref") or entry.get("command_log_path")
        if not isinstance(command_id, str) or not isinstance(log_ref, str):
            continue

        matched = _find_command_log_record(
            repo_root=repo_root,
            command_id=command_id,
            log_ref=log_ref,
        )
        if matched is None:
            continue
        if matched.get("tool_name") != "run_quality_checks":
            continue

        # Reject failed MCP runs as evidence: ok!=true means the
        # quality_check itself failed, so the record cannot prove a
        # successful quality check.
        if matched.get("ok") is not True:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                f"ok must be true (got {matched.get('ok')!r}). Failed MCP "
                f"runs cannot serve as tool-execution evidence."
            )
            continue

        # source_source_id is required when a run_quality_checks record
        # is referenced — without it we cannot pin the canonical cross-phase
        # placement and would risk accepting evidence from a sibling/older
        # generation.
        if source_source_id is None:
            violations.append(
                f"{trial_meta_path}:source_source_id must be declared "
                f"when source_command_ref includes a run_quality_checks "
                f"record (command_id={command_id})."
            )
            continue
        if canonical_qc_ref is None:
            # source_id present but source_meta.json missing — already
            # reported above. Skip per-entry violation to avoid duplication.
            continue
        log_ref_norm = log_ref.strip().rstrip("/")
        if log_ref_norm != canonical_qc_ref:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                f"command_log_ref must be the canonical MCP audit log placement "
                f"for source_source_id={source_source_id!r} "
                f"(expected {canonical_qc_ref!r}, got {log_ref_norm!r}). "
                "Non-canonical or cross-generation placements are rejected to "
                "prevent forged or stale tool-execution evidence."
            )
            continue

        command = matched.get("command")
        if not isinstance(command, list) or not command:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} must have non-empty command array"
            )
            continue

        normalized = [str(token).strip() for token in command if str(token).strip()]
        if not normalized:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} must have non-empty command array"
            )
            continue

        executable = Path(normalized[0]).name.lower()
        if executable in FORBIDDEN_QUALITY_CHECK_EXECUTABLES:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} uses forbidden executable ({executable})"
            )
            continue

        if any("quality_check.py" in token.lower() for token in normalized):
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} must not execute quality_check.py directly"
            )
            continue

        if executable not in QUALITY_CHECK_ALLOWED_COMMANDS:
            allowed = sorted(QUALITY_CHECK_ALLOWED_COMMANDS)
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} executable must be one of {allowed}"
            )
            continue

        if executable == "make":
            targets = {token.lower() for token in normalized[1:]}
            if "test" not in targets and "check" not in targets:
                violations.append(
                    f"{trial_meta_path}:run_quality_checks command_id={command_id} make command must include test/check target"
                )

        preset = _quality_check_preset_from_command(normalized)
        raw_cwd = matched.get("cwd")
        cwd_path = (
            _resolve_logged_path(repo_root, raw_cwd)
            if isinstance(raw_cwd, str) and raw_cwd.strip()
            else None
        )

        if build_system == "make" and language in MAKE_QUALITY_CHECK_REQUIRED_LANGUAGES:
            if preset not in {"make_test", "make_check"}:
                violations.append(
                    f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                    f"must use make_test/make_check for toolchain.language={language} and toolchain.build_system=make"
                )
                continue

            if cwd_path is None:
                violations.append(
                    f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                    "must record cwd under source/<source_id>/src"
                )
                continue

            if not generate_src_dirs or not any(
                _path_is_same_or_under(cwd_path, src_dir) for src_dir in generate_src_dirs
            ):
                violations.append(
                    f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                    "must run inside source/<source_id>/src for make-based quality check"
                )
                continue

            makefile_path = cwd_path / "Makefile"
            if not makefile_path.exists():
                violations.append(
                    f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                    f"requires Makefile in quality check cwd ({makefile_path})"
                )
                continue

            required_target = "test" if preset == "make_test" else "check"
            if required_target not in _make_targets(makefile_path):
                violations.append(
                    f"{makefile_path}: missing {required_target} target required by run_quality_checks "
                    f"command_id={command_id}"
                )


def _validate_tests_verdict_summary_consistency(
    repo_root: Path,
    execution: NodeExecution,
    violations: list[str],
    *,
    require_verdict: bool,
) -> None:
    """tests.md test ids == verdict.json#per_test, and summary.json counts == that verdict.

    `require_verdict` is the stage's `require_orchestration`: at `post_execute` the conductor has
    NOT authored verdict.json yet (it clears any stale one at the top of execute and writes the
    deterministic verdict only after this gate returns clean — `workflow_conductor._execute_inproc`),
    so a missing verdict is the normal state there and must not be flagged. At `pre_judge` / `full`
    the verdict is authored and its absence is a real defect. (`pre_judge` is `--run-id`-scoped, so
    it sees only the run under judgment. `full` is the unscoped operator/audit stage: it walks every
    run dir, so a superseded run that failed structurally at execute — and correctly holds no verdict
    — is reported there too. Those runs already fail `full` on their missing `semantic_review.json`;
    no conductor path invokes `full`.)
    """
    tests_path = _tests_path_for_execution(repo_root, execution)
    # The ref is LLM-authored: it may name a directory, an over-long path, or an unreadable one.
    # `_validate_ir_source_refs_tests` reports such a ref as the violation it is; this gate must not
    # read (or probe) it in a way that raises before the leaf gets that finding.
    if tests_path is None or not _is_readable_file(tests_path):
        return

    test_ids = _parse_test_ids_from_tests_md(tests_path)
    if not test_ids:
        violations.append(f"{tests_path}: test_id heading not found")
        return

    verdict_path = execution.node_dir / "verdict.json"
    summary_path = execution.node_dir / "summary.json"
    if not verdict_path.exists():
        if require_verdict:
            violations.append(f"{verdict_path}: missing")
        return
    if not summary_path.exists():
        violations.append(f"{summary_path}: missing")
        return

    try:
        verdict = _read_json(verdict_path)
    except json.JSONDecodeError:
        violations.append(f"{verdict_path}: invalid json")
        return
    if not isinstance(verdict, dict):
        violations.append(f"{verdict_path}: must be json object")
        return

    per_test = verdict.get("per_test")
    if not isinstance(per_test, list) or not per_test:
        violations.append(f"{verdict_path}:per_test must be non-empty list")
        return

    status_by_test: dict[str, str] = {}
    duplicate_test_ids: set[str] = set()
    invalid_entries = False
    for idx, item in enumerate(per_test):
        if not isinstance(item, dict):
            violations.append(f"{verdict_path}:per_test[{idx}] must be object")
            invalid_entries = True
            continue
        raw_test_id = item.get("test_id")
        if not isinstance(raw_test_id, str) or not raw_test_id.strip():
            violations.append(f"{verdict_path}:per_test[{idx}].test_id must be non-empty string")
            invalid_entries = True
            continue
        test_id = raw_test_id.strip()
        raw_status = item.get("status")
        if raw_status is None:
            raw_status = item.get("outcome")
        if not isinstance(raw_status, str):
            violations.append(
                f"{verdict_path}:per_test[{idx}] must define status/outcome string"
            )
            invalid_entries = True
            continue
        status = raw_status.strip().lower()
        if status not in TEST_OUTCOME_VALUES:
            violations.append(
                f"{verdict_path}:per_test[{idx}].status/outcome must be one of {sorted(TEST_OUTCOME_VALUES)}"
            )
            invalid_entries = True
            continue

        if test_id in status_by_test:
            duplicate_test_ids.add(test_id)
        status_by_test[test_id] = status

    if duplicate_test_ids:
        violations.append(
            f"{verdict_path}:per_test has duplicated test_id entries ({sorted(duplicate_test_ids)})"
        )
    if invalid_entries:
        return

    expected_ids = set(test_ids)
    actual_ids = set(status_by_test.keys())
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)
    if missing:
        violations.append(f"{verdict_path}:per_test missing test_id entries from tests.md ({missing})")
    if extra:
        violations.append(f"{verdict_path}:per_test has unknown test_id entries ({extra})")
    if missing or extra:
        return

    computed_counts = {key: 0 for key in TEST_OUTCOME_VALUES}
    for status in status_by_test.values():
        computed_counts[status] += 1

    try:
        summary = _read_json(summary_path)
    except json.JSONDecodeError:
        violations.append(f"{summary_path}: invalid json")
        return
    if not isinstance(summary, dict):
        violations.append(f"{summary_path}: must be json object")
        return

    counts = summary.get("counts")
    if not isinstance(counts, dict):
        violations.append(f"{summary_path}:counts must be object")
        return

    for key in ("pass", "fail", "xfail", "skipped"):
        value = counts.get(key)
        if not isinstance(value, int) or value < 0:
            violations.append(f"{summary_path}:counts.{key} must be integer >= 0")
            continue
        if value != computed_counts[key]:
            violations.append(
                f"{summary_path}:counts.{key} must equal verdict.per_test aggregate ({computed_counts[key]})"
            )

    blocked_value = counts.get("blocked")
    if blocked_value is not None:
        if not isinstance(blocked_value, int) or blocked_value < 0:
            violations.append(f"{summary_path}:counts.blocked must be integer >= 0 when present")
        elif blocked_value != computed_counts["blocked"]:
            violations.append(
                f"{summary_path}:counts.blocked must equal verdict.per_test aggregate ({computed_counts['blocked']})"
            )


def _collect_numeric_leaves(obj: Any, _depth: int = 0) -> list[float | None]:
    """Recursively collect numeric / null leaves from a JSON object/array (depth limit 8)."""
    if _depth > 8:
        return []
    if isinstance(obj, bool):
        return []  # bool is a subclass of int, so check it first
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if obj is None:
        return [None]
    if isinstance(obj, list):
        result: list[float | None] = []
        for item in obj:
            result.extend(_collect_numeric_leaves(item, _depth + 1))
        return result
    if isinstance(obj, dict):
        result = []
        for v in obj.values():
            result.extend(_collect_numeric_leaves(v, _depth + 1))
        return result
    return []


def _validate_metrics_basis_not_trivial(
    execution: NodeExecution,
    violations: list[str],
) -> None:
    """Verify that metrics_basis.json is not all-zero / all-null."""
    metrics_path = execution.node_dir / "raw" / "metrics_basis.json"
    if not metrics_path.exists():
        return  # existence check is handled by _validate_raw_evidence

    try:
        data = _read_json(metrics_path)
    except json.JSONDecodeError:
        return  # JSON syntax errors are already handled by another function

    if not isinstance(data, dict):
        return

    numeric_values = _collect_numeric_leaves(data)
    if not numeric_values:
        return  # skip if there is no numeric field at all

    non_trivial_count = sum(
        1 for v in numeric_values if v is not None and v != 0.0
    )
    if non_trivial_count == 0:
        violations.append(
            f"{metrics_path}: all numeric fields are zero or null "
            "(trivial placeholder detected)"
        )


def _validate_llm_semantic_review(
    repo_root: Path,
    execution: NodeExecution,
    violations: list[str],
    *,
    require_llm_review: bool,
) -> None:
    review_path = execution.node_dir / LLM_REVIEW_FILENAME
    if not review_path.exists():
        if require_llm_review:
            violations.append(f"{review_path}: missing")
        return

    try:
        data = _read_json(review_path)
    except json.JSONDecodeError:
        violations.append(f"{review_path}: invalid json")
        return

    if not isinstance(data, dict):
        violations.append(f"{review_path}: must be json object")
        return

    review_method = data.get("review_method")
    if review_method != "llm_semantic_review":
        violations.append(
            f"{review_path}:review_method must be llm_semantic_review"
        )

    decision = data.get("decision")
    if decision not in {"pass", "fail"}:
        violations.append(f"{review_path}:decision must be pass/fail")
    elif decision != "pass":
        violations.append(f"{review_path}:decision is fail")

    scope = data.get("scope")
    if not isinstance(scope, dict):
        violations.append(f"{review_path}:scope must be object")
        return

    for key in ("model_ref", "runner_ref"):
        ref = scope.get(key)
        if not isinstance(ref, str) or not ref.startswith("workspace/"):
            violations.append(
                f"{review_path}:scope.{key} must start with workspace/"
            )
            continue
        target = repo_root / ref
        if not target.exists():
            violations.append(
                f"{review_path}:scope.{key} target not found ({ref})"
            )

    raw_refs = scope.get("raw_refs")
    if not isinstance(raw_refs, list) or not raw_refs:
        violations.append(f"{review_path}:scope.raw_refs must be non-empty list")
    else:
        for idx, ref in enumerate(raw_refs):
            if not isinstance(ref, str) or not ref.startswith("workspace/"):
                violations.append(
                    f"{review_path}:scope.raw_refs[{idx}] must start with workspace/"
                )
                continue
            target = repo_root / ref
            if not target.exists():
                violations.append(
                    f"{review_path}:scope.raw_refs[{idx}] target not found ({ref})"
                )


def _source_fingerprint(execution: NodeExecution) -> SourceFingerprint | None:
    generate_root = execution.pipeline_dir / "source"
    gen_dirs = sorted(d for d in generate_root.iterdir() if d.is_dir()) if generate_root.exists() else []
    if not gen_dirs:
        return None

    src_dir = gen_dirs[-1] / "src"
    if not src_dir.exists():
        return None

    hasher = hashlib.sha256()
    included = 0
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".o", ".mod", ".a", ".so"}:
            continue
        if path.name in {"simulate"}:
            continue
        if "bin" in path.parts:
            continue
        rel = path.relative_to(src_dir).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(path.read_bytes())
        included += 1
    if included == 0:
        return None

    return SourceFingerprint(
        node_key=execution.node_key,
        pipeline_dir=execution.pipeline_dir,
        digest=hasher.hexdigest(),
    )


def _resolve_pipeline_roots(
    repo_root: Path, workspace_root: str, raw_values: list[str] | None
) -> list[Path] | None:
    if not raw_values:
        return None

    workspace_path = repo_root / workspace_root
    pipelines_path = workspace_path / "pipelines"
    roots: list[Path] = []
    for raw in raw_values:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(workspace_path.resolve())
        except ValueError:
            raise ValueError(
                f"pipeline_root must be under {workspace_path}: {candidate}"
            ) from None
        try:
            candidate.relative_to(pipelines_path.resolve())
        except ValueError:
            raise ValueError(
                f"pipeline_root must be under {pipelines_path}: {candidate}"
            ) from None
        roots.append(candidate)
    return roots


def _validate_orchestration_hierarchy(
    workspace_path: Path,
    executions: list[NodeExecution],
    violations: list[str],
    in_flight_agent_run_ids: set[str] | None = None,
    current_orchestration_id: str | None = None,
) -> None:
    in_flight_agent_run_ids = in_flight_agent_run_ids or set()
    orchestrations_root = workspace_path / "orchestrations"
    if not orchestrations_root.exists() or not orchestrations_root.is_dir():
        violations.append(
            f"{orchestrations_root}: missing; workflow must start with orchestration agent"
        )
        return

    orchestration_dirs = sorted(
        path for path in orchestrations_root.iterdir() if path.is_dir()
    )
    if not orchestration_dirs:
        violations.append(
            f"{orchestrations_root}: no orchestration run found"
        )
        return

    node_safes = sorted({execution.pipeline_dir.parent.name for execution in executions})

    # Phase-4 D2: scope the cross-orchestration integrity scan to the CURRENT
    # orchestration when its id is supplied (pre_judge passes --orchestration-id).
    # The conductor runs one orchestration per node (run_workflow `_run_node`), so
    # workspace/orchestrations/ accumulates the current run, its dependency runs,
    # AND debris from prior/crashed unrelated runs. The per-orchestration internal
    # consistency checks below (dangling agent_graph edges, role-parent rules) must
    # only police the run being judged: an unrelated crashed orchestration's
    # dangling edge previously failed a healthy run (a fresh run inherited foreign
    # debris). Dependencies are validated by their OWN pre_judge + the conductor's
    # workflow_launch_check readiness gate, so they need not be re-policed here.
    # When the id is absent (legacy callers / --stage full) the scan covers all
    # orchestrations, preserving prior behavior.
    if current_orchestration_id is not None:
        scoped = [d for d in orchestration_dirs if d.name == current_orchestration_id]
        if not scoped:
            violations.append(
                f"{orchestrations_root}/{current_orchestration_id}: current "
                "orchestration directory not found"
            )
            return
        orchestration_dirs = scoped
        # Restrict coverage to the node(s) this orchestration actually produced, so
        # scoping to one dir does not spuriously flag a dependency node's steps as
        # missing (their step_results live in the dependency's own orchestration).
        # Intersect UNCONDITIONALLY — even when this orchestration has no steps dir
        # (own_node_safes empty): leaving node_safes unnarrowed would retain the other
        # executions' nodes and demand their step_results of this single scoped dir,
        # defeating the scoping. An orchestration genuinely missing its steps still
        # fails closed via the per-orchestration "steps_root: missing" check below.
        own_steps_root = scoped[0] / "steps"
        own_node_safes = (
            {p.name for p in own_steps_root.iterdir() if p.is_dir()}
            if own_steps_root.is_dir() else set()
        )
        node_safes = sorted(set(node_safes) & own_node_safes)

    step_coverage = {
        (node_safe, step): False
        for node_safe in node_safes
        for step in REQUIRED_WORKFLOW_STEPS
    }
    has_orchestration_role = False
    has_step_role = False
    has_substep_role = False

    for orchestration_dir in orchestration_dirs:
        meta_path = orchestration_dir / "orchestration_meta.json"
        graph_path = orchestration_dir / "agent_graph.json"
        runs_path = orchestration_dir / "agent_runs.jsonl"
        preflight_path = orchestration_dir / "preflight.json"
        graph_edges: list[tuple[int, str, str]] = []
        run_records: dict[str, dict[str, Any]] = {}
        run_roles: dict[str, str] = {}
        seen_context_ids: dict[str, str] = {}

        # In-flight tolerance for the self-judging exception ONLY.
        #
        # record-launch appends a child's agent_graph edge BEFORE the child runs,
        # while the child's agent_runs.jsonl entry and its step_result.json are
        # written only AFTER it returns. The Validate.judge substep runs --stage
        # pre_judge from inside its own (still-executing) run, so its own edge and
        # the validate step_result are necessarily unrecorded at that moment.
        #
        # The ONLY trustworthy proof that a run is genuinely in-flight RIGHT NOW
        # is the live caller declaring its own agent_run_id: a static artifact
        # (an active_children marker, the single-active-child pointer) cannot be
        # distinguished from a stale leftover after a crash/partial cleanup, and
        # such markers are not written for every backend. We therefore exempt
        # exactly the agent_run_id(s) the caller passes via
        # --in-flight-agent-run-id, and only after verifying from the persisted
        # launch request that each is the validate/judge substep (so the flag can
        # never be used to suppress unrelated missing-record violations). Nothing
        # is exempted by marker presence alone — an orphaned edge with no
        # live-caller declaration still fails, fail-closed.
        inflight_exempt_arids: set[str] = set()
        inflight_exempt_node_safe: dict[str, str] = {}
        # Populated by the agent_graph edge scan below with the subset of
        # declared in-flight arids that are ACTUALLY observed as a dangling edge
        # (child present in agent_graph.json but not yet in agent_runs.jsonl).
        # Only these get the validate step_result exemption — a flag naming an
        # arid with a plausible launch request but no dangling edge (stale /
        # mistyped / cross-orchestration) must not suppress anything.
        observed_inflight_dangling: set[str] = set()
        for raw_arid in in_flight_agent_run_ids:
            arid = raw_arid.strip()
            if not arid:
                continue
            req_path = orchestration_dir / "launches" / f"{arid}.request.json"
            try:
                req_doc = _read_json(req_path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(req_doc, dict):
                continue
            step_name = req_doc.get("step")
            substep_name = req_doc.get("substep")
            node_key = req_doc.get("node_key")
            # The live caller running --stage pre_judge is either the judge leaf itself
            # (historic, self-judging) or the deterministic post_judge substep (G3 split:
            # post_judge runs the gate AFTER the judge returns, so post_judge's own
            # agent_graph edge is the dangling in-flight one). Accept both validate substeps;
            # any other (step, substep) declared in-flight is ignored so the flag can never
            # suppress unrelated missing-record violations.
            if not (
                isinstance(step_name, str) and step_name.strip().lower() == "validate"
                and isinstance(substep_name, str)
                and substep_name.strip().lower() in ("judge", "post_judge")
            ):
                continue
            inflight_exempt_arids.add(arid)
            if isinstance(node_key, str) and node_key.strip():
                node_safe = _node_key_to_safe(node_key.strip())
                if node_safe is not None:
                    inflight_exempt_node_safe[arid] = node_safe

        for required in (meta_path, graph_path, runs_path, preflight_path):
            if not required.exists():
                violations.append(f"{required}: missing")

        if preflight_path.exists():
            try:
                preflight = _read_json(preflight_path)
            except json.JSONDecodeError:
                violations.append(f"{preflight_path}: invalid json")
            else:
                if not isinstance(preflight, dict):
                    violations.append(f"{preflight_path}: must be json object")
                else:
                    if preflight.get("status") != "pass":
                        violations.append(f"{preflight_path}:status must be pass")
                    if preflight.get("can_launch_step_agents") is not True:
                        violations.append(
                            f"{preflight_path}:can_launch_step_agents must be true"
                        )
                    if preflight.get("can_launch_substep_agents") is not True:
                        violations.append(
                            f"{preflight_path}:can_launch_substep_agents must be true"
                        )
                    feature_states = preflight.get("feature_states")
                    if not isinstance(feature_states, dict):
                        violations.append(
                            f"{preflight_path}:feature_states must be object"
                        )
                    elif feature_states.get("multi_agent") is not True:
                        violations.append(
                            f"{preflight_path}:feature_states.multi_agent must be true"
                        )

                    checks = preflight.get("checks")
                    if not isinstance(checks, list):
                        violations.append(f"{preflight_path}:checks must be list")
                    else:
                        multi_agent_check_pass = None
                        for item in checks:
                            if not isinstance(item, dict):
                                continue
                            if item.get("name") != "multi_agent_enabled":
                                continue
                            pass_value = item.get("pass")
                            if isinstance(pass_value, bool):
                                multi_agent_check_pass = pass_value
                                break
                        if multi_agent_check_pass is not True:
                            violations.append(
                                f"{preflight_path}:checks.multi_agent_enabled.pass must be true"
                            )

        if meta_path.exists():
            try:
                meta = _read_json(meta_path)
            except json.JSONDecodeError:
                violations.append(f"{meta_path}: invalid json")
            else:
                if not isinstance(meta, dict):
                    violations.append(f"{meta_path}: must be json object")
                else:
                    orchestration_id = meta.get("orchestration_id")
                    if not isinstance(orchestration_id, str) or not orchestration_id.strip():
                        violations.append(
                            f"{meta_path}:orchestration_id must be non-empty string"
                        )

        if graph_path.exists():
            try:
                graph = _read_json(graph_path)
            except json.JSONDecodeError:
                violations.append(f"{graph_path}: invalid json")
            else:
                if not isinstance(graph, dict):
                    violations.append(f"{graph_path}: must be json object")
                else:
                    edges = graph.get("edges")
                    if not isinstance(edges, list) or not edges:
                        violations.append(f"{graph_path}:edges must be non-empty list")
                    else:
                        for idx, edge in enumerate(edges):
                            if not isinstance(edge, dict):
                                violations.append(f"{graph_path}:edges[{idx}] must be object")
                                continue
                            parent = edge.get("parent_agent_run_id")
                            child = edge.get("child_agent_run_id")
                            relation = edge.get("relation_type")
                            if not isinstance(parent, str) or not parent.strip():
                                violations.append(
                                    f"{graph_path}:edges[{idx}].parent_agent_run_id must be non-empty string"
                                )
                            if not isinstance(child, str) or not child.strip():
                                violations.append(
                                    f"{graph_path}:edges[{idx}].child_agent_run_id must be non-empty string"
                                )
                            if not isinstance(relation, str) or not relation.strip():
                                violations.append(
                                    f"{graph_path}:edges[{idx}].relation_type must be non-empty string"
                                )
                            if (
                                isinstance(parent, str)
                                and parent.strip()
                                and isinstance(child, str)
                                and child.strip()
                            ):
                                graph_edges.append((idx, parent.strip(), child.strip()))

        if runs_path.exists():
            lines = [line.strip() for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not lines:
                violations.append(f"{runs_path}: must be non-empty jsonl")
            for idx, line in enumerate(lines):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    violations.append(f"{runs_path}:line {idx + 1} invalid json")
                    continue
                if not isinstance(item, dict):
                    violations.append(f"{runs_path}:line {idx + 1} must be json object")
                    continue
                run_id = item.get("agent_run_id")
                status = item.get("status")
                if not isinstance(run_id, str) or not run_id.strip():
                    violations.append(f"{runs_path}:line {idx + 1} missing agent_run_id")
                    continue
                run_id = run_id.strip()
                if run_id in run_records:
                    violations.append(
                        f"{runs_path}:line {idx + 1} duplicate agent_run_id ({run_id})"
                    )
                    continue
                if not isinstance(status, str) or not status.strip():
                    violations.append(f"{runs_path}:line {idx + 1} missing status")
                    continue
                status = status.strip().lower()
                started_at = item.get("started_at")
                if not isinstance(started_at, str) or not started_at.strip():
                    violations.append(f"{runs_path}:line {idx + 1} missing started_at")
                if status in AGENT_TERMINAL_STATUSES:
                    finished_at = item.get("finished_at")
                    if not isinstance(finished_at, str) or not finished_at.strip():
                        violations.append(
                            f"{runs_path}:line {idx + 1} terminal status requires finished_at"
                        )

                role_l = _agent_role(item)
                if role_l is None:
                    violations.append(f"{runs_path}:line {idx + 1} missing agent role")
                    continue
                run_records[run_id] = item
                run_roles[run_id] = role_l

                if role_l == "orchestration":
                    has_orchestration_role = True
                elif role_l == "step":
                    has_step_role = True
                elif role_l == "substep":
                    has_substep_role = True

                if role_l in {"step", "substep"}:
                    parent = item.get("parent_agent_run_id")
                    if not isinstance(parent, str) or not parent.strip():
                        violations.append(
                            f"{runs_path}:line {idx + 1} missing parent_agent_run_id for {role_l}"
                        )
                    backend = item.get("agent_backend")
                    if not isinstance(backend, str) or not backend.strip():
                        violations.append(
                            f"{runs_path}:line {idx + 1} missing agent_backend for {role_l}"
                        )
                    model = item.get("agent_model")
                    if not isinstance(model, str) or not model.strip():
                        violations.append(
                            f"{runs_path}:line {idx + 1} missing agent_model for {role_l}"
                        )
                    context_id = item.get("context_id")
                    if not isinstance(context_id, str) or not context_id.strip():
                        violations.append(
                            f"{runs_path}:line {idx + 1} missing context_id for {role_l}"
                        )
                    else:
                        context_token = context_id.strip()
                        previous = seen_context_ids.get(context_token)
                        if previous is not None and previous != run_id:
                            violations.append(
                                f"{runs_path}:line {idx + 1} context_id must be unique across step/substep ({context_token})"
                            )
                        else:
                            seen_context_ids[context_token] = run_id
                    context_isolated = item.get("context_isolated")
                    if context_isolated is not True:
                        violations.append(
                            f"{runs_path}:line {idx + 1} context_isolated must be true for {role_l}"
                        )
                    agent_session_id = item.get("agent_session_id")
                    if not isinstance(agent_session_id, str) or not agent_session_id.strip():
                        violations.append(
                            f"{runs_path}:line {idx + 1} missing agent_session_id for {role_l}"
                        )
                    elif _is_sequential_agent_token(agent_session_id):
                        violations.append(
                            f"{runs_path}:line {idx + 1} agent_session_id must not be sequential placeholder ({agent_session_id})"
                        )

                    expected_launch_prefix = (
                        f"workspace/orchestrations/{orchestration_dir.name}/launches/"
                    )
                    expected_agent_prefix = (
                        f"workspace/orchestrations/{orchestration_dir.name}/agents/{run_id}/dialogs/"
                    )
                    launch_refs: dict[str, str] = {}
                    for key in (
                        "launch_request_ref",
                        "launch_response_ref",
                        "launch_prompt_ref",
                        "launch_reply_ref",
                    ):
                        launch_ref = item.get(key)
                        if not isinstance(launch_ref, str) or not launch_ref.strip():
                            violations.append(
                                f"{runs_path}:line {idx + 1} missing {key} for {role_l}"
                            )
                            continue
                        ref_token = launch_ref.strip()
                        if not ref_token.startswith(expected_launch_prefix):
                            violations.append(
                                f"{runs_path}:line {idx + 1} {key} must start with {expected_launch_prefix} ({ref_token})"
                            )
                            continue
                        launch_refs[key] = ref_token
                        launch_path = workspace_path.parent / ref_token
                        if not launch_path.exists():
                            violations.append(
                                f"{runs_path}:line {idx + 1} {key} target not found ({ref_token})"
                            )
                            continue
                        if key in {"launch_prompt_ref", "launch_reply_ref"}:
                            if not launch_path.is_file():
                                violations.append(
                                    f"{runs_path}:line {idx + 1} {key} target must be file ({ref_token})"
                                )
                                continue
                            launch_text = launch_path.read_text(
                                encoding="utf-8", errors="ignore"
                            )
                            if not launch_text.strip():
                                violations.append(
                                    f"{runs_path}:line {idx + 1} {key} target must be non-empty ({ref_token})"
                                )
                            if key == "launch_prompt_ref":
                                # Downgrade to the reduced slim / pure marker set ONLY when the
                                # structured launch REQUEST confirms it (_launch_request_is_slim_repair
                                # / _launch_request_is_pure) AND the recorded prompt is actually
                                # slim- / pure-shaped (sentinel anchored at line 0). Relying on
                                # prompt text alone would let a record whose prompt was replaced
                                # with a slim/pure-looking body escape the full markers.
                                request_confirms_slim = False
                                request_confirms_pure = False
                                req_payload_for_launch = None
                                req_ref = launch_refs.get("launch_request_ref")
                                if isinstance(req_ref, str):
                                    req_path = workspace_path.parent / req_ref
                                    if req_path.is_file():
                                        try:
                                            req_payload_for_launch = _read_json(req_path)
                                        except json.JSONDecodeError:
                                            req_payload_for_launch = None
                                        if isinstance(req_payload_for_launch, dict):
                                            request_confirms_slim = _launch_request_is_slim_repair(
                                                req_payload_for_launch)
                                            request_confirms_pure = _launch_request_is_pure(
                                                req_payload_for_launch)
                                # Anchored slim/pure detection takes precedence over the substring
                                # deterministic check when an untrusted findings excerpt happens to
                                # embed the deterministic sentinel.
                                prompt_is_pure = _is_pure_launch_prompt_text(launch_text)
                                # Request and prompt must AGREE on pure: a pure request with a
                                # non-pure prompt (or a pure prompt from a non-pure request) is an
                                # inconsistent record that must not silently pass.
                                if request_confirms_pure != prompt_is_pure:
                                    violations.append(
                                        f"{runs_path}:line {idx + 1} {key} pure-launch mismatch: "
                                        f"request leaf_mode=pure is {request_confirms_pure} but "
                                        f"prompt-is-pure is {prompt_is_pure}"
                                    )
                                is_pure = request_confirms_pure and prompt_is_pure
                                is_slim = (
                                    request_confirms_slim
                                    and _is_slim_launch_prompt_text(launch_text))
                                is_deterministic = DETERMINISTIC_PROMPT_SENTINEL in launch_text
                                required_markers = _required_launch_prompt_markers_for_role(
                                    role_l, deterministic=is_deterministic, slim=is_slim,
                                    pure=is_pure)
                                missing_markers = [
                                    marker
                                    for marker in required_markers
                                    if not _launch_prompt_marker_present(marker, launch_text)
                                ]
                                if missing_markers:
                                    violations.append(
                                        f"{runs_path}:line {idx + 1} {key} missing launch-prompt template markers ({', '.join(missing_markers)})"
                                    )
                                if is_pure:
                                    # Independent audit of the persisted pure record (tamper /
                                    # drift detection), so it re-checks the value invariants the
                                    # runtime enforces at launch — the marker sweep above only
                                    # proves the marker NAME is present.
                                    # (1) prompt_contract_version must equal the transport's
                                    # contract, in BOTH the structured request and the rendered
                                    # prompt line; an obsolete/forged version passing the audit
                                    # would let a stale-contract producer look compliant.
                                    req_version = (
                                        req_payload_for_launch.get("prompt_contract_version")
                                        if isinstance(req_payload_for_launch, dict) else None
                                    )
                                    if req_version != PURE_PROMPT_CONTRACT_VERSION:
                                        violations.append(
                                            f"{runs_path}:line {idx + 1} pure launch request "
                                            f"prompt_contract_version must be "
                                            f"{PURE_PROMPT_CONTRACT_VERSION!r} (got {req_version!r})"
                                        )
                                    expected_version_line = (
                                        f"prompt_contract_version: {PURE_PROMPT_CONTRACT_VERSION}"
                                    )
                                    if expected_version_line not in launch_text:
                                        violations.append(
                                            f"{runs_path}:line {idx + 1} pure launch prompt must "
                                            f"carry {expected_version_line!r}"
                                        )
                                    # (2) A pure launch writes no output manifest — its ABSENCE is
                                    # the mock-green tripwire (a record-launch that skipped the pure
                                    # write-authorization branch would leave one). And the
                                    # capability must be the truthful zero-authority record
                                    # (mode=pure_readonly, write_roots==[], mcp_permissions==[]).
                                    om_path = (
                                        orchestration_dir / "output_manifests" / f"{run_id}.json"
                                    )
                                    if om_path.exists():
                                        violations.append(
                                            f"{runs_path}:line {idx + 1} pure launch must NOT have "
                                            f"an output manifest ({om_path.name} exists)"
                                        )
                                    cap_path = (
                                        orchestration_dir / "capabilities" / f"{run_id}.json"
                                    )
                                    cap_doc = None
                                    if cap_path.is_file():
                                        try:
                                            cap_doc = _read_json(cap_path)
                                        except json.JSONDecodeError:
                                            cap_doc = None
                                    if not isinstance(cap_doc, dict):
                                        violations.append(
                                            f"{runs_path}:line {idx + 1} pure launch capability "
                                            f"missing/unreadable ({cap_path.name})"
                                        )
                                    else:
                                        if str(cap_doc.get("mode", "")).strip() != PURE_CAPABILITY_MODE:
                                            violations.append(
                                                f"{runs_path}:line {idx + 1} pure launch capability "
                                                f"mode must be {PURE_CAPABILITY_MODE!r} (got {cap_doc.get('mode')!r})"
                                            )
                                        if cap_doc.get("write_roots") != []:
                                            violations.append(
                                                f"{runs_path}:line {idx + 1} pure launch capability "
                                                f"write_roots must be [] (got {cap_doc.get('write_roots')!r})"
                                            )
                                        # A pure leaf invokes no gate/MCP, so its capability must
                                        # carry an EXPLICIT empty mcp_permissions list (part of the
                                        # zero-authority record the producer always emits). No
                                        # `get` default: absence (a truncated/hand-crafted record)
                                        # and a non-list value must be flagged too — only a present
                                        # `[]` is compliant. Mirrors the write_roots check above.
                                        if cap_doc.get("mcp_permissions") != []:
                                            violations.append(
                                                f"{runs_path}:line {idx + 1} pure launch capability "
                                                f"mcp_permissions must be [] (got {cap_doc.get('mcp_permissions')!r})"
                                            )
                                    # (3) The read manifest must be DENY-ALL (empty
                                    # allowed_read_roots — the enforcing allowlist for a leaf that
                                    # reads no file) and the sandbox must be the READ-ONLY profile
                                    # (readonly + write_roots==[]). Auditing these here catches a
                                    # pure launch mistakenly provisioned through the generic
                                    # (writable/read-granting) record-launch path even though the
                                    # capability/output-manifest signals looked pure.
                                    rman_path = (
                                        orchestration_dir / "read_manifests" / f"{run_id}.json"
                                    )
                                    rman_doc = None
                                    if rman_path.is_file():
                                        try:
                                            rman_doc = _read_json(rman_path)
                                        except json.JSONDecodeError:
                                            rman_doc = None
                                    if not isinstance(rman_doc, dict):
                                        violations.append(
                                            f"{runs_path}:line {idx + 1} pure launch read manifest "
                                            f"missing/unreadable ({rman_path.name})"
                                        )
                                    elif rman_doc.get("allowed_read_roots") != []:
                                        violations.append(
                                            f"{runs_path}:line {idx + 1} pure launch read manifest "
                                            f"allowed_read_roots must be [] (deny-all; got "
                                            f"{rman_doc.get('allowed_read_roots')!r})"
                                        )
                                    sbx_path = (
                                        orchestration_dir / "sandbox_profiles" / f"{run_id}.json"
                                    )
                                    sbx_doc = None
                                    if sbx_path.is_file():
                                        try:
                                            sbx_doc = _read_json(sbx_path)
                                        except json.JSONDecodeError:
                                            sbx_doc = None
                                    if not isinstance(sbx_doc, dict):
                                        violations.append(
                                            f"{runs_path}:line {idx + 1} pure launch sandbox profile "
                                            f"missing/unreadable ({sbx_path.name})"
                                        )
                                    else:
                                        if sbx_doc.get("readonly") is not True:
                                            violations.append(
                                                f"{runs_path}:line {idx + 1} pure launch sandbox "
                                                f"profile must be readonly (got readonly="
                                                f"{sbx_doc.get('readonly')!r})"
                                            )
                                        if sbx_doc.get("write_roots") != []:
                                            violations.append(
                                                f"{runs_path}:line {idx + 1} pure launch sandbox "
                                                f"profile write_roots must be [] (got "
                                                f"{sbx_doc.get('write_roots')!r})"
                                            )
                                    # (5) M-C 修正2 mirror: a PASSING pure terminal row carries an
                                    # output_refs of EXACTLY [] (the host writes the bundle artifacts
                                    # after the child window; a pure leaf holds no write authority). A
                                    # non-empty output_refs is forged provenance, and a MISSING field
                                    # is not "empty" either — a tampered record that drops it must be
                                    # rejected, not waved through. The same exact-[] invariant
                                    # _validate_terminal_run_payload enforces at record time, re-audited
                                    # here for a persisted record.
                                    if str(item.get("status", "")).strip().lower() == "pass":
                                        pure_orefs = item.get("output_refs")
                                        if pure_orefs != []:
                                            violations.append(
                                                f"{runs_path}:line {idx + 1} pure pass row must have "
                                                f"output_refs of exactly [] (the host writes bundle "
                                                f"artifacts after the child window; got "
                                                f"{pure_orefs!r})"
                                            )

                    for key in ("agent_result_ref", "agent_summary_ref"):
                        agent_ref = item.get(key)
                        if not isinstance(agent_ref, str) or not agent_ref.strip():
                            violations.append(
                                f"{runs_path}:line {idx + 1} missing {key} for {role_l}"
                            )
                            continue
                        ref_token = agent_ref.strip()
                        if not ref_token.startswith(expected_agent_prefix):
                            violations.append(
                                f"{runs_path}:line {idx + 1} {key} must start with {expected_agent_prefix} ({ref_token})"
                            )
                            continue
                        agent_path = workspace_path.parent / ref_token
                        if not agent_path.exists():
                            violations.append(
                                f"{runs_path}:line {idx + 1} {key} target not found ({ref_token})"
                            )
                            continue
                        if key == "agent_result_ref":
                            try:
                                result_payload = _read_json(agent_path)
                            except json.JSONDecodeError:
                                violations.append(
                                    f"{agent_path}: agent result must be valid json object"
                                )
                            else:
                                if not isinstance(result_payload, dict):
                                    violations.append(
                                        f"{agent_path}: agent result must be json object"
                                    )
                                else:
                                    result_run_id = result_payload.get("agent_run_id")
                                    if (
                                        not isinstance(result_run_id, str)
                                        or result_run_id.strip() != run_id
                                    ):
                                        violations.append(
                                            f"{agent_path}:agent_run_id must equal agent_runs agent_run_id ({run_id})"
                                        )
                        else:
                            if not agent_path.is_file():
                                violations.append(
                                    f"{runs_path}:line {idx + 1} {key} target must be file ({ref_token})"
                                )
                                continue
                            summary_text = agent_path.read_text(encoding="utf-8", errors="ignore")
                            if not summary_text.strip():
                                violations.append(
                                    f"{runs_path}:line {idx + 1} {key} target must be non-empty ({ref_token})"
                                )
                            elif not _has_informative_agent_summary(summary_text):
                                violations.append(
                                    f"{runs_path}:line {idx + 1} agent.summary.txt must include status and output_refs or failure reason"
                                )

                    request_ref = launch_refs.get("launch_request_ref")
                    prompt_ref = launch_refs.get("launch_prompt_ref")
                    if request_ref is not None and prompt_ref is not None:
                        request_path = workspace_path.parent / request_ref
                        if request_path.exists():
                            try:
                                request_payload = _read_json(request_path)
                            except json.JSONDecodeError:
                                violations.append(
                                    f"{request_path}: launch request must be valid json object"
                                )
                            else:
                                if not isinstance(request_payload, dict):
                                    violations.append(
                                        f"{request_path}: launch request must be json object"
                                    )
                                else:
                                    payload_prompt_ref = request_payload.get("launch_prompt_ref")
                                    if (
                                        not isinstance(payload_prompt_ref, str)
                                        or payload_prompt_ref.strip() != prompt_ref
                                    ):
                                        violations.append(
                                            f"{request_path}:launch_prompt_ref must equal agent_runs launch_prompt_ref ({prompt_ref})"
                                        )

                    response_ref = launch_refs.get("launch_response_ref")
                    reply_ref = launch_refs.get("launch_reply_ref")
                    if response_ref is not None and reply_ref is not None:
                        response_path = workspace_path.parent / response_ref
                        if response_path.exists():
                            try:
                                response_payload = _read_json(response_path)
                            except json.JSONDecodeError:
                                violations.append(
                                    f"{response_path}: launch response must be valid json object"
                                )
                            else:
                                if not isinstance(response_payload, dict):
                                    violations.append(
                                        f"{response_path}: launch response must be json object"
                                    )
                                else:
                                    launch_session_id = _extract_launch_response_agent_session_id(
                                        response_payload
                                    )
                                    if launch_session_id is None:
                                        violations.append(
                                            f"{response_path}: child agent identifier missing from launch response"
                                        )
                                    else:
                                        if (
                                            isinstance(agent_session_id, str)
                                            and agent_session_id.strip()
                                            and agent_session_id.strip() != launch_session_id
                                        ):
                                            violations.append(
                                                f"{response_path}: child agent identifier must equal agent_runs agent_session_id ({agent_session_id})"
                                            )
                                    payload_reply_ref = response_payload.get("launch_reply_ref")
                                    if (
                                        not isinstance(payload_reply_ref, str)
                                        or payload_reply_ref.strip() != reply_ref
                                    ):
                                        violations.append(
                                            f"{response_path}:launch_reply_ref must equal agent_runs launch_reply_ref ({reply_ref})"
                                        )
                                    launch_reply = response_payload.get("launch_reply")
                                    if (
                                        isinstance(launch_reply, str)
                                        and re.fullmatch(r"[^\n]+ launched\.", launch_reply.strip()) is not None
                                    ):
                                        violations.append(
                                            f"{response_path}: launch_reply must not be generic launched-only text"
                                        )

                                    child_response_path = (
                                        workspace_path.parent
                                        / expected_agent_prefix
                                        / "child.response.json"
                                    )
                                    if not child_response_path.exists():
                                        violations.append(
                                            f"{child_response_path}: missing"
                                        )
                                    else:
                                        try:
                                            child_response_payload = _read_json(child_response_path)
                                        except json.JSONDecodeError:
                                            violations.append(
                                                f"{child_response_path}: launch response must be valid json object"
                                            )
                                        else:
                                            if child_response_payload != response_payload:
                                                violations.append(
                                                    f"{child_response_path}: must equal launches response payload"
                                                )

                    context_id = item.get("context_id")
                    if isinstance(context_id, str) and _is_sequential_agent_token(context_id):
                        violations.append(
                            f"{runs_path}:line {idx + 1} context_id must not be sequential placeholder ({context_id})"
                        )

        # Children that a `reopen-phase` cross-phase retry consumed AND that were
        # diverted to agent_runs_invalid.jsonl (terminal-payload validation, e.g. an
        # unauthorized write). record-launch wrote their agent_graph edge before the
        # run, and _prune_orphan_agent_graph_edges deliberately KEEPS that edge (so an
        # UN-consumed invalid attempt still surfaces). Once reopen has superseded such
        # a run, the kept edge must not block pass — mirror the same-named exemption in
        # _validate_orchestration_completion_for_pass (orchestration_runtime.py). Both
        # loads are fail-tolerant: a missing/corrupt file widens the requirement back to
        # every edge rather than wedging the gate.
        superseded_arids: set[str] = set()
        superseded_path = orchestration_dir / "reopen" / "superseded_runs.json"
        if superseded_path.is_file():
            try:
                superseded_doc = _read_json(superseded_path)
            except (OSError, json.JSONDecodeError):
                superseded_doc = None
            superseded_ids = (
                superseded_doc.get("superseded_agent_run_ids")
                if isinstance(superseded_doc, dict)
                else superseded_doc
            )
            if isinstance(superseded_ids, list):
                superseded_arids = {
                    s.strip() for s in superseded_ids if isinstance(s, str) and s.strip()
                }
        invalid_arids: set[str] = set()
        invalid_runs_path = orchestration_dir / "agent_runs_invalid.jsonl"
        if invalid_runs_path.is_file():
            try:
                invalid_lines = invalid_runs_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                invalid_lines = []
            for raw_invalid in invalid_lines:
                token = raw_invalid.strip()
                if not token:
                    continue
                try:
                    invalid_item = json.loads(token)
                except json.JSONDecodeError:
                    continue
                if not isinstance(invalid_item, dict):
                    continue
                invalid_arid = invalid_item.get("agent_run_id")
                if isinstance(invalid_arid, str) and invalid_arid.strip():
                    invalid_arids.add(invalid_arid.strip())
        superseded_invalid_arids = superseded_arids & invalid_arids

        for edge_idx, parent_id, child_id in graph_edges:
            parent_role = run_roles.get(parent_id)
            child_role = run_roles.get(child_id)
            if parent_role is None:
                violations.append(
                    f"{graph_path}:edges[{edge_idx}] parent_agent_run_id not found in agent_runs.jsonl ({parent_id})"
                )
                continue
            if child_role is None:
                if child_id in inflight_exempt_arids:
                    # The live judge declared this run as its own in-flight
                    # agent_run_id (--in-flight-agent-run-id) and the launch
                    # request confirms it is the validate/judge substep; its
                    # agent_runs entry is appended only after it returns. Record
                    # the graph evidence so the validate step_result exemption
                    # below requires this same dangling edge.
                    #
                    # We exempt ONLY the missing-child record. The parent role is
                    # already known from agent_runs.jsonl and must still be valid:
                    # a substep can never be a parent, regardless of the in-flight
                    # child, so keep failing closed on that malformed hierarchy.
                    observed_inflight_dangling.add(child_id)
                    if parent_role == "substep":
                        violations.append(
                            f"{graph_path}:edges[{edge_idx}] substep must not be parent role"
                        )
                    continue
                if child_id in superseded_invalid_arids:
                    # A reopen-consumed unauthorized-write trigger: superseded by
                    # reopen-phase AND diverted to agent_runs_invalid.jsonl (no
                    # agent_runs.jsonl row). Its edge is deliberately KEPT by
                    # _prune_orphan_agent_graph_edges so an UN-consumed invalid attempt
                    # still fails; once reopen has consumed and superseded it, tolerate
                    # the kept edge. The tight superseded-AND-invalid conjunction keeps
                    # an un-consumed invalid terminal attempt (not in superseded_runs)
                    # and an arbitrarily corrupt edge failing closed. Mirrors the
                    # same-named exemption in _validate_orchestration_completion_for_pass.
                    #
                    # As with the in-flight exemption above, this tolerates ONLY the
                    # missing-child record. The parent role is known from
                    # agent_runs.jsonl and the hierarchy invariant still holds: a
                    # substep can never be a parent, so keep failing closed on that
                    # malformed edge.
                    if parent_role == "substep":
                        violations.append(
                            f"{graph_path}:edges[{edge_idx}] substep must not be parent role"
                        )
                    continue
                violations.append(
                    f"{graph_path}:edges[{edge_idx}] child_agent_run_id not found in agent_runs.jsonl ({child_id})"
                )
                continue
            if parent_role == "orchestration" and child_role not in {"step", "substep"}:
                violations.append(
                    f"{graph_path}:edges[{edge_idx}] orchestration parent must connect to step or substep child"
                )
            if parent_role == "step" and child_role != "substep":
                violations.append(
                    f"{graph_path}:edges[{edge_idx}] step parent must connect to substep child"
                )
            if parent_role == "substep":
                violations.append(
                    f"{graph_path}:edges[{edge_idx}] substep must not be parent role"
                )

        # The in-flight judge's own validate step_result.json is written only
        # after it returns. Exempt the validate step from the missing-result
        # check ONLY for a declared in-flight judge that was actually observed as
        # a dangling agent_graph edge (graph evidence it is the live, not-yet-
        # recorded child). Without that evidence — a stale/mistyped arid, or a
        # judge that already has an agent_runs entry — a missing validate
        # step_result is a real gap and must surface rather than be masked.
        for arid, node_safe in inflight_exempt_node_safe.items():
            if arid not in observed_inflight_dangling:
                continue
            key = (node_safe, "validate")
            if key in step_coverage:
                step_coverage[key] = True

        for run_id, role_l in run_roles.items():
            item = run_records[run_id]
            parent = item.get("parent_agent_run_id")
            if role_l == "step":
                if not isinstance(parent, str) or not parent.strip():
                    continue
                parent_role = run_roles.get(parent.strip())
                if parent_role not in {None, "orchestration"}:
                    violations.append(
                        f"{runs_path}:agent_run_id={run_id} step parent must be orchestration role"
                    )
            if role_l == "substep":
                if not isinstance(parent, str) or not parent.strip():
                    continue
                parent_role = run_roles.get(parent.strip())
                if parent_role not in {None, "step", "orchestration"}:
                    violations.append(
                        f"{runs_path}:agent_run_id={run_id} substep parent must be orchestration or step role"
                    )

        steps_root = orchestration_dir / "steps"
        if not steps_root.exists() or not steps_root.is_dir():
            violations.append(f"{steps_root}: missing")
            continue

        for node_safe in node_safes:
            for step in REQUIRED_WORKFLOW_STEPS:
                step_dir = steps_root / node_safe / step
                if not step_dir.exists() or not step_dir.is_dir():
                    continue
                result_files = sorted(step_dir.glob("*/step_result.json"))
                if result_files:
                    step_coverage[(node_safe, step)] = True
                    for result_path in result_files:
                        executor_run_id = result_path.parent.name
                        executor_role = run_roles.get(executor_run_id)
                        if executor_role is None:
                            violations.append(
                                f"{result_path}: parent directory must match existing executor agent_run_id ({executor_run_id})"
                            )
                        elif step in SUBSTEP_WORKFLOW_STEPS and executor_role not in {"orchestration", "step"}:
                            violations.append(
                                f"{result_path}: parent directory must be orchestration or step role agent_run_id ({executor_run_id})"
                            )
                        elif step not in SUBSTEP_WORKFLOW_STEPS and executor_role != "step":
                            violations.append(
                                f"{result_path}: parent directory must be step role agent_run_id ({executor_run_id})"
                            )
                        step_run_record = run_records.get(executor_run_id, {})
                        run_step = step_run_record.get("step")
                        if executor_role == "step" and isinstance(run_step, str) and run_step.strip().lower() != step:
                            violations.append(
                                f"{result_path}: step directory name ({step}) does not match agent_runs step field ({run_step})"
                            )
                        run_node_key = step_run_record.get("node_key")
                        if isinstance(run_node_key, str) and run_node_key.strip():
                            run_node_safe = _node_key_to_safe(run_node_key)
                            if run_node_safe is not None and run_node_safe != node_safe:
                                violations.append(
                                    f"{result_path}: node_key mismatch between step_result path ({node_safe}) and agent_runs ({run_node_key})"
                                )
                        try:
                            result_data = _read_json(result_path)
                        except json.JSONDecodeError:
                            violations.append(f"{result_path}: invalid json")
                            continue
                        if not isinstance(result_data, dict):
                            violations.append(f"{result_path}: must be json object")
                            continue
                        required_outputs = result_data.get("required_outputs")
                        failed_substeps = result_data.get("failed_substeps")
                        if not isinstance(required_outputs, list):
                            violations.append(
                                f"{result_path}:required_outputs must be list"
                            )
                        if not isinstance(failed_substeps, list):
                            violations.append(
                                f"{result_path}:failed_substeps must be list"
                            )
                        executor = result_data.get("executor_agent_run_id")
                        if not isinstance(executor, str) or not executor.strip():
                            violations.append(
                                f"{result_path}:executor_agent_run_id must be non-empty string"
                            )
                        elif executor.strip() != executor_run_id:
                            violations.append(
                                f"{result_path}:executor_agent_run_id must match step_result directory agent_run_id"
                            )
                        substep_run_ids = result_data.get("substep_agent_run_ids")
                        if not isinstance(substep_run_ids, list):
                            violations.append(
                                f"{result_path}:substep_agent_run_ids must be list"
                            )
                        elif step in SUBSTEP_WORKFLOW_STEPS and not substep_run_ids:
                            violations.append(
                                f"{result_path}:substep_agent_run_ids must be non-empty list for {step}"
                            )
                        else:
                            for sub_idx, sub_id in enumerate(substep_run_ids):
                                if not isinstance(sub_id, str) or not sub_id.strip():
                                    violations.append(
                                        f"{result_path}:substep_agent_run_ids[{sub_idx}] must be non-empty string"
                                    )
                                    continue
                                sub_item = run_records.get(sub_id.strip())
                                sub_role = run_roles.get(sub_id.strip())
                                if sub_role != "substep":
                                    violations.append(
                                        f"{result_path}:substep_agent_run_ids[{sub_idx}] must reference substep role run"
                                    )
                                    continue
                                parent = sub_item.get("parent_agent_run_id")
                                if not isinstance(parent, str) or parent.strip() != executor_run_id:
                                    violations.append(
                                        f"{result_path}:substep_agent_run_ids[{sub_idx}] parent_agent_run_id must equal executor_agent_run_id"
                                    )

    if not has_orchestration_role:
        violations.append(
            f"{orchestrations_root}: agent_runs.jsonl must include orchestration role"
        )
    if not has_step_role:
        step_required = any(
            covered and step not in SUBSTEP_WORKFLOW_STEPS
            for (_, step), covered in step_coverage.items()
        )
        if step_required:
            violations.append(
                f"{orchestrations_root}: agent_runs.jsonl must include step role"
            )
    if not has_substep_role:
        substep_required = any(
            covered and step in SUBSTEP_WORKFLOW_STEPS
            for (_, step), covered in step_coverage.items()
        )
        if substep_required:
            violations.append(
                f"{orchestrations_root}: agent_runs.jsonl must include substep role"
            )

    missing_step_results = [
        f"{node_safe}/{step}"
        for (node_safe, step), covered in sorted(step_coverage.items())
        if not covered
    ]
    if missing_step_results:
        violations.append(
            f"{orchestrations_root}: missing step_result.json for {missing_step_results}"
        )


def _resolve_ir_dir(repo_root: Path, workspace_root: str, raw_ir_ref: str) -> Path:
    workspace_path = (repo_root / workspace_root).resolve()
    candidate = Path(raw_ir_ref)
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(workspace_path.resolve())
    except ValueError as exc:
        raise ValueError(
            f"ir_ref must be under {workspace_path}: {candidate}"
        ) from exc
    ir_root = workspace_path / "ir"
    try:
        candidate.relative_to(ir_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"ir_ref must be under {ir_root}: {candidate}"
        ) from exc
    return candidate


def _resolve_pipeline_dir_for_stage(
    repo_root: Path, workspace_root: str, raw_pipeline_ref: str
) -> Path:
    workspace_path = (repo_root / workspace_root).resolve()
    candidate = Path(raw_pipeline_ref)
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(workspace_path.resolve())
    except ValueError as exc:
        raise ValueError(
            f"pipeline_root must be under {workspace_path}: {candidate}"
        ) from exc
    pipelines_root = workspace_path / "pipelines"
    try:
        candidate.relative_to(pipelines_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"pipeline_root must be under {pipelines_root}: {candidate}"
        ) from exc
    return candidate


def _plan_dependency_node_key(ir_dir: Path) -> str | None:
    """The node_key of the node this IR belongs to, as the compile stage sees it.

    The unified IR carries its own node_key under ``dependency.node_key`` and ``meta.node_key``;
    nothing writes it at the top level. Reading only the top level silently returned None for every
    real IR, which disabled every node_key-conditioned compile gate (notably the multidimensional
    problem state_contract checks) and deferred those violations to Validate.
    """
    dep_path = ir_dir / "spec.ir.yaml"
    if not dep_path.exists():
        return None
    try:
        data = _read_yaml(dep_path)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    sections: list[Any] = [
        data.get("dependency"),
        data.get("meta"),
        data,
    ]
    for section in sections:
        if not isinstance(section, dict):
            continue
        nk = section.get("node_key")
        if isinstance(nk, str) and nk.strip():
            return nk.strip()
    return None


def _try_load_optional_plan_yaml(ir_dir: Path, name: str, violations: list[str]) -> None:
    path = ir_dir / name
    if not path.exists():
        return
    try:
        data = _read_yaml(path)
    except yaml.YAMLError:
        violations.append(f"{path}: invalid yaml")
        return
    if data is None:
        violations.append(f"{path}: must be non-null yaml document")
        return
    if not isinstance(data, dict):
        violations.append(f"{path}: must be mapping at top level")


def validate_compile_stage(
    repo_root: Path,
    workspace_root: str,
    ir_ref: str,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_compile_stage_impl(repo_root, workspace_root, ir_ref)


def _validate_compile_stage_impl(
    repo_root: Path,
    workspace_root: str,
    ir_ref: str,
) -> list[str]:
    violations: list[str] = []
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"]
    try:
        ir_dir = _resolve_ir_dir(repo_root, workspace_root, ir_ref)
    except ValueError as exc:
        return [str(exc)]

    derived_path = ir_dir / "spec.ir.yaml"
    direct_spec_vars: set[str] | None = None
    if not derived_path.exists():
        violations.append(f"{derived_path}: missing")
    else:
        _validate_io_contract_file(repo_root, derived_path, violations)
        direct_spec_vars = _extract_spec_var_names(derived_path)

    algo_path = ir_dir / "spec.ir.yaml"
    if not algo_path.exists():
        violations.append(f"{algo_path}: missing")
    else:
        nk = _plan_dependency_node_key(ir_dir)
        _validate_algorithm_contract_file(
            repo_root,
            algo_path,
            violations,
            multidim_node_key=nk,
            direct_spec_vars=direct_spec_vars,
        )

    for optional in ("spec.ir.yaml", "spec.ir.yaml", "spec.ir.yaml"):
        _try_load_optional_plan_yaml(ir_dir, optional, violations)
    _validate_ir_source_refs_tests(repo_root, ir_dir, violations)
    _validate_ir_meta_json(ir_dir, violations)
    _validate_compile_dependency_consistency(repo_root, ir_dir, violations)
    _validate_component_dep_operations(repo_root, ir_dir, violations)
    _validate_component_dep_operations_membership(repo_root, ir_dir, violations)
    _validate_local_operation_lowering(repo_root, ir_dir, violations)
    _validate_test_predicates(repo_root, ir_dir, violations)
    _validate_case_ids(ir_dir, violations)
    _validate_infrastructure_public_api(repo_root, ir_dir, violations)
    _validate_component_public_api(repo_root, ir_dir, violations)
    _validate_harness_dependency_consistency(repo_root, ir_dir, violations)
    _validate_harness_render_preconditions(repo_root, ir_dir, violations)

    return violations


def _validate_case_ids(ir_dir: Path, violations: list[str]) -> None:
    """Every ``case.test_case_set[].case_id`` is a filesystem-and-Fortran-safe token — for ALL
    node kinds, not only the M3c host-rendered ones.

    A case_id becomes a PATH at runtime: every node's runner writes its per-case snapshot to
    ``raw/state_snapshots/<case_id>.json`` (the argv the conductor's ``read_case_ids`` builds), so
    a case_id like ``../../evil`` makes the (honest, cleanly-compiling) runner write OUTSIDE the
    run directory. The M3c renderer's ``_case_ids`` gate bounds only the literals IT embeds, so it
    covers host-rendered nodes; a non-M3c leaf-authored runner has no such gate. This spec-input
    gate closes the gap uniformly at Compile — a violation routes to ``compile.generate``, and the
    id must match the same ``[A-Za-z0-9._-]`` (no ``..``) grammar the renderer pins."""
    from tools.runner_renderer import _CASE_ID_TOKEN_RE

    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return  # missing IR already flagged upstream
    try:
        ir = _read_yaml(derived_path)
    except yaml.YAMLError:
        return  # malformed IR already flagged upstream
    if not isinstance(ir, dict):
        return
    case_block = ir.get("case")
    tcs = case_block.get("test_case_set") if isinstance(case_block, dict) else None
    if not isinstance(tcs, list):
        return
    unsafe: list[str] = []
    for c in tcs:
        if not isinstance(c, dict):
            continue
        cid = c.get("case_id")
        if not isinstance(cid, str):
            continue
        token = cid.strip()
        if token and (not _CASE_ID_TOKEN_RE.match(token) or ".." in token):
            unsafe.append(token)
    if unsafe:
        violations.append(
            f"{derived_path}: case.test_case_set has case_id(s) {sorted(set(unsafe))} that are "
            "not safe tokens; a case_id is concatenated into the per-case snapshot path "
            "(raw/state_snapshots/<case_id>.json), so it must match [A-Za-z0-9._-] with no '..' "
            "(else the run writes outside its directory)")


def _validate_component_dep_operations(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """Deterministic compile gate: every ``component/`` direct dependency must author a
    NON-EMPTY ``operations`` list (each entry a non-empty string).

    The failure this pins is a closure fail_closed with no repairable signal. The
    generate-side gate (``_validate_dependency_operation_on_model_files``) requires a
    component dep's model to ``use <dep>_model`` + ``call <dep>__*`` UNCONDITIONALLY, while
    the host injects that dependency's published call-site interfaces
    (``_resolve_dependency_facts``) keyed off the IR's ``operations`` list. When Compile
    authors ``operations: []`` (an authoring wobble — the 7/19 run authored the real op
    names, 7/21 authored ``[]``), the injected ``<dependency_facts>`` name no ops, yet the
    leaf must still emit the calls — so a pure (tool-less) leaf invents symbol names /
    argument orders that ``Generate.syntax`` rejects every retry until the budget is
    exhausted. Pinning non-emptiness at Compile catches the wobble at IR-authoring time and
    routes (via ``classify_compile_static_failure`` -> ``COMPILE_STATIC_FAILURE_ROUTING``)
    back to ``compile.generate`` for a warm re-author. The host also carries a resume-safe
    fallback (``_resolve_dependency_facts`` surfaces all ``<dep>__`` subroutines when
    ``operations`` is empty) so an already-certified wobbly IR still converges; this gate is
    the forward-looking pin that stops the wobble from being certified in the first place.

    Only ``component/`` deps are gated: an ``infrastructure`` (harness) dep correctly
    authors ``operations: []`` (the physics leaf never calls the harness API — the
    host-rendered runner is the sole caller), and ``profile`` / ``problem`` deps are not
    called through the ``<dep>__*`` operation surface. No-op on a missing / unparseable IR
    (already flagged upstream) or a node with no component dependency."""
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return
    try:
        ir = _read_yaml(derived_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return
    if not isinstance(ir, dict):
        return
    dep = ir.get("dependency")
    direct_deps = dep.get("direct_deps") if isinstance(dep, dict) else None
    if not isinstance(direct_deps, list):
        return
    for entry in direct_deps:
        # Resolve the node_key whether the entry is a bare string or a dict. Walk
        # `direct_deps` directly (NOT `_component_dep_spec_ids`, which drops the entry
        # shape we must inspect here — bare string vs. dict, and the `operations` field).
        if isinstance(entry, str):
            node_key = entry.strip()
            ops = None
            ops_present = False
        elif isinstance(entry, dict):
            nk = entry.get("node_key")
            node_key = nk.strip() if isinstance(nk, str) else ""
            ops = entry.get("operations")
            ops_present = "operations" in entry
        else:
            continue
        if not node_key.startswith("component/"):
            continue
        # EVERY entry must be a non-empty string — a single malformed entry (a non-string or
        # blank) is enough to fail, not merely "no valid entry exists". A mixed list like
        # `["dep__op", 3, ""]` is certified-malformed IR: the dependency-facts resolver
        # silently drops the invalid entries, so certifying them hides an authoring error the
        # (unambiguous, structural) gate should catch here.
        ops_is_list = isinstance(ops, list)
        invalid_ops = (
            [o for o in ops if not (isinstance(o, str) and o.strip())]
            if ops_is_list else []
        )
        if ops_is_list and ops and not invalid_ops:
            continue  # non-empty list, every entry a non-empty string
        if isinstance(entry, str):
            detail = "is declared as a bare string (no `operations` field)"
        elif not ops_present:
            detail = "is missing its `operations` field"
        elif not ops_is_list:
            detail = f"has a non-list `operations` ({type(ops).__name__})"
        elif not ops:
            detail = "has an empty `operations: []`"
        elif len(invalid_ops) == len(ops):
            detail = "has an `operations` list with no valid (non-empty string) entries"
        else:
            detail = (
                f"has `operations` entries that are not non-empty strings ({invalid_ops!r}); "
                "every entry must name a `<dep_spec_id>__*` subroutine"
            )
        violations.append(
            f"{derived_path}: component dependency {node_key!r} {detail}; a component "
            "dependency must author a non-empty `operations` list naming the "
            "`<dep_spec_id>__*` subroutines this node calls. The generate gate requires "
            "the model to `use <dep>_model` + `call <dep>__*`, and the host injects those "
            "call-site interfaces from `operations`; an empty list starves the injected "
            "<dependency_facts> while the calls are still required, so the leaf cannot "
            "converge (its retry budget exhausts)"
        )


def _validate_component_dep_operations_membership(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """L3 deterministic compile gate: every ``component/`` direct dependency's declared
    ``operations`` (when NON-EMPTY and all-valid — the empty / malformed shapes are
    ``_validate_component_dep_operations``'s province) must be a SUBSET of that dependency's
    published operation names, as resolved host-side into the ``dependency_surface.json``
    sidecar the conductor authors at compile-phase start.

    This determinizes the old V4c-ii "operations ⊆ published" check that phase_01 left to the
    LLM as 'not deterministically checkable'. Once L1 pins each component's public op names into
    its certified IR, the published surface IS a deterministic input (the sidecar), so a
    fabricated dep operation name (the 2026-07-23 ``__apply`` drift) is an unambiguous structural
    failure. The violation message embeds the FULL certified catalog + its source tag for the
    dep, because the warm-resume slim repair prompt carries only findings — the leaf must see the
    real names here to converge.

    Inert (skip) when: the sidecar file is absent (a legacy tree / a non-conductor-driven
    compile), a dep has NO sidecar entry (an unresolved graph edge), or its entry's ``source`` is
    ``unresolved`` (the surface could not be resolved — never manufacture a violation from a
    resolution gap). A sidecar that is unreadable, or an entry whose ``published_operations`` is
    the wrong shape, is fail-closed. The skip→fail-closed asymmetry is a deliberate rollout
    affordance; its removal after fleet re-auth is tracked in
    ``docs/design/deterministic_followups.md``. No-op on a missing/unparseable IR or a node
    with no component dependency."""
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return
    try:
        ir = _read_yaml(derived_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return
    if not isinstance(ir, dict):
        return
    dep = ir.get("dependency")
    direct_deps = dep.get("direct_deps") if isinstance(dep, dict) else None
    if not isinstance(direct_deps, list):
        return

    sidecar_path = ir_dir / "dependency_surface.json"
    if not sidecar_path.is_file():
        return  # inert: no sidecar authored (legacy tree / not a conductor-driven compile)
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        violations.append(
            f"{derived_path}: dependency_surface.json sidecar is unreadable or malformed")
        return
    surface_by_key: dict[str, dict] = {}
    entries = sidecar.get("dependencies") if isinstance(sidecar, dict) else None
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("node_key"), str):
                surface_by_key[e["node_key"].strip()] = e

    for entry in direct_deps:
        if not isinstance(entry, dict):
            continue
        nk = entry.get("node_key")
        node_key = nk.strip() if isinstance(nk, str) else ""
        if not node_key.startswith("component/"):
            continue
        ops = entry.get("operations")
        # Only a non-empty, all-valid operations list is membership-checked here; the empty /
        # malformed shapes are _validate_component_dep_operations' province (double-reporting
        # them here would only add noise).
        if not isinstance(ops, list) or not ops:
            continue
        declared = [o.strip() for o in ops if isinstance(o, str) and o.strip()]
        if len(declared) != len(ops):
            continue  # malformed entry — handled by _validate_component_dep_operations

        # Exact-node_key lookup (incl. `@version`): the sidecar is keyed by the graph's resolved
        # node_keys. A version-drifted `direct_deps` entry — which the version-AGNOSTIC
        # `_validate_compile_dependency_consistency` would still pass — finds no entry and this
        # gate goes inert (fail-open, consistent with the tracked rollout affordance); the leaf is
        # shown the full versioned node_key in the injected catalog, so a drift is unlikely.
        surface = surface_by_key.get(node_key)
        if not isinstance(surface, dict):
            continue  # inert: no sidecar entry (unresolved graph edge / legacy)
        source_tag = str(surface.get("source") or "").strip()
        if source_tag == "unresolved":
            continue  # inert: surface could not be resolved — not a violation
        published_raw = surface.get("published_operations")
        if not isinstance(published_raw, list):
            violations.append(
                f"{derived_path}: dependency_surface.json entry for {node_key!r} is malformed "
                "(published_operations is not a list)")
            continue
        published = {
            p.strip() for p in published_raw if isinstance(p, str) and p.strip()
        }
        if not published:
            # A resolved-but-EMPTY published surface (a degenerate component publishing nothing,
            # or a certified source with no `<dep_spec_id>__` subroutine) cannot meaningfully
            # bound a subset — checking against it would reject EVERY authored op, a
            # non-convergent loop. Treat it like `unresolved` (inert), matching the
            # compile.generate renderer (`_build_dependency_surface_facts`), which sends the leaf
            # to the dependency's §5 for an empty surface; a genuinely wrong call is still caught
            # downstream at Generate.
            continue
        # Fortran symbols are case-INsensitive, so compare casefolded (matching the L1b/L1c
        # sibling gates and the `use`/`call` the linker resolves): a case-variant of a real
        # published name links fine and must not be a false reject.
        published_cf = {p.casefold() for p in published}
        unknown = [d for d in declared if d.casefold() not in published_cf]
        if unknown:
            catalog = ", ".join(sorted(published)) or "(none)"
            violations.append(
                f"{derived_path}: component dependency {node_key!r} declares operation(s) "
                f"{unknown} that are NOT in its published surface. The published operations of "
                f"{node_key} are exactly: [{catalog}] (source: {source_tag or 'unknown'}). "
                "Author dependency.direct_deps[].operations using ONLY names from that "
                "published set (copy the operation_id verbatim); a fabricated name starves the "
                "injected <dependency_facts> and the pure leaf cannot converge (its retry "
                "budget exhausts)"
            )


_LOWERING_STEP_STRUCTURAL_KEYS = frozenset(
    {"step_id", "step_kind", "operation_ref", "inputs", "outputs"}
)


def _validate_local_operation_lowering(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """Deterministic compile gate (presence floor): every LOCAL operation an algorithm invokes
    must carry SOME lowering signal in the IR — it may not be a bare, unelaborated
    ``operation_ref`` string with nothing behind it.

    A LOCAL op is one that ``algorithm.steps[].operation_ref`` names but that is NOT resolved
    through a direct dependency (no ``dependency.direct_deps[].operations[]`` entry matches the
    same ``<dep_spec_id>__<op>`` ABI string — the same exact-string flavour
    ``_validate_component_dep_operations`` uses; the ABI name is identical on both sides, so no
    normalization is needed). A dependency-resolved op is lowered by the callee, not here.

    The failure this pins is a name-only Compile authoring wobble: the IR lists a LOCAL op as a
    step's ``operation_ref`` but supplies no formula, no derived-field rule, no elaboration —
    the ``p0_interface_reconstruct`` shape a from-scratch component author can emit. The pure
    (tool-less) Generate leaf has only the IR to work from, so a name with no lowering behind it
    forces it to INVENT the physics — a divergence the completeness of the numerics cannot be
    checked against at Compile. Catching the *absence of any signal* here routes (via
    ``classify_compile_static_failure`` -> ``COMPILE_STATIC_FAILURE_ROUTING``, the existing
    ``compile.generate`` warm-reopen wiring — this is one check inside the existing compile
    stage, NOT a new substep) back for a re-author.

    This is a PRESENCE FLOOR only: it never inspects the CONTENT of a formula for correctness or
    completeness. A present-but-wrong or present-but-incomplete formula is out of scope here and
    remains the province of ``Compile.verify`` V2 (the ``major`` remand for name-only /
    under-specified lowering). A false reject is always avoidable by Compile writing the op name
    into a derived-field rule / invariant, or a one-line ``description`` on the step — the same
    thing the verify remand asks for.

    Lowering signal (op passes if ANY of the three holds):
      1. Some step that references the op carries a non-empty string value under a key OTHER than
         the structural set ``{step_id, step_kind, operation_ref, inputs, outputs}`` (e.g. a
         ``description``).
      2. Some referencing step's ``inputs ∪ outputs`` intersects the ``derived_field_rules[].name``
         set — the real advdiff flux/boundary shape, where the formula lives in a dfr entry keyed
         by the step's output (or, for the input guard, its ``guard_pass`` input) rather than by
         the op name.
      3. The op name — full, or the bare tail after stripping a ``<spec_id>__`` prefix — appears
         as a substring anywhere in the concatenation of every string value of every
         ``derived_field_rules`` entry (keys vary: ``rule`` / ``definition`` / ``constraint`` /
         ``notes`` …) plus every ``invariants[]`` entry — the real euler/harness shape.

    ``infrastructure/`` nodes (the harness) are EXEMPT as a whole: their ops are the runner glue
    governed by the §5 ``public_api`` gate family, not the physics lowering surface. When the
    node_key is absent the gate applies (default-on). No-op on a missing / unparseable IR or a
    node with no LOCAL op."""
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return
    try:
        ir = _read_yaml(derived_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return
    if not isinstance(ir, dict):
        return

    node_key = _plan_dependency_node_key(ir_dir)
    if isinstance(node_key, str) and node_key.startswith("infrastructure/"):
        return  # harness ops are governed by the §5 public_api gate family

    algorithm = ir.get("algorithm")
    if not isinstance(algorithm, dict):
        return
    steps = algorithm.get("steps")
    if not isinstance(steps, list) or not steps:
        return

    # Ops resolved through a direct dependency are lowered by the callee, not here.
    dep = ir.get("dependency")
    direct_deps = dep.get("direct_deps") if isinstance(dep, dict) else None
    dep_ops: set[str] = set()
    if isinstance(direct_deps, list):
        for entry in direct_deps:
            if isinstance(entry, dict):
                # `or []` would only guard a FALSY value; a truthy non-list scalar
                # (`operations: 5`) still crashes the loop. isinstance-guard like the sibling
                # `_validate_component_dep_operations` — a malformed shape is flagged there, not
                # crashed here (this gate's contract is no-op / no-crash on a malformed IR).
                ops = entry.get("operations")
                if isinstance(ops, list):
                    for op in ops:
                        if isinstance(op, str) and op.strip():
                            dep_ops.add(op.strip())

    # derived_field_rules: name set (signal 2) + a blob of every string value (signal 3),
    # joined with invariants[].
    dfr = algorithm.get("derived_field_rules")
    dfr_names: set[str] = set()
    text_parts: list[str] = []
    if isinstance(dfr, list):
        for item in dfr:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                dfr_names.add(name.strip())
            for value in item.values():
                if isinstance(value, str):
                    text_parts.append(value)
    invariants = algorithm.get("invariants")
    if isinstance(invariants, list):  # a scalar `invariants:` is flagged by the contract gate
        for inv in invariants:
            if isinstance(inv, str):
                text_parts.append(inv)
    lowering_blob = "\n".join(text_parts)

    # Group the referencing steps per LOCAL op, in first-appearance order, keeping each op's
    # first step index/id for the message.
    local_ops: list[str] = []
    op_steps: dict[str, list[dict]] = {}
    op_first: dict[str, tuple[int, str]] = {}
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        op_ref = step.get("operation_ref")
        if not isinstance(op_ref, str) or not op_ref.strip():
            continue
        op = op_ref.strip()
        if op in dep_ops:
            continue  # dependency-resolved: lowered by the callee
        if op not in op_steps:
            local_ops.append(op)
            op_steps[op] = []
            sid = step.get("step_id")
            op_first[op] = (idx, sid.strip() if isinstance(sid, str) and sid.strip() else "")
        op_steps[op].append(step)

    for op in local_ops:
        if _op_has_lowering_signal(op, op_steps[op], dfr_names, lowering_blob):
            continue
        first_idx, first_sid = op_first[op]
        where = f"steps[{first_idx}]"
        if first_sid:
            where += f" (step_id {first_sid!r})"
        violations.append(
            f"{derived_path}: local operation {op!r} (first referenced at {where}) carries no "
            "lowering signal in the IR — no per-step elaboration (a non-structural field such as "
            "`description`), no `derived_field_rules` entry keyed by a step input/output, and no "
            "mention of the op name in any derived_field_rules / invariants text. A LOCAL "
            "operation (one not resolved through a `dependency.direct_deps[].operations[]` entry) "
            "must be lowered in the IR so the pure Generate leaf need not invent the physics from "
            "a bare name. This is a presence floor: adding the op name to a derived_field_rule / "
            "invariant, or a one-line step `description`, satisfies it. Judging whether a "
            "present formula is COMPLETE remains the province of Compile.verify V2 (the `major` "
            "remand for under-specified lowering)."
        )


def _op_has_lowering_signal(
    op: str, steps: list[dict], dfr_names: set[str], lowering_blob: str
) -> bool:
    """True when the LOCAL ``op`` carries any of the three lowering signals (see
    ``_validate_local_operation_lowering``). Presence-only: never inspects formula content."""
    for step in steps:
        # Signal 1: a non-structural, non-empty string field on a referencing step.
        for key, value in step.items():
            if key in _LOWERING_STEP_STRUCTURAL_KEYS:
                continue
            if isinstance(value, str) and value.strip():
                return True
        # Signal 2: a referencing step's inputs ∪ outputs names a derived_field_rules entry.
        io_tokens: set[str] = set()
        for field_name in ("inputs", "outputs"):
            values = step.get(field_name)
            if isinstance(values, list):
                for token in values:
                    if isinstance(token, str) and token.strip():
                        io_tokens.add(token.strip())
        if io_tokens & dfr_names:
            return True
    # Signal 3: the op name (full or bare `<spec_id>__` tail) is mentioned in the lowering text.
    bare_tail = op.split("__", 1)[1] if "__" in op else op
    if op in lowering_blob:
        return True
    if bare_tail and bare_tail in lowering_blob:
        return True
    return False


def _validate_harness_dependency_consistency(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """R1/M3c-β deterministic compile gate: an M3c physics node that declares an
    ``infrastructure`` (runner-harness) dependency must declare EXACTLY the harness derived
    from its own target — ``harness_<language>_<target.class>`` (e.g. ``harness_fortran_cpu``).
    A wrong / multiple / mistyped harness dependency is caught at Compile (cheap) rather than
    surfacing as a render / link failure, because the runner glue is host-rendered against
    exactly this harness.

    No-op when the node declares NO infrastructure dependency (a pre-M3c legacy node keeps
    its leaf-authored runner — the mass opt-in is M3d) or when the node is itself an
    infrastructure node. Routes (via ``classify_compile_static_failure``) back to
    ``compile.generate`` to re-author ``dependency.direct_deps``."""
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return
    try:
        ir = _read_yaml(derived_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return
    if not isinstance(ir, dict):
        return
    meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
    if str(meta.get("spec_kind") or "").strip() == "infrastructure":
        return
    infra = _infra_direct_dep_node_keys(ir)
    if not infra:
        return  # no harness dependency declared -> legacy path, nothing to pin
    impl = ir.get("impl_defaults") if isinstance(ir.get("impl_defaults"), dict) else {}
    tc = impl.get("toolchain") if isinstance(impl.get("toolchain"), dict) else {}
    target = impl.get("target") if isinstance(impl.get("target"), dict) else {}
    language = str(tc.get("language") or "").strip().lower()
    hw_class = str(target.get("class") or "").strip().lower()
    if not language or not hw_class:
        violations.append(
            f"{derived_path}: node declares an infrastructure dependency but "
            "impl_defaults.toolchain.language / impl_defaults.target.class is missing — "
            "cannot derive the expected harness id")
        return
    expected = f"harness_{language}_{hw_class}"
    if len(infra) != 1:
        violations.append(
            f"{derived_path}: a physics node must declare exactly one infrastructure (harness) "
            f"dependency; found {infra} (expected the single {expected!r})")
        return
    dep_spec = _spec_id_from_node_key(infra[0])
    if dep_spec != expected:
        violations.append(
            f"{derived_path}: declared infrastructure dependency {infra[0]!r} (spec_id "
            f"{dep_spec!r}) does not match the harness derived from this node's target "
            f"(language={language}, class={hw_class}): expected {expected!r}")


def _validate_harness_render_preconditions(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """R1/M3c-β deterministic compile gate: pre-empt every Compile-authored render
    precondition of an M3c physics node's host-rendered runner.

    A harness-backed (M3c) node's ``<spec_id>_runner.f90`` is rendered host-side by the
    conductor (``runner_renderer.render_runner``) from the IR alone. Any IR *content* the
    renderer cannot faithfully render (a ``time_variable`` other than the harness's fixed key
    ``t``; a snapshot variable colliding with a reserved key ``t``/``case_id``/``step``; an
    unparseable ``shape_expr`` or rank>4; a ``verdict.fields`` outside the harness fold surface
    ``{overall, failed_checks}``; a required raw variable absent from the snapshot schema; a test
    no predicate targets) makes ``render_runner`` raise ``RenderError``. That render
    runs *inside the conductor* before the Generate substeps, so its fail_closed kills the whole
    workflow rather than retrying — the E2E #3 failure class (orch ``…dcf0533e``): a
    Compile-authored value caught at an unrecoverable position.

    This gate mirrors those preconditions at Compile (cheap, pre-Generate) by delegating to
    ``runner_renderer.ir_content_violations``, which INVOKES ``render_runner`` itself with the
    same ``(ir, spec_id, harness_spec_id)`` the conductor's ``_write_runner`` uses — an exact
    mirror by construction, so a defect routes back to ``compile.generate`` (warm re-author)
    instead. The renderer keeps every assertion as a defense-in-depth backstop.

    EXCLUDED (by ``ir_content_violations``, via ``RenderError.identity``): node-identity defects
    a re-author cannot repair — the spec_id / derived-name length and >1 infra dep. These are
    NOT hoisted here (routing an unrepairable defect to a warm-resume retry would only spin).
    Neither identity defect can reach the render backstop from a live run. M3d bounds spec_id
    length at SPEC-INPUT, before any phase runs: ``runner_renderer.spec_id_length_violation`` is
    the canonical capture point, enforced unconditionally by ``resolve_node`` (workflow_conductor)
    and mirrored over the whole closure by run_workflow's dependency visit — so a spec_id over 55
    is an early, clear rejection rather than a late workflow-kill, and the derived
    ``<spec_id>_runner``/``_checks``/``_model`` names (spec_id + 7) stay inside the f2008 63-char
    limit. A node declaring >1 infrastructure dep is not M3c (``_conductor_authors_runner``
    requires exactly one), so its runner is never host-rendered. The renderer keeps both as
    defense-in-depth backstops. The catalog's former over-length offender (a 61-char
    ``advection_diffusion`` profile node) has since been renamed, and no catalog ``spec_id``
    now exceeds the bound — the gate stands as a guard on future additions.

    No-op only when the node is not M3c (legacy leaf-authored runner)."""
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return
    try:
        ir = _read_yaml(derived_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return
    if not isinstance(ir, dict) or not _ir_is_m3c_physics(ir):
        return
    # Derive the node's spec_id from its node IDENTITY — the same source the conductor's
    # `_write_runner` uses (`refs.spec_id`, from the node key), NOT the optional `meta.spec_id`.
    # Gating on `meta.spec_id` was a defect: a harness-backed IR with `meta.spec_id` absent or
    # stale skipped this gate while the conductor still host-rendered (using the node key) and
    # fail-closed at render on the very content errors this gate must hoist (time_variable,
    # reserved keys). Prefer `dependency.node_key` (gated for consistency against the
    # dependency_graph sidecar), then `meta.spec_id`, then a placeholder — and never skip. The
    # CONTENT preconditions hoisted here are independent of the spec_id VALUE (only the excluded
    # identity/length checks use it), so any non-empty spec_id surfaces them identically.
    dep = ir.get("dependency") if isinstance(ir.get("dependency"), dict) else {}
    node_key = dep.get("node_key")
    spec_id = _spec_id_from_node_key(node_key) if isinstance(node_key, str) and node_key else None
    if not spec_id:
        meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
        ms = meta.get("spec_id")
        spec_id = ms.strip() if isinstance(ms, str) and ms.strip() else "node"
    # Mirror the conductor's `_write_runner`: the harness spec_id is the single infrastructure
    # direct dep's spec_id (`_ir_is_m3c_physics` guarantees exactly one).
    infra = _infra_direct_dep_node_keys(ir)
    if len(infra) != 1:  # defensive; _ir_is_m3c_physics already pins this
        return
    harness_sid = infra[0].partition("@")[0].partition("/")[2]
    from tools.runner_renderer import ir_content_violations

    for msg in ir_content_violations(ir, spec_id.strip(), harness_sid):
        violations.append(f"{derived_path}: {msg}")


def _validate_public_api_name_surface(
    derived_path: Path,
    spec_ops: set[str],
    spec_types: set[str],
    public_api: dict,
    violations: list[str],
) -> None:
    """Set-equality of the IR ``public_api`` NAME surface against the controlled_spec §5 name
    lists: ``public_api.published_operations[].operation_id`` == ``spec_ops`` and
    ``public_api.published_types`` == ``spec_types``. Appends one violation per missing / extra
    name. Shared by the infrastructure gate (which then ALSO pins the §5.1 signatures /
    module_parameters) and the component gate (``_validate_component_public_api``, NAMES ONLY).
    The messages are language-neutral (they name §5, not any backend), so both callers reuse
    them verbatim."""
    ops_raw = public_api.get("published_operations")
    ir_ops = {
        entry["operation_id"].strip()
        for entry in (ops_raw if isinstance(ops_raw, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("operation_id"), str)
        and entry["operation_id"].strip()
    }
    for missing in sorted(spec_ops - ir_ops):
        violations.append(
            f"{derived_path}:public_api.published_operations omits controlled_spec §5 "
            f"operation_id '{missing}'")
    for extra in sorted(ir_ops - spec_ops):
        violations.append(
            f"{derived_path}:public_api.published_operations declares operation_id '{extra}' "
            "absent from controlled_spec §5")

    types_raw = public_api.get("published_types")
    ir_types = {
        token.strip()
        for token in (types_raw if isinstance(types_raw, list) else [])
        if isinstance(token, str) and token.strip()
    }
    for missing in sorted(spec_types - ir_types):
        violations.append(
            f"{derived_path}:public_api.published_types omits controlled_spec §5 "
            f"derived type '{missing}'")
    for extra in sorted(ir_types - spec_types):
        violations.append(
            f"{derived_path}:public_api.published_types declares type '{extra}' "
            "absent from controlled_spec §5")


def _validate_component_public_api(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """L1 deterministic public-API gate (``component`` nodes only): the IR's ``public_api``
    must enumerate EXACTLY the published operation NAME surface the controlled_spec §5 declares
    ("The only published ``operation_id`` is ...") — NAMES ONLY.

    This is the source-of-truth pin for a component's public op names. Without it the pure
    generate leaf re-picks a component's public op name on every regeneration (the 2026-07-23
    closure fail: the three advdiff components each authored a fresh name — ``__compute_flux`` /
    ``__advance`` / ``__apply`` — and the profile consumer then authored a fabricated dep
    ``operations`` entry the facts resolver silently dropped, starving the leaf's mandatory
    ``call`` until the retry budget exhausted). Pinning the names into the certified component IR
    lets a consumer's Compile be shown a real catalog (the L2 ``dependency_surface.json`` sidecar)
    and lets L1b prove the generated source realizes exactly those names.

    Unlike the infrastructure gate this pins NAMES ONLY: ``signatures`` and ``module_parameters``
    are FORBIDDEN keys on a component (the argument ABI stays derived post-hoc from the certified
    source at Build — freezing an unverified signature into a component IR both breaks the spec's
    language-neutrality and pins an ABI no gate checks). §5 is parsed with the same
    ``_parse_public_api_from_controlled_spec`` the infrastructure gate uses; it extracts exactly
    the component's one published op across the whole component corpus.

    Fail-closed, mirroring the infrastructure gate: a missing/unresolvable controlled_spec ref, a
    §5 parsing to zero operations, an absent ``public_api``, or a forbidden ``signatures`` /
    ``module_parameters`` key is a violation that routes (via ``classify_compile_static_failure``)
    back to ``compile.generate``. No-op on a non-component node or a missing/unparseable IR
    (flagged upstream)."""
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return  # missing IR already flagged upstream
    try:
        ir = _read_yaml(derived_path)
    except yaml.YAMLError:
        return  # malformed IR already flagged upstream
    if not isinstance(ir, dict):
        return

    meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
    if meta.get("spec_kind") != "component":
        return  # name-surface pin is component-only (infra has its own fuller gate)

    spec_id = meta.get("spec_id")
    if not isinstance(spec_id, str) or not spec_id.strip():
        violations.append(
            f"{derived_path}:meta.spec_id missing "
            "(required to pin a component node's public_api to controlled_spec §5)")
        return
    spec_id = spec_id.strip()

    source_refs = meta.get("source_refs") if isinstance(meta.get("source_refs"), dict) else {}
    cs_ref = source_refs.get("controlled_spec")
    if not isinstance(cs_ref, str) or not cs_ref.strip():
        violations.append(
            f"{derived_path}:meta.source_refs.controlled_spec missing "
            "(cannot pin component public_api to §5)")
        return
    cs_path = Path(cs_ref.strip())
    if not cs_path.is_absolute():
        cs_path = repo_root / cs_path
    if not _is_readable_file(cs_path):
        violations.append(
            f"{derived_path}:controlled_spec ({cs_ref}) unresolvable "
            "(cannot pin component public_api to §5)")
        return

    spec_ops, spec_types = _parse_public_api_from_controlled_spec(cs_path, spec_id)
    if not spec_ops:
        violations.append(
            f"{derived_path}:controlled_spec ({cs_ref}) §5 parsed 0 published operation_ids "
            "(unrecognized published-operation form — cannot pin component public_api)")
        return

    public_api = ir.get("public_api")
    if not isinstance(public_api, dict):
        violations.append(
            f"{derived_path}:public_api missing — a component node must enumerate its "
            "controlled_spec §5 published operation NAMES (public_api.published_operations, "
            "and public_api.published_types if any)")
        return

    # FORBIDDEN keys: a component pins names only. `signatures` / `module_parameters` belong to
    # an infrastructure node (a full §5.1 signature pin); on a component they would freeze an ABI
    # no gate checks and break the spec's backend-agnostic language-neutrality.
    for forbidden in ("signatures", "module_parameters"):
        if forbidden in public_api:
            violations.append(
                f"{derived_path}:public_api.{forbidden} is forbidden on a component node — a "
                "component publishes operation NAMES only (the argument ABI is derived from the "
                "certified source at Build, never frozen into the IR)")

    _validate_public_api_name_surface(
        derived_path, spec_ops, spec_types, public_api, violations)


def _validate_infrastructure_public_api(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """R1 deterministic public-API gate (``infrastructure`` nodes only): the IR's
    ``public_api`` block must enumerate EXACTLY the published surface the controlled_spec
    §5 declares ("the published operation_ids are exactly: ..."). An infrastructure node
    (the R1 runner harness) exists to publish a reusable operation surface that consuming
    physics-node runners link against; if Compile→IR drops a published operation that no
    single test exercises as its primary op (e.g. a helper emitter or a writer), Generate
    never publishes it and the runner reimplements it locally — defeating the harness. This
    gate pins the surface at Compile (cheap, pre-Generate) instead of leaving the drift for
    the ~17-min Generate.verify leaf to catch nondeterministically.

    Checks: ``meta.spec_kind == "infrastructure"`` (else no-op — physics nodes have no
    exact-published contract; their interface is derived post-hoc). The controlled_spec is
    resolved via ``meta.source_refs.controlled_spec`` and its §5 parsed with
    ``_parse_public_api_from_controlled_spec``; the IR's
    ``public_api.published_operations[].operation_id`` set must equal the §5 operation set
    and ``public_api.published_types`` must equal the §5 derived-type set. The IR's
    ``public_api.signatures`` and ``public_api.module_parameters`` are additionally pinned == the
    §5.1 canonical interface block (the leaf's only carrier of the signature bodies and the
    module-parameter values, since Generate.generate is walled off from controlled_spec). Fail-closed
    (never a silent no-op): a missing/unresolvable controlled_spec ref, a §5 parsing to zero
    operations, or an absent ``public_api`` block is itself a violation.

    A violation routes (via ``classify_compile_static_failure``) back to ``compile.generate``
    to re-author the IR's ``public_api``."""
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return  # missing IR already flagged upstream
    try:
        ir = _read_yaml(derived_path)
    except yaml.YAMLError:
        return  # malformed IR already flagged upstream
    if not isinstance(ir, dict):
        return

    meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
    if meta.get("spec_kind") != "infrastructure":
        return  # exact-published-surface contract is infrastructure-only

    # The §5.1 signature pin renders the structured signatures to the target language, and only a
    # Fortran backend exists (tools/lang_backend_fortran). A non-Fortran infrastructure node is
    # fail-closed here — never silently rendered as Fortran and compared against non-Fortran source
    # — until its language backend is implemented. (No such node exists yet: the sole harness is
    # fortran/cpu.) The §5 published-NAME surface is language-neutral, but without a signature
    # backend the node cannot certify at all, so stop early with one clear message.
    impl = ir.get("impl_defaults") if isinstance(ir.get("impl_defaults"), dict) else {}
    tc = impl.get("toolchain") if isinstance(impl.get("toolchain"), dict) else {}
    language = str(tc.get("language") or "").strip().lower()
    if language and language != "fortran":
        violations.append(
            f"{derived_path}: infrastructure signature pinning has only a Fortran language backend "
            f"(tools/lang_backend_fortran); a '{language}' infrastructure node needs its own backend "
            "before its §5.1 / public_api.signatures can be pinned")
        return

    spec_id = meta.get("spec_id")
    if not isinstance(spec_id, str) or not spec_id.strip():
        violations.append(
            f"{derived_path}:meta.spec_id missing "
            "(required to pin an infrastructure node's public_api to controlled_spec §5)")
        return
    spec_id = spec_id.strip()

    source_refs = meta.get("source_refs") if isinstance(meta.get("source_refs"), dict) else {}
    cs_ref = source_refs.get("controlled_spec")
    if not isinstance(cs_ref, str) or not cs_ref.strip():
        violations.append(
            f"{derived_path}:meta.source_refs.controlled_spec missing "
            "(cannot pin infrastructure public_api to §5)")
        return
    cs_path = Path(cs_ref.strip())
    if not cs_path.is_absolute():
        cs_path = repo_root / cs_path
    if not _is_readable_file(cs_path):
        violations.append(
            f"{derived_path}:controlled_spec ({cs_ref}) unresolvable "
            "(cannot pin infrastructure public_api to §5)")
        return

    spec_ops, spec_types = _parse_public_api_from_controlled_spec(cs_path, spec_id)
    if not spec_ops:
        violations.append(
            f"{derived_path}:controlled_spec ({cs_ref}) §5 parsed 0 published operation_ids "
            "(unrecognized 'published operation_ids are exactly' form — cannot pin public_api)")
        return

    public_api = ir.get("public_api")
    if not isinstance(public_api, dict):
        violations.append(
            f"{derived_path}:public_api missing — an infrastructure node must enumerate its "
            "complete controlled_spec §5 published surface (public_api.published_operations "
            "and public_api.published_types)")
        return

    _validate_public_api_name_surface(
        derived_path, spec_ops, spec_types, public_api, violations)

    # §5.1 canonical interface block: cross-check its signature set against §5's name lists so
    # the two halves of the spec (prose surface + machine-readable signatures) cannot drift, and
    # pin the IR's public_api.signatures AND public_api.module_parameters == §5.1 so the
    # Generate.generate leaf — which is walled off from controlled_spec.md (phase_02 §2-1) —
    # carries the exact signatures and module-parameter values to publish in its IR. The signature
    # bodies and the parameter declarations are pinned against the GENERATED source separately by
    # the Generate.static gate (_validate_infrastructure_generated_signatures).
    op_stanzas, type_stanzas, iface_err = _parse_canonical_interface_from_controlled_spec(cs_path)
    if iface_err:
        violations.append(
            f"{derived_path}:controlled_spec ({cs_ref}) §5.1 {iface_err} — the canonical "
            "interface block must fence exactly the §5 published surface")
        return
    iface_ops = set(op_stanzas)
    iface_types = set(type_stanzas)
    for missing in sorted(spec_ops - iface_ops):
        violations.append(
            f"{derived_path}:controlled_spec §5.1 omits a signature for §5 operation_id "
            f"'{missing}'")
    for extra in sorted(iface_ops - spec_ops):
        violations.append(
            f"{derived_path}:controlled_spec §5.1 declares a procedure signature '{extra}' "
            "absent from the §5 operation list")
    for missing in sorted(spec_types - iface_types):
        violations.append(
            f"{derived_path}:controlled_spec §5.1 omits a definition for §5 derived type "
            f"'{missing}'")
    for extra in sorted(iface_types - spec_types):
        violations.append(
            f"{derived_path}:controlled_spec §5.1 defines a derived type '{extra}' absent from "
            "the §5 derived-type list")

    # IR public_api.signatures == §5.1 (the leaf's only source of the signatures to publish).
    _validate_ir_signatures_against_section51(
        derived_path, public_api, op_stanzas, type_stanzas, violations)

    # IR public_api.module_parameters == §5.1 module_parameters (value-pinned). The Generate.static
    # gate pins these declarations (name AND value) against the GENERATED source, but the
    # Generate.generate leaf is walled off from controlled_spec.md — so, like the signatures, the IR
    # is the only carrier that gets the values (dp / case_id_len) to the leaf. Pin them here.
    _validate_ir_module_parameters_against_section51(
        derived_path, public_api, cs_path, violations)


def _split_top_level_commas(text: str) -> list[str]:
    """Split ``text`` on commas that are not inside ``()`` / ``[]`` — so an entity list
    ``a(:), b(2,2), c`` splits into ``a(:)`` / ``b(2,2)`` / ``c`` (the comma inside ``(2,2)`` is
    NOT a separator)."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _declaration_atoms(logical_line: str) -> list[str]:
    """Canonicalize a declaration into one line per declared entity, so a combined declarator
    (``integer, intent(in) :: a, b`` — legal Fortran, ABI-identical, and explicitly permitted by
    §5.1's "formatting may differ") compares equal to the one-per-line form. Non-declarations (a
    ``subroutine``/``function`` header, an ``end`` line — no ``::``) pass through unchanged. The
    shared type-spec + attributes (before ``::``) is prefixed onto each comma-separated entity of
    the entity list (after ``::``, split on top-level commas so an array-spec comma stays intact).
    """
    if "::" not in logical_line:
        return [logical_line]
    lhs, _sep, rhs = logical_line.partition("::")
    entities = [e.strip() for e in _split_top_level_commas(rhs)]
    entities = [e for e in entities if e]
    if not entities:
        return [logical_line]
    lhs = lhs.strip()
    return [f"{lhs} :: {entity}" for entity in entities]


def _stanza_atoms(lines: list[str]) -> tuple[str, ...]:
    """Ordered, normalized, per-entity atoms of a stanza — every declaration split into one atom
    per declared name (``_declaration_atoms``) then normalized. This is the canonical comparison
    unit for both gates: it makes combined vs one-per-line declarations, and formatting /
    continuation / comment / case / whitespace differences, all compare equal, while a genuine
    name / type / rank / intent / component drift still differs. A closing ``end [type|subroutine|
    function] [name]`` is canonicalized to drop the optional trailing name (bare ``end type`` — the
    common Fortran style — is byte-for-ABI identical to ``end type <name>``; the type/proc name is
    already pinned by the header)."""
    out: list[str] = []
    for line in lines:
        line = _canonicalize_end_line(line)
        for atom in _declaration_atoms(line):
            norm = _normalize_fortran_line(atom)
            if norm:
                out.append(norm)
    return tuple(out)


def _stanza_line_set(lines: list[str]) -> frozenset[str]:
    """Per-entity atom SET of a stanza — used where declaration order is immaterial (a procedure's
    dummy-argument declarations, which Fortran permits in any order and which the header line
    already pins for call order)."""
    return frozenset(_stanza_atoms(lines))


def _stanza_line_list(lines: list[str]) -> tuple[str, ...]:
    """Per-entity atom LIST of a stanza, order preserved — used where order is part of the contract
    (a derived type's component layout; a verbatim IR transcription)."""
    return _stanza_atoms(lines)


def _validate_ir_signatures_against_section51(
    derived_path: Path,
    public_api: dict[str, Any],
    op_stanzas: dict[str, list[str]],
    type_stanzas: dict[str, list[str]],
    violations: list[str],
) -> None:
    """Pin the IR's ``public_api.signatures`` == the controlled_spec §5.1 canonical interface
    block. Each entry is ``{symbol, signature}`` (``symbol`` names the published op/type;
    ``signature`` is its language-neutral structured signature — Objective B). Validation: every
    entry is a mapping with a non-empty string ``symbol`` + a mapping ``signature``; the Fortran
    backend renders each ``signature`` to EXACTLY ONE stanza whose own header name equals the
    declared ``symbol`` (a lying ``symbol`` or a struct that renders to no/many stanzas is
    fail-closed); no symbol is declared twice; the symbol set equals §5.1's; and each symbol's
    normalized stanza LIST equals §5.1's (ordered — a derived type's component layout is part of the
    §5 compatibility contract, so a component reorder must NOT be accepted). A drift here becomes a
    drift in the model the Generate leaf transcribes, so it is a Compile fail to Compile.generate."""
    from tools.lang_backend_fortran import SignatureParseError, render_symbol_to_fortran

    spec51: dict[str, tuple[str, ...]] = {}
    for name, lines in {**op_stanzas, **type_stanzas}.items():
        spec51[name] = _stanza_line_list(lines)

    sigs_raw = public_api.get("signatures")
    if not isinstance(sigs_raw, list) or not sigs_raw:
        violations.append(
            f"{derived_path}:public_api.signatures missing — an infrastructure node must "
            "transcribe every controlled_spec §5.1 signature (each {symbol, signature}) so the "
            "Generate leaf can publish them verbatim")
        return

    ir_stanzas: dict[str, tuple[str, ...]] = {}
    for idx, entry in enumerate(sigs_raw):
        if not isinstance(entry, dict):
            violations.append(
                f"{derived_path}:public_api.signatures[{idx}] is not a mapping "
                "(expected {symbol, signature})")
            continue
        symbol = entry.get("symbol")
        signature = entry.get("signature")
        if not isinstance(symbol, str) or not symbol.strip():
            violations.append(
                f"{derived_path}:public_api.signatures[{idx}] missing a non-empty 'symbol'")
            continue
        symbol = symbol.strip()
        if not isinstance(signature, dict) or not signature:
            violations.append(
                f"{derived_path}:public_api.signatures['{symbol}'] missing a mapping 'signature' "
                "(a language-neutral structured signature)")
            continue
        try:
            interface = render_symbol_to_fortran(signature)
        except SignatureParseError as exc:
            violations.append(
                f"{derived_path}:public_api.signatures['{symbol}'] signature is not renderable: {exc}")
            continue
        e_ops, e_types, e_errors = _parse_interface_stanzas(interface)
        for err in e_errors:
            violations.append(f"{derived_path}:public_api.signatures['{symbol}'] {err}")
        parsed = {**e_ops, **e_types}
        if len(parsed) != 1:
            violations.append(
                f"{derived_path}:public_api.signatures['{symbol}'].signature must render to exactly "
                f"one signature stanza (found {len(parsed)})")
            continue
        (parsed_name, parsed_lines), = parsed.items()
        if parsed_name != symbol:
            violations.append(
                f"{derived_path}:public_api.signatures['{symbol}'].signature declares a different "
                f"symbol '{parsed_name}'")
            continue
        if symbol in ir_stanzas:
            violations.append(
                f"{derived_path}:public_api.signatures declares symbol '{symbol}' more than once")
            continue
        ir_stanzas[symbol] = _stanza_line_list(parsed_lines)

    for missing in sorted(set(spec51) - set(ir_stanzas)):
        violations.append(
            f"{derived_path}:public_api.signatures omits controlled_spec §5.1 signature "
            f"'{missing}'")
    for extra in sorted(set(ir_stanzas) - set(spec51)):
        violations.append(
            f"{derived_path}:public_api.signatures declares a signature '{extra}' absent from "
            "controlled_spec §5.1")
    for name in sorted(set(spec51) & set(ir_stanzas)):
        # A derived type's component ORDER is part of the §5 compatibility contract, so it is
        # compared as an ordered list; a procedure's dummy-declaration order is Fortran-immaterial
        # (the header line already pins call order) and is compared as a set.
        if name in type_stanzas:
            match = ir_stanzas[name] == spec51[name]
        else:
            match = frozenset(ir_stanzas[name]) == frozenset(spec51[name])
        if not match:
            violations.append(
                f"{derived_path}:public_api.signatures['{name}'] does not match controlled_spec "
                "§5.1 (argument name/type/rank/intent/result or component-layout/order drift)")


def _validate_ir_module_parameters_against_section51(
    derived_path: Path,
    public_api: dict[str, Any],
    cs_path: Path,
    violations: list[str],
) -> None:
    """Pin the IR's ``public_api.module_parameters`` == the controlled_spec §5.1 module-level
    parameters (``dp = float64`` / ``case_id_len = 64``), by name AND value. These are part of the
    published ABI, and the Generate.static gate value-pins them against the GENERATED source — but
    the Generate.generate leaf is walled off from controlled_spec.md (phase_02 §2-1), so the IR is
    the only carrier that gets the values to the leaf. A drop / extra / value drift here becomes a
    drift (or a fail-closed Generate.static miss) in the generated model, so it is a Compile fail to
    Compile.generate.

    Validation (fail-closed): each §5.1 entry is ``{name, base?, value}``. Names are compared
    case-insensitively (Fortran identifiers are), so ``dp``/``DP`` are the same parameter. A §5.1
    that declares the same module-parameter name twice (even case-only) is itself a violation (it
    cannot be pinned coherently, and the un-deduped Generate.static source pin would demand
    contradictory declarations). If §5.1 declares zero module parameters, an absent IR
    ``module_parameters`` key passes; otherwise it is a violation.
    A present-but-non-list key fail-closes (mirrors ``load_structured_signatures``' present-but-null
    rule). Each IR entry must be a well-formed module parameter (``_validate_module_parameter``);
    a duplicate name, an omitted §5.1 name, an extra name, or a value drift is a violation. Values
    compare whitespace-and-case-insensitively (all whitespace removed, then case-folded — matching
    the Generate.static source pin), so YAML int ``64`` == string ``"64"`` and the neutral kind
    token ``float64`` == ``FLOAT64`` (both §5.1 and IR carry the neutral value); ``base`` is not
    compared (the validator constrains it to integer/absent and the renderer fixes it)."""
    from tools.lang_backend_fortran import SignatureParseError, _validate_module_parameter

    def _norm(value: Any) -> str:
        # Case-fold (Fortran identifiers are case-insensitive, so the neutral `float64` == `FLOAT64`)
        # and remove ALL whitespace, matching the Generate.static source pin (_stanza_atoms strips
        # every space). Applied to BOTH sides of the compare below (the IR value and the raw §5.1
        # value). The fold cannot make two DIFFERENT ABI values compare equal: the IR side is
        # validated by `_require_neutral_parameter_value` (via `_validate_module_parameter`) to be a
        # number or a `float64`/`float32` token — no internal whitespace, expression, or character
        # literal whose folded value would be ambiguous — so a §5.1 value that folds to match it must
        # be that same neutral value; a §5.1 value that is NOT neutral (a stray character literal,
        # `float 64`) either differs after folding (→ a value-drift violation) or is caught first by
        # the render-check (`_parse_canonical_interface_from_controlled_spec`) that also renders §5.1.
        return "".join(str(value).split()).lower()

    def _norm_name(name: str) -> str:
        # Fortran identifiers are case-insensitive, so `dp` and `DP` are the SAME module parameter.
        # Compare and dedupe on the case-folded name so a case-only "duplicate" (§5.1 declaring both
        # `dp` and `DP` with diverging values) cannot slip a contradictory declaration past this gate
        # into an unsatisfiable Generate.static pin, and a case-only §5.1↔IR name variant is not a
        # false drift.
        return name.strip().lower()

    # Build the §5.1 name->value map (keyed by case-folded name), but FAIL CLOSED on a duplicate name
    # rather than collapsing it (a plain dict comprehension keeps the last value). A repeated §5.1
    # module parameter cannot be pinned coherently: an identical duplicate would be accepted against a
    # single IR/source declaration, and a differing duplicate (`dp = float64` and `dp = float32`, or
    # the case-only `dp`/`DP`) would pass this map yet require BOTH contradictory `integer, parameter`
    # lines in the source at Generate.static (which renders the full list, un-deduped) — an
    # unsatisfiable contract that wedges Generate. Catch it here at Compile with a clear message.
    spec_params: dict[str, Any] = {}
    spec_dupe_names: list[str] = []
    for mp in _section51_module_parameters(cs_path):
        name = _norm_name(mp["name"])
        if name in spec_params:
            spec_dupe_names.append(name)
        else:
            spec_params[name] = mp["value"]
    for name in sorted(set(spec_dupe_names)):
        violations.append(
            f"{cs_path}: §5.1 declares module parameter '{name}' more than once (case-insensitively) "
            "— each module parameter must be declared once (a duplicate collapses the value pin and "
            "can require contradictory declarations in the generated source at Generate.static)")
    if spec_dupe_names:
        return

    if "module_parameters" not in public_api:
        if spec_params:
            violations.append(
                f"{derived_path}:public_api.module_parameters missing — an infrastructure node must "
                "transcribe every controlled_spec §5.1 module-level parameter (each {name, base?, "
                "value}); the Generate.generate leaf is walled off from controlled_spec so the IR "
                "is the only carrier of the pinned values")
        return

    mps_raw = public_api.get("module_parameters")
    if not isinstance(mps_raw, list):
        violations.append(
            f"{derived_path}:public_api.module_parameters must be a list (got "
            f"{type(mps_raw).__name__}); a present-but-null key must fail closed")
        return

    ir_params: dict[str, Any] = {}
    for idx, entry in enumerate(mps_raw):
        try:
            _validate_module_parameter(entry, f"public_api.module_parameters[{idx}]")
        except SignatureParseError as exc:
            violations.append(f"{derived_path}:{exc}")
            continue
        name = _norm_name(entry["name"])
        if name in ir_params:
            violations.append(
                f"{derived_path}:public_api.module_parameters declares parameter '{name}' more "
                "than once (case-insensitively)")
            continue
        ir_params[name] = entry["value"]

    for missing in sorted(set(spec_params) - set(ir_params)):
        violations.append(
            f"{derived_path}:public_api.module_parameters omits controlled_spec §5.1 module "
            f"parameter '{missing}'")
    for extra in sorted(set(ir_params) - set(spec_params)):
        violations.append(
            f"{derived_path}:public_api.module_parameters declares parameter '{extra}' absent from "
            "controlled_spec §5.1")
    for name in sorted(set(spec_params) & set(ir_params)):
        if _norm(ir_params[name]) != _norm(spec_params[name]):
            violations.append(
                f"{derived_path}:public_api.module_parameters['{name}'] value "
                f"'{ir_params[name]}' does not match controlled_spec §5.1 value "
                f"'{spec_params[name]}'")


# Sentinel embedded in the Generate.static stale-IR violation so the conductor can route it as a
# TERMINAL failure (fail_closed) rather than a warm Generate.generate retry: the leaf cannot mutate
# the certified IR, so retrying Generate is futile — the fix is a re-certification, not a re-author.
# workflow_conductor.Conductor._gate_static_check keys on this exact string.
STALE_DEPENDENCY_IR_MARKER = "[stale-dependency-ir]"


def _validate_infrastructure_generated_signatures(
    repo_root: Path, execution: NodeExecution, model_files: list[Path], violations: list[str]
) -> None:
    """R1/M3c-α deterministic signature gate (``infrastructure`` nodes only, ``Generate.static``):
    the generated model source must publish every §5.1 canonical signature verbatim (normalized:
    comments stripped, ``&`` continuations joined, case-folded, whitespace-insensitive).

    The Compile-stage ``_validate_infrastructure_public_api`` pins the published *names* (the IR's
    ``public_api`` set == §5 == §5.1). This gate pins the published *signatures*: each argument
    name, order, type, rank, ``intent``, and ``result`` name the §5.1 block declares must appear
    in the generated ``<spec_id>_model.f90``. It closes the known scope gap where the exact
    published signature of the generated ``.f90`` rested on the ~17-min ``Generate.verify`` leaf
    plus a Build link error — moving it to a cheap deterministic ``Generate.static`` check so a
    signature drift routes straight back to ``Generate.generate``.

    Infra-only: resolved via the IR ``meta.spec_kind``. A non-infra node is a no-op. But when the
    node is INFRASTRUCTURE (per its ``node_key``) yet the IR / §5.1 cannot be resolved, that is
    fail-closed, never a silent skip — Compile has already certified the IR + §5.1 exist, so their
    absence at Generate is a real regression that must not let a drifted model pass unchecked."""
    infra_by_key = str(execution.node_key).split("/", 1)[0].strip() == "infrastructure"

    def _fail_closed_if_infra(reason: str) -> None:
        if infra_by_key:
            violations.append(
                f"{execution.pipeline_dir}: infrastructure node's published signatures cannot be "
                f"pinned at Generate.static ({reason}) — Compile certified them, so this is "
                "fail-closed")

    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        _fail_closed_if_infra("IR unresolvable from lineage")
        return
    ir_path = ir_dir / "spec.ir.yaml"
    if not ir_path.is_file():
        _fail_closed_if_infra("IR spec.ir.yaml missing")
        return
    try:
        ir = _read_yaml(ir_path)
    except yaml.YAMLError:
        _fail_closed_if_infra("IR spec.ir.yaml is malformed YAML")
        return
    if not isinstance(ir, dict):
        _fail_closed_if_infra("IR spec.ir.yaml is not a mapping")
        return
    meta = ir.get("meta") if isinstance(ir.get("meta"), dict) else {}
    if meta.get("spec_kind") != "infrastructure":
        _fail_closed_if_infra("IR meta.spec_kind is not 'infrastructure'")
        return
    # Past this point the IR confirms an infrastructure node, so a missing/unresolvable
    # controlled_spec is fail-closed here too (Compile certified it resolves).
    source_refs = meta.get("source_refs") if isinstance(meta.get("source_refs"), dict) else {}
    cs_ref = source_refs.get("controlled_spec")
    if not isinstance(cs_ref, str) or not cs_ref.strip():
        _fail_closed_if_infra("IR meta.source_refs.controlled_spec missing")
        return
    cs_path = Path(cs_ref.strip())
    if not cs_path.is_absolute():
        cs_path = repo_root / cs_path
    if not _is_readable_file(cs_path):
        _fail_closed_if_infra(f"controlled_spec ({cs_ref}) unresolvable")
        return

    # Only a Fortran signature backend exists (tools/lang_backend_fortran); render+compare below is
    # Fortran. A non-Fortran infrastructure node is fail-closed rather than pinned against the wrong
    # language. (Compile's _validate_infrastructure_public_api already fail-closes it, so this is a
    # defense-in-depth stop; no non-Fortran infra node exists yet.)
    impl = ir.get("impl_defaults") if isinstance(ir.get("impl_defaults"), dict) else {}
    tc = impl.get("toolchain") if isinstance(impl.get("toolchain"), dict) else {}
    language = str(tc.get("language") or "").strip().lower()
    if language and language != "fortran":
        loc = model_files[0] if model_files else ir_path
        violations.append(
            f"{loc}: a '{language}' infrastructure node's signatures cannot be pinned — only a "
            "Fortran language backend is implemented (tools/lang_backend_fortran)")
        return

    op_stanzas, type_stanzas, iface_err = _parse_canonical_interface_from_controlled_spec(cs_path)
    if iface_err:
        violations.append(
            f"{cs_path}: §5.1 canonical interface block {iface_err} — cannot pin the generated "
            "model signatures against it")
        return

    # Backward-compatibility guard for a stale / pre-contract certified IR. An IR compiled BEFORE the
    # public_api.module_parameters contract carries no such key; a partially-migrated or corrupt one
    # can carry an empty/null list or drifted values. On a --resume into Generate, Compile.static does
    # NOT re-run, so that IR reaches here unvalidated; the Generate leaf now authors the `integer,
    # parameter` lines from public_api.module_parameters, so any of those shapes would make it emit
    # none/wrong and this gate would fail below with a confusing source drift that re-running Generate
    # can never repair. Run the SAME comparison Compile.static uses (IR module_parameters == §5.1 by
    # normalized name+value): ANY mismatch means the certified IR is stale/corrupt, so fail closed
    # with the actionable re-certify signal + the terminal marker, rather than a warm-retry drift.
    pub = ir.get("public_api")
    stale_ir_violations: list[str] = []
    _validate_ir_module_parameters_against_section51(
        ir_path, pub if isinstance(pub, dict) else {}, cs_path, stale_ir_violations)
    if stale_ir_violations:
        loc = model_files[0] if model_files else ir_path
        violations.append(
            f"{loc}: {STALE_DEPENDENCY_IR_MARKER} the certified IR at {ir_path} does not carry the "
            "controlled_spec §5.1 module parameters the current contract pins (absent, empty, null, "
            "or drifted public_api.module_parameters — a pre-contract or corrupt IR that "
            "Compile.static, skipped on this resume, would have rejected) — re-certify the harness "
            "(run_workflow.py --with-deps, which the harness version bump makes freshness re-run) so "
            "Compile transcribes the module-parameter values into the IR; a certified IR cannot be "
            "repaired by re-running Generate")
        return

    target = model_files[0] if model_files else (repo_root / "<model>")
    # Parse the generated source into per-symbol stanzas (a procedure stanza is header + its
    # declarations + body; a type stanza is its full block) so each pinned signature is checked
    # WITHIN its own procedure/type. A GLOBAL source line-set would let a drifted declaration in
    # one procedure be masked by an identical (correct) declaration in another — `intent(in) :: n`
    # is common — so the scoping is load-bearing, not cosmetic.
    combined = "\n".join(
        model_file.read_text(encoding="utf-8", errors="ignore") for model_file in model_files
    )
    src_ops, src_types, _src_errors = _parse_interface_stanzas(combined)
    src_lists: dict[str, tuple[str, ...]] = {}
    for name, lines in {**src_ops, **src_types}.items():
        src_lists[name] = _stanza_line_list(lines)

    for name in sorted({**op_stanzas, **type_stanzas}):
        spec_lines = op_stanzas.get(name) or type_stanzas.get(name) or []
        is_type = name in type_stanzas
        kind = "derived type" if is_type else "procedure"
        have = src_lists.get(name)
        if have is None:
            violations.append(
                f"{target}: generated model source does not publish controlled_spec §5.1 {kind} "
                f"'{name}' (no {kind} of that name/header found — the published surface must match "
                "the pinned §5.1 signature)")
            continue
        if is_type:
            # A derived type's WHOLE component layout — names, types, and ORDER, with nothing
            # inserted — is part of the compatibility contract (§5), so the source type block must
            # equal §5.1's atom list EXACTLY. Ordered-subsequence would accept an inserted extra
            # component (widening the published layout); set equality would accept a reorder.
            if have != _stanza_line_list(spec_lines):
                violations.append(
                    f"{target}: derived type '{name}' drifts from controlled_spec §5.1 — its "
                    "published component layout (names/types/order, no extras) does not match the "
                    "pinned definition")
            continue
        # A procedure's dummy-argument declarations may be in any order (Fortran-legal, and the
        # header line already pins call order), so membership — not order — is checked here.
        have_set = frozenset(have)
        for orig in spec_lines:
            missing_atoms = [a for a in _stanza_atoms([orig]) if a not in have_set]
            if missing_atoms:
                violations.append(
                    f"{target}: procedure '{name}' drifts from controlled_spec §5.1 — missing the "
                    f"pinned interface line `{orig.strip()}` (argument name/type/rank/intent/"
                    "result drift from the published surface)")

    # The §5.1 module-level `parameter` declarations (dp / case_id_len) are part of the published
    # ABI but are not stanzas; pin their exact declaration (name AND value) against the source —
    # a `case_id_len = 32` drift would otherwise be invisible (the symbolic decls still match). Use
    # per-entity atoms so a combined `integer, parameter :: dp = real64, case_id_len = 64` matches.
    all_src_atoms = frozenset(
        atom for line in _fortran_logical_lines(combined) for atom in _stanza_atoms([line])
    )
    # Defense-in-depth: `_parse_canonical_interface_from_controlled_spec` above already renders the
    # whole §5.1 struct and short-circuits (iface_err → return) on any parameter the backend cannot
    # lower, so a raise here is not reachable in the current gate order. Guard it anyway — this is
    # the lone backend render not already inside an `except SignatureParseError`, so a future reorder
    # or a new caller must fail closed with a clear violation, never crash the gate.
    from tools.lang_backend_fortran import SignatureParseError
    try:
        param_lines = _section51_parameter_lines(cs_path)
    except SignatureParseError as exc:
        violations.append(
            f"{target}: controlled_spec §5.1 declares a module parameter the language backend "
            f"cannot lower ({exc}) — re-certify the harness so §5.1 carries a neutral parameter "
            "value the generated source can be pinned against")
        param_lines = []
    for pline in param_lines:
        missing_atoms = [a for a in _stanza_atoms([pline]) if a not in all_src_atoms]
        if missing_atoms:
            violations.append(
                f"{target}: generated model source is missing the §5.1 module parameter "
                f"declaration `{pline.strip()}` (a drifted parameter value silently changes the "
                "published ABI)")


def _validate_test_predicates(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """R2 deterministic predicate gate: ``io_contract.test_predicates`` formalizes each
    ``tests.md`` test's pass rule as a machine-evaluable predicate that ``Validate.execute``
    evaluates against ``diagnostics.json`` to author ``verdict.json`` in-process (moving the
    per-test judgment off the judge leaf). This gate makes the DSL correct-by-construction at
    Compile so the judge-time nondeterminism it replaces cannot regress into a compile-time
    authoring error.

    Checks (via ``verdict_evaluator.validate_predicate_schema``): the DSL is present and
    well-formed (op / expected_outcome enums, non-empty ``pass_when.all``, each condition
    carries a ``value``); the predicate ``test_id`` set equals the node's canonical test-id set;
    every ``target_cases`` entry is a declared ``case.test_case_set`` case; and every predicate
    ``ref`` resolves against the declared diagnostics vocabulary — ``verdict.<field>`` /
    ``checks.<id>`` / a per-case metric address pinned in ``diagnostics_contract.metrics``.

    The canonical test-id set is resolved robustly (this gate is the SOLE enforcer of
    predicate-set == tests.md, so it must not silently no-op): the ``tests.md`` set via
    ``meta.source_refs.tests`` (present in every real IR) is preferred, and — as of the R2
    fail-closed hardening — a ``meta.source_refs.tests`` that resolves to a file yielding ZERO
    test_ids is itself a violation (never a silent fallback). Only when the ref is
    absent/unresolvable does it fall back to the same-IR ``io_contract.test_evidence_requirements``
    id set (note: the separate ``test_evidence_requirements`` gate resolves tests.md through the
    same ``meta.source_refs.tests`` ref and pins TER == tests.md, so on any IR that reaches this
    fallback the ref is already a violation — ``_validate_ir_source_refs_tests`` fails Compile on an
    absent or unresolvable ref — and the fallback is a defence-in-depth path, not a silent one that
    the TER gate is trusted to backstop). A degenerate IR carrying
    NEITHER (which fails the io_contract gate anyway) degrades to the predicate ids (a no-op).

    A separate necessary-condition gate (``verdict_evaluator.degenerate_predicate_violations``)
    additionally rejects a structurally-valid but DEGENERATE pass-test set — one where every
    ``expected_outcome=pass`` predicate asserts only ``verdict.*``, so the per-test judgment
    collapses back to the runner's own ``verdict.overall`` (the judge nondeterminism R2 removed).

    A violation routes (via ``classify_compile_static_failure``) back to ``compile.generate``
    to re-author the predicates."""
    from tools.verdict_evaluator import (
        validate_predicate_schema,
        degenerate_predicate_violations,
    )

    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.exists():
        return  # missing IR already flagged upstream
    try:
        ir = _read_yaml(derived_path)
    except yaml.YAMLError:
        return  # malformed IR already flagged upstream
    if not isinstance(ir, dict):
        return

    io_contract = ir.get("io_contract")
    io_contract = io_contract if isinstance(io_contract, dict) else {}
    predicates = io_contract.get("test_predicates")

    case_block = ir.get("case")
    tcs = case_block.get("test_case_set") if isinstance(case_block, dict) else None
    # `.strip()` so this set uses the SAME case_id identity as the runtime argv
    # (`read_case_ids`), the snapshot path, and `_case_id_to_test_ids` — all of which strip.
    # A surrounding-whitespace case_id would otherwise desync predicate membership from runtime.
    case_ids = {
        c["case_id"].strip()
        for c in (tcs if isinstance(tcs, list) else [])
        if isinstance(c, dict) and isinstance(c.get("case_id"), str) and c["case_id"].strip()
    }

    dc = io_contract.get("diagnostics_contract")
    dc = dc if isinstance(dc, dict) else {}
    check_ids = {
        str(c.get("id"))
        for c in (dc.get("checks") if isinstance(dc.get("checks"), list) else [])
        if isinstance(c, dict) and c.get("id")
    }
    # verdict.<field> refs resolve ONLY against the fields the diagnostics_contract actually
    # declares AND requires (no unconditional overall/failed_checks default): per phase_01 V3, a
    # predicate referencing verdict.* requires diagnostics_contract.verdict.required=true with
    # the referenced field in verdict.fields. When required is false the runner is NOT contracted
    # to emit a `verdict` object, so a verdict ref would pass Compile then become a
    # structural_violation at execute instead of being repaired here — hence the required gate.
    verdict_fields: set[str] = set()
    verdict_block = dc.get("verdict") if isinstance(dc.get("verdict"), dict) else {}
    if verdict_block.get("required") is True and isinstance(verdict_block.get("fields"), list):
        verdict_fields |= {str(f) for f in verdict_block["fields"]}
    metric_addrs = {
        str(m) for m in (dc.get("metrics") if isinstance(dc.get("metrics"), list) else [])
    }

    # Canonical test-id set. Prefer tests.md (via meta.source_refs.tests — present in every
    # real IR); fall back to the same-IR test_evidence_requirements set (which the io_contract
    # gate requires to cover tests.md); only a degenerate IR carrying neither degrades to the
    # predicate ids (a no-op — but that IR fails the io_contract gate on its own).
    tests_path = _tests_path_from_ir_document(repo_root, ir)
    test_ids: list[str] | None = None
    if tests_path is not None and _is_readable_file(tests_path):
        test_ids = _parse_test_ids_from_tests_md(tests_path)
        # Fail-closed: a RESOLVABLE tests.md that parses to ZERO test_ids means the file's
        # test-id form isn't recognized (a parser/format drift). Silently degrading to the
        # same-leaf test_evidence_requirements would let a common-mode drop of a tests.md test
        # (from both TER and the predicates) certify undetected. Flag it so the equality pin
        # is never a silent no-op when tests.md is present.
        if not test_ids:
            violations.append(
                f"{derived_path}:tests.md ({tests_path}) resolved but parsed 0 test_ids "
                "(unrecognized test-id form — cannot pin test_predicates set == tests.md)")
    # `not test_ids` (not `is None`): fall through to the same-IR fallback (TER, then predicate
    # ids) for the rest of the schema check so it does not ALSO spuriously report every predicate
    # as an unknown test_id; the resolved-but-empty case above already fails the gate.
    if not test_ids:
        ter = io_contract.get("test_evidence_requirements")
        ter_ids = [
            e["test_id"].strip()
            for e in (ter if isinstance(ter, list) else [])
            if isinstance(e, dict) and isinstance(e.get("test_id"), str) and e["test_id"].strip()
        ]
        if ter_ids:
            test_ids = ter_ids
    if not test_ids:
        test_ids = [
            p["test_id"].strip()
            for p in (predicates if isinstance(predicates, list) else [])
            if isinstance(p, dict) and isinstance(p.get("test_id"), str) and p["test_id"].strip()
        ]

    for msg in validate_predicate_schema(
        predicates,
        case_ids=case_ids,
        test_ids=test_ids,
        check_ids=check_ids,
        verdict_fields=verdict_fields,
        metric_addrs=metric_addrs,
    ):
        violations.append(f"{derived_path}:{msg}")

    # A structurally-valid predicate set can still be DEGENERATE: if every pass test asserts only
    # `verdict.*`, the deterministic per-test judgment collapses to the runner's own verdict.overall
    # (the judge nondeterminism R2 removed). This is a separate necessary-condition gate from the
    # schema check above; it routes back to compile.generate through the same compile_static_violation.
    for msg in degenerate_predicate_violations(predicates):
        violations.append(f"{derived_path}:{msg}")


def _validate_ir_source_refs_tests(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """`meta.source_refs.tests` must resolve to an existing tests.md.

    Three pins reach tests.md through this ref and every one of them returns SILENTLY when it does
    not resolve: `_validate_test_evidence_requirements`, `_validate_tests_verdict_summary_consistency`,
    and the tests.md branch of `_validate_test_predicates`. An IR that mistypes the ref — or that
    keeps a ref to a spec directory a later rename moved — therefore certifies clean with all three
    dark. The ref is LLM-authored, so it is pinned here rather than assumed, mirroring the
    infrastructure `meta.source_refs.controlled_spec` check.
    """
    derived_path = ir_dir / "spec.ir.yaml"
    if not derived_path.is_file():
        return  # absence is already reported by the caller
    try:
        document = _read_yaml(derived_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return  # malformed IR is already reported by the contract-file gates
    if not isinstance(document, dict):
        return

    meta = _ir_section(document, "meta") or {}
    source_refs = meta.get("source_refs")
    tests_ref = source_refs.get("tests") if isinstance(source_refs, dict) else None
    if not isinstance(tests_ref, str) or not tests_ref.strip():
        violations.append(
            f"{derived_path}:meta.source_refs.tests missing "
            "(the only route to tests.md; without it the test-evidence and verdict pins are silent)"
        )
        return

    tests_path = _tests_path_from_ir_document(repo_root, document)
    if tests_path is None or not _is_readable_file(tests_path):
        violations.append(
            f"{derived_path}:meta.source_refs.tests ({tests_ref.strip()}) unresolvable "
            "(no such file; the test-evidence and verdict pins would be silent)"
        )


def _validate_compile_dependency_consistency(
    repo_root: Path, ir_dir: Path, violations: list[str]
) -> None:
    """Deterministic V4 closure gate: the IR's LLM-authored ``dependency.direct_deps`` must
    agree with the conductor-authored sidecar ``dependency_graph.json`` (which encodes
    ``deps.yaml`` + ``spec_catalog.yaml`` and is correct-by-construction; see
    ``workflow_conductor._write_dependency_graph``). ``direct_deps`` is the ONLY graph-shaped
    field still authored by the compile.generate LLM, so this is the remaining deterministic
    check on it — the derived ``all_nodes`` / ``transitive_deps`` moved host-side and no longer
    need V4a/V4b/topo LLM verification.

    A violation is a content failure routed (via ``classify_compile_static_failure``) back to
    ``compile.generate`` for a warm reopen (the LLM re-authors ``direct_deps`` to match
    ``deps.yaml``). Checks: sidecar present + parseable; ``all_nodes`` well-formed with the self
    node present and every ``transitive_deps`` node in ``all_nodes``; and the version-agnostic
    ``(kind, spec_id)`` set of IR ``direct_deps`` equals the host directly-required set
    ``{all_nodes} − {self} − {transitive}``. Version drift is deliberately NOT flagged here
    (soft — the gfortran/link backstop catches a wrong dependency version at Build; the sidecar
    pins the resolved version)."""
    sidecar_path = ir_dir / "dependency_graph.json"
    if not sidecar_path.is_file():
        violations.append(
            f"{sidecar_path}: dependency_graph.json sidecar is missing (conductor-authored "
            "at Compile phase start)")
        return
    try:
        graph = _read_json(sidecar_path)
    except (json.JSONDecodeError, OSError):
        violations.append(f"{sidecar_path}: dependency_graph.json is not valid JSON")
        return
    if not isinstance(graph, dict):
        violations.append(f"{sidecar_path}: dependency_graph.json must be a JSON object")
        return

    self_token = _normalize_node_key_token(str(graph.get("node_key") or ""))
    all_nodes = graph.get("all_nodes")
    if not isinstance(all_nodes, list) or not all_nodes:
        violations.append(f"{sidecar_path}: all_nodes must be a non-empty list")
        return
    all_tokens: set[str] = set()
    for entry in all_nodes:
        nk = entry.get("node_key") if isinstance(entry, dict) else None
        if not (isinstance(nk, str) and nk.strip()):
            violations.append(f"{sidecar_path}: all_nodes entry missing node_key")
            return
        level = entry.get("topo_level")
        if not isinstance(level, int) or isinstance(level, bool) or level < 0:
            violations.append(
                f"{sidecar_path}: all_nodes[{nk!r}] topo_level must be a non-negative int")
            return
        all_tokens.add(_normalize_node_key_token(nk))
    if not self_token or self_token not in all_tokens:
        violations.append(
            f"{sidecar_path}: node_key {graph.get('node_key')!r} not present in all_nodes")
        return

    transitive = graph.get("transitive_deps")
    transitive = transitive if isinstance(transitive, list) else []
    trans_tokens: set[str] = set()
    for entry in transitive:
        nk = entry.get("node_key") if isinstance(entry, dict) else entry
        if not (isinstance(nk, str) and nk.strip()):
            violations.append(f"{sidecar_path}: transitive_deps entry missing node_key")
            return
        tok = _normalize_node_key_token(nk)
        if tok not in all_tokens:
            violations.append(
                f"{sidecar_path}: transitive_deps node {nk!r} not present in all_nodes")
            return
        trans_tokens.add(tok)

    host_direct = all_tokens - {self_token} - trans_tokens

    ir_path = ir_dir / "spec.ir.yaml"
    ir: Any = None
    if ir_path.is_file():
        try:
            ir = _read_yaml(ir_path)
        except (yaml.YAMLError, OSError):
            ir = None
    dep = ir.get("dependency") if isinstance(ir, dict) else None
    dep = dep if isinstance(dep, dict) else {}
    ir_self = dep.get("node_key")
    if isinstance(ir_self, str) and ir_self.strip():
        if _normalize_node_key_token(ir_self) != self_token:
            violations.append(
                f"{ir_dir / 'spec.ir.yaml'}: dependency.node_key {ir_self!r} disagrees with "
                f"dependency_graph.json node_key {graph.get('node_key')!r}")
            return
    ir_direct = dep.get("direct_deps")
    ir_direct = ir_direct if isinstance(ir_direct, list) else []
    ir_direct_tokens: set[str] = set()
    for entry in ir_direct:
        nk = entry.get("node_key") if isinstance(entry, dict) else entry
        if isinstance(nk, str) and nk.strip():
            ir_direct_tokens.add(_normalize_node_key_token(nk))

    if ir_direct_tokens != host_direct:
        missing = sorted(host_direct - ir_direct_tokens)
        extra = sorted(ir_direct_tokens - host_direct)
        violations.append(
            f"{ir_dir / 'spec.ir.yaml'}: dependency.direct_deps disagrees with the "
            f"deterministic dependency closure (deps.yaml via dependency_graph.json); "
            f"missing direct deps {missing}; unexpected direct deps {extra}")


def _lineage_node_key_and_ir_ref(
    pipeline_dir: Path,
) -> tuple[str | None, str | None]:
    lineage_path = pipeline_dir / "lineage.json"
    if not lineage_path.exists():
        return None, None
    try:
        data = _read_json(lineage_path)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    nk = data.get("node_key")
    pr = data.get("ir_ref")
    node_key = nk.strip() if isinstance(nk, str) else None
    ir_ref = pr.strip() if isinstance(pr, str) else None
    return node_key, ir_ref


def _stub_execution(pipeline_dir: Path, node_key: str) -> NodeExecution:
    stub_dir = pipeline_dir / ".semantic_stage_stub"
    return NodeExecution(
        node_key=node_key,
        node_dir=stub_dir,
        exec_dir=stub_dir,
        pipeline_dir=pipeline_dir,
    )


def _latest_source_id(pipeline_dir: Path) -> str | None:
    gen_root = pipeline_dir / "source"
    if not gen_root.is_dir():
        return None
    latest_name: str | None = None
    latest_key: tuple[int, int] | None = None
    for gen_dir in sorted(d for d in gen_root.iterdir() if d.is_dir()):
        parsed = _parse_stage_attempt_id(gen_dir.name, "src")
        if parsed is None:
            continue
        if latest_key is None or parsed > latest_key:
            latest_key = parsed
            latest_name = gen_dir.name
    if latest_name is None:
        return None
    return latest_name


def _pure_gate_build_graph_inputs(
    repo_root: Path, ir_ref: str | None, ir: dict[str, Any], node_key: str
) -> tuple[dict[str, str], tuple[str, ...], dict[str, list[str]]]:
    """(toolchain, dependency_closure, dependency_edges) for the tamper gate's assembly check.

    Mirrors `workflow_conductor._read_toolchain` + `_dependency_closure_nodes` +
    `_build_pure_bundle_graph`'s edge derivation from the SAME dependency sidecar, so the graph
    the gate assembles is the graph the producer's acceptance built. The L6 spec_id-clash raise
    of `_dependency_closure_nodes` is not replicated: at gate time the closure is identical to
    the accepted production closure (same sidecar), so it cannot introduce a new clash — only a
    tampered bundle's own files can collide, which `derive_build_graph` itself detects."""
    impl = (ir.get("impl_defaults") or {}) if isinstance(ir, dict) else {}
    tc = (impl.get("toolchain") or {}) if isinstance(impl, dict) else {}
    target = (impl.get("target") or {}) if isinstance(impl, dict) else {}
    toolchain = {
        "language": str(tc.get("language") or "fortran").lower(),
        "standard": str(tc.get("standard") or "f2008").lower(),
        "build_system": str(tc.get("build_system") or "make").lower(),
        "backend": str(target.get("backend") or "").lower(),
    }
    if str(tc.get("compiler") or "").strip():
        toolchain["compiler"] = str(tc.get("compiler")).strip()
    sidecar = _read_dependency_graph_sidecar(repo_root, ir_ref) or {}
    all_nodes = sidecar.get("all_nodes") if isinstance(sidecar, dict) else None
    levels: dict[str, int] = {}
    closure: list[str] = []
    edges: dict[str, list[str]] = {}
    for entry in all_nodes or []:
        if not (isinstance(entry, dict) and isinstance(entry.get("node_key"), str)):
            continue
        nk = entry["node_key"].strip()
        deps = [d.get("node_key") if isinstance(d, dict) else d
                for d in (entry.get("direct_deps") or [])]
        edges[nk] = [d for d in deps if isinstance(d, str)]
        if nk and nk != node_key and nk not in levels:
            levels[nk] = entry.get("topo_level") or 0
            closure.append(nk)
    closure.sort(key=lambda n: levels.get(n, 0))
    return toolchain, tuple(closure), edges


def _validate_post_generate_bundle(
    repo_root: Path, gen_dir: Path, node_key: str, ir_ref: str | None, violations: list[str]
) -> None:
    """Z2 pure producer (M-C): when the source dir carries a host-written `codegen_bundle.json`,
    re-check the bundle the host accepted against the same contract at the deterministic
    post_generate gate — proof-of-work independent of the producer's own in-conversation
    validation, and a tamper check that the staged source is exactly what the bundle declared.

    Fires ONLY when `codegen_bundle.json` exists (the legacy leaf-authored source tree has none),
    so it is inert on every legacy node. Re-runs the FULL host acceptance contract
    (`codegen_bundle.pure_bundle_contract_violation`: schema + single-node shape + harness
    capability negotiation + IR state bindings + M3c model/checks names + the fixed
    checks-module ABI + assembly-graph collisions) — the SAME layers the producer
    accepted, reconstructed from the IR + dependency sidecar — so a post-write edit that stays
    schema-valid (e.g. swapping in an unsupported `capability_requirements`) cannot slip past a
    validator that only re-ran `validate_bundle`.
    Then verifies every declared `files[]` entry exists on disk with byte-identical content (a
    post-write mutation is a violation), with no UNDECLARED `.f90` staged beyond the host glue
    (`<spec_id>_runner.f90`)."""
    bundle_path = gen_dir / "codegen_bundle.json"
    if not bundle_path.exists():
        return
    from tools.codegen_bundle import (
        pure_bundle_contract_violation, harness_provided_capabilities, derive_build_graph,
        published_operations_from_ir)
    try:
        doc = _read_json(bundle_path)
    except json.JSONDecodeError:
        violations.append(f"{bundle_path}: invalid json")
        return
    if not isinstance(doc, dict):
        violations.append(f"{bundle_path}: must be a JSON object")
        return
    spec_id = node_key.split("/", 1)[1].split("@", 1)[0] if "/" in node_key else ""
    # Reconstruct the acceptance inputs (harness capabilities, IR state vars, build graph) from
    # the IR + dependency sidecar so the tamper gate re-runs the producer's FULL contract, not
    # just schema re-validation.
    ir = _read_yaml(repo_root / ir_ref / "spec.ir.yaml") if ir_ref else {}
    if not isinstance(ir, dict):
        ir = {}
    infra = _infra_direct_dep_node_keys(ir)
    harness_nk = infra[0] if len(infra) == 1 else None
    provided = harness_provided_capabilities(harness_nk) if harness_nk else None
    algorithm = (ir.get("algorithm") or {}) if isinstance(ir, dict) else {}
    toolchain, closure, edges = _pure_gate_build_graph_inputs(repo_root, ir_ref, ir, node_key)

    def _build_graph(d: Any) -> Any:
        return derive_build_graph(
            d, dependency_closure=closure, toolchain=toolchain,
            host_glue_sources=(f"{spec_id}_runner.f90",),
            dependency_edges=edges or None)

    contract = pure_bundle_contract_violation(
        doc, node_key=node_key, spec_id=spec_id,
        ir_state_variables=(algorithm.get("state_variables") or []),
        harness_provided=provided, harness_label=harness_nk, build_graph=_build_graph,
        ir_published_operations=published_operations_from_ir(ir))
    if contract is not None:
        violations.append(
            f"{bundle_path}: host acceptance contract re-check failed ({contract[0]}): "
            f"{contract[1]}")
        return
    files = [e for e in (doc.get("files") or []) if isinstance(e, dict)]
    src_dir = gen_dir / "src"
    src_root = src_dir.resolve()
    declared: set[str] = set()
    for entry in files:
        logical = str(entry.get("logical_path") or "")
        content = entry.get("content")
        if not logical or not isinstance(content, str):
            continue
        declared.add(logical.casefold())
        disk = src_dir / logical
        # `validate_bundle` above already rejected any `..` / absolute logical_path (this
        # returns on its violations), so `disk` is inside src/; the containment check is
        # belt-and-suspenders against a validator bypass on a tampered bundle.
        if not disk.resolve().is_relative_to(src_root):
            violations.append(f"{disk}: logical_path escapes the source tree")
            continue
        if not disk.exists():
            violations.append(f"{disk}: declared by codegen_bundle but not staged")
            continue
        try:
            # Byte comparison (not read_text): avoids a UnicodeDecodeError crash on a tampered
            # binary file (UnicodeDecodeError is a ValueError, NOT an OSError) and sidesteps
            # read_text's universal-newline translation, which would false-flag a CRLF file.
            if disk.read_bytes() != content.encode("utf-8"):
                violations.append(
                    f"{disk}: staged content differs from codegen_bundle.files[] (post-write tamper)")
        except OSError as exc:
            violations.append(f"{disk}: unreadable ({exc})")
    # No UNDECLARED .f90 beyond the single host-rendered runner glue. Match the suffix
    # case-INSENSITIVELY: `rglob("*.f90")` misses an uppercase `extra.F90` on a case-sensitive
    # filesystem, which would let an undeclared Fortran source slip past this provenance check
    # while the rest of the gate treats suffixes case-insensitively.
    allowed_extra = {f"{spec_id}_runner.f90".casefold()}
    if src_dir.is_dir():
        f90_paths = [p for p in src_dir.rglob("*")
                     if p.is_file() and p.suffix.lower() == ".f90"]
        for path in sorted(f90_paths):
            try:
                rel = str(path.relative_to(src_dir)).replace("\\", "/").casefold()
            except ValueError:
                # rglob followed a symlink out of the tree — an undeclared escape, flag it.
                violations.append(f"{path}: staged file escapes the source tree")
                continue
            if rel not in declared and rel not in allowed_extra:
                violations.append(
                    f"{path}: undeclared .f90 staged (not in codegen_bundle.files[] nor the "
                    "host-rendered runner glue)")


def validate_post_generate_stage(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    source_id: str | None,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_post_generate_stage_impl(
            repo_root, workspace_root, pipeline_ref, source_id
        )


def _validate_post_generate_stage_impl(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    source_id: str | None,
) -> list[str]:
    violations: list[str] = []
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"]
    try:
        pipeline_dir = _resolve_pipeline_dir_for_stage(
            repo_root, workspace_root, pipeline_ref
        )
    except ValueError as exc:
        return [str(exc)]

    node_key, ir_ref = _lineage_node_key_and_ir_ref(pipeline_dir)
    if not node_key:
        violations.append(f"{pipeline_dir / 'lineage.json'}: missing node_key")
        return violations

    gen_id = source_id or _latest_source_id(pipeline_dir)
    if not gen_id:
        violations.append(f"{pipeline_dir / 'generate'}: no generation directory found")
        return violations
    if _parse_stage_attempt_id(gen_id, "src") is None:
        violations.append(
            f"{pipeline_dir / 'source' / gen_id}: invalid source_id; expected src_<YYYYMMDD>_<seq3>"
        )
        return violations

    if ir_ref:
        ir_dir = (repo_root / ir_ref).resolve()
        derived_path = ir_dir / "spec.ir.yaml"
        if derived_path.exists():
            _validate_io_contract_file(repo_root, derived_path, violations)
        else:
            violations.append(f"{derived_path}: missing (ir_ref {ir_ref})")

    execution = _stub_execution(pipeline_dir, node_key)
    # Enforce the lineage top-level schema (pipeline_id presence + the
    # <slug>_<YYYYMMDD>_<seq3> format + node_key↔directory binding) at Generate time —
    # the same check post_execute runs via _validate_pipeline_lineage_presence.
    # Previously this ran only at post_execute, so a malformed / non-top-level
    # pipeline_id surfaced far downstream at Validate and forced a corrective generate
    # re-run (audit: orch_20260615T095217Z_74450292). source_id/binary_id/run_id are not
    # required here — the presence check validates only node_key and pipeline_id, which
    # are the fields lineage.json already carries at Generate time.
    _validate_pipeline_lineage_presence([execution], violations)
    _validate_generate_outputs_for_generation(
        repo_root, execution, gen_id, violations
    )

    gen_dir = pipeline_dir / "source" / gen_id
    # Z2 pure producer: re-validate + tamper-check a host-written CodegenBundle (inert when
    # absent, i.e. on every legacy node).
    _validate_post_generate_bundle(repo_root, gen_dir, node_key, ir_ref, violations)
    meta_path = gen_dir / "source_meta.json"
    if meta_path.exists():
        try:
            meta_data = _read_json(meta_path)
        except json.JSONDecodeError:
            violations.append(f"{meta_path}: invalid json")
        else:
            if isinstance(meta_data, dict):
                impl_lang: str | None = None
                if ir_ref:
                    impl_lang = _impl_language_from_plan_dir(
                        repo_root, (repo_root / ir_ref).resolve()
                    )
                _validate_generate_lint_command_logs(
                    repo_root, meta_path, meta_data, impl_lang, violations
                )
                _validate_generate_syntax_command_logs(
                    repo_root, meta_path, meta_data, impl_lang, violations
                )

    return violations


def validate_post_build_stage(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    source_id: str | None,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_post_build_stage_impl(
            repo_root, workspace_root, pipeline_ref, source_id
        )


def _validate_post_build_stage_impl(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    source_id: str | None,
) -> list[str]:
    violations: list[str] = []
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"]
    try:
        pipeline_dir = _resolve_pipeline_dir_for_stage(
            repo_root, workspace_root, pipeline_ref
        )
    except ValueError as exc:
        return [str(exc)]

    gen_id = source_id or _latest_source_id(pipeline_dir)
    if not gen_id:
        violations.append(f"{pipeline_dir / 'generate'}: no generation directory found")
        return violations
    if _parse_stage_attempt_id(gen_id, "src") is None:
        violations.append(
            f"{pipeline_dir / 'source' / gen_id}: invalid source_id; expected src_<YYYYMMDD>_<seq3>"
        )
        return violations

    src_dir = pipeline_dir / "source" / gen_id / "src"
    _validate_fortran_makefile_src_dir(src_dir, violations)
    _build_system, _language = _impl_toolchain_from_pipeline_dir(repo_root, pipeline_dir)
    _validate_makefile_test_no_relink(
        src_dir, violations, build_system=_build_system, language=_language
    )
    _validate_makefile_test_invokes_cases(
        src_dir, violations, build_system=_build_system, language=_language
    )
    return violations


def validate(
    repo_root: Path,
    workspace_root: str,
    pipeline_roots: list[Path] | None = None,
    require_llm_review: bool = True,
    require_orchestration: bool = False,
    run_ids: set[str] | None = None,
    in_flight_agent_run_ids: set[str] | None = None,
    current_orchestration_id: str | None = None,
    require_verdict: bool = True,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_impl(
            repo_root,
            workspace_root,
            pipeline_roots,
            require_llm_review,
            require_orchestration,
            run_ids,
            in_flight_agent_run_ids,
            current_orchestration_id,
            require_verdict,
        )


def _validate_impl(
    repo_root: Path,
    workspace_root: str,
    pipeline_roots: list[Path] | None,
    require_llm_review: bool,
    require_orchestration: bool,
    run_ids: set[str] | None = None,
    in_flight_agent_run_ids: set[str] | None = None,
    current_orchestration_id: str | None = None,
    require_verdict: bool = True,
) -> list[str]:
    violations: list[str] = []
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"]

    workspace_path = repo_root / workspace_root
    if not workspace_path.exists():
        return [f"{workspace_path}: workspace root does not exist"]

    executions = _node_executions(
        workspace_path, pipeline_roots=pipeline_roots, run_ids=run_ids
    )
    # The run node directory under runs/<run_id>/ must be the pipeline's own
    # node_key_safe. Report any artifact-bearing run subdir that deviates (a
    # forged version segment or an unparseable name) — _node_executions only
    # discovers the canonical dir, so without this scan a non-canonical artifact
    # directory would be silently ignored and could pass whenever a canonical
    # execution also exists. Run before the empty-executions check so a lone
    # non-canonical dir fails explicitly instead of "no execution artifacts found".
    _validate_run_node_dir_names(workspace_path, pipeline_roots, violations, run_ids)
    # When --run-id scopes validation, every explicitly requested pipeline root
    # must contribute at least one execution for the requested run. Without this,
    # run-id filtering silently drops any requested root that lacks the scoped run
    # (e.g. a dependency/all_nodes root whose current run id was not supplied), and
    # validation could PASS after checking only a subset of the requested roots —
    # a missing current run for that root would go unreported.
    if run_ids is not None and pipeline_roots:
        covered_roots = {execution.pipeline_dir for execution in executions}
        for root in pipeline_roots:
            if root not in covered_roots:
                violations.append(
                    f"{root}: no execution artifacts found for requested --run-id "
                    f"({', '.join(sorted(run_ids))})"
                )
    if not executions:
        if violations:
            return violations
        return [f"{workspace_path}/pipelines: no execution artifacts found"]

    _validate_pipeline_lineage_presence(
        executions=executions,
        violations=violations,
    )

    # Scope the source_meta sweep to the source dirs the in-scope executions DECLARE, the
    # same lineage scoping the structural source checks already use. With --run-id (the
    # post_execute / pre_judge stages) `executions` is already the current run, so only the
    # current source lineage is strictly checked and superseded attempt dirs are skipped;
    # a full run takes the union over every run's declared source, so only a dir NO run
    # references is skipped. A pipeline whose executions declare no valid source at all
    # (itself a hard _validate_trial_meta violation) falls back to None = sweep everything,
    # so the gate never silently weakens.
    scope_by_pipeline: dict[Path, set[str]] = {}
    for execution in executions:
        declared = _execution_declared_source_id(execution)
        if declared:
            scope_by_pipeline.setdefault(execution.pipeline_dir, set()).add(declared)

    seen_pipeline_dirs: set[Path] = set()
    for execution in executions:
        pd = execution.pipeline_dir
        if pd in seen_pipeline_dirs:
            continue
        seen_pipeline_dirs.add(pd)
        _validate_source_meta_json_files(
            pd, violations, in_scope_source_ids=(scope_by_pipeline.get(pd) or None)
        )

    if require_orchestration:
        _validate_orchestration_hierarchy(
            workspace_path=workspace_path,
            executions=executions,
            violations=violations,
            in_flight_agent_run_ids=in_flight_agent_run_ids,
            current_orchestration_id=current_orchestration_id,
        )

    source_hash_map: dict[str, list[SourceFingerprint]] = {}
    dep_contexts: list[tuple[NodeExecution, set[str], str | None]] = []
    lineage_contexts: list[tuple[NodeLineage, set[str], str | None]] = []
    lineages = _lineage_records(workspace_path, pipeline_roots)
    # Structural SOURCE checks are scoped to the source each execution declares
    # via trial_meta.source_source_id (mirroring --run-id run-scoping). Track the
    # sources already structurally validated so that, in legacy multi-run mode,
    # several runs sharing one source do not re-validate it and emit duplicate
    # violations.
    validated_structural_src_dirs: set[Path] = set()

    for execution in executions:
        _validate_algorithm_contract_schema(repo_root, execution, violations)
        _validate_io_contract_schema(repo_root, execution, violations)
        _validate_trial_meta(repo_root, execution, violations)
        _validate_execution_json_outputs(execution, violations)
        _validate_raw_evidence(repo_root, execution, violations)
        _validate_metrics_basis_not_trivial(execution, violations)
        in_scope_src_dir = _execution_in_scope_src_dir(execution, violations)
        if (
            in_scope_src_dir is not None
            and in_scope_src_dir not in validated_structural_src_dirs
        ):
            validated_structural_src_dirs.add(in_scope_src_dir)
            _validate_generate_outputs(
                repo_root, execution, in_scope_src_dir, violations
            )
            _validate_dependency_operation_usage(
                repo_root, execution, in_scope_src_dir, violations
            )
            _validate_runner_outputs(
                execution, in_scope_src_dir, violations,
                known_case_ids=_case_ids_for_execution(repo_root, execution),
            )
        _validate_run_program_inputs(repo_root, execution, violations)
        _validate_quality_check_commands(repo_root, execution, violations)
        _validate_tests_verdict_summary_consistency(
            repo_root, execution, violations, require_verdict=require_verdict
        )
        _validate_llm_semantic_review(
            repo_root,
            execution,
            violations,
            require_llm_review=require_llm_review,
        )

        fp = _source_fingerprint(execution)
        if fp is not None:
            source_hash_map.setdefault(fp.digest, []).append(fp)
        dep_data = _dependency_resolved_for_execution(repo_root, execution)
        if isinstance(dep_data, dict):
            expected_nodes = _dependency_expected_node_keys(dep_data)
            if expected_nodes:
                dep_contexts.append(
                    (
                        execution,
                        expected_nodes,
                        _dependency_run_token(dep_data),
                    )
                )
    for lineage in lineages:
        if not lineage.dependency_ref or not lineage.dependency_ref.startswith("workspace/"):
            continue
        dep_path = repo_root / lineage.dependency_ref
        if not dep_path.exists():
            continue
        dep_doc_path = _dependency_doc_path(repo_root, dep_path, lineage.ir_ref)
        dep_data: Any = None
        if dep_doc_path is not None:
            try:
                dep_data = _read_yaml(dep_doc_path)
            except (json.JSONDecodeError, yaml.YAMLError, OSError):
                try:
                    dep_data = _read_json(dep_doc_path)
                except (json.JSONDecodeError, OSError):
                    dep_data = None
        if not isinstance(dep_data, dict):
            dep_data = {}
        # Unwrap spec.ir.yaml.dependency for the new IR (node_key + direct_deps).
        if isinstance(dep_data.get("dependency"), dict):
            nested = dep_data["dependency"]
            if "direct_deps" in nested or "node_key" in nested:
                dep_data = nested
        # all_nodes / transitive_deps now live in the dependency node's own
        # conductor-authored sidecar <ir_ref>/dependency_graph.json, not its
        # spec.ir.yaml. Merge them over the IR block (keeping node_key / resolved_at).
        graph = _read_dependency_graph_sidecar(repo_root, lineage.ir_ref)
        if isinstance(graph, dict):
            merged = dict(dep_data)
            for key in ("all_nodes", "transitive_deps"):
                if isinstance(graph.get(key), list):
                    merged[key] = graph[key]
            dep_data = merged
        all_nodes = dep_data.get("all_nodes")
        if not isinstance(all_nodes, list) or not all_nodes:
            continue
        expected_nodes = _dependency_expected_node_keys(dep_data)
        if expected_nodes:
            lineage_contexts.append((lineage, expected_nodes, _dependency_run_token(dep_data)))

    scope_nodes = {_normalize_node_key_token(execution.node_key) for execution in executions}
    scope_nodes_by_token: dict[str, set[str]] = {}
    for execution, _, token in dep_contexts:
        if token is None:
            continue
        scope_nodes_by_token.setdefault(token, set()).add(_normalize_node_key_token(execution.node_key))

    # Cross-pipeline cache: a closure node absent from the current validation scope is still
    # DAG-satisfied when it has its own fully-validated pipeline (the --with-deps model; see
    # _closure_node_validated_in_own_pipeline). Only the token-less "validation scope" branch
    # uses this — the per-token (resolved_at) branch keeps strict single-scope semantics.
    _xp_cache: dict[str, bool] = {}

    def _xp_satisfied(node_token: str) -> bool:
        if node_token not in _xp_cache:
            _xp_cache[node_token] = _closure_node_validated_in_own_pipeline(repo_root, node_token)
        return _xp_cache[node_token]

    seen_dag_violations: set[tuple[Path, str, tuple[str, ...]]] = set()
    for execution, expected_nodes, token in dep_contexts:
        if token is None:
            available_nodes = scope_nodes
            scope_label = "validation scope"
        else:
            available_nodes = scope_nodes_by_token.get(token, set())
            scope_label = f"resolved_at={token}"
        missing = sorted(expected_nodes - available_nodes)
        if token is None and missing:
            missing = [m for m in missing if not _xp_satisfied(m)]
        if missing:
            key = (execution.pipeline_dir, scope_label, tuple(missing))
            if key in seen_dag_violations:
                continue
            seen_dag_violations.add(key)
            violations.append(
                f"{execution.pipeline_dir / 'lineage.json'}: dependency DAG incomplete for {scope_label}; missing node workflows {missing}"
            )

    scope_lineage_nodes = {_normalize_node_key_token(item.node_key) for item in lineages}
    scope_lineage_nodes_by_token: dict[str, set[str]] = {}
    scope_plan_nodes = set()
    scope_plan_nodes_by_token: dict[str, set[str]] = {}

    for lineage, _, token in lineage_contexts:
        node_token = _normalize_node_key_token(lineage.node_key)
        if token is not None:
            scope_lineage_nodes_by_token.setdefault(token, set()).add(node_token)
        plan_ok = False
        if isinstance(lineage.ir_ref, str) and lineage.ir_ref.startswith("workspace/"):
            plan_path = repo_root / lineage.ir_ref
            plan_ok = plan_path.exists() and plan_path.is_dir()
        if plan_ok:
            scope_plan_nodes.add(node_token)
            if token is not None:
                scope_plan_nodes_by_token.setdefault(token, set()).add(node_token)

    seen_issue_violations: set[tuple[Path, str, tuple[str, ...], tuple[str, ...]]] = set()
    for lineage, expected_nodes, token in lineage_contexts:
        if token is None:
            available_pipeline_nodes = scope_lineage_nodes
            available_plan_nodes = scope_plan_nodes
            scope_label = "validation scope"
        else:
            available_pipeline_nodes = scope_lineage_nodes_by_token.get(token, set())
            available_plan_nodes = scope_plan_nodes_by_token.get(token, set())
            scope_label = f"resolved_at={token}"
        missing_pipeline_nodes = sorted(expected_nodes - available_pipeline_nodes)
        missing_plan_nodes = sorted(expected_nodes - available_plan_nodes)
        if token is None:
            # A dependency built in its own pipeline (--with-deps) is issued cross-pipeline.
            missing_pipeline_nodes = [m for m in missing_pipeline_nodes if not _xp_satisfied(m)]
            missing_plan_nodes = [m for m in missing_plan_nodes if not _xp_satisfied(m)]
        if not missing_pipeline_nodes and not missing_plan_nodes:
            continue
        key = (
            lineage.pipeline_dir,
            scope_label,
            tuple(missing_plan_nodes),
            tuple(missing_pipeline_nodes),
        )
        if key in seen_issue_violations:
            continue
        seen_issue_violations.add(key)
        if missing_plan_nodes:
            violations.append(
                f"{lineage.pipeline_dir / 'lineage.json'}: node plans not issued for {scope_label}; missing nodes {missing_plan_nodes}"
            )
        if missing_pipeline_nodes:
            violations.append(
                f"{lineage.pipeline_dir / 'lineage.json'}: node pipelines not issued for {scope_label}; missing nodes {missing_pipeline_nodes}"
            )

    for digest, items in sorted(source_hash_map.items()):
        node_keys = sorted({item.node_key for item in items})
        if len(node_keys) <= 1:
            continue
        pipelines = sorted(
            {
                item.pipeline_dir.relative_to(repo_root).as_posix()
                for item in items
            }
        )
        violations.append(
            "copy_based_artifact_reuse detected: "
            + f"digest={digest[:12]} node_keys={node_keys} pipelines={pipelines}"
        )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--workspace-root", default="workspace")
    parser.add_argument(
        "--stage",
        choices=(
            "full",
            "compile",
            "post_generate",
            "post_build",
            "post_execute",
            "pre_judge",
        ),
        default="full",
        help=(
            "full: default end-to-end validation (requires execution artifacts). "
            "compile: validate IR directory only (spec.ir.yaml structure invariants). "
            "post_generate / post_build: validate one pipeline source tree (requires --pipeline-root). "
            "post_execute: full validation with LLM review and orchestration optional. "
            "pre_judge: full validation with LLM review and orchestration required."
        ),
    )
    parser.add_argument(
        "--ir-ref",
        default=None,
        help="Workspace-relative IR directory (required for --stage compile).",
    )
    parser.add_argument(
        "--source-id",
        default=None,
        help=(
            "source/<source_id> under the pipeline (optional for post_generate/post_build; "
            "defaults to latest lexicographic directory name)."
        ),
    )
    parser.add_argument(
        "--pipeline-root",
        action="append",
        default=None,
        help=(
            "Optional pipeline directory to validate. "
            "Can be repeated. Path must be under workspace/. "
            "Required as a single path for post_generate/post_build."
        ),
    )
    parser.add_argument(
        "--run-id",
        action="append",
        help=(
            "Optional runs/<run_id> to scope validation to. Can be repeated. "
            "Effective for --stage post_execute/pre_judge; when omitted, every "
            "run under the pipeline is validated (legacy behavior). Scoping to "
            "the current run avoids append-only sibling runs from prior "
            "attempts permanently failing the pipeline."
        ),
    )
    parser.add_argument(
        "--in-flight-agent-run-id",
        action="append",
        help=(
            "agent_run_id of a run that is genuinely executing right now and "
            "therefore has not yet appended its agent_runs.jsonl entry / "
            "step_result.json. Can be repeated. Effective only for --stage "
            "pre_judge: the Validate.judge substep passes its OWN agent_run_id so "
            "the self-judging exception (its own agent_graph edge + the validate "
            "step_result, both written only after it returns) is not mis-flagged. "
            "Each id is exempted only after the persisted launch request confirms "
            "it is the validate/judge substep; an orphaned edge with no matching "
            "--in-flight-agent-run-id still fails (fail-closed)."
        ),
    )
    parser.add_argument(
        "--orchestration-id",
        help=(
            "orchestration_id of the run being judged. Effective only for --stage "
            "pre_judge: scopes the cross-orchestration integrity scan to this single "
            "orchestration so an unrelated crashed/prior orchestration's debris (a "
            "dangling agent_graph edge) cannot fail a healthy run. Dependencies are "
            "validated by their own pre_judge + the launch-check readiness gate. When "
            "omitted, the scan covers all orchestrations (legacy behavior)."
        ),
    )
    parser.add_argument(
        "--allow-missing-llm-review",
        action="store_true",
        help="Allow missing semantic_review.json for legacy pipelines.",
    )
    parser.add_argument(
        "--allow-missing-orchestration",
        action="store_true",
        help="Allow missing orchestration artifacts for legacy pipelines.",
    )
    parser.add_argument(
        "--legacy-mode",
        action="store_true",
        help=(
            "Allow legacy compatibility options. "
            "Without this flag, --allow-missing-* options are rejected."
        ),
    )
    args = parser.parse_args(argv)

    if (args.allow_missing_llm_review or args.allow_missing_orchestration) and not args.legacy_mode:
        print(
            "pipeline semantic validation: FAIL\n"
            "- --allow-missing-llm-review/--allow-missing-orchestration require --legacy-mode"
        )
        return 1

    if args.stage == "pre_judge" and (
        args.allow_missing_llm_review or args.allow_missing_orchestration
    ):
        print(
            "pipeline semantic validation: FAIL\n"
            "- --stage pre_judge is incompatible with --allow-missing-llm-review "
            "and --allow-missing-orchestration"
        )
        return 1

    repo_root = Path(args.repo_root).resolve()

    # Pin the active repo_root for the duration of this main() call only,
    # then reset via the captured token. Without scoped reset, a long-lived
    # process (or repeated in-process main() calls in tests / batch tooling)
    # would leak the first repo's context into later validations against a
    # different repo_root, producing order-dependent schema-resolution bugs.
    with _pinned_repo_root_for_schema(repo_root):
        return _main_dispatch(args, repo_root)


def _main_dispatch(args: argparse.Namespace, repo_root: Path) -> int:
    # Wrap stage validators so a broken canonical schema (missing repo-local
    # shape_expr.schema.json, malformed JSON, invalid regex, etc.) produces a
    # structured "pipeline semantic validation: FAIL" line with the offending
    # path instead of an opaque traceback. Orchestration gates rely on the
    # structured output to extract violations and surface a repairable failure.
    try:
        if args.stage == "compile":
            if not args.ir_ref or not str(args.ir_ref).strip():
                print(
                    "pipeline semantic validation: FAIL\n"
                    "- --stage compile requires non-empty --ir-ref"
                )
                return 1
            violations = validate_compile_stage(
                repo_root, args.workspace_root, str(args.ir_ref).strip()
            )
        elif args.stage in ("post_generate", "post_build"):
            roots = args.pipeline_root or []
            if len(roots) != 1:
                print(
                    "pipeline semantic validation: FAIL\n"
                    f"- --stage {args.stage} requires exactly one --pipeline-root "
                    f"(got {len(roots)})"
                )
                return 1
            pipeline_ref = roots[0].strip()
            if args.stage == "post_generate":
                violations = validate_post_generate_stage(
                    repo_root,
                    args.workspace_root,
                    pipeline_ref,
                    args.source_id,
                )
            else:
                violations = validate_post_build_stage(
                    repo_root,
                    args.workspace_root,
                    pipeline_ref,
                    args.source_id,
                )
        else:
            try:
                pipeline_roots = _resolve_pipeline_roots(
                    repo_root=repo_root,
                    workspace_root=args.workspace_root,
                    raw_values=args.pipeline_root,
                )
            except ValueError as exc:
                print(f"pipeline semantic validation: FAIL\n- {exc}")
                return 1

            run_ids = set(args.run_id) if args.run_id else None
            if args.stage == "post_execute":
                violations = validate(
                    repo_root=repo_root,
                    workspace_root=args.workspace_root,
                    pipeline_roots=pipeline_roots,
                    require_llm_review=False,
                    require_orchestration=False,
                    run_ids=run_ids,
                    # The ONLY stage where verdict.json is legitimately absent: the conductor
                    # authors it after this gate returns clean (`_execute_inproc`).
                    require_verdict=False,
                )
            elif args.stage == "pre_judge":
                violations = validate(
                    repo_root=repo_root,
                    workspace_root=args.workspace_root,
                    pipeline_roots=pipeline_roots,
                    require_llm_review=True,
                    require_orchestration=True,
                    run_ids=run_ids,
                    in_flight_agent_run_ids=(
                        set(args.in_flight_agent_run_id)
                        if args.in_flight_agent_run_id
                        else None
                    ),
                    current_orchestration_id=(
                        args.orchestration_id.strip()
                        if args.orchestration_id and args.orchestration_id.strip()
                        else None
                    ),
                )
            else:
                violations = validate(
                    repo_root=repo_root,
                    workspace_root=args.workspace_root,
                    pipeline_roots=pipeline_roots,
                    require_llm_review=not args.allow_missing_llm_review,
                    require_orchestration=not args.allow_missing_orchestration,
                    # NOT `not args.allow_missing_orchestration`: that flag waives the
                    # ORCHESTRATION artifacts, and coupling the verdict to it would let
                    # `--allow-missing-orchestration` silently switch off the verdict/summary pin.
                    require_verdict=True,
                )
    except RuntimeError as exc:
        print(f"pipeline semantic validation: FAIL\n- schema_load_failed: {exc}")
        return 1

    if violations:
        print("pipeline semantic validation: FAIL")
        for line in violations:
            print(f"- {line}")
        return 1

    print("pipeline semantic validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
