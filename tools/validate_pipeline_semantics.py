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


def _required_launch_prompt_markers_for_role(
    role: str,
    deterministic: bool = False,
) -> list[str]:
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
    return json.loads(path.read_text(encoding="utf-8"))


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
) -> None:
    generate_root = pipeline_dir / "source"
    if not generate_root.exists() or not generate_root.is_dir():
        return
    for gen_dir in sorted(generate_root.iterdir()):
        if not gen_dir.is_dir():
            continue
        meta_path = gen_dir / "source_meta.json"
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
        # NOTE: lint is no longer recorded in source_meta.lint_command_ref (the leaf does
        # not run run_linter); the conductor-run lint is certified by post_generate (which now
        # runs in the deterministic generate.static substep, before verify) against the
        # host-authored lint evidence (_validate_generate_lint_command_logs).


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
                                # Identify this snapshot's test_id to scope its
                                # required raw variables. Compile/runner output
                                # shape varies across runs (C-class IR
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
                                candidate_test_ids: list[str] = []
                                raw_test_id = data.get("test_id")
                                if isinstance(raw_test_id, str) and raw_test_id.strip():
                                    candidate_test_ids.append(raw_test_id.strip())
                                mapped_test_id = case_to_test.get(case_token)
                                if mapped_test_id:
                                    candidate_test_ids.append(mapped_test_id)
                                candidate_test_ids.append(case_token)
                                case_required = None
                                for candidate in candidate_test_ids:
                                    if candidate in per_test_required:
                                        case_required = per_test_required[candidate]
                                        break
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
    trial_meta_path = execution.node_dir / "trial_meta.json"
    if not trial_meta_path.exists():
        return None
    try:
        data = _read_json(trial_meta_path)
    except (json.JSONDecodeError, OSError):
        return None
    source_source_id = data.get("source_source_id")
    if not isinstance(source_source_id, str) or not source_source_id.strip():
        return None
    source_source_id = source_source_id.strip()
    # The id is used directly as a path component, so reject anything that is not
    # a single plain directory name: a separator, absolute path, or `.`/`..`
    # traversal would otherwise escape `<pipeline>/source/` and run the structural
    # source checks against an unintended (or out-of-pipeline) directory. Flag it
    # rather than silently skipping so a forged/malformed trial_meta is caught.
    id_parts = Path(source_source_id).parts
    if (
        "/" in source_source_id
        or "\\" in source_source_id
        or len(id_parts) != 1
        or id_parts[0] in (".", "..")
    ):
        violations.append(
            f"{trial_meta_path}:source_source_id={source_source_id!r} must be a "
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

    _validate_fortran_identifier_length(src_dir, violations)
    _validate_fortran_implicit_none_spec_list(
        src_dir,
        _impl_standard_from_pipeline_dir(repo_root, execution.pipeline_dir),
        violations,
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
# name (identifier) to 63 characters. An over-limit identifier compiles nowhere
# and only surfaces at the build step as a compile_error, which forces an
# expensive regenerate -> rebuild retry loop (observed in a past run: an
# over-63-char subroutine name). Catching it here at post_generate fails the
# cheap deterministic generate.static substep instead, before the build phase ever runs.
# (See docs/workflow/phases/phase_02_generate.md; the generated code uses the
# f2008 standard series — cf. the C003 / -std=f2008 note there.)
_FORTRAN_NAME_LIMIT = 63
# Free-form suffixes only. The workflow generates free-form Fortran (`.f90`),
# and the stripper below handles free-form `!` comments. Fixed-form sources
# (`.f` / `.for`) use column-1 `C` / `c` / `*` comment markers that this
# stripper does not understand, so scanning them would mis-report a long word
# in a comment as an over-limit identifier; they are intentionally excluded.
_FORTRAN_SOURCE_SUFFIXES = (".f90", ".f95", ".f03", ".f08")
_FORTRAN_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# The F2018 spec-list form `implicit none (external)` / `implicit none (type, external)`
# is what fortitude rule C003 wants, but under `-std=f2008` it is a compile_error
# (`Error: Fortran 2018: IMPLICIT NONE with spec list`). A parenthesised argument list
# after `implicit none` is the tell; plain `implicit none` (no `(`) never matches.
_FORTRAN_IMPLICIT_NONE_SPEC_LIST_RE = re.compile(
    r"\bimplicit\s+none\s*\(", re.IGNORECASE
)
# Fortran standards that DO accept the `implicit none` spec-list form. When the
# resolved toolchain standard is one of these the check below stays silent;
# f2008 (and unresolved/None, treated as the f2008 series the generator targets)
# get the compile_error flagged early.
_IMPLICIT_NONE_SPEC_LIST_STANDARDS = frozenset({"f2018", "f2023"})


def _strip_fortran_comments_and_strings(
    line: str, quote: str | None = None
) -> tuple[str, str | None]:
    """Drop quoted strings and the trailing free-form ``!`` comment from a line.

    Returns ``(code, quote)`` where ``quote`` is the still-open string delimiter
    at end of line (or ``None``). Pass it back in for the next physical line so a
    `&`-continued character literal carries its in-string state across lines —
    otherwise a long word on the continuation line would be scanned as code and
    falsely flagged. Free-form only — see _FORTRAN_SOURCE_SUFFIXES for why
    fixed-form sources are not scanned. (A token inside a string is never an
    identifier, so over-carrying state can only under-report, never false-flag;
    the build step remains the backstop.)
    """
    out: list[str] = []
    for ch in line:
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "!":
            break
        out.append(ch)
    return "".join(out), quote


def _validate_fortran_identifier_length(src_dir: Path, violations: list[str]) -> None:
    """Flag any Fortran identifier exceeding the 63-char f2008 name limit.

    Any identifier-like token longer than 63 characters is necessarily an
    invalid name (no keyword or intrinsic is that long), so reporting it is
    safe. Each distinct offending name is reported once per file.
    """
    if not src_dir.is_dir():
        return
    for path in sorted(src_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _FORTRAN_SOURCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        seen: set[str] = set()
        quote: str | None = None
        for raw in text.splitlines():
            code, quote = _strip_fortran_comments_and_strings(raw, quote)
            for tok in _FORTRAN_IDENT_RE.findall(code):
                if len(tok) > _FORTRAN_NAME_LIMIT and tok not in seen:
                    seen.add(tok)
                    violations.append(
                        f"{path}: Fortran identifier exceeds the {_FORTRAN_NAME_LIMIT}-char "
                        f"f2008 name limit ({len(tok)} chars): {tok!r}"
                    )


def _validate_fortran_implicit_none_spec_list(
    src_dir: Path, standard: str | None, violations: list[str]
) -> None:
    """Flag the F2018 spec-list `implicit none (...)` form under `-std=f2008`.

    fortitude rule C003 wants the spec-list form, but under the f2008 standard it
    is a compile_error that lint and the (non-compiling) post_generate static gate
    both miss — it only surfaces at Build or the standard-aware verify persona
    (observed in orch_20260702T032026Z_75ad595e). Catching it here fails the cheap
    deterministic generate.static substep and warm-resumes Generate.generate first.
    The check stays silent when the toolchain standard explicitly accepts the form
    (f2018/f2023); f2008 and unresolved/None (the f2008 series the generator
    targets) are flagged. Reuses the free-form comment/string stripper so a
    spec-list inside a comment or literal is never false-flagged; reports each file
    once. See docs/workflow/phases/phase_02_generate.md §2-1 (C003 workaround).

    Accepted limitation (same as _validate_fortran_identifier_length): a
    `implicit none &`-continued onto the next line before the `(` is scanned as
    two physical lines and not flagged. This can only under-report, never
    false-flag; the generator emits the form on one line and the Build step is
    the backstop.
    """
    if standard in _IMPLICIT_NONE_SPEC_LIST_STANDARDS:
        return
    if not src_dir.is_dir():
        return
    for path in sorted(src_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _FORTRAN_SOURCE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        quote: str | None = None
        flagged = False
        for raw in text.splitlines():
            code, quote = _strip_fortran_comments_and_strings(raw, quote)
            if not flagged and _FORTRAN_IMPLICIT_NONE_SPEC_LIST_RE.search(code):
                flagged = True
                violations.append(
                    f"{path}: F2018 spec-list `implicit none (...)` is a compile_error "
                    f"under -std=f2008 (Fortran 2018: IMPLICIT NONE with spec list); use "
                    f"plain `implicit none` with the `! allow(C003)` directive on the line "
                    f"immediately before it (see docs/workflow/phases/phase_02_generate.md §2-1)"
                )


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

    _validate_fortran_identifier_length(src_dir, violations)
    _validate_fortran_implicit_none_spec_list(
        src_dir,
        _impl_standard_from_pipeline_dir(repo_root, execution.pipeline_dir),
        violations,
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
    runner_files = sorted(src_dir.glob("*_runner.f90"))
    _validate_runner_source_files(
        execution, runner_files, violations,
        known_case_ids=_case_ids_for_execution(repo_root, execution),
    )

    if dep_spec_ids:
        _validate_dependency_operation_on_model_files(
            model_files, dep_spec_ids, violations
        )


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


def _impl_standard_from_pipeline_dir(
    repo_root: Path, pipeline_dir: Path
) -> str | None:
    """Resolve `spec.ir.yaml#impl_defaults.toolchain.standard` (lowercased), or
    None when unresolvable. Sibling of `_impl_toolchain_from_pipeline_dir` — kept
    separate so that function's `(build_system, language)` return arity (3+ call
    sites) stays unchanged. Used to gate the `implicit none` spec-list check."""
    ir_dir = _ir_dir_from_pipeline_dir(repo_root, pipeline_dir)
    if ir_dir is None:
        return None
    contract_path = ir_dir / "spec.ir.yaml"
    if not contract_path.exists():
        return None
    try:
        data = _read_yaml(contract_path)
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    impl_defaults = data.get("impl_defaults")
    toolchain = (
        impl_defaults.get("toolchain")
        if isinstance(impl_defaults, dict)
        else data.get("toolchain")
    )
    if not isinstance(toolchain, dict):
        return None
    standard = toolchain.get("standard")
    return standard.strip().lower() if isinstance(standard, str) and standard.strip() else None


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
            "diagnostics_contract",
            "source",
        ):
            if key in section:
                flattened[key] = section[key]
        return flattened
    return data


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _algorithm_contract_for_execution(
    repo_root: Path, execution: NodeExecution
) -> dict[str, Any] | None:
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return None

    contract_path = ir_dir / "spec.ir.yaml"
    if not contract_path.exists():
        return None

    data = _read_yaml(contract_path)
    return data if isinstance(data, dict) else None


def _algorithm_contract_path_for_execution(
    repo_root: Path, execution: NodeExecution
) -> Path | None:
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


def _io_contract_path_for_execution(
    repo_root: Path, execution: NodeExecution
) -> Path | None:
    ir_dir = _ir_dir_for_execution(repo_root, execution)
    if ir_dir is None:
        return None
    return ir_dir / "spec.ir.yaml"


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
    contract = _io_contract_for_execution(repo_root, execution)
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


def _case_id_to_test_id(
    repo_root: Path, execution: NodeExecution
) -> dict[str, str]:
    """Map each IR case_id to its tests.md test_id (case.test_case_set).

    Used to scope per-snapshot evidence: a snapshot file named for a case need
    only carry the raw variables that case's test declares in
    io_contract.test_evidence_requirements (e.g. an input-guard rejection case
    that produces no output state), not the global union of declared variables.
    """
    data = _algorithm_contract_for_execution(repo_root, execution)
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


def _case_ids_for_execution(repo_root: Path, execution: NodeExecution) -> set[str]:
    """All declared ``case.test_case_set[].case_id`` for the execution's IR.

    Used by the post_generate snapshot-filename check to avoid a false positive on
    a hardcoded ``raw/state_snapshots/<case_id>.json`` literal that legitimately
    matches a declared case (it satisfies the deliverable gate). Unlike
    ``_case_id_to_test_id`` this includes cases whose ``test_id`` is null.
    """
    data = _algorithm_contract_for_execution(repo_root, execution)
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


def _validate_generate_lint_command_logs(
    repo_root: Path,
    meta_path: Path,
    data: dict[str, Any],
    impl_language: str | None,
    violations: list[str],
) -> None:
    """Certify the conductor-run static lint for Generate against its host-authored,
    leaf-non-writable evidence (`<pipeline_root>/lint_evidence/<source_id>.json`).

    Static lint is no longer run by the leaf (it is the deterministic `generate.lint`
    substep run in-process by the conductor — Conductor._lint_inproc). The evidence
    certificate cannot be forged by the leaf (the pipeline root is read-only inside the
    sandbox), so this validates against it rather than the former leaf-written
    `source_meta.lint_command_ref` (which is now ignored)."""
    # meta_path = <pipeline_root>/source/<source_id>/source_meta.json
    source_id = meta_path.parent.name
    pipeline_root = meta_path.parents[2]
    from tools.hooks.lint_evidence import lint_evidence_path, read_lint_evidence

    # post_generate now runs in the deterministic `generate.static` substep, which executes
    # BEFORE `generate.verify` sets verification_status=pass — but the conductor already wrote
    # the lint evidence in `generate.lint`. Certify whenever that evidence exists (the
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
            "verification_status=pass; lint is run by the conductor (generate.lint)"
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

        canonical_refs_lint = _canonical_mcp_log_refs_for_lint(meta_path, repo_root)
        log_ref_norm = log_ref.strip().rstrip("/")
        if canonical_refs_lint and log_ref_norm not in canonical_refs_lint:
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}].command_log_ref "
                f"must be the canonical MCP audit log placement "
                f"(expected one of {sorted(canonical_refs_lint)!r}, got {log_ref_norm!r}). "
                "Non-canonical placements are rejected to prevent forged tool-execution "
                "evidence."
            )
            continue

        matched = _find_command_log_record(repo_root, command_id.strip(), log_ref.strip())
        if matched is None:
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}]: command log not found "
                f"for command_id={command_id!r}"
            )
            continue
        if matched.get("tool_name") != "run_linter":
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}]: command_id={command_id!r} "
                f"tool_name must be run_linter"
            )
            continue
        if matched.get("ok") is not True:
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}]: command_id={command_id!r} "
                "run_linter did not succeed (ok must be true)"
            )
            continue
        command = matched.get("command")
        if not isinstance(command, list) or not command:
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}]: command log missing command"
            )
            continue
        inferred = _infer_run_linter_preset_from_command(command)
        if inferred != preset_decl_l:
            violations.append(
                f"{meta_path}: lint evidence run_linter[{idx}]: logged command does not match "
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


def _semantic_required_sources(repo_root: Path, execution: NodeExecution) -> set[str]:
    contract = _io_contract_for_execution(repo_root, execution)
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
            "source",
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
        repo_root=repo_root,
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


def _diagnostics_contract_check_ids(contract: dict[str, Any]) -> list[str]:
    """Return the declared checks[].id list (empty when absent/malformed)."""
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
    contract_path = _io_contract_path_for_execution(repo_root, execution)
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
    if isinstance(contract.get("algorithm"), dict):
        contract = contract["algorithm"]

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
        execution, runner_files, violations, known_case_ids=known_case_ids
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
                                is_deterministic = DETERMINISTIC_PROMPT_SENTINEL in launch_text
                                required_markers = _required_launch_prompt_markers_for_role(
                                    role_l, deterministic=is_deterministic)
                                missing_markers = [
                                    marker
                                    for marker in required_markers
                                    if not _launch_prompt_marker_present(marker, launch_text)
                                ]
                                if missing_markers:
                                    violations.append(
                                        f"{runs_path}:line {idx + 1} {key} missing launch-prompt template markers ({', '.join(missing_markers)})"
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
    dep_path = ir_dir / "spec.ir.yaml"
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
    _validate_ir_meta_json(ir_dir, violations)
    _validate_compile_dependency_consistency(repo_root, ir_dir, violations)

    return violations


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

    seen_pipeline_dirs: set[Path] = set()
    for execution in executions:
        pd = execution.pipeline_dir
        if pd in seen_pipeline_dirs:
            continue
        seen_pipeline_dirs.add(pd)
        _validate_source_meta_json_files(pd, violations)

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
