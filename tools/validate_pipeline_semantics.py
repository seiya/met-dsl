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
TEST_OUTCOME_VALUES = {"pass", "fail", "xfail", "skipped", "blocked"}
# Bundled schema lives next to this validator; used as the canonical fallback
# when no target repo_root is in scope (tests, ad-hoc invocation) and as the
# default canonical reference for the validator's pinned rules.
_BUNDLED_SHAPE_EXPR_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "spec" / "schema" / "plan" / "shape_expr.schema.json"
)
# Active repo_root for schema resolution. Set by main() via --repo-root and by
# stage-level entry points (validate, validate_plan_stage, ...). When set, the
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
        `<repo_root>/spec/schema/plan/shape_expr.schema.json`. If it is
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
        candidate = chosen / "spec" / "schema" / "plan" / "shape_expr.schema.json"
        if not candidate.is_file():
            raise RuntimeError(
                f"shape_expr schema not found at {candidate}. "
                "Canonical source: spec/schema/plan/shape_expr.schema.json. "
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
            "Canonical source: spec/schema/plan/shape_expr.schema.json"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"shape_expr schema {schema_path} is unreadable: {exc}. "
            "Canonical source: spec/schema/plan/shape_expr.schema.json"
        ) from exc
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"shape_expr schema {schema_path} is malformed JSON: {exc}. "
            "Canonical source: spec/schema/plan/shape_expr.schema.json"
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
            "Canonical source: spec/schema/plan/shape_expr.schema.json"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"shape_expr schema {schema_path} is unreadable: {exc}. "
            "Canonical source: spec/schema/plan/shape_expr.schema.json"
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
REQUIRED_WORKFLOW_STEPS = ("plan", "generate", "build", "execute", "judge")
SUBSTEP_WORKFLOW_STEPS = frozenset({"plan", "generate", "tune"})
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
    "gen": re.compile(r"^gen_(\d{8})_(\d{3})$"),
    "build": re.compile(r"^build_(\d{8})_(\d{3})$"),
    "exec": re.compile(r"^exec_(\d{8})_(\d{3})$"),
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


def _required_launch_prompt_markers_for_role(
    role: str,
) -> list[str]:
    markers = [
        "orchestration_id:",
        "agent_run_id:",
        "parent_agent_run_id:",
        "plan_ref:",
        "pipeline_ref:",
        "dependency_ref:",
        "skill_name:",
        "skill_ref:",
        "skill_must_read_refs:",
        "必須要件:",
    ]
    if role == "substep":
        return [
            "あなたは substep agent である。",
            "対象 node_key:",
            "対象 step:",
            "対象 substep:",
            *markers,
        ]
    if role == "step":
        return [
            "あなたは step agent である。",
            "対象 node_key:",
            "対象 step:",
            *markers,
        ]
    return []


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
    required_sources: set[str],
    violations: list[str],
) -> None:
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

        if required_sources and required_sources.isdisjoint(dependency_sources):
            violations.append(
                f"{model_file}: subroutine {sub_name} does not include required semantic sources "
                f"in intent(out) dataflow (required={sorted(required_sources)})"
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


def _validate_problem_state_array_usage(
    repo_root: Path,
    execution: NodeExecution,
    model_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not _is_multidim_problem_node(execution):
        return

    contract = _algorithm_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return
    kernel_contract = _algorithm_state_contract(contract)
    if not isinstance(kernel_contract, dict):
        return

    raw_state_variables = kernel_contract.get("state_variables")
    if not isinstance(raw_state_variables, list):
        return

    state_names: list[str] = []
    for item in raw_state_variables:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            state_names.append(name.strip().lower())

    if not state_names:
        return

    subroutine_pattern = re.compile(
        r"subroutine\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)(.*?)end\s+subroutine",
        re.DOTALL,
    )
    intent_out_pattern = re.compile(r"intent\s*\(\s*out\s*\)\s*::\s*([^\n!]+)")
    candidate_found = False
    for match in subroutine_pattern.finditer(lowered):
        sub_name = match.group(1)
        body = match.group(3)
        out_vars: set[str] = set()
        for out_match in intent_out_pattern.finditer(body):
            out_vars.update(_split_fortran_names(out_match.group(1)))
        if len(out_vars) < 3:
            continue
        candidate_found = True

        missing_array_refs: list[str] = []
        for name in sorted(set(state_names)):
            array_ref = re.search(rf"\b{re.escape(name)}\s*\(", body)
            if array_ref is None:
                missing_array_refs.append(name)
        if missing_array_refs:
            violations.append(
                f"{model_file}: subroutine {sub_name} must reference state arrays declared in algorithm state_contract ({missing_array_refs})"
            )

    if not candidate_found:
        return


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


def _extract_first_diagnostics_block(lowered: str) -> str | None:
    start = -1
    for marker in ("/diagnostics.json", "'diagnostics.json'", "\"diagnostics.json\""):
        start = lowered.find(marker)
        if start >= 0:
            break
    if start < 0:
        return None

    close_idx = lowered.find("close(", start)
    if close_idx < 0:
        return lowered[start:]
    return lowered[start:close_idx]


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


def _extract_call_arg_vars(lowered: str) -> list[str]:
    for _, args_raw, _ in _iter_fortran_calls(lowered):
        names = _split_fortran_names(args_raw)
        if names:
            return names
    return []


def _strip_quoted_strings(text: str) -> str:
    no_single = re.sub(r"'(?:''|[^'])*'", "''", text)
    return re.sub(r"\"(?:\"\"|[^\"])*\"", "\"\"", no_single)


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


def _parse_makefile_rules(makefile_text: str) -> dict[str, set[str]]:
    rules: dict[str, set[str]] = {}
    assignment_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:+?]?=")

    for line in _makefile_logical_lines(makefile_text):
        if assignment_pattern.match(line):
            continue
        if ":" not in line:
            continue

        target_raw, prereq_raw = line.split(":", 1)
        target_tokens = target_raw.split()
        if not target_tokens:
            continue

        prereq_expr = prereq_raw.split(";", 1)[0].replace("|", " ")
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


def _validate_fortran_makefile_src_dir(src_dir: Path, violations: list[str]) -> None:
    if not src_dir.is_dir():
        return

    src_files = sorted(
        p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() == ".f90"
    )
    if len(src_files) < 2:
        return

    deps_by_stem = _fortran_source_module_deps(src_files)
    required_object_deps = {
        stem: deps for stem, deps in deps_by_stem.items() if deps
    }
    if not required_object_deps:
        return

    makefile_path = src_dir / "Makefile"
    if not makefile_path.exists():
        violations.append(
            f"{makefile_path}: missing for fortran module dependency build"
        )
        return

    rules = _parse_makefile_rules(
        makefile_path.read_text(encoding="utf-8", errors="ignore")
    )
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


def _validate_fortran_makefile_dependencies(generate_root: Path, violations: list[str]) -> None:
    if not generate_root.exists():
        return

    gen_dirs = sorted(d for d in generate_root.iterdir() if d.is_dir())
    for gen_dir in gen_dirs:
        src_dir = gen_dir / "src"
        if not src_dir.exists():
            continue
        _validate_fortran_makefile_src_dir(src_dir, violations)


def _validate_problem_runner_diagnostics_dependency(
    execution: NodeExecution,
    runner_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not execution.node_key.startswith("problem/"):
        return

    diagnostics_block = _extract_first_diagnostics_block(lowered)
    if diagnostics_block is None:
        return

    call_args = _extract_call_arg_vars(lowered)
    if not call_args:
        return

    diagnostics_no_strings = _strip_quoted_strings(diagnostics_block)
    referenced_args = [
        name
        for name in call_args
        if re.search(rf"\b{re.escape(name)}\b", diagnostics_no_strings)
    ]
    numeric_literal_count = len(
        re.findall(r"[-+]?\d+(?:\.\d+)?(?:d|e)?[-+]?\d*", diagnostics_block)
    )
    if not referenced_args and numeric_literal_count >= 5:
        violations.append(
            f"{runner_file}: diagnostics block does not reference model call arguments and appears constant-heavy"
        )


def _validate_problem_runner_nonphysical_casepath_input(
    execution: NodeExecution,
    runner_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    if not execution.node_key.startswith("problem/"):
        return

    if "get_command_argument(1,case_path)" in lowered.replace(" ", ""):
        call_args = _extract_call_arg_vars(lowered)
        if not call_args:
            return

        suspicious_inputs: set[str] = set()
        assign_pattern = re.compile(
            r"^\s*([a-z_][a-z0-9_]*)\s*=\s*([^\n!]+)",
            re.MULTILINE,
        )
        for match in assign_pattern.finditer(lowered):
            lhs = match.group(1).strip()
            rhs = match.group(2).lower()
            if "len_trim(case_path)" in rhs or "command_argument_count()" in rhs:
                suspicious_inputs.add(lhs)

        if suspicious_inputs.intersection(call_args):
            violations.append(
                f"{runner_file}: model call input depends on case_path length/argument count and is non-physical"
            )


def _validate_runner_perf_json_serialization(
    runner_file: Path,
    lowered: str,
    violations: list[str],
) -> None:
    perf_block = _extract_first_output_block(lowered, "perf.json")
    if perf_block is None:
        return

    if re.search(r"write\s*\([^)]*,\s*['\"][^'\"]*f0\.\d+[^'\"]*['\"]", perf_block):
        violations.append(
            f"{runner_file}: perf.json block uses Fortran F0 formatting; JSON numeric serialization must be leading-zero safe"
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
    plan_ref: str | None
    dependency_ref: str | None


@dataclass
class SourceFingerprint:
    node_key: str
    pipeline_dir: Path
    digest: str


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _node_executions(
    workspace_root: Path, pipeline_roots: list[Path] | None = None
) -> list[NodeExecution]:
    result: list[NodeExecution] = []
    targets = _pipeline_targets(workspace_root, pipeline_roots)

    def has_execution_artifacts(node_dir: Path) -> bool:
        markers = (
            node_dir / "diagnostics.json",
            node_dir / "perf.json",
            node_dir / "trial_meta.json",
            node_dir / "quality_check.json",
            node_dir / "raw" / "metrics_basis.json",
        )
        return any(path.exists() for path in markers)

    for pipeline_dir in targets:
        if not pipeline_dir.is_dir():
            continue
        execute_root = pipeline_dir / "execute"
        if not execute_root.exists():
            continue
        for exec_dir in sorted(execute_root.iterdir()):
            if not exec_dir.is_dir():
                continue
            for kind_dir in sorted(exec_dir.iterdir()):
                if not kind_dir.is_dir():
                    continue
                for spec_dir in sorted(kind_dir.iterdir()):
                    if not spec_dir.is_dir():
                        continue
                    if not has_execution_artifacts(spec_dir):
                        continue
                    node_key = f"{kind_dir.name}/{spec_dir.name}"
                    result.append(
                        NodeExecution(
                            node_key=node_key,
                            node_dir=spec_dir,
                            exec_dir=exec_dir,
                            pipeline_dir=pipeline_dir,
                        )
                    )
    return result


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
        plan_ref = lineage.get("plan_ref")
        dep_ref = lineage.get("dependency_ref")
        records.append(
            NodeLineage(
                node_key=node_key.strip(),
                pipeline_dir=pipeline_dir,
                plan_ref=plan_ref if isinstance(plan_ref, str) else None,
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


def _validate_generate_meta_json_files(
    pipeline_dir: Path,
    violations: list[str],
) -> None:
    generate_root = pipeline_dir / "generate"
    if not generate_root.exists() or not generate_root.is_dir():
        return
    for gen_dir in sorted(generate_root.iterdir()):
        if not gen_dir.is_dir():
            continue
        meta_path = gen_dir / "generate_meta.json"
        if not meta_path.exists():
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
        for key in required_meta_keys_for_step("generate"):
            if key not in data:
                continue
            val = data.get(key)
            if key == "attempt_count" and not isinstance(val, int):
                violations.append(f"{meta_path}:attempt_count must be integer")
            elif key == "verification_status" and (not isinstance(val, str) or not val.strip()):
                violations.append(f"{meta_path}:verification_status must be non-empty string")
            elif key == "last_fail_reason" and val is not None and not isinstance(val, str):
                violations.append(f"{meta_path}:last_fail_reason must be string or null")
            elif key == "debug_mode" and not isinstance(val, bool):
                violations.append(f"{meta_path}:debug_mode must be boolean")
            elif key == "context_isolated" and not isinstance(val, bool):
                violations.append(f"{meta_path}:context_isolated must be boolean")
        status_token = str(data.get("verification_status", "")).strip().lower()
        if status_token == "pass":
            _validate_generate_meta_lint_shape(meta_path, data, violations)


def _validate_plan_meta_json(plan_dir: Path, violations: list[str]) -> None:
    meta_path = plan_dir / STAGE_META_FILENAME_BY_STEP["plan"]
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
    required_keys = required_meta_keys_for_step("plan")
    for key in required_keys:
        if key not in data:
            violations.append(f"{meta_path}: missing required key {key!r}")
    if "attempt_count" in data and not isinstance(data.get("attempt_count"), int):
        violations.append(f"{meta_path}:attempt_count must be integer")
    if "verification_status" in data and (
        not isinstance(data.get("verification_status"), str)
        or not str(data.get("verification_status")).strip()
    ):
        violations.append(f"{meta_path}:verification_status must be non-empty string")
    if "last_fail_reason" in data and data.get("last_fail_reason") is not None and not isinstance(
        data.get("last_fail_reason"), str
    ):
        violations.append(f"{meta_path}:last_fail_reason must be string or null")
    if "debug_mode" in data and not isinstance(data.get("debug_mode"), bool):
        violations.append(f"{meta_path}:debug_mode must be boolean")
    if "context_isolated" in data and not isinstance(data.get("context_isolated"), bool):
        violations.append(f"{meta_path}:context_isolated must be boolean")
    if data.get("context_isolated") is False:
        reason = data.get("constraint_reason")
        if not isinstance(reason, str) or not reason.strip():
            violations.append(
                f"{meta_path}: requires non-empty constraint_reason when context_isolated=false"
            )


_MCP_AUDIT_LOG_BASENAME: str = "mcp_command_log.jsonl"


def _canonical_mcp_log_refs_for_lint(meta_path: Path, repo_root: Path) -> set[str]:
    """Canonical command_log_ref placements for `generate_meta.json` lint validation.

    Only one canonical placement: sibling under `<gen_dir>/src/`. A child agent
    that writes a forged mcp_command_log.jsonl elsewhere and points the
    `lint_command_ref.run_linter[].command_log_ref` at it should be rejected.
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

    # `source_generation_id` is a hard requirement for execute trial_meta —
    # without it, validators cannot bind quality_check evidence to a specific
    # generation, and a writer could otherwise omit the field to silently
    # bypass `tool_name` / mandatory `run_program` checks. Legacy artifacts
    # under active validation must be re-recorded; post-migration writers
    # always emit the field.
    _src_gen_raw = data.get("source_generation_id")
    if not isinstance(_src_gen_raw, str) or not _src_gen_raw.strip():
        violations.append(
            f"{trial_meta_path}:source_generation_id is required (single "
            "trusted source for cross-phase quality_check provenance and "
            "the gate for strict source_command_ref validation)."
        )
    # `source_build_id` binds run_program evidence to the specific build whose
    # binary this execute used. Without it, a trial_meta could attribute its
    # results to one build while having actually executed a sibling build's
    # binary (mixed-build attribution).
    _src_build_raw = data.get("source_build_id")
    _trial_source_build_id: str | None = None
    if not isinstance(_src_build_raw, str) or not _src_build_raw.strip():
        violations.append(
            f"{trial_meta_path}:source_build_id is required (binds "
            "run_program evidence to the specific build whose binary the "
            "execute run consumed)."
        )
    else:
        _trial_source_build_id = _src_build_raw.strip()
        # Verify the referenced build directory exists with a bin/ subdirectory.
        _build_bin = (
            execution.pipeline_dir
            / "build"
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
    # placement is enforced separately for lint_command_ref where the validator
    # inspects tool_name/ok and the forge becomes high-impact.
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
        for line in log_path.read_text(encoding="utf-8").splitlines():
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
        # build_meta.json, not execute trial_meta — accepting it here would
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
                            missing_state = sorted(name for name in state_variables if name not in keys)
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
                                f"{schema_path}: missing required state_variables from derived_contract ({sorted(missing_required)})"
                            )
                        for name, expected_shape in expected_state_variables.items():
                            declared_shape = state_variable_shapes.get(name)
                            if declared_shape is None:
                                continue
                            if _canonical_shape_expr(expected_shape) != _canonical_shape_expr(declared_shape):
                                violations.append(
                                    f"{schema_path}: variable {name} shape_expr must match derived_contract ({expected_shape})"
                                )

                    if expected_time_variable and expected_time_variable != time_variable:
                        violations.append(
                            f"{schema_path}: time_variable must match derived_contract ({expected_time_variable})"
                        )
                    if expected_time_variable and _canonical_shape_expr(expected_time_shape_expr) != _canonical_shape_expr(time_shape_expr):
                        violations.append(
                            f"{schema_path}: time_shape_expr must match derived_contract ({expected_time_shape_expr})"
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


def _validate_generate_outputs(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    generate_root = execution.pipeline_dir / "generate"
    if not generate_root.exists():
        violations.append(f"{generate_root}: missing")
        return

    model_files, expected_model_name = _node_model_files(generate_root, execution)
    if not model_files:
        if expected_model_name is None:
            violations.append(f"{generate_root}: model source not found")
        else:
            violations.append(
                f"{generate_root}: node model source not found ({expected_model_name})"
            )
        return

    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)
    required_sources = _semantic_required_sources(repo_root, execution)

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
            required_sources=required_sources,
            violations=violations,
        )
        _validate_problem_metric_only_scalar_kernel(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )
        _validate_problem_state_array_usage(
            repo_root=repo_root,
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

    _validate_fortran_makefile_dependencies(
        generate_root=generate_root,
        violations=violations,
    )


def _validate_generate_outputs_for_generation(
    repo_root: Path,
    execution: NodeExecution,
    generation_id: str,
    violations: list[str],
) -> None:
    gen_dir = execution.pipeline_dir / "generate" / generation_id
    if not gen_dir.is_dir():
        violations.append(
            f"{gen_dir}: missing generate directory for generation_id={generation_id!r}"
        )
        return
    src_dir = gen_dir / "src"
    if not src_dir.is_dir():
        violations.append(f"{src_dir}: missing src directory")
        return

    model_files, expected_model_name = _model_files_in_src_dir(src_dir, execution)
    if not model_files:
        if expected_model_name is None:
            violations.append(f"{src_dir}: model source not found")
        else:
            violations.append(
                f"{src_dir}: node model source not found ({expected_model_name})"
            )
        return

    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)
    required_sources = _semantic_required_sources(repo_root, execution)

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
            required_sources=required_sources,
            violations=violations,
        )
        _validate_problem_metric_only_scalar_kernel(
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )
        _validate_problem_state_array_usage(
            repo_root=repo_root,
            execution=execution,
            model_file=model_file,
            lowered=lowered,
            violations=violations,
        )

    _validate_fortran_makefile_src_dir(src_dir, violations)
    runner_files = sorted(src_dir.glob("*_runner.f90"))
    _validate_runner_source_files(execution, runner_files, violations)

    if dep_spec_ids:
        _validate_dependency_operation_on_model_files(
            model_files, dep_spec_ids, violations
        )


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
    try:
        dep_data = _read_json(dep_path)
    except json.JSONDecodeError:
        return None
    return dep_data if isinstance(dep_data, dict) else None


def _plan_dir_for_execution(repo_root: Path, execution: NodeExecution) -> Path | None:
    lineage_path = execution.pipeline_dir / "lineage.json"
    if not lineage_path.exists():
        return None

    lineage = _read_json(lineage_path)
    plan_ref = lineage.get("plan_ref")
    if not isinstance(plan_ref, str) or not plan_ref.startswith("workspace/"):
        return None

    plan_dir = repo_root / plan_ref
    if not plan_dir.exists() or not plan_dir.is_dir():
        return None
    return plan_dir


def _derived_contract_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    plan_dir = _plan_dir_for_execution(repo_root, execution)
    if plan_dir is None:
        return None

    contract_path = plan_dir / "derived_contract.json"
    if not contract_path.exists():
        return None

    try:
        data = _read_json(contract_path)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _algorithm_contract_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    plan_dir = _plan_dir_for_execution(repo_root, execution)
    if plan_dir is None:
        return None

    contract_path = plan_dir / "algorithm.resolved.yaml"
    if not contract_path.exists():
        return None

    data = _read_yaml(contract_path)
    return data if isinstance(data, dict) else None


def _algorithm_contract_path_for_execution(
    repo_root: Path, execution: NodeExecution
) -> Path | None:
    plan_dir = _plan_dir_for_execution(repo_root, execution)
    if plan_dir is None:
        return None
    return plan_dir / "algorithm.resolved.yaml"


def _impl_contract_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    plan_dir = _plan_dir_for_execution(repo_root, execution)
    if plan_dir is None:
        return None

    contract_path = plan_dir / "impl.resolved.yaml"
    if not contract_path.exists():
        return None

    try:
        data = _read_yaml(contract_path)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


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
    generate_root = pipeline_dir / "generate"
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


def _derived_contract_path_for_execution(
    repo_root: Path, execution: NodeExecution
) -> Path | None:
    plan_dir = _plan_dir_for_execution(repo_root, execution)
    if plan_dir is None:
        return None
    return plan_dir / "derived_contract.json"


def _tests_path_from_contract(repo_root: Path, contract: dict[str, Any]) -> Path | None:
    source = contract.get("source")
    if not isinstance(source, dict):
        return None
    tests_ref = source.get("tests")
    if not isinstance(tests_ref, str) or not tests_ref.strip():
        return None

    tests_path = Path(tests_ref.strip())
    if not tests_path.is_absolute():
        tests_path = repo_root / tests_path
    return tests_path


def _tests_path_for_execution(repo_root: Path, execution: NodeExecution) -> Path | None:
    contract = _derived_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return None
    return _tests_path_from_contract(repo_root, contract)


def _parse_test_ids_from_tests_md(tests_path: Path) -> list[str]:
    test_ids: list[str] = []
    seen: set[str] = set()
    for raw_line in tests_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        match = TEST_ID_HEADING_PATTERN.match(line)
        if not match:
            continue
        test_id = match.group(1).strip()
        if not test_id or test_id in seen:
            continue
        seen.add(test_id)
        test_ids.append(test_id)
    return test_ids


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


def _metrics_basis_entries(
    metrics_basis: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    raw_entries = metrics_basis.get("per_test")
    if raw_entries is None:
        raw_entries = metrics_basis.get("tests")

    entries: dict[str, dict[str, Any]] = {}
    problems: list[str] = []

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
            if test_id in entries:
                problems.append(f"per_test has duplicated test_id ({test_id})")
                continue
            entries[test_id] = item
        return entries, problems

    if isinstance(raw_entries, dict):
        for raw_test_id, item in raw_entries.items():
            if not isinstance(raw_test_id, str) or not raw_test_id.strip():
                problems.append("tests keys must be non-empty strings")
                continue
            if not isinstance(item, dict):
                problems.append(f"tests[{raw_test_id!r}] must be object")
                continue
            entries[raw_test_id.strip()] = item
        return entries, problems

    problems.append("must contain per_test list or tests object")
    return entries, problems


def _metrics_basis_variable_keys(entry: dict[str, Any]) -> set[str]:
    for field_name in ("raw_variables", "variables", "evidence"):
        raw_value = entry.get(field_name)
        if isinstance(raw_value, dict):
            return {
                key.strip()
                for key in raw_value
                if isinstance(key, str) and key.strip()
            }

    ignored_keys = {
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
    return {
        key.strip()
        for key in entry
        if isinstance(key, str) and key.strip() and key not in ignored_keys
    }


def _impl_language_from_plan_dir(repo_root: Path, plan_dir: Path) -> str | None:
    impl_path = plan_dir / "impl.resolved.yaml"
    if not impl_path.exists():
        return None
    try:
        data = _read_yaml(impl_path)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
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


def _validate_generate_meta_lint_shape(
    meta_path: Path, data: dict[str, Any], violations: list[str]
) -> None:
    ref = data.get("lint_command_ref")
    if ref is None:
        violations.append(f"{meta_path}: missing lint_command_ref")
        return
    if not isinstance(ref, dict):
        violations.append(f"{meta_path}: lint_command_ref must be json object")
        return
    run_entries = ref.get("run_linter")
    if run_entries is None:
        violations.append(f"{meta_path}: lint_command_ref.run_linter must be present")
        return
    if not isinstance(run_entries, list):
        violations.append(f"{meta_path}: lint_command_ref.run_linter must be array")
        return
    if not run_entries:
        violations.append(f"{meta_path}: lint_command_ref.run_linter must be non-empty")
        return
    for idx, item in enumerate(run_entries):
        if not isinstance(item, dict):
            violations.append(f"{meta_path}: lint_command_ref.run_linter[{idx}] must be object")
            continue
        for key in ("command_id", "command_log_ref", "preset"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                violations.append(
                    f"{meta_path}: lint_command_ref.run_linter[{idx}].{key} must be non-empty string"
                )


def _validate_generate_lint_command_logs(
    repo_root: Path,
    meta_path: Path,
    data: dict[str, Any],
    impl_language: str | None,
    violations: list[str],
) -> None:
    """Validate MCP run_linter command logs for Generate static lint."""
    status = data.get("verification_status")
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return

    if not impl_language:
        violations.append(
            f"{meta_path}: cannot validate static lint without impl.resolved.yaml toolchain.language"
        )
        return

    expected = _LINT_PRESET_FOR_LANGUAGE.get(impl_language)
    if expected is None:
        violations.append(
            f"{meta_path}: toolchain.language={impl_language!r} has no static lint mapping"
        )
        return

    ref = data.get("lint_command_ref")
    if ref is None:
        violations.append(
            f"{meta_path}: missing lint_command_ref when verification_status=pass"
        )
        return
    if not isinstance(ref, dict):
        violations.append(
            f"{meta_path}: lint_command_ref must be json object when verification_status=pass"
        )
        return
    run_entries = ref.get("run_linter")
    if run_entries is None:
        violations.append(
            f"{meta_path}: lint_command_ref.run_linter must be present when verification_status=pass"
        )
        return
    if not isinstance(run_entries, list):
        violations.append(
            f"{meta_path}: lint_command_ref.run_linter must be array when verification_status=pass"
        )
        return

    if not run_entries:
        violations.append(
            f"{meta_path}: lint_command_ref.run_linter must be non-empty when "
            "verification_status=pass and static lint applies"
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
                    f"{meta_path}: lint_command_ref.run_linter entries must be objects"
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
                f"{meta_path}: lint_command_ref.run_linter must have exactly one entry "
                f"for toolchain.language={impl_language}"
            )
        else:
            entry = run_entries[0]
            if not isinstance(entry, dict):
                violations.append(
                    f"{meta_path}: lint_command_ref.run_linter[0] must be object"
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
                f"{meta_path}: lint_command_ref.run_linter[{idx}] must be object"
            )
            continue
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref")
        preset_decl = entry.get("preset")
        if not isinstance(command_id, str) or not command_id.strip():
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}].command_id invalid"
            )
            continue
        if not isinstance(log_ref, str) or not log_ref.strip():
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}].command_log_ref invalid"
            )
            continue
        if not isinstance(preset_decl, str) or not preset_decl.strip():
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}].preset invalid"
            )
            continue
        preset_decl_l = preset_decl.strip().lower()
        if preset_decl_l not in _LINT_ALLOWED_PRESETS:
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}].preset must be one of "
                f"{sorted(_LINT_ALLOWED_PRESETS)}"
            )
            continue
        if preset_decl_l == "mixed":
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}].preset must not be "
                "'mixed'; record separate fortitude and cppcheck entries"
            )
            continue

        canonical_refs_lint = _canonical_mcp_log_refs_for_lint(meta_path, repo_root)
        log_ref_norm = log_ref.strip().rstrip("/")
        if canonical_refs_lint and log_ref_norm not in canonical_refs_lint:
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}].command_log_ref "
                f"must be the canonical MCP audit log placement "
                f"(expected one of {sorted(canonical_refs_lint)!r}, got {log_ref_norm!r}). "
                "Non-canonical placements are rejected to prevent forged tool-execution "
                "evidence."
            )
            continue

        matched = _find_command_log_record(repo_root, command_id.strip(), log_ref.strip())
        if matched is None:
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}]: command log not found "
                f"for command_id={command_id!r}"
            )
            continue
        if matched.get("tool_name") != "run_linter":
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}]: command_id={command_id!r} "
                f"tool_name must be run_linter"
            )
            continue
        if matched.get("ok") is not True:
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}]: command_id={command_id!r} "
                "run_linter did not succeed (ok must be true)"
            )
            continue
        command = matched.get("command")
        if not isinstance(command, list) or not command:
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}]: command log missing command"
            )
            continue
        inferred = _infer_run_linter_preset_from_command(command)
        if inferred != preset_decl_l:
            violations.append(
                f"{meta_path}: lint_command_ref.run_linter[{idx}]: logged command does not match "
                f"preset {preset_decl_l!r} (inferred {inferred!r})"
            )


def _find_command_log_record(
    repo_root: Path, command_id: str, log_ref: str
) -> dict[str, Any] | None:
    log_path = repo_root / log_ref if log_ref.startswith("workspace/") else Path(log_ref)
    if not log_path.exists():
        return None

    for raw_line in log_path.read_text(encoding="utf-8").splitlines():
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
    """Validate shape_expr against spec/schema/plan/shape_expr.schema.json.

    Allowed forms (canonical source: spec/schema/plan/shape_expr.schema.json):
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
            "See spec/schema/plan/shape_expr.schema.json for canonical forms; "
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
            # `_validate_derived_contract_schema` separately rejects schemas
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
    contract = _derived_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return None

    raw_requirements = contract.get("raw_requirements")
    if not isinstance(raw_requirements, dict):
        return None
    return raw_requirements


def _required_raw_evidence(
    repo_root: Path, execution: NodeExecution
) -> set[str]:
    required: set[str] = {"metrics_basis.json", "execution_trace.json"}
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


def _semantic_required_sources(repo_root: Path, execution: NodeExecution) -> set[str]:
    contract = _derived_contract_for_execution(repo_root, execution)
    if not isinstance(contract, dict):
        return set()

    required: set[str] = set()

    semantic_dep = contract.get("semantic_dependency")
    if isinstance(semantic_dep, dict):
        raw_sources = semantic_dep.get("required_sources")
        if isinstance(raw_sources, list):
            for item in raw_sources:
                token = None
                if isinstance(item, str):
                    token = item.strip().lower()
                elif isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        token = name.strip().lower()
                if token and FORTRAN_IDENTIFIER_PATTERN.fullmatch(token):
                    required.add(token)

    io_contract = contract.get("io_contract")
    if isinstance(io_contract, dict):
        outputs = io_contract.get("outputs")
        if isinstance(outputs, list):
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str):
                    continue
                token = name.strip().lower()
                if FORTRAN_IDENTIFIER_PATTERN.fullmatch(token):
                    required.add(token)
    return required


def _validate_derived_contract_file(
    repo_root: Path, contract_path: Path, violations: list[str]
) -> None:
    try:
        contract = _read_json(contract_path)
    except json.JSONDecodeError:
        violations.append(f"{contract_path}: invalid json")
        return

    if not isinstance(contract, dict):
        violations.append(f"{contract_path}: must be json object")
        return

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
            f"{contract_path}:numerical_kernel_contract must not appear in derived_contract.json; move generation contract to algorithm.resolved.yaml"
        )

    _validate_test_evidence_requirements(
        repo_root=repo_root,
        contract_path=contract_path,
        contract=contract,
        snapshot_reference_variables=snapshot_reference_variables,
        snapshot_required=snapshot_required,
        violations=violations,
    )


def _validate_derived_contract_schema(
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    contract_path = _derived_contract_path_for_execution(repo_root, execution)
    if contract_path is None:
        violations.append(
            f"{execution.pipeline_dir / 'lineage.json'}: plan_ref missing; cannot resolve derived_contract.json"
        )
        return
    if not contract_path.exists():
        violations.append(f"{contract_path}: missing")
        return
    _validate_derived_contract_file(repo_root, contract_path, violations)


def _extract_spec_var_names(derived_path: Path) -> set[str] | None:
    """Return spec-traceable variable names from derived_contract.json for provenance checks.

    Collects names from io_contract.inputs/outputs AND raw_requirements evidence schema
    variables — the union of these two sets constitutes the full set of variables that
    can be legitimately traced back to the external spec or evidence artifacts.

    Returns None when the source is unreliable (parse error or non-dict items found in
    io_contract), which signals the caller to skip provenance checking rather than
    hard-fail with an incomplete symbol set.
    """
    try:
        data = _read_json(derived_path)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    ic = data.get("io_contract")
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
                # false failures (the derived_contract validator will flag this separately).
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
    rr = data.get("raw_requirements")
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
                    "(canonical source: spec/schema/plan/shape_expr.schema.json)"
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
    # direct spec I/O (from derived_contract.json), temporaries, or derived_field_rules.
    # Only performed when direct_spec_vars is provided (plan-stage with derived_contract.json).
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
                if not isinstance(shape_expr, str) or not shape_expr.strip():
                    violations.append(
                        f"{contract_path}:state_contract.state_variables[{idx}].shape_expr must be non-empty string"
                    )
                elif not _parse_shape_expr(shape_expr)[0]:
                    violations.append(
                        f"{contract_path}:state_contract.state_variables[{idx}].shape_expr invalid"
                    )

        update_paths = state_contract.get("required_update_paths")
        if not isinstance(update_paths, list) or not all(
            isinstance(token, str) and token.strip() for token in update_paths
        ):
            violations.append(
                f"{contract_path}:state_contract.required_update_paths must be non-empty string list"
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
    contract_path = _algorithm_contract_path_for_execution(repo_root, execution)
    if contract_path is None:
        violations.append(
            f"{execution.pipeline_dir / 'lineage.json'}: plan_ref missing; cannot resolve algorithm.resolved.yaml"
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
    repo_root: Path,
    contract_path: Path,
    contract: dict[str, Any],
    snapshot_reference_variables: set[str],
    snapshot_required: bool,
    violations: list[str],
) -> None:
    tests_path = _tests_path_from_contract(repo_root, contract)
    if tests_path is None or not tests_path.exists():
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
    contract = _derived_contract_for_execution(repo_root, execution)
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
    entries, problems = _metrics_basis_entries(metrics_basis)
    for problem in problems:
        violations.append(f"{metrics_basis_path}: {problem}")
    if problems:
        return

    expected_test_ids = set(test_requirements)
    actual_test_ids = set(entries)
    missing = sorted(expected_test_ids - actual_test_ids)
    extra = sorted(actual_test_ids - expected_test_ids)
    if missing:
        violations.append(
            f"{metrics_basis_path}: missing per-test evidence for test_id ({missing})"
        )
    if extra:
        violations.append(
            f"{metrics_basis_path}: has unknown per-test evidence test_id ({extra})"
        )

    for test_id, required_variables in sorted(test_requirements.items()):
        entry = entries.get(test_id)
        if not isinstance(entry, dict):
            continue
        variable_keys = _metrics_basis_variable_keys(entry)
        missing_variables = sorted(required_variables - variable_keys)
        if missing_variables:
            violations.append(
                f"{metrics_basis_path}: test_id {test_id} missing required_raw_variables ({missing_variables})"
            )


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
    if isinstance(all_nodes, list):
        for item in all_nodes:
            expected.update(_dep_node_key_tokens(item))

    node_key = dep_data.get("node_key")
    if isinstance(node_key, str) and node_key.strip():
        expected.update(_dep_node_key_tokens(node_key))

    if not expected:
        for field in ("direct_deps", "transitive_deps"):
            deps = dep_data.get(field)
            if not isinstance(deps, list):
                continue
            for item in deps:
                expected.update(_dep_node_key_tokens(item))
        if isinstance(node_key, str) and node_key.strip():
            expected.update(_dep_node_key_tokens(node_key))

    return expected


def _dependency_run_token(dep_data: dict[str, Any]) -> str | None:
    resolved_at = dep_data.get("resolved_at")
    if isinstance(resolved_at, str) and resolved_at.strip():
        return resolved_at.strip()
    return None


def _spec_id_from_node_key(node_key: str) -> str | None:
    if "/" not in node_key:
        return None
    body = node_key.split("/", 1)[1]
    spec_id = body.split("@", 1)[0].strip()
    return spec_id or None


def _node_model_files(
    generate_root: Path, execution: NodeExecution
) -> tuple[list[Path], str | None]:
    spec_id = _spec_id_from_node_key(execution.node_key)
    if spec_id is None:
        return sorted(generate_root.glob("*/src/*_model.f90")), None

    expected_name = f"{spec_id}_model.f90"
    targets: list[Path] = []
    for gen_dir in sorted(d for d in generate_root.iterdir() if d.is_dir()):
        src_dir = gen_dir / "src"
        if not src_dir.exists():
            continue
        candidate = src_dir / expected_name
        if candidate.exists():
            targets.append(candidate)
    return targets, expected_name


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
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    dep_spec_ids = _component_dep_spec_ids(repo_root, execution)
    if not dep_spec_ids:
        return

    generate_root = execution.pipeline_dir / "generate"
    if not generate_root.exists():
        return

    model_files, expected_model_name = _node_model_files(generate_root, execution)
    if not model_files:
        if expected_model_name is not None:
            violations.append(
                f"{generate_root}: node model source not found ({expected_model_name})"
            )
        return

    _validate_dependency_operation_on_model_files(
        model_files, dep_spec_ids, violations
    )


def _validate_runner_source_files(
    execution: NodeExecution,
    runner_files: list[Path],
    violations: list[str],
) -> None:
    for runner_file in runner_files:
        text = runner_file.read_text(encoding="utf-8", errors="ignore").lower()
        for output_name in FORBIDDEN_RUNNER_OUTPUTS:
            if output_name in text:
                violations.append(
                    f"{runner_file}: forbidden runner output write detected ({output_name})"
                )
        _validate_problem_runner_diagnostics_dependency(
            execution=execution,
            runner_file=runner_file,
            lowered=text,
            violations=violations,
        )
        _validate_problem_runner_nonphysical_casepath_input(
            execution=execution,
            runner_file=runner_file,
            lowered=text,
            violations=violations,
        )
        _validate_runner_perf_json_serialization(
            runner_file=runner_file,
            lowered=text,
            violations=violations,
        )


def _validate_runner_outputs(execution: NodeExecution, violations: list[str]) -> None:
    generate_root = execution.pipeline_dir / "generate"
    runner_files = sorted(generate_root.glob("*/src/*_runner.f90"))
    if not runner_files:
        return
    _validate_runner_source_files(execution, runner_files, violations)


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
    pipeline_dir: Path, repo_root: Path, source_generation_id: str
) -> str | None:
    """Canonical command_log_ref placement for the trial's specific generation.

    skills/workflow-execute/SKILL.md L20 mandates run_quality_checks against
    `project_dir=generate/<gen_id>/src/`. The canonical placement is bound
    strictly to the trial_meta's declared `source_generation_id` — sibling
    or older generations under the same pipeline are NOT acceptable. This
    prevents a child from pointing trial_meta at a stale/unrelated
    generation's audit log to forge quality-check evidence.
    """
    gen_id = source_generation_id.strip()
    if not gen_id:
        return None
    canonical = pipeline_dir / "generate" / gen_id / "src" / _MCP_AUDIT_LOG_BASENAME
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
    _trial_source_build_id = data.get("source_build_id")
    _build_bin_abs: Path | None = None
    if isinstance(_trial_source_build_id, str) and _trial_source_build_id.strip():
        _build_bin_abs = (
            execution.pipeline_dir
            / "build"
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
            isinstance(arg, str) and arg.endswith("case.resolved.yaml")
            for arg in command
        )
        if not has_case_resolved:
            violations.append(
                f"{trial_meta_path}:run_program command_id={command_id} must include case.resolved.yaml"
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
    # `source_generation_id` (single source of truth). Sibling/older
    # generations under the same pipeline are not acceptable evidence for
    # this execute run.
    source_generation_id_raw = data.get("source_generation_id")
    source_generation_id: str | None = None
    if isinstance(source_generation_id_raw, str) and source_generation_id_raw.strip():
        source_generation_id = source_generation_id_raw.strip()
    canonical_qc_ref: str | None = None
    if source_generation_id is not None:
        canonical_qc_ref = _canonical_log_ref_for_run_quality_checks(
            execution.pipeline_dir, repo_root, source_generation_id
        )
        # Verify the declared generation actually exists (generate_meta.json
        # present) and is in pass state. A forged source_generation_id could
        # otherwise authorize an arbitrary path, and pointing at a failed or
        # superseded generation would credit stale evidence to this run.
        if source_generation_id is not None and canonical_qc_ref is not None:
            gen_meta = (
                execution.pipeline_dir
                / "generate"
                / source_generation_id
                / "generate_meta.json"
            )
            if not gen_meta.is_file():
                violations.append(
                    f"{trial_meta_path}:source_generation_id={source_generation_id!r} "
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
                        f"{trial_meta_path}:source_generation_id={source_generation_id!r} "
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

        # source_generation_id is required when a run_quality_checks record
        # is referenced — without it we cannot pin the canonical cross-phase
        # placement and would risk accepting evidence from a sibling/older
        # generation.
        if source_generation_id is None:
            violations.append(
                f"{trial_meta_path}:source_generation_id must be declared "
                f"when source_command_ref includes a run_quality_checks "
                f"record (command_id={command_id})."
            )
            continue
        if canonical_qc_ref is None:
            # generation_id present but generate_meta.json missing — already
            # reported above. Skip per-entry violation to avoid duplication.
            continue
        log_ref_norm = log_ref.strip().rstrip("/")
        if log_ref_norm != canonical_qc_ref:
            violations.append(
                f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                f"command_log_ref must be the canonical MCP audit log placement "
                f"for source_generation_id={source_generation_id!r} "
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
                    "must record cwd under generate/<generation_id>/src"
                )
                continue

            if not generate_src_dirs or not any(
                _path_is_same_or_under(cwd_path, src_dir) for src_dir in generate_src_dirs
            ):
                violations.append(
                    f"{trial_meta_path}:run_quality_checks command_id={command_id} "
                    "must run inside generate/<generation_id>/src for make-based quality check"
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
    repo_root: Path, execution: NodeExecution, violations: list[str]
) -> None:
    tests_path = _tests_path_for_execution(repo_root, execution)
    if tests_path is None or not tests_path.exists():
        return

    test_ids = _parse_test_ids_from_tests_md(tests_path)
    if not test_ids:
        violations.append(f"{tests_path}: test_id heading not found")
        return

    verdict_path = execution.node_dir / "verdict.json"
    summary_path = execution.node_dir / "summary.json"
    if not verdict_path.exists():
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
    """JSON オブジェクト/配列から数値・null リーフを再帰収集する（深さ上限 8）。"""
    if _depth > 8:
        return []
    if isinstance(obj, bool):
        return []  # bool は int のサブクラスなので先にチェック
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
    """metrics_basis.json が全ゼロ/全 null でないことを検証する。"""
    metrics_path = execution.node_dir / "raw" / "metrics_basis.json"
    if not metrics_path.exists():
        return  # 存在チェックは _validate_raw_evidence が担当

    try:
        data = _read_json(metrics_path)
    except json.JSONDecodeError:
        return  # JSON 構文エラーは他の関数で処理済み

    if not isinstance(data, dict):
        return

    numeric_values = _collect_numeric_leaves(data)
    if not numeric_values:
        return  # 数値フィールドが一切なければスキップ

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
    generate_root = execution.pipeline_dir / "generate"
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
) -> None:
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
                                required_markers = _required_launch_prompt_markers_for_role(role_l)
                                missing_markers = [
                                    marker for marker in required_markers if marker not in launch_text
                                ]
                                if missing_markers:
                                    violations.append(
                                        f"{runs_path}:line {idx + 1} {key} missing workflow-orchestration template markers ({', '.join(missing_markers)})"
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

        for edge_idx, parent_id, child_id in graph_edges:
            parent_role = run_roles.get(parent_id)
            child_role = run_roles.get(child_id)
            if parent_role is None:
                violations.append(
                    f"{graph_path}:edges[{edge_idx}] parent_agent_run_id not found in agent_runs.jsonl ({parent_id})"
                )
                continue
            if child_role is None:
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


def _resolve_plan_dir(repo_root: Path, workspace_root: str, raw_plan_ref: str) -> Path:
    workspace_path = (repo_root / workspace_root).resolve()
    candidate = Path(raw_plan_ref)
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(workspace_path.resolve())
    except ValueError as exc:
        raise ValueError(
            f"plan_ref must be under {workspace_path}: {candidate}"
        ) from exc
    plans_root = workspace_path / "plans"
    try:
        candidate.relative_to(plans_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"plan_ref must be under {plans_root}: {candidate}"
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


def _plan_dependency_node_key(plan_dir: Path) -> str | None:
    dep_path = plan_dir / "dependency.resolved.yaml"
    if not dep_path.exists():
        return None
    try:
        data = _read_yaml(dep_path)
    except yaml.YAMLError:
        return None
    if isinstance(data, dict):
        nk = data.get("node_key")
        if isinstance(nk, str) and nk.strip():
            return nk.strip()
    return None


def _try_load_optional_plan_yaml(plan_dir: Path, name: str, violations: list[str]) -> None:
    path = plan_dir / name
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


def validate_plan_stage(
    repo_root: Path,
    workspace_root: str,
    plan_ref: str,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_plan_stage_impl(repo_root, workspace_root, plan_ref)


def _validate_plan_stage_impl(
    repo_root: Path,
    workspace_root: str,
    plan_ref: str,
) -> list[str]:
    violations: list[str] = []
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"]
    try:
        plan_dir = _resolve_plan_dir(repo_root, workspace_root, plan_ref)
    except ValueError as exc:
        return [str(exc)]

    derived_path = plan_dir / "derived_contract.json"
    direct_spec_vars: set[str] | None = None
    if not derived_path.exists():
        violations.append(f"{derived_path}: missing")
    else:
        _validate_derived_contract_file(repo_root, derived_path, violations)
        direct_spec_vars = _extract_spec_var_names(derived_path)

    algo_path = plan_dir / "algorithm.resolved.yaml"
    if not algo_path.exists():
        violations.append(f"{algo_path}: missing")
    else:
        nk = _plan_dependency_node_key(plan_dir)
        _validate_algorithm_contract_file(
            repo_root,
            algo_path,
            violations,
            multidim_node_key=nk,
            direct_spec_vars=direct_spec_vars,
        )

    for optional in ("case.resolved.yaml", "impl.resolved.yaml", "dependency.resolved.yaml"):
        _try_load_optional_plan_yaml(plan_dir, optional, violations)
    _validate_plan_meta_json(plan_dir, violations)

    return violations


def _lineage_node_key_and_plan_ref(
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
    pr = data.get("plan_ref")
    node_key = nk.strip() if isinstance(nk, str) else None
    plan_ref = pr.strip() if isinstance(pr, str) else None
    return node_key, plan_ref


def _stub_execution(pipeline_dir: Path, node_key: str) -> NodeExecution:
    stub_dir = pipeline_dir / ".semantic_stage_stub"
    return NodeExecution(
        node_key=node_key,
        node_dir=stub_dir,
        exec_dir=stub_dir,
        pipeline_dir=pipeline_dir,
    )


def _latest_generation_id(pipeline_dir: Path) -> str | None:
    gen_root = pipeline_dir / "generate"
    if not gen_root.is_dir():
        return None
    latest_name: str | None = None
    latest_key: tuple[int, int] | None = None
    for gen_dir in sorted(d for d in gen_root.iterdir() if d.is_dir()):
        parsed = _parse_stage_attempt_id(gen_dir.name, "gen")
        if parsed is None:
            continue
        if latest_key is None or parsed > latest_key:
            latest_key = parsed
            latest_name = gen_dir.name
    if latest_name is None:
        return None
    return latest_name


def validate_post_generate_stage(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    generation_id: str | None,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_post_generate_stage_impl(
            repo_root, workspace_root, pipeline_ref, generation_id
        )


def _validate_post_generate_stage_impl(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    generation_id: str | None,
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

    node_key, plan_ref = _lineage_node_key_and_plan_ref(pipeline_dir)
    if not node_key:
        violations.append(f"{pipeline_dir / 'lineage.json'}: missing node_key")
        return violations

    gen_id = generation_id or _latest_generation_id(pipeline_dir)
    if not gen_id:
        violations.append(f"{pipeline_dir / 'generate'}: no generation directory found")
        return violations
    if _parse_stage_attempt_id(gen_id, "gen") is None:
        violations.append(
            f"{pipeline_dir / 'generate' / gen_id}: invalid generation_id; expected gen_<YYYYMMDD>_<seq3>"
        )
        return violations

    if plan_ref:
        plan_dir = (repo_root / plan_ref).resolve()
        derived_path = plan_dir / "derived_contract.json"
        if derived_path.exists():
            _validate_derived_contract_file(repo_root, derived_path, violations)
        else:
            violations.append(f"{derived_path}: missing (plan_ref {plan_ref})")

    execution = _stub_execution(pipeline_dir, node_key)
    _validate_generate_outputs_for_generation(
        repo_root, execution, gen_id, violations
    )

    gen_dir = pipeline_dir / "generate" / gen_id
    meta_path = gen_dir / "generate_meta.json"
    if meta_path.exists():
        try:
            meta_data = _read_json(meta_path)
        except json.JSONDecodeError:
            violations.append(f"{meta_path}: invalid json")
        else:
            if isinstance(meta_data, dict):
                impl_lang: str | None = None
                if plan_ref:
                    impl_lang = _impl_language_from_plan_dir(
                        repo_root, (repo_root / plan_ref).resolve()
                    )
                _validate_generate_lint_command_logs(
                    repo_root, meta_path, meta_data, impl_lang, violations
                )

    return violations


def validate_post_build_stage(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    generation_id: str | None,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_post_build_stage_impl(
            repo_root, workspace_root, pipeline_ref, generation_id
        )


def _validate_post_build_stage_impl(
    repo_root: Path,
    workspace_root: str,
    pipeline_ref: str,
    generation_id: str | None,
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

    gen_id = generation_id or _latest_generation_id(pipeline_dir)
    if not gen_id:
        violations.append(f"{pipeline_dir / 'generate'}: no generation directory found")
        return violations
    if _parse_stage_attempt_id(gen_id, "gen") is None:
        violations.append(
            f"{pipeline_dir / 'generate' / gen_id}: invalid generation_id; expected gen_<YYYYMMDD>_<seq3>"
        )
        return violations

    src_dir = pipeline_dir / "generate" / gen_id / "src"
    _validate_fortran_makefile_src_dir(src_dir, violations)
    return violations


def validate(
    repo_root: Path,
    workspace_root: str,
    pipeline_roots: list[Path] | None = None,
    require_llm_review: bool = True,
    require_orchestration: bool = False,
) -> list[str]:
    with _pinned_repo_root_for_schema(repo_root):
        return _validate_impl(
            repo_root,
            workspace_root,
            pipeline_roots,
            require_llm_review,
            require_orchestration,
        )


def _validate_impl(
    repo_root: Path,
    workspace_root: str,
    pipeline_roots: list[Path] | None,
    require_llm_review: bool,
    require_orchestration: bool,
) -> list[str]:
    violations: list[str] = []
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"]

    workspace_path = repo_root / workspace_root
    if not workspace_path.exists():
        return [f"{workspace_path}: workspace root does not exist"]

    executions = _node_executions(workspace_path, pipeline_roots=pipeline_roots)
    if not executions:
        return [f"{workspace_path}/pipelines: no execution artifacts found"]

    _validate_pipeline_lineage_presence(
        executions=executions,
        violations=violations,
    )

    seen_pipeline_dirs: set[Path] = set()
    for execution in executions:
        pd = execution.pipeline_dir
        if pd in seen_pipeline_dirs:
            continue
        seen_pipeline_dirs.add(pd)
        _validate_generate_meta_json_files(pd, violations)

    if require_orchestration:
        _validate_orchestration_hierarchy(
            workspace_path=workspace_path,
            executions=executions,
            violations=violations,
        )

    source_hash_map: dict[str, list[SourceFingerprint]] = {}
    dep_contexts: list[tuple[NodeExecution, set[str], str | None]] = []
    lineage_contexts: list[tuple[NodeLineage, set[str], str | None]] = []
    lineages = _lineage_records(workspace_path, pipeline_roots)

    for execution in executions:
        _validate_algorithm_contract_schema(repo_root, execution, violations)
        _validate_derived_contract_schema(repo_root, execution, violations)
        _validate_trial_meta(repo_root, execution, violations)
        _validate_execution_json_outputs(execution, violations)
        _validate_raw_evidence(repo_root, execution, violations)
        _validate_metrics_basis_not_trivial(execution, violations)
        _validate_generate_outputs(repo_root, execution, violations)
        _validate_dependency_operation_usage(repo_root, execution, violations)
        _validate_runner_outputs(execution, violations)
        _validate_run_program_inputs(repo_root, execution, violations)
        _validate_quality_check_commands(repo_root, execution, violations)
        _validate_tests_verdict_summary_consistency(repo_root, execution, violations)
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
        try:
            dep_data = _read_json(dep_path)
        except json.JSONDecodeError:
            continue
        if not isinstance(dep_data, dict):
            continue
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

    seen_dag_violations: set[tuple[Path, str, tuple[str, ...]]] = set()
    for execution, expected_nodes, token in dep_contexts:
        if token is None:
            available_nodes = scope_nodes
            scope_label = "validation scope"
        else:
            available_nodes = scope_nodes_by_token.get(token, set())
            scope_label = f"resolved_at={token}"
        missing = sorted(expected_nodes - available_nodes)
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
        if isinstance(lineage.plan_ref, str) and lineage.plan_ref.startswith("workspace/"):
            plan_path = repo_root / lineage.plan_ref
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
            "plan",
            "post_generate",
            "post_build",
            "post_execute",
            "pre_judge",
        ),
        default="full",
        help=(
            "full: default end-to-end validation (requires execution artifacts). "
            "plan: validate plan directory only (derived_contract + algorithm + optional YAML). "
            "post_generate / post_build: validate one pipeline generate tree (requires --pipeline-root). "
            "post_execute: full validation with LLM review and orchestration optional. "
            "pre_judge: full validation with LLM review and orchestration required."
        ),
    )
    parser.add_argument(
        "--plan-ref",
        default=None,
        help="Workspace-relative plan directory (required for --stage plan).",
    )
    parser.add_argument(
        "--generation-id",
        default=None,
        help=(
            "generate/<generation_id> under the pipeline (optional for post_generate/post_build; "
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
        if args.stage == "plan":
            if not args.plan_ref or not str(args.plan_ref).strip():
                print(
                    "pipeline semantic validation: FAIL\n"
                    "- --stage plan requires non-empty --plan-ref"
                )
                return 1
            violations = validate_plan_stage(
                repo_root, args.workspace_root, str(args.plan_ref).strip()
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
                    args.generation_id,
                )
            else:
                violations = validate_post_build_stage(
                    repo_root,
                    args.workspace_root,
                    pipeline_ref,
                    args.generation_id,
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

            if args.stage == "post_execute":
                violations = validate(
                    repo_root=repo_root,
                    workspace_root=args.workspace_root,
                    pipeline_roots=pipeline_roots,
                    require_llm_review=False,
                    require_orchestration=False,
                )
            elif args.stage == "pre_judge":
                violations = validate(
                    repo_root=repo_root,
                    workspace_root=args.workspace_root,
                    pipeline_roots=pipeline_roots,
                    require_llm_review=True,
                    require_orchestration=True,
                )
            else:
                violations = validate(
                    repo_root=repo_root,
                    workspace_root=args.workspace_root,
                    pipeline_roots=pipeline_roots,
                    require_llm_review=not args.allow_missing_llm_review,
                    require_orchestration=not args.allow_missing_orchestration,
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
