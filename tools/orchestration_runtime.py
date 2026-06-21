#!/usr/bin/env python3
"""Helpers for workflow orchestration artifacts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

# fcntl is POSIX-only. Used by Adv-24 to serialize agent_runs.jsonl
# duplicate-check + append against concurrent finalizers. On non-POSIX
# platforms the lock degrades to a no-op (single-process workflows are the
# only supported configuration there).
try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — non-POSIX
    _fcntl = None  # type: ignore[assignment]

# Codex round 25 F1 → round 27 F1: PyYAML is required by the dependency-
# readiness paths (`_read_deps_yaml`, `_load_spec_catalog_from_bytes`) but
# NOT by recovery commands (`set-status`, `record-timeout`,
# `workflow-launch-check` for leaf nodes, etc.). Module-level import would
# brick every CLI command when the package is missing — that expanded the
# blast radius from a localized feature failure to a full control-plane
# outage. Use a lazy resolver instead so:
#   - paths that parse YAML get an actionable RuntimeError when the package
#     is missing (distinct from missing repo data, with install hint),
#   - all other commands (status updates, audit reads, cleanup) remain
#     usable without PyYAML installed.


def _require_yaml() -> Any:
    """Lazy PyYAML resolver. Raises a distinct RuntimeError when PyYAML is
    not installed so the diagnostic is "install PyYAML" rather than
    "deps.yaml missing/unparseable" (Codex round 25 F1 + round 27 F1)."""
    try:
        import yaml as _yaml_mod
    except ImportError as exc:  # pragma: no cover — install gap
        raise RuntimeError(
            "tools.orchestration_runtime: PyYAML is required for parsing "
            "deps.yaml / spec_catalog.yaml. Install with `pip install PyYAML`. "
            "Recovery commands (set-status, record-timeout, etc.) that do "
            "not parse YAML remain usable without it."
        ) from exc
    return _yaml_mod

try:
    from tools.hooks.common import (
        _normalize_rel_posix,
        _utc_now_iso,
        _ALLOWED_BYPRODUCT_EXTENSIONS,
        _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES,
        _COMPILER_BYPRODUCT_EXTENSIONS,
        validate_pipeline_semantics_stage,
    )
    from tools.meta_contracts import (
        STAGE_META_FILENAME_BY_STEP,
        missing_required_meta_keys,
    )
except ModuleNotFoundError:  # pragma: no cover - import bootstrap for direct CLI execution
    _THIS_FILE = Path(__file__).resolve()
    _REPO_ROOT = _THIS_FILE.parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from tools.hooks.common import (
        _normalize_rel_posix,
        _utc_now_iso,
        _ALLOWED_BYPRODUCT_EXTENSIONS,
        _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES,
        _COMPILER_BYPRODUCT_EXTENSIONS,
        validate_pipeline_semantics_stage,
    )
    from tools.meta_contracts import (
        STAGE_META_FILENAME_BY_STEP,
        missing_required_meta_keys,
    )

TERMINAL_STATUSES = {"pass", "fail", "blocked", "timeout", "cancel"}
# Budget (chars) for a child's verbatim final reply (launches/<arid>.reply.txt). The reply
# lands in the orchestration transcript twice (the Agent tool return + the record-reply
# argument) and is re-read every turn (cache_read scales with context × turns), so an
# over-long reply is a primary driver of the quadratic orchestration cost. record_agent_run
# flags an over-budget reply as telemetry by default; METDSL_ENFORCE_REPLY_BUDGET=1 makes it
# a hard fail so the child must be re-launched with a terse final message (full detail belongs
# in the child's artifacts, which the orchestration reads on demand — not in the reply).
REPLY_BUDGET_CHARS = 2000
# Phases that run via substep agents (the orchestration agent aggregates and is the
# step_result executor). Build is the only no-substep phase (step agent is the executor).
# Mirrors SUBSTEP_WORKFLOW_STEPS in tools/validate_pipeline_semantics.py.
SUBSTEP_AWARE_STEPS = frozenset({"compile", "generate", "validate"})
# Idempotency target of `set-status`. A status once in this set rejects both a same-value and an
# other-terminal transition, except for the permitted promotion (fail -> fail_closed). Appending the
# failure narrative is done in failure_analysis.json.
# A same-value re-call permits a cleanup retry only when the cleanup_committed marker is absent (F2).
IDEMPOTENT_TERMINAL_STATUSES = TERMINAL_STATUSES | {"fail_closed"}


_DEPENDENCY_READINESS_STAGES: tuple[str, ...] = (
    "ir_ref",
    "pipeline_ref",
    "aggregate_verdict",
)


def _read_deps_yaml(repo_root: Path, spec_ref: Any) -> dict[str, Any] | None:
    if not isinstance(spec_ref, str) or not spec_ref.strip():
        return None
    deps_path = (repo_root / spec_ref.strip() / "deps.yaml").resolve()
    try:
        deps_path.relative_to(repo_root.resolve())
    except ValueError:
        return None
    if not deps_path.is_file():
        return None
    # Codex round 27 F1: resolve PyYAML BEFORE the try-block so its
    # install-required `RuntimeError` propagates to the caller. Catching it
    # here would silently turn "package missing" into "deps.yaml unparseable"
    # and let dependency_readiness fall through to its "no deps" branch.
    yaml_mod = _require_yaml()
    try:
        return yaml_mod.safe_load(deps_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _deps_yaml_bytes_are_canonical_empty(deps_bytes: bytes) -> bool:
    """Codex round 34 F1: a CONSERVATIVE byte-level recognizer for the
    canonical empty-deps form of `deps.yaml`. Returns True only when the
    file content unambiguously declares both `components: []` and
    `profiles: []` under a single `dependencies:` key — any deviation
    (extra keys, populated lists, exotic YAML syntax) returns False so
    the caller falls back to full YAML parsing.

    This lets `_compute_initial_dependency_readiness` recognize a true
    leaf without invoking PyYAML, so a brand-new no-deps orchestration
    remains launchable during a controller packaging issue. The recognizer
    is intentionally strict: false negatives are safe (caller parses with
    PyYAML if available, else fails closed); false positives would be a
    fail-open (orchestration treated as leaf when it actually has deps).
    """
    text = deps_bytes.decode("utf-8", errors="ignore")
    significant: list[str] = []
    for raw_line in text.splitlines():
        # Strip trailing whitespace and inline comments.
        line = raw_line.split("#", 1)[0].rstrip()
        if line.strip() == "":
            continue
        significant.append(line)
    # Only accept the two key orderings, with optional trailing newline.
    canonical_forms = [
        ["dependencies:", "  components: []", "  profiles: []"],
        ["dependencies:", "  profiles: []", "  components: []"],
    ]
    return significant in canonical_forms


class SpecCatalogCorruption(Exception):
    """Codex round 33 F2: distinct failure mode for `spec_catalog.yaml`
    parse / top-level schema corruption. Distinguishes a repository-wide
    catalog outage from an ordinary "no matching dep" verification result
    so observability tooling and the CLI exit can fail loud instead of
    silently flipping every readiness flag to False."""


@lru_cache(maxsize=16)
def _load_spec_catalog_from_bytes(
    content_bytes: bytes,
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Parse `spec_catalog.yaml` bytes into `(spec_kind, spec_id) → versions`.

    Codex round 26 F1: cache keyed on FILE CONTENT bytes (not mtime).
    Restore/copy workflows that preserve mtime while changing content
    previously left the cache returning the OLD parsed dict even though
    `_dependency_set_fingerprint` always reads current bytes — letting
    readiness be persisted from stale catalog with a fingerprint derived
    from the new bytes. Keying on content makes cache hits semantic:
    same bytes → same parsed dict, no drift possible.
    """
    # Codex round 35 F2: zero-byte catalog is corruption, not "no specs
    # yet". The previous lenient early-return collapsed a truncated or
    # partially restored `spec_catalog.yaml` into an ordinary readiness=
    # false dep miss instead of surfacing the repo-wide outage. The
    # missing-file case was already promoted to `SpecCatalogCorruption`
    # by `_load_spec_catalog` (round 34 F2); empty file content reaches
    # this layer only when the file exists but holds zero bytes, which
    # is just as broken.
    if not content_bytes:
        raise SpecCatalogCorruption(
            "spec_catalog.yaml is empty (zero-byte file). Dependency "
            "resolution requires the canonical registry; this is a "
            "repository-wide outage, not a normal dependency miss."
        )
    # Codex round 27 F1: resolve PyYAML BEFORE the try-block so a missing
    # package raises `RuntimeError` to the caller instead of being swallowed
    # as "catalog unparseable → empty dict" (which would make every
    # `_matching_dep_versions` call return [] and fail readiness silently).
    yaml_mod = _require_yaml()
    # Codex round 33 F2: catalog parse/schema problems propagate as a
    # distinct `SpecCatalogCorruption` exception rather than being
    # downgraded to `{}` (which made downstream resolution treat
    # repository-wide catalog corruption as "no matching dep" — a normal
    # negative verification — and let `mark_dependency_readiness` complete
    # with all readiness flags false, hiding an outage as an ordinary
    # dependency miss). Distinct failure mode matches the treatment of
    # malformed deps.yaml (`deps_yaml_malformed_schema`).
    try:
        doc = yaml_mod.safe_load(content_bytes.decode("utf-8"))
    except Exception as exc:
        raise SpecCatalogCorruption(
            f"spec_catalog.yaml could not be parsed as YAML: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise SpecCatalogCorruption(
            f"spec_catalog.yaml top-level must be a mapping; got {type(doc).__name__}"
        )
    specs = doc.get("specs")
    if not isinstance(specs, list):
        raise SpecCatalogCorruption(
            "spec_catalog.yaml is missing the canonical `specs:` list "
            f"(found {type(specs).__name__})"
        )
    grouped: dict[tuple[str, str], list[str]] = {}
    for entry in specs:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("spec_kind")
        sid = entry.get("spec_id")
        ver = entry.get("spec_version")
        if not (isinstance(kind, str) and isinstance(sid, str) and isinstance(ver, str)):
            continue
        kind_token = kind.strip()
        sid_token = sid.strip()
        ver_token = ver.strip()
        # Codex round 15 F2: reject catalog entries with path-unsafe tokens.
        if not (
            _is_safe_path_token(kind_token)
            and _is_safe_path_token(sid_token)
            and _is_safe_path_token(ver_token)
        ):
            continue
        grouped.setdefault((kind_token, sid_token), []).append(ver_token)
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for key, versions in grouped.items():
        deduped = sorted(set(versions), key=_parse_semver, reverse=True)
        out[key] = tuple(deduped)
    return out


def _load_spec_catalog(repo_root_str: str) -> dict[tuple[str, str], tuple[str, ...]]:
    """Read catalog bytes each call and dispatch to a content-keyed cache.

    Codex round 26 F1: previously keyed on `(repo_root, mtime_ns)`, which
    could be bypassed by edits that preserve mtime (restore-from-cache,
    `cp -p`, etc.). Hashing on content (via the bytes themselves as the
    `lru_cache` key) guarantees a cache hit only when the file contents
    are byte-identical, so dependency resolution and `_dependency_set_fingerprint`
    cannot diverge.
    """
    # Codex round 34 F2: missing / unreadable catalog is now a HARD failure
    # (`SpecCatalogCorruption`). Previously it returned `{}` which downstream
    # dep resolution treated as "no matching versions" — making a repo-wide
    # registry outage look like an ordinary readiness=false dependency miss
    # and sending operators to the wrong layer for recovery. All three
    # production call sites (`_certify_and_collect_dep_artifacts`,
    # `_verify_dependency_readiness`, `_relevant_catalog_subset_bytes`) only
    # invoke this function AFTER deps.yaml entries are confirmed non-empty,
    # so leaf orchestrations (which need no catalog) are unaffected.
    catalog_path = Path(repo_root_str) / "spec" / "registry" / "spec_catalog.yaml"
    if not catalog_path.is_file():
        raise SpecCatalogCorruption(
            f"spec_catalog.yaml is missing at {catalog_path}. Dependency "
            "resolution requires the canonical registry; this is a "
            "repository-wide outage, not a normal dependency miss."
        )
    try:
        content = catalog_path.read_bytes()
    except OSError as exc:
        raise SpecCatalogCorruption(
            f"spec_catalog.yaml at {catalog_path} is unreadable: {exc}"
        ) from exc
    return _load_spec_catalog_from_bytes(content)


# Preserve the existing `_load_spec_catalog.cache_clear()` call surface used by
# tests; delegate to the content-keyed inner cache.
_load_spec_catalog.cache_clear = _load_spec_catalog_from_bytes.cache_clear  # type: ignore[attr-defined]


def resolve_spec_ref_for(
    repo_root: Path, spec_kind: Any, spec_id: Any
) -> str | None:
    """Map a catalog `(spec_kind, spec_id)` to its spec_ref (spec directory).

    The dependency closure driver (`tools/run_workflow.py --with-deps`) needs
    the spec_ref PATH of each dependency node so it can run that node's own
    workflow. `_load_spec_catalog` only retains versions per `(kind, spec_id)`
    and drops paths, so this is a separate, path-preserving read of the same
    canonical registry.

    The spec_ref is the dirname of the entry's `deps_path`
    (e.g. `spec/.../advdiff1d_linear/deps.yaml` → `spec/.../advdiff1d_linear`);
    `controlled_spec_path` is used as a fallback. Returns the repo-relative
    spec_ref string, or None when no catalog entry matches.

    Assumes one spec directory per `(spec_kind, spec_id)` (version lives inside
    `controlled_spec.md`, not in the directory path). When multiple entries for
    the same `(kind, spec_id)` resolve to DIFFERENT directories, that ambiguity
    is fail-closed (returns None) rather than silently picking one.

    `spec_kind` / `spec_id` are validated with `_is_safe_path_token` before any
    path use, consistent with the rest of the dependency-resolution layer.
    """
    if not (_is_safe_path_token(spec_kind) and _is_safe_path_token(spec_id)):
        return None
    catalog_path = Path(repo_root) / "spec" / "registry" / "spec_catalog.yaml"
    if not catalog_path.is_file():
        raise SpecCatalogCorruption(
            f"spec_catalog.yaml is missing at {catalog_path}. Dependency "
            "resolution requires the canonical registry."
        )
    yaml_mod = _require_yaml()
    try:
        doc = yaml_mod.safe_load(catalog_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SpecCatalogCorruption(
            f"spec_catalog.yaml could not be parsed as YAML: {exc}"
        ) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("specs"), list):
        raise SpecCatalogCorruption(
            "spec_catalog.yaml is missing the canonical `specs:` list"
        )
    repo_root_resolved = Path(repo_root).resolve()
    found: set[str] = set()
    for entry in doc["specs"]:
        if not isinstance(entry, dict):
            continue
        if entry.get("spec_kind") != spec_kind or entry.get("spec_id") != spec_id:
            continue
        ref_source = entry.get("deps_path") or entry.get("controlled_spec_path")
        if not isinstance(ref_source, str) or not ref_source.strip():
            continue
        spec_ref = str(Path(ref_source.strip()).parent).replace("\\", "/").strip("/")
        if not spec_ref:
            continue
        # Confine to the repo tree (reject traversal) before trusting the path.
        candidate = (repo_root_resolved / spec_ref).resolve()
        try:
            candidate.relative_to(repo_root_resolved)
        except ValueError:
            continue
        found.add(spec_ref)
    if len(found) != 1:
        # No match → None. Multiple distinct dirs for one (kind, spec_id) →
        # ambiguous; fail closed rather than silently choosing.
        return None
    return next(iter(found))


_SEMVER_RE = re.compile(
    r"^(?P<core>\d+(?:\.\d+)*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


def _parse_semver(
    v: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, Any], ...]]:
    """Parse a semver-style version string into a 3-tuple sort key.

    Returns `(numeric_core, no_prerelease_flag, prerelease_key)`:
    - `numeric_core`: dot-separated leading digits as ints (e.g. `1.0.0` → `(1, 0, 0)`).
    - `no_prerelease_flag`: `1` when no `-prerelease` suffix is present, `0` otherwise.
      Per SemVer §11, a version without prerelease has HIGHER precedence than the
      same numeric core with prerelease (`1.0.0 > 1.0.0-rc1`). Tuple comparison
      gives that ordering because `1 > 0`.
    - `prerelease_key`: tuple of per-token sort keys for the prerelease segment
      (numeric tokens sort before alpha; `(0, int)` < `(1, str)`).

    Build metadata (`+xxx`) is parsed but **excluded from ordering** (SemVer §10).

    Codex round 13 F2: the previous parser only accepted `[\\d.]` and coerced
    every non-numeric component to `0`, breaking `1.0.0-rc1` and friends.
    """
    s = v.strip()
    m = _SEMVER_RE.match(s)
    if not m:
        # Malformed: sort below any well-formed value.
        return ((0,), 0, ())
    core_str = m.group("core")
    nums = tuple(int(tok) for tok in core_str.split("."))
    pre = m.group("pre")
    if pre is None:
        return (nums, 1, ())
    pre_key: list[tuple[int, Any]] = []
    for tok in pre.split("."):
        if tok.isdigit():
            pre_key.append((0, int(tok)))
        else:
            pre_key.append((1, tok))
    return (nums, 0, tuple(pre_key))


# Accept any version syntax `_parse_semver` can handle: numeric core plus
# optional `-prerelease` / `+build` suffix.
_CONSTRAINT_OP_RE = re.compile(
    r"^\s*(>=|<=|==|!=|>|<)?"
    r"\s*(\d+(?:\.\d+)*(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)\s*$"
)


def _matches_version_constraint(version: str, constraint: str | None) -> bool:
    """Evaluate `version` against an AND-joined constraint expression.

    Supported operators: `>=`, `>`, `<=`, `<`, `==`, `!=`. Bare versions (no
    operator) are treated as `==`. Whitespace-separated terms are AND-combined.
    Empty / None constraint → always true.

    Operator semantics (Codex round 14 F2):
    - Ordering operators (`>`, `>=`, `<`, `<=`) use SemVer-numeric precedence:
      build metadata (`+xxx`) is ignored (SemVer §10), prerelease ranks below
      its release (SemVer §11).
    - Equality operators (`==`, `!=`) compare **normalized full strings**,
      including any `+build` suffix. This prevents `==1.0.0+cpu` from
      silently matching `1.0.0+gpu` — workspace artifact roots are keyed by
      the full version string, so equality must distinguish build variants.
    """
    if not constraint or not constraint.strip():
        return True
    v_norm = version.strip()
    v_key = _parse_semver(v_norm)
    for term in constraint.split():
        m = _CONSTRAINT_OP_RE.match(term)
        if not m:
            return False
        op = m.group(1) or "=="
        rhs = m.group(2).strip()
        if op == "==":
            if v_norm != rhs:
                return False
            continue
        if op == "!=":
            if v_norm == rhs:
                return False
            continue
        rv = _parse_semver(rhs)
        if op == ">=" and not (v_key >= rv):
            return False
        if op == ">" and not (v_key > rv):
            return False
        if op == "<=" and not (v_key <= rv):
            return False
        if op == "<" and not (v_key < rv):
            return False
    return True


def _constraint_is_exact_string_match_only(constraint: str | None) -> bool:
    """True iff every term in `constraint` is an exact `==<full-version>` clause.

    Used to gate range-constraint resolution across build-metadata variants:
    if a constraint contains any inequality (`>=`, `<`, etc.) it must not
    silently match different build variants of the same numeric release.
    """
    if not constraint or not constraint.strip():
        return False
    for term in constraint.split():
        m = _CONSTRAINT_OP_RE.match(term)
        if not m:
            return False
        op = m.group(1)
        if op != "==":
            return False
    return True


def _matching_dep_versions(
    catalog: dict[tuple[str, str], tuple[str, ...]],
    kind: str,
    spec_id: str,
    constraint: str | None,
) -> tuple[str, ...]:
    """Return every catalog version of `(kind, spec_id)` satisfying `constraint`,
    in descending semver order. Empty tuple → unresolvable dep (fail-closed).

    Codex round 13 F1: returning ALL matching versions (not just the max) lets
    `_verify_dependency_readiness` evaluate per-stage artifact presence across
    ANY of them. A newer catalog entry without artifacts no longer blocks
    parents whose older matching version is fully verified.

    Codex round 16 F2: when a range/inequality constraint matches catalog
    versions that share the same ordering key (i.e., differ only by build
    metadata, e.g. `1.0.0+cpu` vs `1.0.0+gpu`), the resolution is ambiguous
    — distinct workspace artifact roots cannot be silently selected. Return
    an empty tuple in that case so the caller fails closed. Exact-string
    constraints (`==1.0.0+cpu`) are unaffected: the operator already pins a
    specific full version.
    """
    versions = catalog.get((kind, spec_id))
    if not versions:
        return ()
    matched = tuple(v for v in versions if _matches_version_constraint(v, constraint))
    if len(matched) > 1 and not _constraint_is_exact_string_match_only(constraint):
        ordering_keys = {_parse_semver(v) for v in matched}
        if len(ordering_keys) < len(matched):
            # Two or more matched versions share the same numeric+prerelease
            # ordering key, differing only by build metadata. Range resolution
            # is ambiguous; fail closed (caller treats as unresolvable dep).
            return ()
    return matched


def _resolve_dep_version(
    catalog: dict[tuple[str, str], tuple[str, ...]],
    kind: str,
    spec_id: str,
    constraint: str | None,
) -> str | None:
    """Return the highest matching version, or None if no version matches.
    Kept for callers that need a single representative version (e.g. for
    fingerprint pinning); readiness verification uses `_matching_dep_versions`."""
    matched = _matching_dep_versions(catalog, kind, spec_id, constraint)
    return matched[0] if matched else None


_DEPS_YAML_ALLOWED_KEYS: frozenset[str] = frozenset({"components", "profiles"})

# Codex round 15 F2: identifiers from deps.yaml / spec_catalog.yaml are
# interpolated into workspace/<kind>/<safe>/ paths. ANY of `spec_kind`,
# `spec_id`, `spec_version` containing path separators or traversal sequences
# would let the verifier walk out of the dependency subtree and treat
# unrelated files as readiness evidence. Reject anything outside this strict
# safe-token grammar before path construction or fingerprint inclusion.
_SAFE_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


def _is_safe_path_token(s: Any) -> bool:
    """Strict whitelist for spec identifiers used in workspace paths.

    Rejects empty / non-str values, `..` traversal substrings, and any
    character outside `[A-Za-z0-9._+-]`. Path separators (`/`, `\\`), null
    bytes, and shell metacharacters are all rejected. Used to gate
    `spec_kind` / `spec_id` / `spec_version` values from deps.yaml and
    spec_catalog.yaml before they are interpolated into filesystem paths.
    """
    if not isinstance(s, str) or not s:
        return False
    if ".." in s:
        return False
    return bool(_SAFE_ID_TOKEN_RE.match(s))


def _parse_dep_entries(
    deps_doc: dict[str, Any]
) -> tuple[list[tuple[str, str, str | None]], bool]:
    """Parse direct dependencies from a deps.yaml document.

    Returns `(entries, well_formed)`:
    - `entries`: list of `(spec_kind, spec_id, version_constraint)` triples.
      Constraint is None when the entry omits it (bare-string or dict without
      `version_constraint`).
    - `well_formed`: False when ANY structural problem is detected. Caller
      MUST treat well_formed=False as fail-closed.

    Strict schema enforced (Codex round 12 F1): the `dependencies` block must
    be a dict containing EXACTLY the canonical keys `{"components", "profiles"}`
    and nothing else. Unknown keys (e.g. typoed `component:`) or missing
    canonical keys mark the document as malformed — previously these silently
    yielded an empty entry list which `_verify_dependency_readiness` collapsed
    to vacuous-true readiness.
    """
    entries: list[tuple[str, str, str | None]] = []
    deps = deps_doc.get("dependencies")
    if not isinstance(deps, dict):
        return entries, False
    keys = set(deps.keys())
    if keys - _DEPS_YAML_ALLOWED_KEYS:
        return entries, False
    if keys != _DEPS_YAML_ALLOWED_KEYS:
        return entries, False
    well_formed = True
    for kind_key, id_field in (("components", "component_id"), ("profiles", "profile_id")):
        items = deps.get(kind_key)
        if not isinstance(items, list):
            well_formed = False
            continue
        kind = kind_key.rstrip("s")  # "components" -> "component"
        for item in items:
            if not isinstance(item, dict):
                # Codex round 22 F1: only canonical dict form is accepted.
                # Bare string items were silently normalized by taking the
                # final `/`-segment — that lets entries like
                # `"profile/foo"`, `"x/y/z"`, or `"../dep_a"` be rewritten
                # to different IDs and pass the gate against the wrong dep.
                # Require explicit `{component_id|profile_id, version_constraint}`.
                well_formed = False
                continue
            sid = item.get(id_field)
            constraint = item.get("version_constraint")
            if not (isinstance(sid, str) and sid.strip()):
                well_formed = False
                continue
            sid_token = sid.strip()
            # Codex round 15 F2: reject path-traversal in spec_id before
            # any workspace path is composed downstream.
            if not _is_safe_path_token(sid_token):
                well_formed = False
                continue
            if constraint is not None and not isinstance(constraint, str):
                well_formed = False
                continue
            c = constraint.strip() if isinstance(constraint, str) and constraint.strip() else None
            entries.append((kind, sid_token, c))
    return entries, well_formed


# Codex round 31 F2: the freshness reader MUST share the same canonical
# grammar as the writer (`_SLUG_DATE_SEQ3_PATTERN` defined further down).
# The previous reader regex (`^.+_(\d{8})_(\d{3})$`) accepted ANY prefix —
# uppercase letters, underscores between slug parts, slashes in the path
# tail, etc. — so a planted `FAKE__20991231_999/binary_meta.json` outranked
# legitimate artifacts despite never having been issued by any writer.
# Anchoring both ends to the strict slug grammar closes that trust gap.
# This pattern is identical to `_SLUG_DATE_SEQ3_PATTERN` plus capture
# groups for date and seq; kept in lock-step by construction (see the
# `assert` at module load below).
_FRESHNESS_CANONICAL_ID_RE = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*_([0-9]{8})_([0-9]{3})$"
)


def _freshness_key_from_id(name: str) -> tuple[str, int] | None:
    """Parse a canonical runtime-issued id (`<slug>_<YYYYMMDD>_<seq3>`) and
    return an ordering key `(date_str, seq_int)`. None when the name does
    not match the strict canonical grammar.

    Codex round 35 F1: the ordering key no longer includes the slug. The
    earlier `(date, seq, name)` tuple meant that two artifacts with the
    same `<YYYYMMDD>_<seq3>` but different slugs were silently ranked by
    slug, picking the lex-larger one. Because `reserve_phase_root` does
    NOT enforce global `(date, seq)` uniqueness across orchestrations, a
    concurrent or retried run could mint a colliding id whose slug
    happens to sort later and become the chosen artifact for downstream
    dependency_readiness — driving the gate with the wrong evidence.
    Round 35 makes such a collision an explicit ambiguity (the selector
    returns None and a `freshness_id_collision` reason); callers fail
    closed with no silent slug-tiebreaker.

    Codex round 31 F2: the matcher uses the same grammar as the writer
    (`_SLUG_DATE_SEQ3_PATTERN`).
    """
    m = _FRESHNESS_CANONICAL_ID_RE.match(name)
    if m is None:
        return None
    return (m.group(1), int(m.group(2)))


def _select_max_by_id_extracted(
    candidates: list[Path], id_extractor: Callable[[Path], str | None]
) -> Path | None:
    """Filter `candidates` to those whose extracted id matches the canonical
    grammar, then return the candidate whose id has the greatest
    `(date, seq)` ordering. None if no canonical candidate exists OR if
    two or more candidates share the maximum `(date, seq)` — the latter
    is a "collision ambiguity" that must fail closed because the
    runtime cannot pick the "right" artifact when distinct IDs claim the
    same canonical position (Codex round 35 F1).
    """
    scored: list[tuple[tuple[str, int], Path]] = []
    for p in candidates:
        id_name = id_extractor(p)
        if id_name is None:
            continue
        key = _freshness_key_from_id(id_name)
        if key is None:
            continue
        scored.append((key, p))
    if not scored:
        return None
    max_key = max(kv[0] for kv in scored)
    tied = [p for k, p in scored if k == max_key]
    if len(tied) > 1:
        # Ambiguous: ≥2 distinct canonical IDs claim the same (date, seq).
        # Emit a stderr diagnostic so operators see WHICH paths collided
        # and return None so callers fail closed. Logging via stderr (not
        # phase_state_log) keeps this helper free of orchestration_id
        # context; the upstream `_compute_dep_readiness_and_fingerprint`
        # path surfaces the gate-level fail_reason.
        try:
            collisions = ", ".join(sorted(str(t) for t in tied))
            print(
                f"freshness_id_collision at (date={max_key[0]}, seq={max_key[1]}): "
                f"{collisions}",
                file=sys.stderr,
            )
        except Exception:
            pass
        return None
    return tied[0]


def _latest_meta_under(root: Path, glob_pattern: str) -> Path | None:
    """Return the latest meta file under `root` matching `glob_pattern`,
    selected by parsed canonical id `(date, seq)` from the enclosing
    directory name. Both `*/ir_meta.json` (ir_id parent) and
    `binary/*/binary_meta.json` (binary_id parent) put the runtime-issued
    id directly above the file. Non-canonical enclosing names are filtered
    out (defense against stray `zzz/` directories).
    """
    candidates = [p for p in root.glob(glob_pattern) if p.is_file()]
    return _select_max_by_id_extracted(candidates, lambda p: p.parent.name)


def _latest_aggregate_verdict_under(
    pipe_root: Path, *, bound_to_binary_id: str | None = None
) -> Path | None:
    """Return the latest aggregate_verdict.json under `pipe_root` ordered
    by parsed canonical `run_id`. Non-canonical run_ids are filtered out.

    Codex round 24: when `bound_to_binary_id` is provided, restrict
    candidates to verdicts whose sibling `trial_meta.json` records
    `source_binary_id == bound_to_binary_id`. This binds the chosen verdict
    to the specific binary `pipeline_ref` certified, preventing a passing
    verdict for an OLDER binary from satisfying execution readiness while
    a NEWER (un-validated) binary is selected for pipeline_ref. Verdicts
    missing the sibling trial_meta.json (or its `source_binary_id` field)
    are treated as unbound and excluded.
    """
    def _run_id_of(p: Path) -> str | None:
        try:
            parts = p.relative_to(pipe_root).parts
        except ValueError:
            return None
        if len(parts) >= 4 and parts[0] == "runs":
            return parts[1]
        return None
    candidates: list[Path] = []
    for p in pipe_root.rglob("aggregate_verdict.json"):
        if not p.is_file():
            continue
        if bound_to_binary_id is not None:
            trial_meta = p.parent / "trial_meta.json"
            if not trial_meta.is_file():
                continue
            try:
                trial_doc = json.loads(trial_meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(trial_doc, dict):
                continue
            src_bin = trial_doc.get("source_binary_id")
            if not (isinstance(src_bin, str) and src_bin.strip() == bound_to_binary_id):
                continue
        candidates.append(p)
    return _select_max_by_id_extracted(candidates, _run_id_of)


def _latest_pipeline_dir(safe_root: Path) -> Path | None:
    """Return the latest pipeline_id directory under
    `workspace/pipelines/<safe>/`, ordered by parsed `(date, seq)` from
    the canonical id suffix. Non-canonical pipeline directory names are
    filtered out (Codex round 23 F2).

    Codex round 11 F2 (still in effect): both pipeline_ref and
    aggregate_verdict are bound to the SAME selected pipeline dir to
    eliminate cross-run mixing.
    """
    if not safe_root.is_dir():
        return None
    candidates = [p for p in safe_root.iterdir() if p.is_dir()]
    return _select_max_by_id_extracted(candidates, lambda d: d.name)


def _verify_dep_stage(
    repo_root: Path, kind: str, spec_id: str, version: str, stage: str
) -> bool:
    """Check whether the **current** dep artifact for `(kind, id, version)`
    evidences `stage` completion.

    "Current" = most recent by mtime under the versioned workspace directory.
    Historical artifacts from earlier passing runs do NOT satisfy the gate
    (Codex round 5 fix): a stale pass cannot unblock a new launch.

    stage ∈ {"ir_ref", "pipeline_ref", "aggregate_verdict"}:

    - ir_ref: latest `workspace/ir/<safe>/*/ir_meta.json` has verification_status=pass.
    - pipeline_ref: latest `workspace/pipelines/<safe>/*/binary/*/binary_meta.json`
      has verification_status=pass.
    - aggregate_verdict: latest `workspace/pipelines/<safe>/**/aggregate_verdict.json`
      has its top-level `aggregate_verdict` field set to `pass` or `xfail`
      (per docs/GLOSSARY.md).
    """
    # Defensive: every caller validates upstream, but recheck before
    # composing a filesystem path (Codex round 15 F2 defense-in-depth).
    if not (
        _is_safe_path_token(kind)
        and _is_safe_path_token(spec_id)
        and _is_safe_path_token(version)
    ):
        return False
    safe = f"{kind}__{spec_id}__{version}"
    if stage == "ir_ref":
        root = repo_root / "workspace" / "ir" / safe
        if not root.is_dir():
            return False
        latest = _latest_meta_under(root, "*/ir_meta.json")
        if latest is None:
            return False
        try:
            doc = json.loads(latest.read_text(encoding="utf-8"))
        except Exception:
            return False
        return (
            isinstance(doc, dict)
            and str(doc.get("verification_status", "")).strip().lower() == "pass"
        )
    if stage in {"pipeline_ref", "aggregate_verdict"}:
        # Codex round 11 F2: both pipeline_ref and aggregate_verdict are
        # evaluated against the SAME selected pipeline run (latest pipeline_id
        # under workspace/pipelines/<safe>/). Selecting them independently
        # would let a newer incomplete run's passing binary be combined with
        # an older run's passing verdict — execution readiness would erroneously
        # pass even though no single run was end-to-end pass.
        safe_root = repo_root / "workspace" / "pipelines" / safe
        pipe_dir = _latest_pipeline_dir(safe_root)
        if pipe_dir is None:
            return False
        if stage == "pipeline_ref":
            latest = _latest_meta_under(pipe_dir, "binary/*/binary_meta.json")
            if latest is None:
                return False
            try:
                doc = json.loads(latest.read_text(encoding="utf-8"))
            except Exception:
                return False
            return (
                isinstance(doc, dict)
                and str(doc.get("verification_status", "")).strip().lower() == "pass"
            )
        # stage == "aggregate_verdict"
        # Codex round 24: bind the verdict to the SAME binary that
        # pipeline_ref would select; reject verdicts produced for a
        # different (older) binary even when newer binaries lack verdicts.
        latest_binary = _latest_meta_under(pipe_dir, "binary/*/binary_meta.json")
        if latest_binary is None:
            return False
        chosen_binary_id = latest_binary.parent.name
        latest = _latest_aggregate_verdict_under(
            pipe_dir, bound_to_binary_id=chosen_binary_id,
        )
        if latest is None:
            return False
        try:
            doc = json.loads(latest.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(doc, dict):
            return False
        verdict = str(doc.get("aggregate_verdict", "")).strip().lower()
        # docs/GLOSSARY.md: "a state in which the latest aggregate_verdict is `pass` or `xfail`"
        return verdict in {"pass", "xfail"}
    raise ValueError(f"unknown readiness stage: {stage!r}")


def _stage_status_from_bytes(stage: str, raw: bytes) -> bool:
    """Decide whether `raw` (one artifact file's bytes) satisfies `stage`."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    if stage in {"ir_ref", "pipeline_ref"}:
        return (
            isinstance(doc, dict)
            and str(doc.get("verification_status", "")).strip().lower() == "pass"
        )
    if stage == "aggregate_verdict":
        if not isinstance(doc, dict):
            return False
        verdict = str(doc.get("aggregate_verdict", "")).strip().lower()
        return verdict in {"pass", "xfail"}
    raise ValueError(f"unknown readiness stage: {stage!r}")


def _read_candidate_artifact_bytes(
    repo_root: Path, kind: str, spec_id: str, version: str
) -> dict[str, bytes]:
    """Read each candidate stage's latest artifact bytes ONCE for one
    `(kind, spec_id, version)` triple. Returns a dict whose presence of a
    `stage` key indicates the file existed and was successfully read.
    """
    out: dict[str, bytes] = {}
    if not (
        _is_safe_path_token(kind)
        and _is_safe_path_token(spec_id)
        and _is_safe_path_token(version)
    ):
        return out
    safe = f"{kind}__{spec_id}__{version}"
    ir_root = repo_root / "workspace" / "ir" / safe
    if ir_root.is_dir():
        latest = _latest_meta_under(ir_root, "*/ir_meta.json")
        if latest is not None:
            try:
                out["ir_ref"] = latest.read_bytes()
            except OSError:
                pass
    safe_root = repo_root / "workspace" / "pipelines" / safe
    pipe_dir = _latest_pipeline_dir(safe_root)
    if pipe_dir is not None:
        latest_binary = _latest_meta_under(pipe_dir, "binary/*/binary_meta.json")
        chosen_binary_id: str | None = None
        if latest_binary is not None:
            chosen_binary_id = latest_binary.parent.name
            try:
                out["pipeline_ref"] = latest_binary.read_bytes()
            except OSError:
                pass
        # Codex round 24: only consider verdicts bound to the chosen binary
        # (via trial_meta.source_binary_id). A passing verdict for an older
        # binary cannot satisfy aggregate_verdict_verified while a newer
        # un-validated binary is selected for pipeline_ref.
        if chosen_binary_id is not None:
            latest_verdict = _latest_aggregate_verdict_under(
                pipe_dir, bound_to_binary_id=chosen_binary_id,
            )
            if latest_verdict is not None:
                try:
                    out["aggregate_verdict"] = latest_verdict.read_bytes()
                except OSError:
                    pass
    return out


def _level_from_stage_bytes(stage_bytes: dict[str, bytes]) -> int:
    """Cumulative readiness level achieved by one `(kind, sid, version)`:

      0 = no ir / ir fail
      1 = ir pass
      2 = ir + pipeline pass
      3 = ir + pipeline + verdict pass

    Used by `_certify_and_collect_dep_artifacts` to choose ONE certified
    version per dep (highest matched version achieving the maximum level).
    """
    if not _stage_status_from_bytes("ir_ref", stage_bytes.get("ir_ref", b"")):
        return 0
    if not _stage_status_from_bytes("pipeline_ref", stage_bytes.get("pipeline_ref", b"")):
        return 1
    if not _stage_status_from_bytes("aggregate_verdict", stage_bytes.get("aggregate_verdict", b"")):
        return 2
    return 3


def _certify_and_collect_dep_artifacts(
    repo_root: Path, spec_ref: Any
) -> dict[str, Any]:
    """Single-pass: read every candidate dep artifact ONCE, select the
    certified version per dep, and return both the certification decision
    and the bytes to feed into the fingerprint (Codex round 17 F1+F2).

    Returns a dict with:
      - `deps_doc_valid` (bool): True iff deps.yaml parsed as a dict.
      - `entries_well_formed` (bool): True iff the deps.yaml schema is strict.
      - `has_entries` (bool): True iff deps.yaml lists any direct deps.
      - `certified_entries`: list of `(kind, spec_id, certified_version, level)`
        in deps.yaml order. `certified_version` is the HIGHEST matching
        catalog version that achieved the MAX level (any of {0,1,2,3}).
        When no version was matched / no artifacts existed, level is 0 and
        `certified_version` is None.
      - `artifact_bytes_in_order`: list of `(stage, kind, sid, version, bytes)`
        for ONLY the certified version of each dep — the canonical input to
        the fingerprint hash. Walking only certified deps' artifacts means
        unrelated historical-version artifact churn does NOT invalidate
        readiness at the launch-time fingerprint check.
    """
    snap: dict[str, Any] = {
        "deps_doc_valid": False,
        "entries_well_formed": False,
        "has_entries": False,
        "certified_entries": [],
        "artifact_bytes_in_order": [],
    }
    deps_doc = _read_deps_yaml(repo_root, spec_ref)
    if not isinstance(deps_doc, dict):
        return snap
    snap["deps_doc_valid"] = True
    entries, well_formed = _parse_dep_entries(deps_doc)
    snap["entries_well_formed"] = well_formed
    if not well_formed:
        return snap
    if not entries:
        snap["has_entries"] = False
        return snap
    snap["has_entries"] = True
    catalog = _load_spec_catalog(str(repo_root.resolve()))
    for kind, spec_id, constraint in entries:
        matched = _matching_dep_versions(catalog, kind, spec_id, constraint)
        if not matched:
            snap["certified_entries"].append((kind, spec_id, None, 0))
            continue
        best_v: str | None = None
        best_level = -1
        best_bytes: dict[str, bytes] = {}
        # matched is descending; iterate so ties prefer the higher version.
        for v in matched:
            stage_bytes = _read_candidate_artifact_bytes(repo_root, kind, spec_id, v)
            level = _level_from_stage_bytes(stage_bytes)
            if level > best_level:
                best_level = level
                best_v = v
                best_bytes = stage_bytes
        if best_v is None:
            snap["certified_entries"].append((kind, spec_id, None, 0))
            continue
        snap["certified_entries"].append((kind, spec_id, best_v, best_level))
        for stage in _DEPENDENCY_READINESS_STAGES:
            if stage in best_bytes:
                snap["artifact_bytes_in_order"].append(
                    (stage, kind, spec_id, best_v, best_bytes[stage])
                )
    return snap


def _walk_dep_artifacts(
    repo_root: Path, spec_ref: Any
) -> Iterator[tuple[str, str, str, str, bytes]]:
    """Yield artifact bytes for ONLY the certified version per dep.

    Backed by `_certify_and_collect_dep_artifacts` so the fingerprint
    hash narrows to artifacts that actually contributed to readiness
    (Codex round 17 F2). Order is canonical: deps.yaml entry order,
    one version per entry, stages in (ir_ref, pipeline_ref, aggregate_verdict).
    """
    snap = _certify_and_collect_dep_artifacts(repo_root, spec_ref)
    for chunk in snap["artifact_bytes_in_order"]:
        yield chunk


def _dep_fingerprint_header_bytes(repo_root: Path, spec_ref: Any) -> bytes:
    """Header bytes prefixed to the dep-set fingerprint hash.

    Codex round 19 F1: the catalog contribution is NOT the entire
    `spec_catalog.yaml` bytes — that would let unrelated catalog churn
    (publishing a spec your orchestration does not depend on, editing
    metadata elsewhere) invalidate every in-flight orchestration. Instead,
    hash a deterministic representation of ONLY the catalog versions for
    `(spec_kind, spec_id)` pairs that appear in this orchestration's
    `deps.yaml`. Edits outside that subset cannot change resolution and
    therefore cannot legitimately invalidate readiness.
    """
    spec_token = spec_ref.strip() if isinstance(spec_ref, str) and spec_ref.strip() else ""
    deps_bytes: bytes = b""
    if spec_token:
        deps_path = (repo_root / spec_token / "deps.yaml").resolve()
        try:
            deps_path.relative_to(repo_root.resolve())
            if deps_path.is_file():
                deps_bytes = deps_path.read_bytes()
        except (ValueError, OSError):
            deps_bytes = b""
    catalog_subset_bytes = _relevant_catalog_subset_bytes(repo_root, spec_ref)
    return spec_token.encode("utf-8") + b"\x00" + deps_bytes + b"\x00" + catalog_subset_bytes


def _no_deps_leaf_fingerprint(repo_root: Path, spec_ref: Any) -> str:
    """Byte-only fingerprint that matches the full dep-set fingerprint
    iff the orchestration is a leaf with no direct dependencies.

    Codex round 30 F1: the round-29 PyYAML-missing leaf fallback trusted
    `certified_deps == []` from persisted meta without proving it still
    described the *current* deps.yaml — a stale meta or direct edit could
    fail-open the gate. This helper recomputes the fingerprint header
    bytes WITHOUT parsing YAML (catalog subset is empty by definition
    when there are no deps) and SHA-256s them. For a true leaf, this
    equals the original `_dependency_set_fingerprint` value because:
      - `_relevant_catalog_subset_bytes` returns `b""` when deps.yaml has
        no entries, so catalog_subset is empty.
      - `artifact_bytes_in_order` is empty for a leaf, so the artifact
        block is empty.
    The hash therefore reduces to `SHA256(spec_token \\x00 deps_bytes \\x00 b"")`.
    If the persisted fingerprint matches this byte-only recomputation,
    deps.yaml hasn't been mutated since mark concluded "no deps" — so
    trusting `certified_deps == []` is safe even without PyYAML.

    Edits to deps.yaml (adding/changing deps) or to spec_ref change the
    bytes → fingerprint mismatch → reject. Forging persisted meta cannot
    succeed without also reconstructing matching deps.yaml bytes.
    """
    spec_token = spec_ref.strip() if isinstance(spec_ref, str) and spec_ref.strip() else ""
    deps_bytes: bytes = b""
    if spec_token:
        deps_path = (repo_root / spec_token / "deps.yaml").resolve()
        try:
            deps_path.relative_to(repo_root.resolve())
            if deps_path.is_file():
                deps_bytes = deps_path.read_bytes()
        except (ValueError, OSError):
            deps_bytes = b""
    h = hashlib.sha256()
    h.update(spec_token.encode("utf-8"))
    h.update(b"\x00")
    h.update(deps_bytes)
    h.update(b"\x00")
    # catalog subset = empty for a no-deps leaf (round 19 F1 semantics).
    return h.hexdigest()


def _relevant_catalog_subset_bytes(repo_root: Path, spec_ref: Any) -> bytes:
    """Deterministic serialization of the catalog subset relevant to
    `spec_ref`'s deps: for each `(spec_kind, spec_id)` appearing in
    deps.yaml, the sorted set of catalog versions for that pair. Returns
    empty bytes when deps.yaml is missing, malformed, or has no entries.

    Catalog edits to OTHER specs do not contribute to the hash, so they
    cannot invalidate this orchestration's readiness.
    """
    deps_doc = _read_deps_yaml(repo_root, spec_ref)
    if not isinstance(deps_doc, dict):
        return b""
    entries, well_formed = _parse_dep_entries(deps_doc)
    if not well_formed or not entries:
        return b""
    catalog = _load_spec_catalog(str(repo_root.resolve()))
    seen: set[tuple[str, str]] = set()
    items: list[tuple[str, str, list[str]]] = []
    for kind, spec_id, _constraint in entries:
        key = (kind, spec_id)
        if key in seen:
            continue
        seen.add(key)
        versions = list(catalog.get(key, ()))
        # Catalog tuples are already sorted descending; re-sort lexicographically
        # so the serialized representation is stable across runs.
        items.append((kind, spec_id, sorted(versions)))
    items.sort()
    return json.dumps(items, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compute_dep_readiness_and_fingerprint(
    repo_root: Path, spec_ref: Any
) -> tuple[dict[str, bool] | None, str, list[dict[str, str]], str | None]:
    """Single-pass: derive readiness booleans, fingerprint, the certified
    `(spec_kind, spec_id, spec_version)` per dep, and any verification-error
    reason from the SAME artifact byte snapshots (Codex round 16 F1 + round
    17 F1/F2 + round 21 F2).

    Per-dep certified version is the HIGHEST matching catalog version that
    achieved the max readiness level (ir → ir+pipeline → ir+pipeline+verdict).
    The fingerprint hashes ONLY the certified version's artifacts plus the
    header (spec_ref + deps.yaml + spec_catalog.yaml subset).

    Returns `(verified_dict, fingerprint_hex, certified_deps, fail_reason)`:
      - `verified_dict`: None when verification cannot run. Else a dict of
        stage booleans.
      - `fingerprint_hex`: stable hash of header + certified artifacts.
      - `certified_deps`: list of {"spec_kind", "spec_id", "spec_version"} in
        deps.yaml order.
      - `fail_reason`: None on success; otherwise one of
        `"deps_yaml_missing_or_unparseable"` (deps.yaml file absent or YAML
        parse failed) or `"deps_yaml_malformed_schema"` (deps.yaml parsed
        but the dependency-block schema is invalid — unknown keys, missing
        canonical lists, list items in wrong shape, etc.). Callers that
        write persisted state (`mark_dependency_readiness`) treat both as
        hard verification failures and fail closed with the specific reason
        so the audit trail records WHICH defect occurred.
    """
    # Codex round 33 F2: catalog corruption is a distinct hard failure,
    # not a normal "no matching dep" verification result. Convert the
    # exception into a specific fail_reason so observability tooling, the
    # CLI exit, and persisted state all record WHICH defect occurred.
    try:
        snap = _certify_and_collect_dep_artifacts(repo_root, spec_ref)
        header = _dep_fingerprint_header_bytes(repo_root, spec_ref)
    except SpecCatalogCorruption:
        # Without a valid catalog there is no fingerprint we can stably
        # compute. Return `verified=None` with the distinct
        # `spec_catalog_corrupt` fail_reason so the same fail-closed
        # persistence + raise path used for other hard verification
        # failures (`deps_yaml_*`) handles this case loudly.
        return (None, "", [], "spec_catalog_corrupt")
    h = hashlib.sha256()
    h.update(header)
    for stage, kind, sid, version, raw in snap["artifact_bytes_in_order"]:
        # Codex round 19 F2: prefix each artifact's bytes with its identity
        # (kind/spec_id/version/stage). Without this, a higher matching
        # version whose ir_meta.json bytes happen to equal the older
        # certified version's bytes would recompute as the new certified
        # version yet produce an identical fingerprint — gate would trust
        # the persisted certified_deps even though they now point to a
        # different version.
        h.update(b"\x00")
        h.update(f"{kind}__{sid}__{version}__{stage}".encode("utf-8"))
        h.update(b"\x00")
        h.update(raw)
    fingerprint = h.hexdigest()
    if not snap["deps_doc_valid"]:
        return None, fingerprint, [], "deps_yaml_missing_or_unparseable"
    if not snap["entries_well_formed"]:
        # Codex round 21 F2: malformed deps.yaml schema is a HARD verification
        # failure, not a normal negative result. Distinct reason so observability
        # tooling (and the CLI exit) can differentiate spec defects from
        # ordinary "no artifacts" cases.
        return None, fingerprint, [], "deps_yaml_malformed_schema"
    if not snap["has_entries"]:
        return (
            {f"{s}_verified": True for s in _DEPENDENCY_READINESS_STAGES},
            fingerprint,
            [],
            None,
        )
    results: dict[str, bool] = {f"{s}_verified": True for s in _DEPENDENCY_READINESS_STAGES}
    certified_deps: list[dict[str, str]] = []
    for kind, spec_id, cert_v, level in snap["certified_entries"]:
        if cert_v is None:
            for s in _DEPENDENCY_READINESS_STAGES:
                results[f"{s}_verified"] = False
            certified_deps.append({"spec_kind": kind, "spec_id": spec_id, "spec_version": ""})
            continue
        if level < 1:
            results["ir_ref_verified"] = False
        if level < 2:
            results["pipeline_ref_verified"] = False
        if level < 3:
            results["aggregate_verdict_verified"] = False
        certified_deps.append({"spec_kind": kind, "spec_id": spec_id, "spec_version": cert_v})
    return results, fingerprint, certified_deps, None


def _verify_dependency_readiness(
    repo_root: Path, spec_ref: Any
) -> dict[str, bool] | None:
    """Verify each direct-dep's artifacts and aggregate per-stage detail flags.

    Returns:
      - None if deps.yaml is missing/unparseable (caller decides fail-closed).
      - dict {ir_ref_verified, pipeline_ref_verified, aggregate_verdict_verified}
        otherwise. A stage is True iff EVERY listed direct dep passes its
        per-stage artifact check. Empty deps → vacuous true for every stage.
    """
    deps_doc = _read_deps_yaml(repo_root, spec_ref)
    if not isinstance(deps_doc, dict):
        return None
    entries, well_formed = _parse_dep_entries(deps_doc)
    if not well_formed:
        # Codex round 7 F1: malformed deps.yaml must NOT degrade to vacuous-true.
        return {f"{stage}_verified": False for stage in _DEPENDENCY_READINESS_STAGES}
    if not entries:
        return {f"{stage}_verified": True for stage in _DEPENDENCY_READINESS_STAGES}
    catalog = _load_spec_catalog(str(repo_root.resolve()))
    results: dict[str, bool] = {f"{s}_verified": True for s in _DEPENDENCY_READINESS_STAGES}
    for kind, spec_id, constraint in entries:
        # Codex round 13 F1 + round 14 F1 (same-version coherence):
        # readiness for each stage requires SOME single catalog version V to
        # satisfy a cumulative chain. Specifically, the per-dep contribution is:
        #
        #   ir_ref          ← ∃ V where ir_ref passes for V
        #   pipeline_ref    ← ∃ V where ir_ref AND pipeline_ref pass for the SAME V
        #   aggregate_verdict ← ∃ V where ir_ref AND pipeline_ref AND aggregate_verdict pass for the SAME V
        #
        # This blocks the cross-version mix where ir_ref is satisfied by one
        # version and pipeline_ref by another — execution_readiness would
        # otherwise certify a chain that never existed as a coherent dep run.
        # Constraint membership remains required: only catalog versions that
        # match the dependency's version_constraint contribute.
        matched_versions = _matching_dep_versions(catalog, kind, spec_id, constraint)
        if not matched_versions:
            for s in _DEPENDENCY_READINESS_STAGES:
                results[f"{s}_verified"] = False
            continue
        any_ir = False
        any_ir_pipe = False
        any_ir_pipe_verdict = False
        for v in matched_versions:
            if not _verify_dep_stage(repo_root, kind, spec_id, v, "ir_ref"):
                continue
            any_ir = True
            if not _verify_dep_stage(repo_root, kind, spec_id, v, "pipeline_ref"):
                continue
            any_ir_pipe = True
            if _verify_dep_stage(repo_root, kind, spec_id, v, "aggregate_verdict"):
                any_ir_pipe_verdict = True
                break  # full chain found; further versions can't downgrade.
        if not any_ir:
            results["ir_ref_verified"] = False
        if not any_ir_pipe:
            results["pipeline_ref_verified"] = False
        if not any_ir_pipe_verdict:
            results["aggregate_verdict_verified"] = False
    return results


def _dependency_set_fingerprint(repo_root: Path, spec_ref: Any) -> str:
    """Fingerprint identifying the dependency set the readiness flags apply to.

    Combines normalized `spec_ref`, the bytes of `<spec_ref>/deps.yaml`, and
    the bytes of `spec/registry/spec_catalog.yaml`. When ANY of those inputs
    differ from the value stored on `dependency_readiness`, the persisted
    flags refer to a stale dependency set and MUST be reset.

    Codex round 6 F1: includes spec_ref + deps.yaml so `spec_ref` repointing
    or deps.yaml edits invalidate stale `true` flags.

    Codex round 7 F2: also includes spec_catalog.yaml so catalog drift
    (new matching version added, previous version removed, constraint
    ambiguity introduced) invalidates persisted readiness. Without this,
    `_resolve_dep_version` outcomes can drift while readiness booleans
    silently stay true.
    """
    # Codex round 16 F1: route through the shared `_walk_dep_artifacts`
    # walker so the fingerprint observed at gate time uses the same canonical
    # ordering and read pattern as `_compute_dep_readiness_and_fingerprint`
    # at mark time. The same on-disk state ALWAYS produces the same hash.
    h = hashlib.sha256()
    h.update(_dep_fingerprint_header_bytes(repo_root, spec_ref))
    for stage, kind, sid, version, raw in _walk_dep_artifacts(repo_root, spec_ref):
        # Round 19 F2: identity prefix; see _compute_dep_readiness_and_fingerprint.
        h.update(b"\x00")
        h.update(f"{kind}__{sid}__{version}__{stage}".encode("utf-8"))
        h.update(b"\x00")
        h.update(raw)
    return h.hexdigest()


def _compute_initial_dependency_readiness(
    repo_root: Path, spec_ref: Any
) -> dict[str, Any]:
    """Compute the canonical `dependency_readiness` payload for a fresh orchestration.

    Replaces the previous all-true default which fail-opens `workflow-launch-check`
    when no real readiness builder is wired up. Semantics:

    - If `spec_ref` is missing or the target `deps.yaml` cannot be parsed, return
      a fail-closed payload (all flags false) so the gate refuses to launch until
      a real verifier writes verified state.
    - If `deps.yaml` lists no `components` and no `profiles`, dependency readiness
      is vacuously satisfied — return all flags true (matches the audit's empty-
      dependency case where launches should proceed).
    - Otherwise, return fail-closed. A future readiness builder must explicitly
      flip these flags after verifying each direct dependency's `ir_meta.json` /
      `pipeline_meta.json` / `aggregate_verdict`. The fail-closed default ensures
      that gate behaviour does not silently trust unverified state.
    """
    # Codex round 34 F1: detect a canonical empty-deps leaf via a strict
    # BYTE-LEVEL recognizer BEFORE touching PyYAML. Round 33 made every
    # init call `_dependency_set_fingerprint` up front, so a controller
    # PyYAML outage caused `write_preflight` to drop into its degraded
    # branch and persist an all-false readiness record for FRESH leaf
    # orchestrations — a brand-new no-deps workflow could not launch.
    # The byte-level recognizer is intentionally conservative: false
    # negatives just defer to PyYAML parsing (or fail-closed under
    # outage); false positives would be fail-open, so the grammar is
    # restricted to the canonical pattern.
    spec_token = spec_ref.strip() if isinstance(spec_ref, str) and spec_ref.strip() else ""
    deps_bytes_for_leaf: bytes | None = None
    if spec_token:
        deps_path = (repo_root / spec_token / "deps.yaml").resolve()
        try:
            deps_path.relative_to(repo_root.resolve())
            if deps_path.is_file():
                deps_bytes_for_leaf = deps_path.read_bytes()
        except (ValueError, OSError):
            deps_bytes_for_leaf = None
    if deps_bytes_for_leaf is not None and _deps_yaml_bytes_are_canonical_empty(
        deps_bytes_for_leaf
    ):
        # Byte-confirmed leaf: full fingerprint == byte-only fingerprint
        # (catalog subset is empty by construction; no artifact bytes).
        return {
            "direct_dependency_compile_readiness": True,
            "direct_dependency_execution_readiness": True,
            "detail": {
                "ir_ref_verified": True,
                "pipeline_ref_verified": True,
                "aggregate_verdict_verified": True,
            },
            "dep_set_fingerprint": _no_deps_leaf_fingerprint(repo_root, spec_ref),
            "certified_deps": [],
        }
    # Codex round 33 F1: PyYAML errors past this point propagate so
    # `write_preflight` can decide between "preserve existing verified
    # record" and "write fail-closed". Non-leaf specs cannot be verified
    # without YAML, so a PyYAML outage MUST fail-closed for those.
    fingerprint = _dependency_set_fingerprint(repo_root, spec_ref)
    trivial: dict[str, Any] = {
        "direct_dependency_compile_readiness": True,
        "direct_dependency_execution_readiness": True,
        "detail": {
            "ir_ref_verified": True,
            "pipeline_ref_verified": True,
            "aggregate_verdict_verified": True,
        },
        "dep_set_fingerprint": fingerprint,
        # Codex round 31 F1: persist `certified_deps: []` in the initial
        # trivial-leaf payload so the round-30 PyYAML-missing leaf shortcut
        # is satisfied without requiring a subsequent
        # `mark-dependency-readiness` run. Previously this field was only
        # written by `mark_dependency_readiness`, so a fresh
        # `init → preflight → workflow-launch-check` for a no-deps spec
        # failed closed under PyYAML outage even though it is a legitimate
        # vacuous-leaf. The empty list IS the byte-level proof of no deps,
        # cryptographically bound to the dep_set_fingerprint that hashes
        # the deps.yaml bytes.
        "certified_deps": [],
    }
    fail_closed: dict[str, Any] = {
        "direct_dependency_compile_readiness": False,
        "direct_dependency_execution_readiness": False,
        "detail": {
            "ir_ref_verified": False,
            "pipeline_ref_verified": False,
            "aggregate_verdict_verified": False,
        },
        "dep_set_fingerprint": fingerprint,
        # Fail-closed payloads explicitly omit `certified_deps`: there is
        # no proof of any verification state to record. The PyYAML-missing
        # leaf shortcut requires `certified_deps == []`, so its absence
        # alone reliably fails the gate (consistent with the rest of the
        # fail-closed semantics).
    }
    # Codex round 33 F1: PyYAML errors propagate; write_preflight handles
    # the "preserve existing or write degraded fail-closed" decision based
    # on whether prior verified state exists. Letting `_read_deps_yaml` /
    # `_require_yaml` raise here is the correct behavior so the caller can
    # distinguish "could not evaluate" from "evaluated to fail-closed".
    deps_doc = _read_deps_yaml(repo_root, spec_ref)
    if not isinstance(deps_doc, dict):
        return fail_closed
    # Codex round 12 F1: route trivial-leaf detection through the strict
    # schema validator instead of a loose `len() == 0` check. Unknown
    # dependency keys (e.g. typoed `component:`) or missing canonical keys
    # now fail-closed rather than silently producing vacuous-true readiness.
    entries, well_formed = _parse_dep_entries(deps_doc)
    if not well_formed:
        return fail_closed
    if not entries:
        return trivial
    return fail_closed
# Termination reasons for which Judge's pre_phase_complete verification does not require semantic_review (treated as incomplete).
JUDGE_SEMANTIC_REVIEW_SKIPPED_STATUSES = frozenset({"timeout", "cancel"})
SUPPORTED_BACKENDS = {"codex", "claude"}
PREFLIGHT_TTL_DEFAULT_SECONDS: int = 1800
VALID_REPAIR_STRATEGIES = frozenset({"none", "reuse", "restart"})
VALID_ISSUE_SEVERITIES = frozenset({"none", "minor", "major", "critical"})

# Must match tools/validate_workspace_root.py (canonical pipeline/plan id directory naming).
_NODE_KEY_SAFE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$"
)
# Strict per-component validators for node_key in the canonical
# `<spec_kind>/<spec_id>@<spec_version>` form. Used to reject malicious or
# malformed values before they flow into capability write_roots / path prefixes
# (a node_key like `../etc/passwd@1.0.0` would otherwise produce a write_root
# of `releases/../etc/passwd/`, escaping the intended release subtree).
_NODE_KEY_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NODE_KEY_ID_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
_NODE_KEY_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._-]*$")
_SLUG_DATE_SEQ3_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$")

# `run_id` is the one id in the family that is NOT a `<slug>_<date>_<seq3>`
# value: it carries a fixed literal `run_` prefix (`run_<YYYYMMDD>_<seq3>`,
# e.g. `run_20260605_001`). A slug-shaped run_id like `run-rsn-p0_20260605_001`
# (mistakenly generalized from the ir_id/pipeline_id naming rule) MATCHES
# `_SLUG_DATE_SEQ3_PATTERN` (slug=`run-rsn-p0`) and therefore used to slip
# through the `_matches_phase_contract` write-acceptance boundary, yet the
# Validate `post_execute` run discovery in `tools/validate_pipeline_semantics.py`
# only recognizes the canonical `runs/run_<date>_<seq>/...` layout and reports
# "no execution artifacts found". The validate run-dir branches in
# `_matches_phase_contract` enforce THIS stricter shape so the format drift
# fails fast at launch (record-launch raises "outside phase contract") instead
# of a silent downstream no-match. IMPORTANT: keep this in lock-step with the
# canonical run dir grammar that `post_execute` discovery relies on.
_RUN_ID_RE = re.compile(r"^run_[0-9]{8}_[0-9]{3}$")
# Canonical source_id format for the Generate step: `src_<YYYYMMDD>_<seq3>`.
# This is the ONLY accepted prefix — unlike ir_id / pipeline_id which use an
# arbitrary slug, source_id always starts with the literal `src_` prefix.
# Validated at record-launch time so a malformed source_id (e.g. inheriting
# the ir_id slug format) fails fast before the generate agent wastes a full
# substep run that would only be caught by generate.verify.
_SOURCE_ID_RE = re.compile(r"^src_[0-9]{8}_[0-9]{3}$")

# Codex round 31 F2 → round 36: keep reader (`_FRESHNESS_CANONICAL_ID_RE`)
# and writer (`_SLUG_DATE_SEQ3_PATTERN`) grammars in lock-step. The capture
# groups in the reader (`([0-9]{8})` / `([0-9]{3})`) are the only intentional
# difference; their pattern bodies must be identical so a future edit to one
# without the other surfaces at module load instead of silently re-opening
# the trust gap fixed in round 31. Round 36 promotes this from `assert` (which
# `python -O` elides) to an unconditional RuntimeError so the load-bearing
# invariant survives optimized interpreter modes. Round 36 also tightens the
# reader's digit classes to `[0-9]` (same as the writer) for genuine
# equivalence — `\d` would match Unicode digits.
if _SLUG_DATE_SEQ3_PATTERN.pattern != (
    _FRESHNESS_CANONICAL_ID_RE.pattern
    .replace(r"([0-9]{8})", r"[0-9]{8}")
    .replace(r"([0-9]{3})", r"[0-9]{3}")
):
    raise RuntimeError(
        "freshness reader and writer regexes have diverged — round 31 F2 "
        "invariant violated. Reader: "
        f"{_FRESHNESS_CANONICAL_ID_RE.pattern!r}; writer: "
        f"{_SLUG_DATE_SEQ3_PATTERN.pattern!r}."
    )


def _parse_node_key_strict(node_key: Any) -> tuple[str, str, str]:
    """Validate `node_key` and return `(spec_kind, spec_id_dotted, spec_version)`.

    Enforces the canonical form `<spec_kind>/<spec_id>@<spec_version>` so that
    components downstream (write_roots, release path prefixes, node_key_safe
    derivation) cannot be tricked into path traversal via segments like `..`,
    embedded slashes, null bytes, or whitespace.

    Raises `ValueError` on any deviation from the canonical form.
    """
    if not isinstance(node_key, str):
        raise ValueError("node_key must be a string")
    token = node_key.strip()
    if not token:
        raise ValueError("node_key must be non-empty")
    if "\x00" in token:
        raise ValueError(f"node_key contains null byte: {node_key!r}")
    if "/" not in token or "@" not in token:
        raise ValueError(
            f"node_key must match '<spec_kind>/<spec_id>@<spec_version>': {node_key!r}"
        )
    spec_kind, tail = token.split("/", 1)
    spec_id_dotted, spec_version = tail.rsplit("@", 1)
    spec_kind = spec_kind.strip()
    spec_id_dotted = spec_id_dotted.strip()
    spec_version = spec_version.strip()
    if not spec_kind or not spec_id_dotted or not spec_version:
        raise ValueError(
            f"node_key must match '<spec_kind>/<spec_id>@<spec_version>': {node_key!r}"
        )
    if not _NODE_KEY_KIND_RE.match(spec_kind):
        raise ValueError(f"node_key spec_kind is invalid: {node_key!r}")
    if "/" in spec_id_dotted or "\\" in spec_id_dotted:
        raise ValueError(f"node_key spec_id must not contain path separators: {node_key!r}")
    id_segments = spec_id_dotted.split(".")
    if any(not seg for seg in id_segments):
        # rejects '..', leading '.', trailing '.'
        raise ValueError(f"node_key spec_id has empty dot-segment: {node_key!r}")
    for seg in id_segments:
        if not _NODE_KEY_ID_SEGMENT_RE.match(seg):
            raise ValueError(f"node_key spec_id segment is invalid: {node_key!r}")
    if not _NODE_KEY_VERSION_RE.match(spec_version):
        raise ValueError(f"node_key spec_version is invalid: {node_key!r}")
    return spec_kind, spec_id_dotted, spec_version

# Safe agent_run_id characters: alphanumerics, hyphens, underscores.
# Rejects path separators (/, \), dots (..), null bytes, and other traversal vectors.
_AGENT_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
DEFAULT_BACKEND_COMMANDS = {
    "codex": "codex",
    "claude": "claude",
}

# Child agent `skill_must_read_refs`: split workflow spec (see docs/workflow/).
WORKFLOW_CORE_REF = "docs/workflow/WORKFLOW_CORE.md"
WORKFLOW_PHASE_DOC_BY_STEP: dict[str, str] = {
    "compile": "docs/workflow/phases/phase_01_compile.md",
    "generate": "docs/workflow/phases/phase_02_generate.md",
    "build": "docs/workflow/phases/phase_03_build.md",
    "validate": "docs/workflow/phases/phase_04_validate.md",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, body: str) -> None:
    """Adv-27: write `body` to `path` atomically via temp-file + os.replace.

    Plain `path.write_text()` truncates and rewrites in place, so a concurrent
    reader (e.g. `validate_workspace_root` polling `orchestration_meta.json`)
    can transiently observe an empty or truncated file mid-write and treat the
    orchestration as not-active — falsely flagging sanctioned tmp scripts.
    Writing to a sibling temp file then renaming guarantees readers see
    either the previous full content or the new full content, never partial.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use NamedTemporaryFile in the same directory so os.replace is atomic
    # (cross-device renames are not). delete=False because we hand the file
    # off to os.replace explicitly.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    # L-NEW-3: cleanup MUST run on BaseException too (KeyboardInterrupt /
    # SystemExit), otherwise SIGINT during a long write campaign accumulates
    # `.tmp` litter under workspace/orchestrations/. try/finally with a
    # `replaced` flag distinguishes successful rename (no cleanup) from any
    # failure path including signal-driven exits.
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(str(tmp_path), str(path))
        replaced = True
    finally:
        if not replaced:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, text: str) -> None:
    body = text if text.endswith("\n") else f"{text}\n"
    _atomic_write_text(path, body)


def _orchestration_root(repo_root: Path, orchestration_id: str) -> Path:
    return repo_root / "workspace" / "orchestrations" / orchestration_id


def _active_child_agent_run_id_path(repo_root: Path, orchestration_id: str) -> Path:
    """The active child `agent_run_id` management file specific to the Claude backend."""
    return _orchestration_root(repo_root, orchestration_id) / "active_child_agent_run_id.txt"


def _active_children_dir(repo_root: Path, orchestration_id: str) -> Path:
    """Backend-neutral per-arid active-child marker directory (Adv-16).

    Each launched child writes `active_children/<agent_run_id>.txt` here.
    deactivate_child / record_agent_run terminal / record_timeout's success
    path remove the marker. record_timeout REFUSES to proceed while the
    marker exists — this provides Codex with the same liveness
    handshake protection that Claude got from active_child_agent_run_id.txt.
    """
    return _orchestration_root(repo_root, orchestration_id) / "active_children"


def _active_child_marker_path(repo_root: Path, orchestration_id: str, agent_run_id: str) -> Path:
    return _active_children_dir(repo_root, orchestration_id) / f"{agent_run_id}.txt"


def _clear_stale_active_child_markers(
    repo_root: Path, orchestration_id: str
) -> list[str]:
    """Clear a stale active_child window left by a host that died mid-launch.

    When the host process exits while a child launch is in flight (interrupted /
    hung child, or a token-limit kill), no live orchestration agent runs
    deactivate-child / record-timeout, so `active_child_agent_run_id.txt` and the
    `active_children/<arid>.txt` markers persist. The Claude-backend sequential
    check in `record_launch` then rejects the next launch, permanently wedging the
    documented recovery (`launch_incomplete_active_child` / `llm_launch_interrupted`).

    This is only safe to call when the orchestration is being reset from a TERMINAL
    status — a terminal status proves no child is actually running, so the markers
    are stale by definition. Removes the legacy active-child file and every per-arid
    marker, returning the cleared arids for audit.
    """
    cleared: list[str] = []
    active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
    try:
        pointed = active_path.read_text(encoding="utf-8").strip()
    except OSError:
        pointed = ""
    if pointed:
        cleared.append(pointed)
    active_path.unlink(missing_ok=True)
    markers_dir = _active_children_dir(repo_root, orchestration_id)
    if markers_dir.is_dir():
        for marker in sorted(markers_dir.glob("*.txt")):
            arid = marker.stem
            if arid and arid not in cleared:
                cleared.append(arid)
            marker.unlink(missing_ok=True)
    return cleared


def _prune_orphan_agent_graph_edges(
    repo_root: Path, orchestration_id: str
) -> list[str]:
    """Remove agent_graph.json edges left by an ABANDONED child launch.

    `record_launch` writes the parent→child graph edge BEFORE the active-child
    marker, so a launch abandoned mid-flight (the dangling-launch case) leaves an
    orphan edge with no terminal `agent_runs.jsonl` row for the child. On resume the
    agent re-launches under a fresh agent_run_id (never reusing the abandoned one),
    so that edge stays orphaned forever — and `_validate_orchestration_completion_for_pass`
    rejects `set-status pass` with "agent_graph edge child_agent_run_id missing from
    agent_runs.jsonl". Prune those orphan edges so the recovered run can reach pass.

    Scope is deliberately narrow on BOTH sides so this does NOT hide unrelated
    integrity problems. An edge is pruned only when its child:

    (1) WAS genuinely launched via record_launch — proven by a durable
        `launches/<child>.request.json` (the same `is_owner_via_launch` signal). This
        excludes an arbitrarily-corrupted graph edge whose child_agent_run_id was never
        launched: that edge has no launch artifact, so it is kept and
        `_validate_orchestration_completion_for_pass` still rejects the corruption; AND

    (2) appears in NONE of these "completed/attempted" sources:
          - an `agent_runs.jsonl` row (terminalized normally);
          - a `step_result.json` reference (executor_agent_run_id / substep_agent_run_ids)
            — a child a step_result vouches for, whose missing run row is corruption;
          - an `agent_runs_invalid.jsonl` row — a child diverted there by terminal-payload
            validation (sandbox / session-id / output-manifest). It has no
            `agent_runs.jsonl` row, but its edge must be kept so validation surfaces the
            invalid terminal attempt;
          - a `child_returns/<child>.txt` ack — the Agent tool already returned, so a
            missing run row is lost finalization, not abandonment.

    The remaining set — launched but with no record of returning/attempting to
    terminalize — is exactly the abandoned dangling launch. Both criteria derive from
    durable artifacts only, so this is idempotent and does not depend on the active-child
    markers the resume reset clears just before this runs.

    Only meaningful from the terminal-reset path (a terminal status proves no child
    run is still pending a row). Launch artifacts (launches/<arid>.*, the incident
    snapshot) are kept for forensics — only the spurious graph edge is removed.
    Returns the pruned child arids.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    graph_path = root / "agent_graph.json"
    if not graph_path.is_file():
        return []
    # A corrupt agent_graph.json must not abort resume (recovery is exactly for such
    # runs); degrade to no-op pruning.
    try:
        graph = _load_graph(graph_path)
    except (OSError, json.JSONDecodeError):
        return []
    edges = graph.get("edges")
    if not isinstance(edges, list) or not edges:
        return []

    protected = _protected_child_arids(repo_root, orchestration_id)
    launches_dir = root / "launches"
    kept: list[Any] = []
    pruned: list[str] = []
    for edge in edges:
        child = edge.get("child_agent_run_id") if isinstance(edge, dict) else None
        child_id = child.strip() if isinstance(child, str) and child.strip() else None
        was_launched = (
            child_id is not None and (launches_dir / f"{child_id}.request.json").is_file()
        )
        if child_id is not None and was_launched and child_id not in protected:
            pruned.append(child_id)
        else:
            kept.append(edge)
    if pruned:
        graph["edges"] = kept
        _write_json(graph_path, graph)
    return pruned


def _protected_child_arids(repo_root: Path, orchestration_id: str) -> set[str]:
    """Child arids with durable evidence they reached / completed / attempted a run.

    Any child appearing here is NOT an abandoned launch: it has a terminal
    `agent_runs.jsonl` row, is vouched for by a `step_result.json`, has an
    `agent_runs_invalid.jsonl` entry (terminal-payload validation diverted it), or
    left a `child_returns/<arid>.txt` ack (the Agent tool already returned). Used to
    keep `_prune_orphan_agent_graph_edges` from pruning such edges AND to keep the
    resume orphan-tombstone set from mislabeling these as expected orphans.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    protected: set[str] = set(_load_run_records(root).keys())
    for result_path in _iter_step_result_paths(root):
        # A malformed / partially written step_result must NOT block resume — resume is
        # the recovery path for exactly such failed/corrupt runs. Skip it: a corrupt
        # step_result still makes _validate_orchestration_completion_for_pass reject the
        # eventual set-status(pass), so no bad pass can slip through from skipping here.
        try:
            doc = _read_json(result_path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        executor = doc.get("executor_agent_run_id")
        if isinstance(executor, str) and executor.strip():
            protected.add(executor.strip())
        substeps = doc.get("substep_agent_run_ids")
        if isinstance(substeps, list):
            for sub in substeps:
                if isinstance(sub, str) and sub.strip():
                    protected.add(sub.strip())
    invalid_path = root / "agent_runs_invalid.jsonl"
    if invalid_path.is_file():
        try:
            for line in invalid_path.read_text(encoding="utf-8").splitlines():
                token = line.strip()
                if not token:
                    continue
                try:
                    rec = json.loads(token)
                except json.JSONDecodeError:
                    continue
                rid = rec.get("agent_run_id") if isinstance(rec, dict) else None
                if isinstance(rid, str) and rid.strip():
                    protected.add(rid.strip())
        except OSError:
            pass
    # A child_returns/<arid>.txt ack means the Agent tool already RETURNED for that
    # child (record-child-return ran). A missing run row is then incomplete
    # finalization / corruption, not an abandoned launch.
    returns_dir = _child_returns_dir(repo_root, orchestration_id)
    if returns_dir.is_dir():
        for ack in returns_dir.glob("*.txt"):
            if ack.stem:
                protected.add(ack.stem)
    return protected


def _latest_launch_incident_ref(repo_root: Path, orchestration_id: str) -> str | None:
    """Newest `launch_incident.runtime.*.json` (repo-relative), or None.

    The filename suffix is a random uuid fragment (`uuid4().hex[:12]`), so
    lexicographic order is meaningless — ranking by it could attach the tombstone
    to an older / unrelated incident. Rank by each snapshot's own `detected_at`
    (its authoritative logical time), falling back to file mtime when that field is
    absent or unparseable.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    snaps = list(root.glob("launch_incident.runtime.*.json"))
    if not snaps:
        return None

    def _recency_key(p: Path) -> float:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        try:
            doc = _read_json(p)
        except (OSError, json.JSONDecodeError):
            doc = None
        if isinstance(doc, dict):
            detected = doc.get("detected_at")
            if isinstance(detected, str) and detected.strip():
                s = detected.strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                try:
                    return datetime.fromisoformat(s).timestamp()
                except ValueError:
                    pass
        return mtime

    newest = max(snaps, key=_recency_key)
    return str(newest.relative_to(repo_root))


def _write_orphan_launch_tombstones(
    repo_root: Path, orchestration_id: str, orphan_arids: list[str]
) -> list[str]:
    """Write a `launches/<arid>.pruned.json` tombstone for each abandoned-launch arid
    cleared/pruned during resume.

    A dangling launch (host died mid-Agent-call) leaves orphan launch artifacts
    (`launches/<arid>.{prompt,request,response,reply}`, `capabilities/<arid>.json`,
    `output_manifests/<arid>.json`) with NO terminal `agent_runs.jsonl` row and NO
    `child_returns/<arid>.txt` ack. Resume keeps those artifacts for forensics but,
    without a marker, a later manual inspection / audit tool cannot distinguish them
    from a genuine protocol violation (a launched child that vanished). The tombstone
    records that the runtime intentionally pruned the orphan during recovery. It lives
    under `launches/` (already baseline-exempt via the runtime prefix in
    `_should_ignore_runtime_snapshot_path`), so writing it never contaminates a diff.
    Idempotent: an existing tombstone is overwritten with the same content.

    Candidates are derived from the DURABLE residual launch artifacts
    (`launches/*.request.json`), not only from the destructive resume steps'
    return values (`_clear_stale_active_child_markers` deletes markers,
    `_prune_orphan_agent_graph_edges` removes edges). This makes the tombstone
    resilient to an interrupted resume retry: if the host dies after those deletions
    but before the meta commit, the retry re-enters terminal_reset with empty
    cleared/pruned lists — yet the launch artifacts persist, so the orphan is still
    found and tombstoned. The passed `orphan_arids` are folded in as a supplementary
    hint. The set is then filtered to GENUINE orphans: launched
    (`launches/<arid>.request.json` exists) AND not in `_protected_child_arids` (no
    terminal row / step_result vouch / invalid-run entry / child_returns ack) AND with
    no `agents/<arid>/deactivate_snapshot.json`. The deactivate-snapshot exclusion is
    separate from `_protected_child_arids` on purpose: `deactivate-child` CONSUMES the
    `child_returns/<arid>.txt` ack (so a child that returned + deactivated but died
    before `record-agent-run` is no longer ack-protected), yet the durable snapshot
    proves the Agent tool returned — that is a lost-finalization / corruption case,
    not an abandoned launch, so it must not be tombstoned as an "expected orphan". It
    is excluded HERE rather than in `_protected_child_arids` because
    `_prune_orphan_agent_graph_edges` must still prune that child's orphan edge for the
    resumed run's `set-status pass` to be accepted.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    launches_dir = root / "launches"
    launches_dir.mkdir(parents=True, exist_ok=True)
    protected = _protected_child_arids(repo_root, orchestration_id)
    # Children that returned + deactivated (Agent tool returned) — proven by the
    # durable deactivate snapshot even after the ack was consumed. Not orphans.
    deactivated: set[str] = {
        p.parent.name for p in (root / "agents").glob("*/deactivate_snapshot.json")
    }
    # Durable candidate source: every launched arid still has its request.json
    # (the tombstone is <arid>.pruned.json, so it is never re-globbed here).
    candidate_set: set[str] = {p.name[: -len(".request.json")] for p in launches_dir.glob("*.request.json")}
    candidate_set.update(a.strip() for a in orphan_arids if isinstance(a, str) and a.strip())
    orphans = sorted(
        arid
        for arid in candidate_set
        if arid
        and arid not in protected
        and arid not in deactivated
        and (launches_dir / f"{arid}.request.json").is_file()
    )
    if not orphans:
        return []
    incident_ref = _latest_launch_incident_ref(repo_root, orchestration_id)
    written: list[str] = []
    for arid in orphans:
        _write_json(
            launches_dir / f"{arid}.pruned.json",
            {
                "schema": "launch_orphan_tombstone/v1",
                "agent_run_id": arid,
                "orchestration_id": orchestration_id,
                "reason": "resume_pruned_orphan",
                "note": (
                    "Abandoned launch (active_child window left open, no terminal "
                    "agent_runs row and no child_returns ack); pruned during "
                    "checkpoint resume. Residual launches/capabilities/output_manifests "
                    "artifacts for this arid are expected orphans, not a violation."
                ),
                "pruned_at": _utc_now_iso(),
                "incident_ref": incident_ref,
            },
        )
        written.append(arid)
    return written


def _reset_stale_child_running_node_steps(
    repo_root: Path, orchestration_id: str
) -> list[dict[str, str]]:
    """Reset any node/step left at `child_running` by an abandoned launch.

    `record_launch` transitions the node/step to `child_running`; an abandoned launch
    (host died mid-flight) leaves it there even after its active-child marker is
    cleared. The phase gates authorize child work when the node/step is
    `child_running` — apply-patch (`_phase_write_requires_child_running`), the MCP
    phase gate, and `run-gate` — so a terminal-reset resume must drop that stale
    authority, otherwise the abandoned child's capability stays phase-authorized and
    agents reading phase_state still see the substep as running. A terminal status
    proves no child is actually running, so any `child_running` node/step is stale →
    reset to `not_started`; the resumed re-launch transitions it back to
    `child_running` for the real new child. Returns the reset [{node_key_safe, step}].
    """
    doc = _load_phase_state(repo_root, orchestration_id)
    if not isinstance(doc, dict):
        return []
    node_states = doc.get("node_states")
    if not isinstance(node_states, dict):
        return []
    reset: list[dict[str, str]] = []
    for node_safe, steps in node_states.items():
        if not isinstance(steps, dict):
            continue
        for step_key, state in list(steps.items()):
            if state == "child_running":
                steps[step_key] = "not_started"
                reset.append({"node_key_safe": str(node_safe), "step": str(step_key)})
    if reset:
        _write_phase_state(repo_root, orchestration_id, doc)
    return reset


def _runs_jsonl_lock_path(repo_root: Path, orchestration_id: str) -> Path:
    """Sidecar lock file used by Adv-24 to serialize concurrent finalizers.

    fcntl.flock is acquired on this file for the duration of
    record_agent_run's read-existing-IDs → append region, eliminating the
    race where two finalizers would both miss each other's in-flight write
    (one tolerated as a truncated trailing line by Adv-21) and commit
    contradictory terminal entries for the same arid.
    """
    return _orchestration_root(repo_root, orchestration_id) / "agent_runs.jsonl.lock"


def _orchestration_meta_lock_path(repo_root: Path, orchestration_id: str) -> Path:
    """Sidecar lock for orchestration_meta.json terminal-status serialization.

    Protects the read-check-write-cleanup-marker critical section in
    `update_orchestration_status` against concurrent terminalizers that would
    otherwise both pass the same-terminal guard and produce nondeterministic
    final `reason_code` / `reason_detail` (last writer wins).
    """
    return _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json.lock"


_FCNTL_FALLBACK_WARNED = False


def _fcntl_warn_once(scope: str) -> None:
    global _FCNTL_FALLBACK_WARNED
    if _FCNTL_FALLBACK_WARNED:
        return
    _FCNTL_FALLBACK_WARNED = True
    import sys as _sys
    print(
        "orchestration_runtime: WARNING — fcntl is unavailable on this "
        f"platform; {scope} serialization is a no-op. Concurrent invocations "
        "may race. Restrict orchestration to a single process or run on a "
        "POSIX system to enable lock serialization.",
        file=_sys.stderr,
    )


@contextlib.contextmanager
def _fcntl_exclusive_lock(lock_path: Path) -> Iterator[None]:
    if _fcntl is None:  # pragma: no cover — non-POSIX
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@contextlib.contextmanager
def _runs_jsonl_exclusive_lock(repo_root: Path, orchestration_id: str) -> Iterator[None]:
    if _fcntl is None:  # pragma: no cover — non-POSIX
        # L-NEW-4: on non-POSIX platforms (Windows, etc.) fcntl is unavailable,
        # so the lock degrades to a no-op. Two concurrent finalizers would
        # then race silently. Single-process workflows are unaffected, but
        # any multi-process driver would defeat Adv-24 serialization without
        # warning. Emit a one-shot stderr message so the operator sees the
        # downgrade rather than discovering it via corrupt state.
        _fcntl_warn_once("agent_runs.jsonl")
        yield
        return
    with _fcntl_exclusive_lock(_runs_jsonl_lock_path(repo_root, orchestration_id)):
        yield


@contextlib.contextmanager
def _orchestration_meta_exclusive_lock(
    repo_root: Path, orchestration_id: str
) -> Iterator[None]:
    """Serialize the terminal-status critical section in update_orchestration_status."""
    if _fcntl is None:  # pragma: no cover — non-POSIX
        _fcntl_warn_once("orchestration_meta.json")
        yield
        return
    with _fcntl_exclusive_lock(_orchestration_meta_lock_path(repo_root, orchestration_id)):
        yield


def _cleanup_committed_dir(repo_root: Path, orchestration_id: str) -> Path:
    """Adv-35: per-arid cleanup-committed marker directory.

    Two-phase finalization invariant: validate_workspace_root may revoke a
    tmp dir's exemption ONLY if both
      (a) a terminal entry for the arid exists in agent_runs.jsonl, AND
      (b) cleanup_committed/<arid>.json exists.
    The committed marker is written *after* the destructive
    `_cleanup_agent_tmp_root` call has succeeded, so a partial failure
    (terminal entry written but cleanup failed mid-way) keeps the run in a
    "cleanup pending" state — recovery scratch under workspace/tmp/<arid>/
    stays exempt for diagnostics rather than getting silently flagged.
    """
    return _orchestration_root(repo_root, orchestration_id) / "cleanup_committed"


def _cleanup_committed_marker_path(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> Path:
    return _cleanup_committed_dir(repo_root, orchestration_id) / f"{agent_run_id}.json"


def _write_cleanup_committed_marker(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> None:
    d = _cleanup_committed_dir(repo_root, orchestration_id)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        _cleanup_committed_marker_path(repo_root, orchestration_id, agent_run_id),
        json.dumps({
            "agent_run_id": agent_run_id,
            "committed_at": _utc_now_iso(),
        }, ensure_ascii=False) + "\n",
    )


def _parent_return_token_path(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> Path:
    """Adv-30: per-arid parent-bound return token stored alongside launch
    artifacts. Generated at record-launch time and required at
    record-child-return time as proof that the caller is the same parent
    that initiated the launch (defense against accidental misrouted ack
    calls from buggy automation that doesn't know the per-arid token)."""
    return _orchestration_root(repo_root, orchestration_id) / "launches" / f"{agent_run_id}.parent_return_token"


def _read_parent_return_token(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> str | None:
    p = _parent_return_token_path(repo_root, orchestration_id, agent_run_id)
    if not p.is_file():
        return None
    try:
        token = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if token else None


def _child_returns_dir(repo_root: Path, orchestration_id: str) -> Path:
    """Per-arid child-return acknowledgment directory (Adv-20).

    The orchestration agent calls `record-child-return --agent-run-id <arid>`
    AFTER it has observed the Agent tool actually returning. The resulting
    `child_returns/<arid>.txt` file is a separate proof, distinct from the
    launch-time `active_children/<arid>.txt` marker, that the orch agent has
    witnessed the Agent tool return for THIS specific arid. deactivate-child
    refuses to clear the active_children marker without this ack — without
    it a misrouted deactivate-child call would erase the only liveness guard
    for a still-running Codex child.
    """
    return _orchestration_root(repo_root, orchestration_id) / "child_returns"


def _child_return_marker_path(repo_root: Path, orchestration_id: str, agent_run_id: str) -> Path:
    return _child_returns_dir(repo_root, orchestration_id) / f"{agent_run_id}.txt"


def record_child_return(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    return_token: str,
    reply_excerpt: str | None = None,
) -> dict[str, Any]:
    """Adv-20/Adv-30: record that the orchestration agent has observed the
    Agent tool returning for this child run.

    The `return_token` MUST match the per-arid token written by record-launch
    to launches/<arid>.parent_return_token. This binds the ack to "the
    process that holds parent-readable launch state" — accidental misrouted
    calls from buggy automation that doesn't know the token will be rejected.

    The token is also embedded in the resulting ack file so deactivate-child
    can re-verify at unlink time.
    """
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("record-child-return requires non-empty --agent-run-id")
    arid = agent_run_id.strip()
    # Path-traversal guard: arid must be a flat token, not a path component.
    if "/" in arid or ".." in arid or arid in {".", ""}:
        raise ValueError(f"record-child-return: invalid agent_run_id {arid!r}")
    if not isinstance(return_token, str) or not return_token.strip():
        raise ValueError(
            "record-child-return requires --return-token <token>. The token "
            "is generated by record-launch and stored in "
            f"workspace/orchestrations/{orchestration_id}/launches/{arid}.parent_return_token."
        )
    return_token = return_token.strip()
    # Require that the launch actually happened (active_children marker for
    # this arid must exist). Prevents recording an ack for a never-launched
    # arid, which would later let an unrelated deactivate-child slip through.
    if not _active_child_marker_path(repo_root, orchestration_id, arid).is_file():
        raise ValueError(
            f"record-child-return: no active_children/{arid}.txt marker — "
            f"either the run was never launched, was already deactivated, or "
            f"already terminated. record-child-return must run BEFORE "
            f"deactivate-child."
        )
    expected_token = _read_parent_return_token(repo_root, orchestration_id, arid)
    if expected_token is None:
        raise ValueError(
            f"record-child-return: missing parent return token at "
            f"launches/{arid}.parent_return_token. The launch may pre-date "
            f"the Adv-30 token mechanism — re-launch via record-launch."
        )
    # secrets.compare_digest avoids timing leaks even though this is local I/O.
    if not secrets.compare_digest(return_token, expected_token):
        raise ValueError(
            f"record-child-return: return_token does not match the parent "
            f"token recorded at record-launch time for {arid!r}. The token "
            f"is per-arid; verify --return-token is the value from "
            f"launches/{arid}.parent_return_token, not a value from another arid."
        )
    returns_dir = _child_returns_dir(repo_root, orchestration_id)
    returns_dir.mkdir(parents=True, exist_ok=True)
    marker = _child_return_marker_path(repo_root, orchestration_id, arid)
    payload = {
        "agent_run_id": arid,
        "recorded_at": _utc_now_iso(),
        "return_token": return_token,
    }
    if isinstance(reply_excerpt, str) and reply_excerpt.strip():
        payload["reply_excerpt"] = reply_excerpt.strip()[:200]
    # M1: atomic write so a concurrent deactivate_child_agent reader never
    # observes a partial JSON body (which would falsely trip the Adv-30
    # "tampered with" raise path).
    _atomic_write_text(marker, json.dumps(payload, ensure_ascii=False) + "\n")
    return payload


def _session_run_index_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "session_run_index.json"


def _read_session_run_index(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    path = _session_run_index_path(repo_root, orchestration_id)
    if not path.is_file():
        return {"entries": []}
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"entries": []}
    if not isinstance(payload, dict):
        return {"entries": []}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        payload["entries"] = []
    return payload


def _append_session_run_index_entry(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    agent_session_id: str,
    context_id: str | None,
    agent_role: str,
    status: str,
) -> None:
    doc = _read_session_run_index(repo_root, orchestration_id)
    entries_obj = doc.get("entries")
    entries = entries_obj if isinstance(entries_obj, list) else []
    normalized_run_id = agent_run_id.strip()
    normalized_session_id = agent_session_id.strip()
    normalized_context_id = context_id.strip() if isinstance(context_id, str) and context_id.strip() else None
    normalized_role = agent_role.strip().lower()
    normalized_status = status.strip().lower()
    for item in entries:
        if not isinstance(item, dict):
            continue
        if str(item.get("agent_run_id", "")).strip() != normalized_run_id:
            continue
        item["agent_session_id"] = normalized_session_id
        item["session_id"] = normalized_session_id
        item["context_id"] = normalized_context_id
        item["agent_role"] = normalized_role
        item["status"] = normalized_status
        item["updated_at"] = _utc_now_iso()
        _write_json(_session_run_index_path(repo_root, orchestration_id), doc)
        return
    entries.append(
        {
            "agent_run_id": normalized_run_id,
            "agent_session_id": normalized_session_id,
            "session_id": normalized_session_id,
            "context_id": normalized_context_id,
            "agent_role": normalized_role,
            "status": normalized_status,
            "recorded_at": _utc_now_iso(),
        }
    )
    doc["entries"] = entries
    _write_json(_session_run_index_path(repo_root, orchestration_id), doc)


# --- Phase 1: access policy / phase state artifact layout (Item 10) ---

DEFAULT_ALLOWED_GATE_SERVICES: tuple[str, ...] = (
    "validate_pipeline_semantics",
    "check_artifact_syntax",
    "validate_workspace_root",
    "orchestration_read",
)

STEP_REQUIRED_CHILD_AGENT: dict[str, str] = {
    "compile": "substep",
    "generate": "substep",
    "build": "step",
    "validate": "substep",
}

FAIL_CLOSED_REASON_CODES = {
    "child_agent_forbidden_by_session_policy",
    "child_agent_unavailable_on_execution_platform",
    "required_child_agent_kind_mismatch",
    "phase_body_started_before_launch",
    "noncanonical_phase_write_attempt",
    "dependency_not_ready",
    "downstream_artifact_not_ready",
    "checkpoint_read_forbidden_without_resume",
    "post_phase_complete_violation",
    "parallel_nodes_not_explicitly_allowed",
    "sandbox_enforcement_violation",
}

# The fail_closed reason an orchestration records when a phase's failure mode is an
# unauthorized write (the nearest FAIL_CLOSED_REASON_CODES fit). Gates the
# unauthorized-write resume directive so it only fires for the current such failure.
_UNAUTHORIZED_WRITE_FAIL_REASON = "noncanonical_phase_write_attempt"

PARALLEL_NODES_ENV_VAR = "METDSL_ALLOW_PARALLEL_NODES"

PHASE_ARTIFACT_GUARDED_PREFIXES: tuple[str, ...] = ("workspace/ir/", "workspace/pipelines/")

STEP_KEYS_FOR_NODE_STATE: tuple[str, ...] = (
    "compile",
    "generate",
    "build",
    "validate",
)


def _access_policies_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "access_policies"


def _access_logs_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "access_logs"


def _violations_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "violations"


def _capabilities_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "capabilities"


def _gates_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "gates"


def _output_manifests_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "output_manifests"


def _read_manifests_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "read_manifests"


def _sandbox_profiles_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "sandbox_profiles"


def _hooks_log_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "hooks" / "workflow_hooks.jsonl"


def _append_workflow_hook_log(
    repo_root: Path,
    orchestration_id: str,
    *,
    hook_name: str,
    status: str,
    detail: dict[str, Any],
) -> None:
    path = _hooks_log_path(repo_root, orchestration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {"ts": _utc_now_iso(), "hook": hook_name, "status": status, **detail}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _phase_state_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "phase_state.json"


def _phase_state_log_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "phase_state_log.jsonl"


def _phase_state_log_has_set_status(
    repo_root: Path, orchestration_id: str, status: str
) -> bool:
    """Return True iff `phase_state_log.jsonl` already contains a canonical
    `set_status` event whose `to` matches `status`.

    Used by `update_orchestration_status` same-terminal replay to detect when
    the original forward-transition call committed meta+cleanup but its
    audit append failed (e.g., write-permission glitch). Replay then backfills
    the canonical event from the persisted meta instead of returning a silent
    no-op that loses the transition record forever.
    """
    path = _phase_state_log_path(repo_root, orchestration_id)
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if (
            isinstance(obj, dict)
            and obj.get("event") == "set_status"
            and obj.get("to") == status
        ):
            return True
    return False


def _ensure_orchestration_audit_dirs(repo_root: Path, orchestration_id: str) -> None:
    root = _orchestration_root(repo_root, orchestration_id)
    for sub in ("access_policies", "access_logs", "violations", "capabilities", "sandbox_profiles"):
        (root / sub).mkdir(parents=True, exist_ok=True)


def _new_phase_state_document(orchestration_id: str) -> dict[str, Any]:
    return {
        "orchestration_id": orchestration_id,
        "current_state": "initialized",
        "node_states": {},
    }


def _append_phase_state_log(
    repo_root: Path,
    orchestration_id: str,
    entry: dict[str, Any],
) -> None:
    path = _phase_state_log_path(repo_root, orchestration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _write_phase_state(repo_root: Path, orchestration_id: str, doc: dict[str, Any]) -> None:
    _write_json(_phase_state_path(repo_root, orchestration_id), doc)


def _load_phase_state(repo_root: Path, orchestration_id: str) -> dict[str, Any] | None:
    path = _phase_state_path(repo_root, orchestration_id)
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"phase_state.json is invalid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"phase_state.json must be object: {path}")
    return data


def _merge_node_states(
    existing: Any,
    orchestration_id: str,
) -> dict[str, dict[str, str]]:
    """Keep the existing node_states so as not to contradict the checkpoint, while filling missing keys."""
    merged: dict[str, dict[str, str]] = {}
    if isinstance(existing, dict):
        for node_key, steps in existing.items():
            if not isinstance(node_key, str) or not node_key.strip():
                continue
            if not isinstance(steps, dict):
                continue
            inner: dict[str, str] = {}
            for sk in STEP_KEYS_FOR_NODE_STATE:
                v = steps.get(sk)
                if isinstance(v, str) and v.strip():
                    inner[sk] = v.strip()
                else:
                    inner[sk] = "not_started"
            merged[node_key.strip()] = inner
    return merged


def init_phase_state_json(
    repo_root: Path,
    orchestration_id: str,
    *,
    reason: str = "init",
) -> dict[str, Any]:
    """Write out `phase_state.json` for a new orchestration."""
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    doc = _new_phase_state_document(orchestration_id)
    _write_phase_state(repo_root, orchestration_id, doc)
    _append_phase_state_log(
        repo_root,
        orchestration_id,
        {
            "ts": _utc_now_iso(),
            "event": reason,
            "from": None,
            "to": doc["current_state"],
        },
    )
    return doc


def _initial_current_state_when_phase_state_missing(
    repo_root: Path,
    orchestration_id: str,
) -> str:
    """The estimated `current_state` when `phase_state.json` is absent in a legacy orchestration."""
    path = _preflight_path(repo_root, orchestration_id)
    if not path.exists():
        return "initialized"
    try:
        payload = _read_json(path)
    except (json.JSONDecodeError, OSError):
        return "initialized"
    if isinstance(payload, dict) and _preflight_allows_agent_launch(payload):
        return "preflight_passed"
    return "initialized"


def merge_phase_state_for_resume(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """On `--resume-from-checkpoint`: keep `node_states` without discarding the existing `phase_state`.

    Because it is a separate file from the completion information of `orchestration_checkpoint.json`, no direct merge is done.
    Initialize only a missing `phase_state.json`; when one exists, do not overwrite `current_state` and
    `node_states`. For audit, append `resume_enabled` to `phase_state_log.jsonl`.
    """
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    existing = _load_phase_state(repo_root, orchestration_id)
    if existing is None:
        inferred = _initial_current_state_when_phase_state_missing(repo_root, orchestration_id)
        _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
        doc = _new_phase_state_document(orchestration_id)
        doc["current_state"] = inferred
        _write_phase_state(repo_root, orchestration_id, doc)
        _append_phase_state_log(
            repo_root,
            orchestration_id,
            {
                "ts": _utc_now_iso(),
                "event": "resume_missing_phase_state",
                "from": None,
                "to": inferred,
                "note": "created for checkpoint resume; inferred from preflight when possible",
            },
        )
        return doc
    orch_id = existing.get("orchestration_id")
    if orch_id != orchestration_id:
        raise RuntimeError(
            f"phase_state.json orchestration_id mismatch: expected {orchestration_id!r}, got {orch_id!r}"
        )
    merged = dict(existing)
    merged["node_states"] = _merge_node_states(merged.get("node_states"), orchestration_id)
    _write_phase_state(repo_root, orchestration_id, merged)
    _append_phase_state_log(
        repo_root,
        orchestration_id,
        {
            "ts": _utc_now_iso(),
            "event": "checkpoint_resume_enabled",
            "from": merged.get("current_state"),
            "to": merged.get("current_state"),
            "note": "orchestration_meta resume_enabled; phase_state preserved",
        },
    )
    return merged


def _transition_phase_state(
    repo_root: Path,
    orchestration_id: str,
    *,
    new_state: str,
    event: str,
) -> dict[str, Any]:
    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        doc = _new_phase_state_document(orchestration_id)
    elif doc.get("orchestration_id") not in (orchestration_id, None):
        raise RuntimeError(
            "phase_state.json orchestration_id mismatch: "
            f"expected {orchestration_id!r}, got {doc.get('orchestration_id')!r}"
        )
    prev = doc.get("current_state")
    doc["current_state"] = new_state
    if doc.get("orchestration_id") != orchestration_id:
        doc["orchestration_id"] = orchestration_id
    if not isinstance(doc.get("node_states"), dict):
        doc["node_states"] = _merge_node_states({}, orchestration_id)
    _write_phase_state(repo_root, orchestration_id, doc)
    _append_phase_state_log(
        repo_root,
        orchestration_id,
        {
            "ts": _utc_now_iso(),
            "event": event,
            "from": prev,
            "to": new_state,
        },
    )
    return doc


def _default_capability_expires_at_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=7)).isoformat().replace("+00:00", "Z")


def _parse_iso_z_expiry(raw: str) -> datetime | None:
    token = raw.strip()
    if not token:
        return None
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _mcp_permissions_for_launch(role: str, step: str) -> list[str]:
    r = role.strip().lower()
    st = step.strip().lower()
    if r == "orchestration":
        return []
    if r not in {"step", "substep"}:
        return []
    if st == "generate":
        return ["run_linter"]
    if st == "build":
        return ["compile_project"]
    if st == "validate":
        return ["run_program", "run_quality_checks"]
    return []


def _write_roots_for_launch(
    *,
    role: str,
    step: str,
    orchestration_id: str,
    ir_ref: str,
    pipeline_ref: str,
    node_key: str = "",
) -> list[str]:
    r = role.strip().lower()
    st = step.strip().lower()
    orch_root = _with_trailing_slash(_normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}"))
    ir_norm = _with_trailing_slash(_normalize_rel_posix(ir_ref))
    pipe_norm = _with_trailing_slash(_normalize_rel_posix(pipeline_ref))
    if r == "orchestration":
        return [orch_root]
    if r not in {"step", "substep"}:
        return []
    if st == "compile":
        return [ir_norm]
    if st == "generate":
        # pipeline_ref contains the unique pipeline_id (reserved by reserve-phase-root),
        # so lineage.json is exclusive to this run — no concurrent agent shares this path.
        # bwrap binds lineage.json's parent directory (not the file) so the agent can create
        # it; the file must not be pre-created before the agent writes it.
        return [
            _with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/source")),
            _normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/lineage.json"),
        ]
    if st == "build":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/binary"))]
    if st == "validate":
        # Validate substeps (execute / judge) write under runs/<run_id>/<node_key_safe>/.
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/runs"))]
    # NOTE: `tune` / `promote` are out-of-scope for the core 5-phase workflow
    # (Spec -> Compile -> Generate -> Build -> Validate). They are retained
    # here as optional flows invoked via a separate entrypoint (Tune / Promote
    # are defined outside the core workflow). The core workflow does not produce these
    # step tokens, so the branches below are reachable only from the optional
    # entrypoints; their tests assert the contract those entrypoints rely on.
    if st == "tune":
        return [_with_trailing_slash(_normalize_rel_posix(f"{pipeline_ref.rstrip('/')}/tune"))]
    if st == "promote":
        # Promote writes to two canonical locations outside the pipeline workspace:
        #   - releases/<spec_kind>/<domain>/<family>/<spec_id>/...
        #     — official release artifacts for THIS spec only
        #   - spec/registry/spec_catalog.yaml
        #     — official_releases registration (shared catalog file)
        #
        # The release subtree is scoped to the current node's spec at the
        # capability level (write_roots), not just at validation time. This
        # prevents a promote agent for spec_x from writing into spec_y's
        # release tree even via direct file writes — a sandbox escape that
        # post-hoc validation cannot recover from once shared checked-in
        # artifacts are mutated.
        if not node_key:
            # Without node_key we cannot derive a per-spec subtree; refuse
            # rather than fall back to the wide tree.
            return []
        try:
            _spec_kind, _spec_id_dotted, _ = _parse_node_key_strict(node_key)
        except ValueError:
            # Strict validator rejects path-traversal / malformed node_keys.
            # Returning [] here triggers the capability-level
            # `capability_invalid_empty_write_roots` failure for the promote
            # step, which is the correct fail-closed behavior for this branch.
            return []
        _spec_id_slashed = _spec_id_dotted.replace(".", "/")
        return [
            _with_trailing_slash(_normalize_rel_posix(
                f"releases/{_spec_kind}/{_spec_id_slashed}"
            )),
            _normalize_rel_posix("spec/registry/spec_catalog.yaml"),
        ]
    return []


def build_capability_document(
    *,
    agent_run_id: str,
    orchestration_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the payload of `capabilities/<agent_run_id>.json`."""
    role_raw = request_payload.get("agent_role")
    role = role_raw.strip().lower() if isinstance(role_raw, str) and role_raw.strip() else ""
    if role not in {"orchestration", "step", "substep"}:
        ss0 = request_payload.get("substep")
        if isinstance(ss0, str) and ss0.strip():
            role = "substep"
        elif isinstance(request_payload.get("step"), str) and str(request_payload.get("step")).strip():
            role = "step"
    if role not in {"orchestration", "step", "substep"}:
        raise ValueError("capability requires agent_role orchestration|step|substep")
    step_raw = request_payload.get("step")
    if not isinstance(step_raw, str) or not step_raw.strip():
        raise ValueError("capability requires step")
    step = step_raw.strip().lower()
    node_raw = request_payload.get("node_key")
    if not isinstance(node_raw, str) or not node_raw.strip():
        raise ValueError("capability requires node_key")
    node_key = node_raw.strip()
    # Reject malformed/traversal-laden node_keys before they flow into
    # write_roots and release path prefixes (e.g. `../etc/passwd@1.0.0`
    # would otherwise yield `releases/../etc/passwd/`).
    _parse_node_key_strict(node_key)
    ir_ref = str(request_payload.get("ir_ref") or "").strip()
    pipeline_ref = str(request_payload.get("pipeline_ref") or "").strip()
    if not ir_ref or not pipeline_ref:
        raise ValueError("capability requires ir_ref and pipeline_ref")

    substep_val: str | None = None
    ss = request_payload.get("substep")
    if isinstance(ss, str) and ss.strip():
        substep_val = ss.strip().lower()

    token = secrets.token_hex(32)
    body: dict[str, Any] = {
        "agent_run_id": agent_run_id.strip(),
        "capability_token": token,
        "orchestration_id": orchestration_id,
        "agent_role": role,
        "node_key": node_key,
        "step": step,
        "write_roots": _write_roots_for_launch(
            role=role,
            step=step,
            orchestration_id=orchestration_id,
            ir_ref=ir_ref,
            pipeline_ref=pipeline_ref,
            node_key=node_key,
        ),
        "mcp_permissions": _mcp_permissions_for_launch(role, step),
        "expires_at": _default_capability_expires_at_iso(),
    }
    if substep_val is not None:
        body["substep"] = substep_val
    # step/substep agents must have at least one write root; an empty list means
    # the request_payload was missing or incomplete and would later cause a
    # fail_closed violation. Fail early here instead.
    if role in {"step", "substep"} and not body.get("write_roots"):
        raise ValueError(
            f"capability_invalid_empty_write_roots: agent_role={role!r} requires at least "
            "one write_root. Check that ir_ref and pipeline_ref in request_payload are "
            "non-empty and the step value is valid."
        )
    return body


def _write_capability_for_launch(
    repo_root: Path,
    orchestration_id: str,
    child_agent_run_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    cap = build_capability_document(
        agent_run_id=child_agent_run_id,
        orchestration_id=orchestration_id,
        request_payload=request_payload,
    )
    out = _capabilities_dir(repo_root, orchestration_id) / f"{child_agent_run_id}.json"
    _write_json(out, cap)
    return cap


def _transition_node_step_phase_state(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    new_state: str,
    event: str,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    """Update `node_states[node_key_safe][step]` of `phase_state.json`."""
    node_safe = _node_key_to_safe(node_key.strip())
    step_key = step.strip().lower()
    if step_key not in STEP_KEYS_FOR_NODE_STATE:
        raise ValueError(f"unsupported workflow step for phase_state: {step_key!r}")

    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        doc = _new_phase_state_document(orchestration_id)
    elif doc.get("orchestration_id") not in (orchestration_id, None):
        raise RuntimeError(
            "phase_state.json orchestration_id mismatch: "
            f"expected {orchestration_id!r}, got {doc.get('orchestration_id')!r}"
        )
    doc["orchestration_id"] = orchestration_id
    ns_any = doc.get("node_states")
    ns: dict[str, Any] = ns_any if isinstance(ns_any, dict) else {}
    inner_any = ns.get(node_safe)
    inner: dict[str, str]
    if isinstance(inner_any, dict):
        inner = {}
        for sk in STEP_KEYS_FOR_NODE_STATE:
            v = inner_any.get(sk)
            inner[sk] = v.strip() if isinstance(v, str) and v.strip() else "not_started"
    else:
        inner = {sk: "not_started" for sk in STEP_KEYS_FOR_NODE_STATE}
    prev = inner.get(step_key, "not_started")
    inner[step_key] = new_state
    ns[node_safe] = inner
    doc["node_states"] = ns
    _write_phase_state(repo_root, orchestration_id, doc)

    log_entry: dict[str, Any] = {
        "ts": _utc_now_iso(),
        "event": event,
        "node_key_safe": node_safe,
        "step": step_key,
        "from": prev,
        "to": new_state,
    }
    if agent_run_id:
        log_entry["agent_run_id"] = agent_run_id
    _append_phase_state_log(repo_root, orchestration_id, log_entry)
    return doc


def _build_step_agents_missing_step_result(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
) -> list[str]:
    """Terminal build `step` agents for the node whose step_result.json is absent.

    This is the true invariant behind the relaunch guard / completion check:
    every terminal build step agent must leave a step_result. Keying the guard
    on this (rather than on a `child_finished` phase-state proxy) avoids wedging
    recovery when the phase transition was interrupted after the result file was
    already written (crash between `_write_json` and the phase transition).
    """
    root = _orchestration_root(repo_root, orchestration_id)
    node_safe = _node_key_to_safe(node_key.strip())
    missing: list[str] = []
    for run_id, payload in _load_run_records(root).items():
        if not isinstance(payload, dict):
            continue
        role = payload.get("agent_role")
        if not (isinstance(role, str) and role.strip().lower() == "step"):
            continue
        step_val = payload.get("step")
        if not (isinstance(step_val, str) and step_val.strip().lower() == "build"):
            continue
        nk_val = payload.get("node_key")
        if not (isinstance(nk_val, str) and nk_val.strip() == node_key.strip()):
            continue
        status = payload.get("status")
        if not (isinstance(status, str) and status.strip().lower() in TERMINAL_STATUSES):
            continue
        result_path = root / "steps" / node_safe / "build" / run_id / "step_result.json"
        if not result_path.exists():
            missing.append(run_id)
    return missing


def _phase_state_allows_write_step_result(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
) -> None:
    """`write_step_result` permits only a step that has reached `child_finished`."""
    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        raise RuntimeError("write_step_result phase gate: phase_state.json missing")
    node_safe = _node_key_to_safe(node_key.strip())
    step_key = step.strip().lower()
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        raise RuntimeError("write_step_result phase gate: phase_state.node_states missing")
    inner = ns.get(node_safe)
    if not isinstance(inner, dict):
        raise RuntimeError(f"write_step_result phase gate: phase_state missing node {node_safe!r}")
    st = inner.get(step_key)
    if not isinstance(st, str):
        raise RuntimeError(
            "write_step_result phase gate: phase_state missing node step "
            f"(node_key_safe={node_safe!r}, step={step_key!r})"
        )
    token = st.strip()
    if token == "child_finished":
        return
    raise RuntimeError(
        "write_step_result phase gate: node step must be child_finished "
        f"(node_key_safe={node_safe!r}, step={step_key!r}, current={token!r})"
    )


def _write_rule_source_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    read_path: str,
    matched_prefix: str | None,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.rule_source_violation.json"
    payload = {
        "kind": "rule_source_violation",
        "agent_run_id": agent_run_id,
        "read_path": read_path,
        "matched_denied_prefix": matched_prefix,
        "evaluated_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return out


def _write_phase_authority_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    actor_role: str,
    rejected_paths: list[str],
    reason: str,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.phase_authority_violation.json"
    payload = {
        "kind": "phase_authority_violation",
        "actor_role": actor_role,
        "agent_run_id": agent_run_id,
        "rejected_paths": rejected_paths,
        "reason": reason,
        "evaluated_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return out


def _write_sandbox_enforcement_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.sandbox_enforcement_violation.json"
    payload: dict[str, Any] = {
        "kind": "sandbox_enforcement_violation",
        "agent_run_id": agent_run_id,
        "reason": reason,
        "evaluated_at": _utc_now_iso(),
    }
    if isinstance(detail, dict):
        payload["detail"] = detail
    _write_json(out, payload)
    return out


def _write_noncanonical_phase_write_attempt(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    actor_role: str,
    attempted_paths: list[str],
    node_key: str | None,
    step: str | None,
    required_child_agent: str | None,
    current_phase_state: str | None,
) -> Path:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    out = (
        _violations_dir(repo_root, orchestration_id)
        / f"{agent_run_id}.noncanonical_phase_write_attempt.json"
    )
    payload = {
        "kind": "noncanonical_phase_write_attempt",
        "agent_run_id": agent_run_id,
        "actor": actor_role,
        "attempted_paths": attempted_paths,
        "node_key": node_key,
        "step": step,
        "required_child_agent": required_child_agent,
        "current_phase_state": current_phase_state,
        "reason_code": "noncanonical_phase_write_attempt",
        "detected_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return out


def _required_child_agent_kind(step: str) -> str:
    step_token = step.strip().lower()
    required = STEP_REQUIRED_CHILD_AGENT.get(step_token)
    if required is None:
        raise ValueError(
            f"unsupported workflow step {step!r}; valid steps: "
            f"{sorted(STEP_REQUIRED_CHILD_AGENT)}"
        )
    return required


def _phase_write_requires_child_running(path: str) -> bool:
    p = _normalize_rel_posix(path)
    return any(p.startswith(prefix) for prefix in PHASE_ARTIFACT_GUARDED_PREFIXES)


def _execution_platform_launchable(preflight: dict[str, Any], required_child_agent: str) -> bool:
    if required_child_agent == "step":
        return preflight.get("can_launch_step_agents") is True
    if required_child_agent == "substep":
        return preflight.get("can_launch_substep_agents") is True
    return False


def _check_session_policy_launchable(
    preflight: dict[str, Any], required_child_agent: str
) -> dict[str, Any]:
    session_policy = preflight.get("session_policy")
    fallback_key = (
        "can_launch_step_agents" if required_child_agent == "step" else "can_launch_substep_agents"
    )
    launchable = False
    scope = "session_policy_missing"
    if isinstance(session_policy, dict):
        key = (
            "allow_step_agent_launch"
            if required_child_agent == "step"
            else "allow_substep_agent_launch"
        )
        if isinstance(session_policy.get(key), bool):
            launchable = bool(session_policy.get(key))
            scope = f"session_policy.{key}"
        elif isinstance(session_policy.get(fallback_key), bool):
            launchable = bool(session_policy.get(fallback_key))
            scope = f"session_policy.{fallback_key}"
    elif isinstance(preflight.get("session_policy_launchable"), bool):
        launchable = bool(preflight.get("session_policy_launchable"))
        scope = "session_policy_launchable"
    return {"launchable": launchable, "blocking_policy_scope": scope}


def _resolve_current_phase_state(
    repo_root: Path, orchestration_id: str, node_key: str, step: str
) -> str | None:
    doc = _load_phase_state(repo_root, orchestration_id)
    if not isinstance(doc, dict):
        return None
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        return None
    node_safe = _node_key_to_safe(node_key)
    inner = ns.get(node_safe)
    if not isinstance(inner, dict):
        return None
    value = inner.get(step.strip().lower())
    return value if isinstance(value, str) else None


def _reject_noncanonical_phase_write(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    actor_role: str,
    attempted_paths: list[str],
    node_key: str | None,
    step: str | None,
    current_phase_state: str | None,
) -> None:
    required: str | None = None
    if isinstance(step, str) and step.strip():
        try:
            required = _required_child_agent_kind(step)
        except ValueError:
            required = None
    _write_noncanonical_phase_write_attempt(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
        actor_role=actor_role,
        attempted_paths=attempted_paths,
        node_key=node_key,
        step=step,
        required_child_agent=required,
        current_phase_state=current_phase_state,
    )
    try:
        update_orchestration_status(
            repo_root,
            orchestration_id,
            status="fail_closed",
            reason_code="noncanonical_phase_write_attempt",
            reason_detail="; ".join(attempted_paths),
            blocking_policy_scope="apply_patch_writes",
        )
    except Exception:
        pass
    raise RuntimeError(
        "apply_patch gate: noncanonical phase write attempt detected before child_running"
    )


def _dependency_ready(
    repo_root: Path, orchestration_id: str, *, step: str
) -> tuple[bool, str | None]:
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        return False, "orchestration_meta_missing"
    step_token = step.strip().lower()
    if step_token not in {"compile", "generate", "build", "validate"}:
        return False, f"unsupported_step_for_dependency_readiness:{step_token}"
    # Codex round 27 F2: serialize the read + recompute + fingerprint
    # comparison against meta writers under the same exclusive lock that
    # `mark_dependency_readiness` / `write_preflight` / `update_orchestration_status`
    # use. Without the lock the read can see a half-written
    # `dep_set_fingerprint` while `_compute_dep_readiness_and_fingerprint`
    # reads catalog/deps bytes that the writer is in the middle of certifying,
    # producing a false-negative `dep_set_fingerprint_stale` that
    # fail-closes a perfectly valid orchestration. Holding the lock for the
    # whole comparison turns the check into a snapshot read.
    with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
        meta = _read_json(meta_path)
        if not isinstance(meta, dict):
            return False, "orchestration_meta_invalid"
        readiness = meta.get("dependency_readiness")
        if not isinstance(readiness, dict):
            return False, "dependency_readiness_missing"
        # Codex round 18 F1: do NOT trust persisted booleans alone. A direct
        # edit of orchestration_meta.json that flips dependency_readiness flags
        # while leaving artifacts (and therefore dep_set_fingerprint) unchanged
        # would otherwise pass the gate. Recompute readiness from live workspace
        # state at launch time and use the recomputed booleans as authoritative;
        # the persisted booleans are advisory / audit-only. The stored fingerprint
        # comparison still runs first as a cheap stale detector — catches catalog
        # or deps.yaml mutation between mark and gate.
        spec_ref = meta.get("spec_ref")
        # Codex round 28 F1: `_compute_dep_readiness_and_fingerprint` reaches
        # `_read_deps_yaml` / `_load_spec_catalog_from_bytes`, which raise
        # `RuntimeError` when PyYAML is not installed (round 27 F1). Letting
        # that escape `_dependency_ready` turns a missing package into an
        # un-handled launch-gate exception (the upstream CLI prints a
        # traceback instead of `dependency_not_ready`). Convert the install
        # failure into a deterministic fail-closed gate result with a
        # specific reason so operators can distinguish "PyYAML missing" from
        # other verification failures.
        try:
            recomputed, current_fp, _certified, fail_reason = (
                _compute_dep_readiness_and_fingerprint(repo_root, spec_ref)
            )
        except RuntimeError as exc:
            if "PyYAML" in str(exc):
                # Codex round 29 F1 → round 30 F1: PyYAML unavailable. A
                # no-deps leaf orchestration (persisted `certified_deps == []`
                # + all detail flags True) needs no YAML parse to verify,
                # BUT the persisted claim must still be bound to the CURRENT
                # deps.yaml bytes. Round 29's plain trust-persisted shortcut
                # let stale meta or a direct edit (empty certified_deps +
                # flipped detail flags) fail-open the gate exactly during a
                # degraded control plane. Round 30 binds the shortcut to a
                # byte-only fingerprint over `spec_ref` + raw `deps.yaml`
                # bytes (catalog subset is empty by definition when there
                # are no deps), so:
                #   - deps.yaml edits → fingerprint mismatch → reject
                #   - forged certified_deps without matching deps.yaml bytes
                #     → fingerprint mismatch → reject
                # The shortcut now requires (a) persisted no-deps claim,
                # (b) all detail flags True, AND (c) live byte fingerprint
                # equals persisted fingerprint.
                persisted_certified = readiness.get("certified_deps")
                persisted_detail = readiness.get("detail")
                persisted_fp = readiness.get("dep_set_fingerprint")
                if (
                    isinstance(persisted_certified, list)
                    and len(persisted_certified) == 0
                    and isinstance(persisted_detail, dict)
                    and persisted_detail.get("ir_ref_verified") is True
                    and persisted_detail.get("pipeline_ref_verified") is True
                    and persisted_detail.get("aggregate_verdict_verified") is True
                    and isinstance(persisted_fp, str)
                    and persisted_fp == _no_deps_leaf_fingerprint(repo_root, spec_ref)
                ):
                    return True, None
                return False, "pyyaml_unavailable"
            raise
        stored_fp = readiness.get("dep_set_fingerprint")
        if stored_fp != current_fp:
            return False, "dep_set_fingerprint_stale"
        if recomputed is None:
            # Codex round 20 F1: production launch checks MUST fail closed when
            # live recomputation cannot run — there is no way to re-verify
            # artifacts, so trusting persisted booleans means trusting whatever
            # `orchestration_meta.json` happens to say. The persisted-boolean
            # fallback is preserved ONLY for test scaffolding via an explicit
            # opt-in env var so unit-test fixtures (`_mark_dependencies_ready`)
            # that intentionally skip building a real deps.yaml continue to work.
            # Production environments do not set this variable; missing/unparseable
            # OR malformed-schema deps.yaml at gate time → reject with the
            # specific fail_reason (Codex round 25 F2: don't collapse distinct
            # verification failures into one generic reason).
            if os.environ.get("METDSL_DEP_READINESS_ALLOW_PERSISTED_FALLBACK") != "1":
                return False, fail_reason or "deps_yaml_missing_or_unparseable"
            if step_token == "compile":
                if readiness.get("direct_dependency_compile_readiness") is not True:
                    return False, "direct_dependency_compile_readiness_not_pass"
                return True, None
            if readiness.get("direct_dependency_execution_readiness") is not True:
                return False, "direct_dependency_execution_readiness_not_pass"
            return True, None
        if step_token == "compile":
            if not recomputed.get("ir_ref_verified"):
                return False, "direct_dependency_compile_readiness_not_pass"
            return True, None
        # generate / build / validate
        required = ("ir_ref_verified", "pipeline_ref_verified", "aggregate_verdict_verified")
        for required_key in required:
            if not recomputed.get(required_key):
                return False, f"dependency_readiness_detail_not_pass:{required_key}"
        return True, None


def workflow_launch_check(
    repo_root: Path,
    *,
    orchestration_id: str,
    node_key: str,
    step: str,
    backend: str,
    require_child_agent: str,
) -> dict[str, Any]:
    required_by_step = _required_child_agent_kind(step)
    required_flag = require_child_agent.strip().lower()
    if required_flag not in {"step", "substep"}:
        raise ValueError("--require-child-agent must be step or substep")

    execution_platform_launchable = False
    session_policy_launchable = True
    blocking_scope = "default_allow"
    reason_code: str | None = None
    reason_detail: str | None = None

    if required_by_step != required_flag:
        reason_code = "required_child_agent_kind_mismatch"
        reason_detail = (
            f"step {step.strip().lower()!r} requires {required_by_step!r}, "
            f"but flag is {required_flag!r}"
        )

    try:
        preflight = _require_preflight_launchable(repo_root, orchestration_id, enforce_live_probe=False)
    except RuntimeError as exc:
        return {
            "status": "fail_closed",
            "orchestration_id": orchestration_id,
            "node_key": node_key,
            "step": step.strip().lower(),
            "required_child_agent": required_flag,
            "required_child_agent_by_step": required_by_step,
            "execution_platform_launchable": False,
            "session_policy_launchable": False,
            "reason_code": "child_agent_unavailable_on_execution_platform",
            "reason_detail": str(exc),
            "blocking_policy_scope": "preflight",
            "next_action": "stop_before_phase_body",
        }
    preflight_backend = preflight.get("backend")
    if isinstance(preflight_backend, str) and preflight_backend.strip().lower() != backend.strip().lower():
        reason_code = reason_code or "child_agent_unavailable_on_execution_platform"
        reason_detail = reason_detail or (
            f"preflight backend mismatch: expected {backend.strip().lower()!r}, "
            f"got {preflight_backend.strip().lower()!r}"
        )

    execution_platform_launchable = _execution_platform_launchable(preflight, required_flag)
    if not execution_platform_launchable and reason_code is None:
        reason_code = "child_agent_unavailable_on_execution_platform"
        reason_detail = f"preflight cannot launch required child agent kind: {required_flag}"

    session_eval = _check_session_policy_launchable(preflight, required_flag)
    session_policy_launchable = bool(session_eval.get("launchable"))
    blocking_scope = str(session_eval.get("blocking_policy_scope") or "default_allow")
    if not session_policy_launchable and reason_code is None:
        reason_code = "child_agent_forbidden_by_session_policy"
        reason_detail = f"session policy forbids required child agent kind: {required_flag}"

    dep_ready, dep_detail = _dependency_ready(repo_root, orchestration_id, step=step)
    if not dep_ready and reason_code is None:
        reason_code = "dependency_not_ready"
        reason_detail = dep_detail

    status = "pass" if reason_code is None else "fail_closed"
    return {
        "status": status,
        "orchestration_id": orchestration_id,
        "node_key": node_key,
        "step": step.strip().lower(),
        "required_child_agent": required_flag,
        "required_child_agent_by_step": required_by_step,
        "execution_platform_launchable": execution_platform_launchable,
        "session_policy_launchable": session_policy_launchable,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "blocking_policy_scope": blocking_scope,
        "next_action": "proceed_phase_body" if status == "pass" else "stop_before_phase_body",
    }


def _resolve_judge_execution_dir(
    repo_root: Path,
    *,
    pipeline_ref: str,
    node_key: str,
    launch_request: dict[str, Any],
) -> tuple[Path | None, str | None]:
    """Return the `runs/<run_id>/<node_key_safe>/` that is the `Validate.judge` input. On failure, (None, reason)."""
    rel = _normalize_rel_posix(pipeline_ref)
    pr_abs = repo_root / rel
    if not pr_abs.is_dir():
        return None, "pipeline_missing"
    nk_safe = _node_key_to_safe(node_key)
    run_id = launch_request.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        cand = pr_abs / "runs" / run_id.strip() / nk_safe
        if cand.is_dir():
            return cand, None
        return None, "judge_run_path_missing"
    runs_root = pr_abs / "runs"
    candidates: list[Path] = []
    if runs_root.is_dir():
        for rid_dir in sorted(runs_root.iterdir()):
            if not rid_dir.is_dir():
                continue
            cand = rid_dir / nk_safe
            if cand.is_dir():
                candidates.append(cand)
    if len(candidates) != 1:
        return None, "judge_run_id_unresolved_or_ambiguous"
    return candidates[0], None


def _downstream_phase_launch_gate(
    repo_root: Path,
    *,
    node_key: str,
    step: str,
    pipeline_ref: str,
    launch_request: dict[str, Any],
) -> tuple[bool, str | None]:
    """Check the downstream phase start condition only when `pipeline_ref` exists on disk."""
    rel = _normalize_rel_posix(pipeline_ref)
    pr_abs = repo_root / rel
    if not pr_abs.is_dir():
        return True, None
    st = step.strip().lower()
    substep = str(launch_request.get("substep") or "").strip().lower()
    if st == "build":
        gen_root = pr_abs / "source"
        if not gen_root.is_dir():
            return False, "downstream:source_dir_missing"
        for gen_dir in sorted(gen_root.iterdir()):
            if not gen_dir.is_dir():
                continue
            meta = gen_dir / "source_meta.json"
            if not meta.is_file():
                continue
            try:
                data = _read_json(meta)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and str(data.get("verification_status", "")).strip().lower() == "pass":
                return True, None
        return False, "downstream:source_meta_verification_status_not_pass"
    if st == "validate" and substep == "execute":
        build_root = pr_abs / "binary"
        if not build_root.is_dir():
            return False, "downstream:binary_dir_missing"
        for bdir in sorted(build_root.iterdir()):
            if not bdir.is_dir():
                continue
            bin_dir = bdir / "bin"
            if bin_dir.is_dir() and any(bin_dir.iterdir()):
                return True, None
        return False, "downstream:binary_bin_dir_missing"
    if st == "validate" and substep == "judge":
        base, err = _resolve_judge_execution_dir(
            repo_root,
            pipeline_ref=pipeline_ref,
            node_key=node_key,
            launch_request=launch_request,
        )
        if base is None:
            return False, f"downstream:{err or 'judge_path'}"
        for name in ("diagnostics.json", "perf.json"):
            if not (base / name).is_file():
                return False, f"downstream:judge_missing:{name}"
        raw_dir = base / "raw"
        if not raw_dir.is_dir():
            return False, "downstream:judge_raw_dir_missing"
        exec_ok = (base / "mcp_command_log.jsonl").is_file() or (
            (base / "stdout.log").is_file() and (base / "stderr.log").is_file()
        )
        if not exec_ok:
            return False, "downstream:judge_execution_record_missing"
        return True, None
    return True, None


def pre_phase_launch(
    repo_root: Path,
    *,
    orchestration_id: str,
    node_key: str,
    step: str,
    backend: str,
    require_child_agent: str,
    launch_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A hook bundling `workflow_launch_check` and the downstream artifact start condition (when `pipeline_ref` exists on disk)."""
    base = workflow_launch_check(
        repo_root,
        orchestration_id=orchestration_id,
        node_key=node_key,
        step=step,
        backend=backend,
        require_child_agent=require_child_agent,
    )
    merged: dict[str, Any] = dict(base)
    merged["hook"] = "pre_phase_launch"
    if merged.get("status") != "pass":
        _append_workflow_hook_log(
            repo_root,
            orchestration_id,
            hook_name="pre_phase_launch",
            status="deny",
            detail={"reason": merged.get("reason_code"), "detail": merged.get("reason_detail")},
        )
        return merged
    if launch_request:
        pr = launch_request.get("pipeline_ref")
        if isinstance(pr, str) and pr.strip():
            ok, reason = _downstream_phase_launch_gate(
                repo_root,
                node_key=node_key,
                step=step,
                pipeline_ref=pr.strip(),
                launch_request=launch_request,
            )
            if not ok:
                merged["status"] = "fail_closed"
                merged["reason_code"] = "downstream_artifact_not_ready"
                merged["reason_detail"] = reason
                merged["next_action"] = "stop_before_phase_body"
                merged["blocking_policy_scope"] = "downstream_artifacts"
                _append_workflow_hook_log(
                    repo_root,
                    orchestration_id,
                    hook_name="pre_phase_launch",
                    status="deny",
                    detail={"reason": "downstream_artifact_not_ready", "detail": reason},
                )
                return merged
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_phase_launch",
        status="allow",
        detail={"node_key": node_key, "step": step.strip().lower()},
    )
    return merged


def pre_orchestration_start(
    repo_root: Path,
    orchestration_id: str,
    *,
    event: str,
) -> dict[str, Any]:
    """A pre-workflow-start hook applied idempotently at the `init` / `preflight` entry."""
    ws = repo_root / "workspace"
    created_ws: str | None = None
    if not ws.exists():
        ws.mkdir(parents=True, exist_ok=True)
        created_ws = "created_workspace_root"
    parallel_explicit = os.environ.get(PARALLEL_NODES_ENV_VAR, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    orch_root = _orchestration_root(repo_root, orchestration_id)
    orch_root.mkdir(parents=True, exist_ok=True)
    meta_path = orch_root / "orchestration_meta.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = _read_json(meta_path)
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            meta = loaded
    meta.setdefault("parallel_nodes_explicit", parallel_explicit)
    meta.setdefault("parallel_nodes_policy", "sequential_default")
    parallel_nodes_explicit_persisted = meta["parallel_nodes_explicit"]
    _write_json(meta_path, meta)
    detail = {
        "event": event,
        "workspace_bootstrap": created_ws,
        "parallel_nodes_explicit": parallel_nodes_explicit_persisted,
    }
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_orchestration_start",
        status="allow",
        detail=detail,
    )
    return {"status": "pass", "hook": "pre_orchestration_start", **detail}


def _load_write_roots_from_cap(roots_obj: Any) -> list[str]:
    """Normalize write_roots from capability JSON at load time.

    Trailing-slash entries are directory roots. All other entries are file pins (exact match),
    including extensionless files like Makefile or LICENSE.
    """
    result: list[str] = []
    for item in (roots_obj if isinstance(roots_obj, list) else []):
        if not isinstance(item, str) or not item.strip():
            continue
        raw = item.strip()
        if raw.endswith("/"):
            result.append(_normalize_rel_posix(raw) + "/")
        else:
            result.append(_normalize_rel_posix(raw))  # file pin: exact match
    return result


# _ALLOWED_BYPRODUCT_EXTENSIONS and _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES are imported
# from tools.hooks.common — the single source of truth for pre-write and terminal policy.


def _path_under_any_write_root(rel_posix: str, write_roots: list[str]) -> bool:
    """Check whether rel_posix is authorized by any write_roots entry.

    write_roots must be pre-normalized by _load_write_roots_from_cap so that
    directory entries have trailing '/' and file pins are exact paths (with or without extension).
    """
    p = _normalize_rel_posix(rel_posix)
    for root in write_roots:
        if not isinstance(root, str) or not root.strip():
            continue
        normalized_root = _normalize_rel_posix(root)
        if not normalized_root:
            continue
        if root.strip().endswith("/"):
            # Directory entry: prefix match
            if _repo_path_under_prefix(p, normalized_root):
                return True
        else:
            # File pin: exact match only
            if p == normalized_root:
                return True
    return False


def gate_apply_patch_writes(
    repo_root: Path,
    *,
    orchestration_id: str,
    actor_role: str,
    changed_paths: Sequence[str],
    agent_run_id: str,
    capability_token: str | None = None,
) -> dict[str, Any]:
    """Check whether the `apply_patch`-equivalent write destination is consistent with the actor's authority.

    On violation, write `phase_authority_violation` and raise a RuntimeError.
    """
    role = actor_role.strip().lower()
    if not agent_run_id.strip():
        raise ValueError("agent_run_id must be non-empty for apply-patch gate")

    normalized_paths = [_normalize_rel_posix(p) for p in changed_paths if str(p).strip()]
    if not normalized_paths:
        return {"allowed": True, "checked_paths": []}

    if role == "orchestration":
        allowed_roots = [
            _with_trailing_slash(
                _normalize_rel_posix(f"workspace/orchestrations/{orchestration_id.strip()}")
            ),
            _with_trailing_slash(_normalize_rel_posix(f"workspace/.pycache/{orchestration_id.strip()}")),
        ]
        bad = [p for p in normalized_paths if not _path_under_any_write_root(p, allowed_roots)]
        if bad:
            _reject_noncanonical_phase_write(
                repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=agent_run_id.strip(),
                actor_role=role,
                attempted_paths=bad,
                node_key=None,
                step=None,
                current_phase_state=None,
            )
        return {"allowed": True, "checked_paths": normalized_paths}

    if role in {"step", "substep"}:
        if not capability_token or not str(capability_token).strip():
            raise ValueError("capability_token is required for step/substep apply-patch gate")
        cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
        if not cap_path.exists():
            raise RuntimeError(f"capability file not found: {cap_path}")
        cap = _read_json(cap_path)
        if not isinstance(cap, dict):
            raise RuntimeError(f"capability must be object: {cap_path}")
        if str(cap.get("capability_token", "")).strip() != str(capability_token).strip():
            _write_phase_authority_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id.strip(),
                actor_role=role,
                rejected_paths=normalized_paths,
                reason="capability_token mismatch",
            )
            raise RuntimeError("apply_patch gate: invalid capability_token")
        roots_obj = cap.get("write_roots")
        roots = _load_write_roots_from_cap(roots_obj)
        node_key = str(cap.get("node_key", "")).strip()
        step = str(cap.get("step", "")).strip().lower()
        if node_key and step:
            for p in normalized_paths:
                if not _phase_write_requires_child_running(p):
                    continue
                current = _resolve_current_phase_state(repo_root, orchestration_id, node_key, step)
                if current != "child_running":
                    _reject_noncanonical_phase_write(
                        repo_root,
                        orchestration_id=orchestration_id,
                        agent_run_id=agent_run_id.strip(),
                        actor_role=role,
                        attempted_paths=[p],
                        node_key=node_key,
                        step=step,
                        current_phase_state=current,
                    )
        bad = [p for p in normalized_paths if not _path_under_any_write_root(p, roots)]
        if bad:
            _write_phase_authority_violation(
                repo_root,
                orchestration_id,
                agent_run_id=agent_run_id.strip(),
                actor_role=role,
                rejected_paths=bad,
                reason="path not under capability write_roots",
            )
            raise RuntimeError("apply_patch gate: path outside write_roots for child agent")
        return {"allowed": True, "checked_paths": normalized_paths}

    raise ValueError(f"unsupported actor_role for apply-patch gate: {actor_role!r}")


def validate_mcp_build_tool_invocation(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    capability_token: str,
    tool_name: str,
    mcp_args: dict[str, Any] | None = None,
) -> None:
    """The phase gate before a `compile_project` / `run_linter` / `run_program` / `run_quality_checks` call."""
    _require_preflight_launchable(repo_root, orchestration_id, enforce_live_probe=False)

    root = _orchestration_root(repo_root, orchestration_id)
    launch_resp = root / "launches" / f"{agent_run_id.strip()}.response.json"
    if not launch_resp.exists():
        raise RuntimeError(
            "MCP phase gate: record-launch did not complete (missing launches/*.response.json) "
            f"for agent_run_id={agent_run_id!r}"
        )

    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        raise RuntimeError("MCP phase gate: phase_state.json missing")
    cur = doc.get("current_state")
    if cur != "preflight_passed":
        raise RuntimeError(f"MCP phase gate: unexpected orchestration current_state: {cur!r}")

    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    if not cap_path.exists():
        raise RuntimeError(f"MCP phase gate: capability file missing: {cap_path}")
    cap = _read_json(cap_path)
    if not isinstance(cap, dict):
        raise RuntimeError(f"MCP phase gate: capability must be object: {cap_path}")
    if str(cap.get("capability_token", "")).strip() != str(capability_token).strip():
        raise RuntimeError("MCP phase gate: capability_token mismatch")

    exp = cap.get("expires_at")
    if isinstance(exp, str):
        exp_dt = _parse_iso_z_expiry(exp)
        if exp_dt is not None and datetime.now(timezone.utc) > exp_dt:
            raise RuntimeError("MCP phase gate: capability token expired")

    perms = cap.get("mcp_permissions")
    allowed = [str(x) for x in perms] if isinstance(perms, list) else []
    if tool_name not in allowed:
        raise RuntimeError(
            f"MCP phase gate: tool {tool_name!r} not permitted by capability "
            f"(allowed={allowed!r})"
        )

    node_raw = cap.get("node_key")
    step_raw = cap.get("step")
    if not isinstance(node_raw, str) or not node_raw.strip():
        raise RuntimeError("MCP phase gate: capability.node_key missing")
    if not isinstance(step_raw, str) or not step_raw.strip():
        raise RuntimeError("MCP phase gate: capability.step missing")
    node_safe = _node_key_to_safe(node_raw.strip())
    step_key = step_raw.strip().lower()
    required_child = _required_child_agent_kind(step_key)
    role = str(cap.get("agent_role", "")).strip().lower()
    if role != required_child:
        raise RuntimeError(
            "MCP phase gate: capability agent_role does not satisfy required child agent kind "
            f"(step={step_key!r}, required={required_child!r}, actual={role!r})"
        )
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        raise RuntimeError("MCP phase gate: phase_state.node_states missing")
    inner = ns.get(node_safe)
    if not isinstance(inner, dict):
        raise RuntimeError(f"MCP phase gate: phase_state missing node {node_safe!r}")
    st = inner.get(step_key)
    if st != "child_running":
        raise RuntimeError(
            "MCP phase gate: node step must be child_running "
            f"(node_key_safe={node_safe!r}, step={step_key!r}, current={st!r})"
        )

    args_obj = mcp_args if isinstance(mcp_args, dict) else {}
    if tool_name == "run_program" and step_key == "validate":
        cmd = args_obj.get("command")
        if not isinstance(cmd, list) or not cmd:
            raise RuntimeError("MCP phase gate: run_program requires non-empty command array")
        joined = " ".join(str(x) for x in cmd)
        if "spec.ir.yaml" not in joined:
            raise RuntimeError(
                "MCP phase gate: Validate.execute run_program command must reference spec.ir.yaml (case section)"
            )
        # Canonical command_log_path enforcement: align with validator-side
        # post_execute check so non-canonical placements fail at MCP-call time
        # rather than after expensive execution. Required canonical:
        #   <pipeline_ref>/runs/<run_id>/<node_safe>/mcp_command_log.jsonl
        # The MCP server's `_resolve_command_log_path` resolves a relative
        # `command_log_path` against `project_dir`; we normalize both to a
        # repo-relative canonical comparison.
        try:
            req_doc_for_log = _read_json(
                _orchestration_root(repo_root, orchestration_id)
                / "launches"
                / f"{agent_run_id.strip()}.request.json"
            )
        except (OSError, json.JSONDecodeError):
            req_doc_for_log = None
        pipeline_ref_for_log: str | None = None
        run_id_for_log: str | None = None
        if isinstance(req_doc_for_log, dict):
            pr_raw = req_doc_for_log.get("pipeline_ref")
            if isinstance(pr_raw, str) and pr_raw.strip():
                pipeline_ref_for_log = _normalize_rel_posix(pr_raw.strip())
            ex_raw = req_doc_for_log.get("run_id")
            if isinstance(ex_raw, str) and ex_raw.strip():
                run_id_for_log = ex_raw.strip()
        if pipeline_ref_for_log and run_id_for_log:
            expected_log_rel = (
                f"{pipeline_ref_for_log}/runs/{run_id_for_log}/"
                f"{node_safe}/mcp_command_log.jsonl"
            )
            project_dir_raw = args_obj.get("project_dir")
            command_log_path_raw = args_obj.get("command_log_path")
            actual_log_rel: str | None = None
            try:
                if (
                    isinstance(command_log_path_raw, str)
                    and command_log_path_raw.strip()
                ):
                    clp_path = Path(command_log_path_raw.strip())
                    if clp_path.is_absolute():
                        try:
                            actual_log_rel = (
                                clp_path.resolve()
                                .relative_to(repo_root.resolve())
                                .as_posix()
                            )
                        except ValueError:
                            actual_log_rel = None
                    else:
                        if (
                            isinstance(project_dir_raw, str)
                            and project_dir_raw.strip()
                        ):
                            base = Path(project_dir_raw.strip())
                            if not base.is_absolute():
                                base = repo_root / base
                            try:
                                actual_log_rel = (
                                    (base / clp_path)
                                    .resolve()
                                    .relative_to(repo_root.resolve())
                                    .as_posix()
                                )
                            except ValueError:
                                actual_log_rel = None
                elif (
                    isinstance(project_dir_raw, str)
                    and project_dir_raw.strip()
                ):
                    base = Path(project_dir_raw.strip())
                    if not base.is_absolute():
                        base = repo_root / base
                    try:
                        actual_log_rel = (
                            (base / "mcp_command_log.jsonl")
                            .resolve()
                            .relative_to(repo_root.resolve())
                            .as_posix()
                        )
                    except ValueError:
                        actual_log_rel = None
            except OSError:
                actual_log_rel = None
            if actual_log_rel != expected_log_rel:
                raise RuntimeError(
                    "MCP phase gate: Execute run_program log placement must be "
                    f"canonical {expected_log_rel!r} (resolved={actual_log_rel!r}). "
                    "Set project_dir to the execute node directory or pass "
                    "command_log_path explicitly so post_execute can verify "
                    "tool-execution evidence at canonical placement."
                )
    if tool_name in {"compile_project", "run_quality_checks"}:
        ir_ref = _launch_ir_ref_for_agent(repo_root, orchestration_id, agent_run_id)
        if ir_ref:
            bs = _impl_resolved_build_system(repo_root, ir_ref)
            if bs == "make":
                if tool_name == "compile_project":
                    req_bs = str(args_obj.get("build_system", "")).strip().lower()
                    if req_bs and req_bs != "make":
                        raise RuntimeError(
                            "MCP phase gate: toolchain.build_system=make requires compile_project "
                            f"build_system make (got {req_bs!r})"
                        )
                if tool_name == "run_quality_checks":
                    preset = str(args_obj.get("preset", "")).strip().lower()
                    if preset not in {"make_test", "make_check"}:
                        raise RuntimeError(
                            "MCP phase gate: toolchain.build_system=make requires run_quality_checks "
                            f"preset make_test or make_check (got {preset!r})"
                        )

    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_command_execute",
        status="allow",
        detail={"mcp_tool": tool_name, "step": step_key},
    )


def _launch_ir_ref_for_agent(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> str | None:
    req_path = _orchestration_root(repo_root, orchestration_id) / "launches" / f"{agent_run_id.strip()}.request.json"
    if not req_path.is_file():
        return None
    try:
        doc = _read_json(req_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    pr = doc.get("ir_ref")
    return pr.strip() if isinstance(pr, str) and pr.strip() else None


def _impl_resolved_build_system(repo_root: Path, ir_ref: str) -> str | None:
    path = repo_root / _normalize_rel_posix(ir_ref) / "spec.ir.yaml"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        if "build_system" not in key.strip().lower():
            continue
        val = rest.strip().strip("\"'")
        return val.lower() or None
    return None


def _gate_script_command(
    *,
    repo_root: Path,
    gate_name: str,
    args_json: dict[str, Any],
) -> list[str]:
    gate = gate_name.strip()
    tools_dir = Path(__file__).resolve().parent
    tool_path: Path
    if gate == "validate_pipeline_semantics":
        tool_path = tools_dir / "validate_pipeline_semantics.py"
    elif gate == "check_artifact_syntax":
        tool_path = tools_dir / "check_artifact_syntax.py"
    elif gate == "validate_workspace_root":
        tool_path = tools_dir / "validate_workspace_root.py"
    else:
        raise ValueError(f"unsupported gate name: {gate_name!r}")
    if not tool_path.exists():
        raise RuntimeError(f"gate script not found: {tool_path}")

    cmd: list[str] = [sys.executable, str(tool_path)]
    positionals = args_json.get("paths")
    if positionals is None:
        positionals = args_json.get("positional_args")
    if positionals is not None:
        if not isinstance(positionals, list) or not all(isinstance(x, str) for x in positionals):
            raise ValueError("args_json.paths/positional_args must be array of strings")
    positional_list: list[str] = [str(x) for x in (positionals or []) if str(x).strip()]

    for key in sorted(args_json.keys()):
        if key in {"paths", "positional_args"}:
            continue
        value = args_json[key]
        if value is None:
            continue
        if isinstance(key, str) and key.startswith("--"):
            flag = key
        else:
            flag = "--" + str(key).strip().replace("_", "-")
        if not flag.strip():
            continue
        if isinstance(value, bool):
            if value:
                cmd.append(flag)
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, (str, int, float)) and str(item).strip():
                    cmd.extend([flag, str(item)])
            continue
        if isinstance(value, (str, int, float)) and str(value).strip():
            cmd.extend([flag, str(value)])

    cmd.extend(positional_list)
    return cmd


_CHECK_ARTIFACT_SYNTAX_EXPECT_TOP_ALLOWED = frozenset({"object", "array"})


def _validate_check_artifact_syntax_args(args_json: dict[str, Any]) -> None:
    paths_value = args_json.get("paths")
    if "path" in args_json:
        raise ValueError(
            "check_artifact_syntax args-json requires 'paths' (list[str]); "
            "single 'path' is unsupported"
        )
    if not isinstance(paths_value, list):
        raise ValueError("check_artifact_syntax args-json requires key 'paths' as list[str]")
    if not paths_value:
        raise ValueError("check_artifact_syntax args-json paths must be a non-empty list")
    for idx, item in enumerate(paths_value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"check_artifact_syntax args-json paths[{idx}] must be non-empty string"
            )

    expect_top = args_json.get("expect_top")
    if expect_top is None:
        return
    if not isinstance(expect_top, str) or expect_top.strip() not in _CHECK_ARTIFACT_SYNTAX_EXPECT_TOP_ALLOWED:
        raise ValueError(
            "check_artifact_syntax args-json expect_top must be one of "
            f"{sorted(_CHECK_ARTIFACT_SYNTAX_EXPECT_TOP_ALLOWED)!r}"
        )


def _extract_gate_violations(stdout: str, stderr: str, returncode: int) -> list[str]:
    lines: list[str] = []
    for source in (stdout, stderr):
        for raw in source.splitlines():
            token = raw.strip()
            if not token:
                continue
            if token.startswith("- ") or token.startswith("FAIL:"):
                lines.append(token)
                continue
            if token.endswith(": FAIL") or " validation: FAIL" in token:
                lines.append(token)
                continue
    deduped: list[str] = []
    seen: set[str] = set()
    for item in lines:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    if returncode != 0 and not deduped:
        deduped.append(f"gate command failed with exit code {returncode}")
    return deduped


def _inline_gate_result(
    repo_root: Path,
    *,
    orchestration_id: str,
    gate_name: str,
    agent_run_id: str,
    args_json: dict[str, Any],
    capability_token: str,
) -> dict[str, Any]:
    gate = gate_name.strip()
    if gate == "orchestration_read":
        read_path = args_json.get("read_path")
        if not isinstance(read_path, str) or not read_path.strip():
            raise ValueError("run-gate orchestration_read requires non-empty args_json.read_path")
        return log_orchestration_read(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id,
            read_path=read_path,
        )
    raise ValueError(f"unsupported inline gate name: {gate_name!r}")


def _gate_python_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["PYTHONPYCACHEPREFIX"] = str((repo_root / "workspace" / ".pycache").resolve())
    return env


def _pre_command_execute_validate_pipeline_semantics(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    args_json: dict[str, Any],
) -> None:
    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    cap = _read_json(cap_path)
    if not isinstance(cap, dict):
        return
    step_key = str(cap.get("step", "")).strip().lower()
    stage_l = validate_pipeline_semantics_stage(step_key=step_key, args_json=args_json)
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_command_execute",
        status="allow",
        detail={"gate": "validate_pipeline_semantics", "stage": stage_l, "step": step_key},
    )


def _validate_run_gate_permissions(
    repo_root: Path,
    *,
    orchestration_id: str,
    gate_name: str,
    agent_run_id: str,
    capability_token: str,
) -> None:
    _require_preflight_launchable(repo_root, orchestration_id, enforce_live_probe=False)
    root = _orchestration_root(repo_root, orchestration_id)

    launch_resp = root / "launches" / f"{agent_run_id.strip()}.response.json"
    if not launch_resp.exists():
        raise RuntimeError(
            "run-gate phase gate: record-launch did not complete "
            f"(missing launches/{agent_run_id.strip()}.response.json)"
        )

    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    if not cap_path.exists():
        raise RuntimeError(f"run-gate phase gate: capability file missing: {cap_path}")
    cap = _read_json(cap_path)
    if not isinstance(cap, dict):
        raise RuntimeError(f"run-gate phase gate: capability must be object: {cap_path}")
    if str(cap.get("capability_token", "")).strip() != capability_token.strip():
        raise RuntimeError("run-gate phase gate: capability_token mismatch")
    exp = cap.get("expires_at")
    if isinstance(exp, str):
        exp_dt = _parse_iso_z_expiry(exp)
        if exp_dt is not None and datetime.now(timezone.utc) > exp_dt:
            raise RuntimeError("run-gate phase gate: capability token expired")

    policy_path = _access_policies_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    if not policy_path.exists():
        raise RuntimeError(f"run-gate phase gate: access policy missing: {policy_path}")
    policy = _read_json(policy_path)
    if not isinstance(policy, dict):
        raise RuntimeError(f"run-gate phase gate: access policy must be object: {policy_path}")
    allowed_svcs = policy.get("allowed_gate_services")
    allowed = [str(x) for x in allowed_svcs] if isinstance(allowed_svcs, list) else []
    if gate_name not in allowed:
        raise RuntimeError(
            f"run-gate phase gate: gate {gate_name!r} not permitted by access policy (allowed={allowed!r})"
        )

    doc = _load_phase_state(repo_root, orchestration_id)
    if doc is None:
        raise RuntimeError("run-gate phase gate: phase_state.json missing")
    if doc.get("current_state") != "preflight_passed":
        raise RuntimeError(
            f"run-gate phase gate: unexpected orchestration current_state: {doc.get('current_state')!r}"
        )
    node_raw = cap.get("node_key")
    step_raw = cap.get("step")
    if not isinstance(node_raw, str) or not node_raw.strip():
        raise RuntimeError("run-gate phase gate: capability.node_key missing")
    if not isinstance(step_raw, str) or not step_raw.strip():
        raise RuntimeError("run-gate phase gate: capability.step missing")
    node_safe = _node_key_to_safe(node_raw.strip())
    step_key = step_raw.strip().lower()
    required_child = _required_child_agent_kind(step_key)
    role = str(cap.get("agent_role", "")).strip().lower()
    if role != required_child:
        raise RuntimeError(
            "run-gate phase gate: capability agent_role does not satisfy required child agent kind "
            f"(step={step_key!r}, required={required_child!r}, actual={role!r})"
        )
    ns = doc.get("node_states")
    if not isinstance(ns, dict):
        raise RuntimeError("run-gate phase gate: phase_state.node_states missing")
    node_state = ns.get(node_safe)
    if not isinstance(node_state, dict):
        raise RuntimeError(f"run-gate phase gate: phase_state missing node {node_safe!r}")
    if node_state.get(step_key) != "child_running":
        raise RuntimeError(
            "run-gate phase gate: node step must be child_running "
            f"(node_key_safe={node_safe!r}, step={step_key!r}, current={node_state.get(step_key)!r})"
        )


def run_gate(
    repo_root: Path,
    *,
    orchestration_id: str,
    gate_name: str,
    agent_run_id: str,
    args_json: dict[str, Any],
    capability_token: str,
) -> dict[str, Any]:
    gate = gate_name.strip()
    if gate not in DEFAULT_ALLOWED_GATE_SERVICES:
        raise ValueError(f"unsupported gate name: {gate_name!r}")
    if not capability_token.strip():
        raise ValueError("capability_token is required for run-gate")
    if not isinstance(args_json, dict):
        raise ValueError("args_json must be object")

    _validate_run_gate_permissions(
        repo_root,
        orchestration_id=orchestration_id,
        gate_name=gate,
        agent_run_id=agent_run_id,
        capability_token=capability_token,
    )
    if gate == "validate_pipeline_semantics":
        _pre_command_execute_validate_pipeline_semantics(
            repo_root,
            orchestration_id,
            agent_run_id,
            args_json,
        )

    arg_validation_error: str | None = None
    if gate == "check_artifact_syntax":
        try:
            _validate_check_artifact_syntax_args(args_json)
        except ValueError as exc:
            arg_validation_error = str(exc)

    inline_result: dict[str, Any] | None = None
    if arg_validation_error is not None:
        violations = [f"args-json validation failed: {arg_validation_error}"]
        status = "fail"
        exit_code = 2
    elif gate == "orchestration_read":
        inline_result = _inline_gate_result(
            repo_root,
            orchestration_id=orchestration_id,
            gate_name=gate,
            agent_run_id=agent_run_id,
            args_json=args_json,
            capability_token=capability_token,
        )
        violations: list[str] = []
        status = "pass"
        exit_code = 0
    else:
        cmd = _gate_script_command(repo_root=repo_root, gate_name=gate, args_json=args_json)
        gate_env = _gate_python_env(repo_root)
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=gate_env,
            text=True,
            capture_output=True,
            check=False,
        )
        violations = _extract_gate_violations(proc.stdout or "", proc.stderr or "", proc.returncode)
        status = "pass" if proc.returncode == 0 else "fail"
        exit_code = proc.returncode
    gate_doc: dict[str, Any] = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "gate": gate,
        "args_json": args_json,
        "status": status,
        "exit_code": exit_code,
        "violations": violations,
        "evaluated_at": _utc_now_iso(),
    }
    if inline_result is not None:
        gate_doc["result"] = inline_result
    if arg_validation_error is not None:
        gate_doc["arg_validation_error"] = arg_validation_error
    out_path = _gates_dir(repo_root, orchestration_id) / agent_run_id.strip() / f"{gate}.json"
    _write_json(out_path, gate_doc)
    gate_ref = (
        f"workspace/orchestrations/{orchestration_id}/gates/"
        f"{agent_run_id.strip()}/{gate}.json"
    )
    result: dict[str, Any] = {"violations": violations, "gate_result_ref": gate_ref}
    if inline_result is not None:
        result["result"] = inline_result
    # Emit a one-line JSON status summary to stderr so agents can consume the
    # gate result without needing to Read the persisted gate file.  The gate
    # file path (gate_result_ref) is in a gates/<arid>/ directory that is
    # outside most agents' read_manifests, causing read_manifest_read_guard
    # blocks when agents attempt to read it directly (observed in
    # orch_20260610T130256Z_ebe96a51).  Agents should redirect stderr to a
    # tmp file (`2>workspace/tmp/<arid>/last_gate_stderr.txt`) and read that
    # instead — which IS in their allowed_tmp_root and thus read-allowed.
    _gate_summary = {
        "gate": gate,
        "status": status,
        "violations": violations,
        "gate_result_ref": gate_ref,
    }
    print(json.dumps(_gate_summary), file=sys.stderr)
    return result


def _write_apply_patch_gate_evidence(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    actor_role: str,
    changed_paths: Sequence[str],
    result_payload: dict[str, Any],
) -> str:
    gate = "apply_patch_writes"
    latest_changed_paths = _normalize_rel_path_list([str(p) for p in changed_paths if str(p).strip()])
    cumulative_changed_paths = _update_cumulative_gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
        changed_paths=latest_changed_paths,
    )
    gate_doc: dict[str, Any] = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "gate": gate,
        "args_json": {
            "actor_role": actor_role,
            "changed_paths": cumulative_changed_paths,
            "latest_changed_paths": latest_changed_paths,
        },
        "status": "pass",
        "exit_code": 0,
        "violations": [],
        "evaluated_at": _utc_now_iso(),
        "result": result_payload,
    }
    out_path = _gates_dir(repo_root, orchestration_id) / agent_run_id.strip() / f"{gate}.json"
    _write_json(out_path, gate_doc)
    return f"workspace/orchestrations/{orchestration_id}/gates/{agent_run_id.strip()}/{gate}.json"


def _allowed_output_manifest_path(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
) -> Path:
    return _output_manifests_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"


def _write_allowed_output_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    allowed_output_paths: Sequence[str],
    allowed_file_tool_paths: Sequence[str] | None = None,
    agent_role: str | None = None,
    allowed_tmp_root: str | None = None,
    mcp_owned_audit_logs: Sequence[str] | None = None,
) -> str:
    normalized = []
    for p in allowed_output_paths:
        if not isinstance(p, str) or not p.strip():
            continue
        raw = p.strip()
        if raw.endswith("/"):
            normalized.append(_normalize_rel_posix(raw) + "/")
        else:
            normalized.append(_normalize_rel_posix(raw))
    file_tool_normalized = [
        _normalize_rel_posix(p)
        for p in (allowed_file_tool_paths or [])
        if isinstance(p, str) and p.strip()
    ]
    mcp_logs_normalized = [
        _normalize_rel_posix(p)
        for p in (mcp_owned_audit_logs or [])
        if isinstance(p, str) and p.strip()
    ]
    payload = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id.strip(),
        "allowed_output_paths": sorted(set(normalized)),
        "allowed_file_tool_paths": sorted(set(file_tool_normalized)),
        "mcp_owned_audit_logs": sorted(set(mcp_logs_normalized)),
        "generated_at": _utc_now_iso(),
    }
    if isinstance(allowed_tmp_root, str) and allowed_tmp_root.strip():
        payload["allowed_tmp_root"] = _normalize_rel_posix(allowed_tmp_root.strip())
    if isinstance(agent_role, str) and agent_role.strip():
        payload["agent_role"] = agent_role.strip()
    out_path = _allowed_output_manifest_path(repo_root, orchestration_id, agent_run_id)
    _write_json(out_path, payload)
    return f"workspace/orchestrations/{orchestration_id}/output_manifests/{agent_run_id.strip()}.json"


def _load_allowed_output_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> dict[str, Any]:
    path = _allowed_output_manifest_path(repo_root, orchestration_id, agent_run_id)
    if not path.exists():
        raise ValueError(f"allowed_output_paths manifest not found: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"allowed_output_paths manifest must be object: {path}")
    return payload


def _validate_paths_against_allowed_output_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    paths: Sequence[str],
) -> None:
    manifest = _load_allowed_output_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
    )
    allowed_obj = manifest.get("allowed_output_paths")
    if not isinstance(allowed_obj, list) or not all(isinstance(x, str) for x in allowed_obj):
        raise ValueError("allowed_output_paths manifest must include string array allowed_output_paths")
    allowed_files: set[str] = set()
    allowed_dirs: list[str] = []
    for p in allowed_obj:
        if not isinstance(p, str) or not p.strip():
            continue
        raw_p = p.strip()
        if raw_p.endswith("/"):
            allowed_dirs.append(_normalize_rel_posix(raw_p))
        else:
            allowed_files.add(_normalize_rel_posix(raw_p))
    if not allowed_files and not allowed_dirs:
        raise ValueError("allowed_output_paths manifest must include non-empty allowed_output_paths")
    tmp_root_raw = manifest.get("allowed_tmp_root", "")
    tmp_norm = ""
    tmp_prefix = ""
    if isinstance(tmp_root_raw, str) and tmp_root_raw.strip():
        tmp_norm = _normalize_rel_posix(tmp_root_raw.strip())
        tmp_prefix = tmp_norm + "/"
    denied: list[str] = []
    invalid_paths: list[str] = []
    for raw in paths:
        rel = _normalize_rel_posix(str(raw))
        if not rel:
            invalid_paths.append(str(raw))
            continue
        if rel in allowed_files:
            continue
        if allowed_dirs and any(_repo_path_under_prefix(rel, d) for d in allowed_dirs):
            # Apply same extension policy as terminal validation — fail before mutation.
            ext = os.path.splitext(rel)[1].lower()
            if ext in _ALLOWED_BYPRODUCT_EXTENSIONS:
                continue
            if ext == "" and os.path.basename(rel).lower() in _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES:
                continue
            denied.append(rel)
            continue
        if tmp_prefix and (rel == tmp_norm or rel.startswith(tmp_prefix)):
            continue
        denied.append(rel)
    if denied or invalid_paths:
        details = [*denied, *[f"<invalid:{token}>" for token in invalid_paths]]
        raise ValueError("allowed_output_paths manifest violation: " + ", ".join(details))


def _allowed_output_paths_for_launch(
    *,
    request_payload: dict[str, Any],
    write_roots: Sequence[str],
) -> list[str]:
    role = str(request_payload.get("agent_role") or "").strip().lower()
    if role not in {"step", "substep"}:
        return [
            _normalize_rel_posix(item)
            for item in write_roots
            if isinstance(item, str) and item.strip()
        ]
    raw_candidates = (
        request_payload.get("allowed_output_paths")
        or request_payload.get("required_outputs")
        or request_payload.get("output_refs")
    )
    if not isinstance(raw_candidates, list):
        raise ValueError(
            "record-launch requires explicit allowed_output_paths list for step/substep agents"
        )
    allowed: list[str] = []
    for idx, item in enumerate(raw_candidates):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"allowed_output_paths[{idx}] must be non-empty string")
        token_raw = item.strip().replace("\\", "/")
        # Reject path-traversal segments BEFORE prefix/contract checks.
        # `_normalize_rel_posix` does not collapse `..`, so a path like
        # `releases/<spec>/.../r1/../../somewhere_else` would pass the
        # startswith() prefix and identifier checks. Refuse any `.` or
        # `..` segment anywhere in the candidate.
        for _seg in token_raw.split("/"):
            if _seg in ("", ".") and not (_seg == "" and token_raw.endswith("/")):
                # Tolerate a trailing slash (directory entry) but not bare "/"
                # repeats elsewhere.
                if _seg == "":
                    # Skip — duplicate slashes get normalized; treat as harmless.
                    continue
            if _seg in (".", ".."):
                raise ValueError(
                    f"allowed_output_paths[{idx}] must not contain '.'/'..' segments: {item!r}"
                )
        if token_raw.endswith("/"):
            # Directory allowlist entry: stored with trailing slash preserved
            token = _normalize_rel_posix(token_raw.rstrip("/")) + "/"
        else:
            token = _normalize_rel_posix(token_raw)
        if not token or token == "/":
            raise ValueError(f"allowed_output_paths[{idx}] must be valid relative path")
        allowed.append(token)
    normalized_roots = [
        _normalize_rel_posix(root)
        for root in write_roots
        if isinstance(root, str) and str(root).strip()
    ]
    step_token = str(request_payload.get("step") or "").strip().lower()
    substep_token = str(request_payload.get("substep") or "").strip().lower()
    ir_ref = _normalize_rel_posix(str(request_payload.get("ir_ref") or ""))
    pipeline_ref = _normalize_rel_posix(str(request_payload.get("pipeline_ref") or ""))
    node_key = str(request_payload.get("node_key") or "").strip()
    node_safe = _node_key_to_safe(node_key) if node_key else ""
    compile_required = {
        f"{ir_ref}/spec.ir.yaml",
        f"{ir_ref}/algorithm.summary.md",
        f"{ir_ref}/ir_meta.json",
    } if ir_ref else set()
    generate_prefix = f"{pipeline_ref}/source/" if pipeline_ref else ""
    build_prefix = f"{pipeline_ref}/binary/" if pipeline_ref else ""
    validate_prefix = f"{pipeline_ref}/runs/" if pipeline_ref else ""
    tune_prefix = f"{pipeline_ref}/tune/" if pipeline_ref else ""
    # source_id closure for cross-phase placement check below.
    source_id = str(request_payload.get("source_id") or "").strip()

    def _matches_phase_contract(path: str) -> bool:
        # Directory allowlist entries (trailing slash): validate the directory itself is permitted.
        if path.endswith("/"):
            dir_path = path.rstrip("/")
            if step_token == "generate":
                if generate_prefix and dir_path.startswith(generate_prefix):
                    # Allow only <gen_id>/src and its subdirectories; the generate root itself is forbidden
                    tail = dir_path[len(generate_prefix):]
                    parts = [seg for seg in tail.split("/") if seg]
                    if len(parts) >= 2 and parts[1] == "src":
                        return True
            return False
        if step_token == "compile":
            return path in compile_required
        if step_token == "generate":
            if pipeline_ref and path == f"{pipeline_ref}/lineage.json":
                return True
            if generate_prefix and path.startswith(generate_prefix):
                if "/src/" in path:
                    return True
                if path.endswith("/source_meta.json"):
                    return True
            return False
        if step_token == "build":
            # Cross-phase exception: in-source Make builds for Fortran/C
            # family run compile_project with project_dir=<gen>/src/, so the
            # MCP audit log lands under the generate tree. Strict bind to
            # request.source_id (record_launch verifies pass-state).
            if generate_prefix and path.startswith(generate_prefix):
                if not source_id:
                    return False
                expected_cross_phase_build = (
                    f"{generate_prefix}{source_id}/src/{_MCP_AUDIT_LOG_BASENAME}"
                )
                return path == expected_cross_phase_build
            if build_prefix and path.startswith(build_prefix):
                # Codex round 29 F2: enforce canonical `<slug>_YYYYMMDD_NNN`
                # on `binary_id` (the first segment after build_prefix).
                # The round-23 freshness selector filters by this same
                # suffix when dependency_readiness inspects another
                # orchestration's `binary/<binary_id>/binary_meta.json` —
                # so a non-canonical binary_id would silently be ignored
                # by downstream dep checks. Reject the write at the
                # earliest boundary instead of letting it land and be
                # silently dropped later.
                tail = path[len(build_prefix):]
                tail_parts = [part for part in tail.split("/") if part]
                if not tail_parts or not _SLUG_DATE_SEQ3_PATTERN.match(tail_parts[0]):
                    return False
                if "/bin/" in path or path.endswith("/binary_meta.json"):
                    return True
                # MCP `compile_project` writes a side-effect command log to
                # `<project_dir>/mcp_command_log.jsonl`. Per
                # `docs/workflow/phases/phase_03_build.md`, the in-phase
                # canonical placement (out-of-source CMake/Meson builds) is
                # directly under `<binary_id>/`.
                if (
                    len(tail_parts) == 2
                    and tail_parts[1] == "mcp_command_log.jsonl"
                ):
                    return True
            return False
        if step_token == "validate" and substep_token == "execute":
            if not validate_prefix or not node_safe:
                return False
            # Cross-phase exception: skills/workflow-validate-execute/SKILL.md
            # mandates `run_quality_checks` against
            # `project_dir=source/<source_id>/src/` for
            # `toolchain.build_system=make` + Fortran/C-family pipelines. The
            # MCP server's default command_log_path resolves to
            # `<project_dir>/mcp_command_log.jsonl`, so the audit log lands in
            # the source tree. This is the only legitimate write the
            # Validate.execute substep makes outside runs/. Strictly bind the
            # allowed cross-phase placement to the launch request's
            # `source_id` field — any other source_id (e.g. an older sibling
            # source under the same pipeline) is not authorized.
            if generate_prefix and path.startswith(generate_prefix):
                if not source_id:
                    return False
                expected_cross_phase = (
                    f"{generate_prefix}{source_id}/src/{_MCP_AUDIT_LOG_BASENAME}"
                )
                return path == expected_cross_phase
            if not path.startswith(validate_prefix):
                return False
            tail = path[len(validate_prefix):]
            tail_parts = [part for part in tail.split("/") if part]
            # Validate.execute contract must be under runs/<run_id>/<node_safe>/...
            # Codex round 29 F2: enforce canonical `run_id` so the round-23
            # freshness filter for `runs/<run_id>/.../aggregate_verdict.json`
            # never has to silently drop legitimate verdicts of downstream
            # consumers.
            # run_id uses the strict `run_<YYYYMMDD>_<seq3>` grammar
            # (`_RUN_ID_RE`), NOT the generic `<slug>_<date>_<seq3>` grammar:
            # a slug-shaped value like `run-rsn-p0_20260605_001` matched the
            # old `_SLUG_DATE_SEQ3_PATTERN` (slug=`run-rsn-p0`) and slipped
            # through here, yet the Validate `post_execute` run discovery only
            # recognizes the literal `run_` layout and silently reported
            # "no execution artifacts found". Rejecting it at this boundary
            # surfaces the mistake at launch instead.
            if (
                len(tail_parts) < 3
                or tail_parts[1] != node_safe
                or not _RUN_ID_RE.fullmatch(tail_parts[0])
            ):
                return False
            rel_under_node = "/".join(tail_parts[2:])
            allowed_files = {
                "diagnostics.json",
                "perf.json",
                "quality_check.json",
                "trial_meta.json",
                "stdout.log",
                "stderr.log",
                "metrics_basis.json",
                "execution_trace.json",
                # MCP `run_program` / `run_quality_checks` side-effect log
                # (phase_04_validate.md).
                "mcp_command_log.jsonl",
            }
            return rel_under_node in allowed_files or rel_under_node.startswith("raw/")
        if step_token == "validate" and substep_token == "judge":
            if not validate_prefix or not node_safe:
                return False
            if not path.startswith(validate_prefix):
                return False
            tail = path[len(validate_prefix):]
            tail_parts = [part for part in tail.split("/") if part]
            # Validate.judge contract must be under runs/<run_id>/<node_safe>/...
            # Codex round 29 F2: same canonical-id enforcement as the
            # execute substep — judge writes the aggregate_verdict that
            # downstream dependency_readiness consumes. run_id uses the strict
            # `run_<YYYYMMDD>_<seq3>` grammar (`_RUN_ID_RE`); see the execute
            # branch above for why the generic slug grammar is insufficient.
            if (
                len(tail_parts) < 3
                or tail_parts[1] != node_safe
                or not _RUN_ID_RE.fullmatch(tail_parts[0])
            ):
                return False
            rel_under_node = "/".join(tail_parts[2:])
            allowed_files = {
                "semantic_review.json",
                "verdict.json",
                "aggregate_verdict.json",
                "summary.json",
                "validate_meta.json",
            }
            return rel_under_node in allowed_files
        # NOTE: `tune` / `promote` step branches below are out-of-scope for
        # core 5-phase workflow (see _write_roots_for_launch for context).
        # They remain to satisfy the optional-flow capability contract.
        if step_token == "tune":
            if not tune_prefix or not path.startswith(tune_prefix):
                return False
            rel_under_tune = path[len(tune_prefix):]
            rel_parts = [part for part in rel_under_tune.split("/") if part]
            # tune contract must be tune/<trial_id>/<artifact>; deeper nesting is forbidden.
            if len(rel_parts) != 2:
                return False
            allowed_files = {
                "spec.ir.yaml",
                "diagnostics.json",
                "perf.json",
                "verdict.json",
                "trial_meta.json",
                "evaluation.json",
                "tune_meta.json",
            }
            base = rel_parts[1]
            return (
                base in allowed_files
                or base.endswith("_meta.json")
            )
        if step_token == "promote":
            # Promote contract per docs/workflow/phases/phase_07_promote.md:
            #   releases/<spec_kind>/<domain>/<family>/<spec_id>/
            #     <target_architecture>/<toolchain_language>/<release_id>/<artifact_path...>
            #   spec/registry/spec_catalog.yaml (exact file)
            #
            # The release tree is constrained to THIS node's spec to prevent
            # cross-spec writes, AND requires the full architecture/language/
            # release_id structure under the spec prefix. A bare prefix-match
            # would let a promote launch declare ad-hoc files like
            # `releases/.../spec_x/README.md` which fall outside the canonical
            # release artifact layout.
            if path == "spec/registry/spec_catalog.yaml":
                return True
            if not node_key:
                return False
            try:
                _spec_kind, _spec_id_dotted, _ = _parse_node_key_strict(node_key)
            except ValueError:
                return False
            _spec_id_slashed = _spec_id_dotted.replace(".", "/")
            required_prefix = f"releases/{_spec_kind}/{_spec_id_slashed}/"
            if not path.startswith(required_prefix):
                return False
            tail = path[len(required_prefix):]
            tail_segments = [seg for seg in tail.split("/") if seg]
            # Tail must be <arch>/<lang>/<release_id>/<artifact_path...>
            # — at least 4 non-empty segments, with the first three being
            # well-formed single-segment identifiers (no path-traversal or
            # control characters).
            if len(tail_segments) < 4:
                return False
            for ident in tail_segments[:3]:
                # Identifier rule: alphanumeric, underscore, hyphen, dot. No
                # leading/trailing dot or slash. Empty already rejected above.
                if ident.startswith(".") or ident.endswith("."):
                    return False
                if not all(c.isalnum() or c in "_-." for c in ident):
                    return False
            return True
        return False

    # Defensive auto-inject: MCP build/validate tooling writes a side-effect
    # command log to `<project_dir>/mcp_command_log.jsonl` (run_linter for
    # generate per skills/workflow-generate-generate, compile_project per
    # docs/workflow/phases/phase_03_build.md, run_program /
    # run_quality_checks per docs/workflow/phases/phase_04_validate.md). If
    # the canonical log path is not pre-listed in allowed_output_paths,
    # record-agent-run rejects it as `unauthorized_write_violation` and
    # fail_closes the orchestration.
    # Single-namespace enforcement for generate/build/validate steps:
    # require listed paths under `<pipeline_ref>/<phase>/` to use exactly one
    # `<source_id>` / `<binary_id>` / `<run_id>`. Otherwise the step could
    # authorize MCP-owned audit logs and outputs across sibling runs in the
    # same pipeline, breaking provenance isolation.
    if step_token == "generate" and generate_prefix:
        gen_ids: set[str] = set()
        for p in allowed:
            if not p.startswith(generate_prefix):
                continue
            tail = p[len(generate_prefix):]
            parts = [s for s in tail.split("/") if s]
            if parts:
                gen_ids.add(parts[0])
        if len(gen_ids) > 1:
            raise ValueError(
                f"allowed_output_paths must target a single source_id; "
                f"got multiple under {generate_prefix!r}: {sorted(gen_ids)!r}"
            )
        if source_id and gen_ids and source_id not in gen_ids:
            raise ValueError(
                f"allowed_output_paths source_id={sorted(gen_ids)!r} does "
                f"not match request source_id={source_id!r}"
            )
    elif step_token == "build" and build_prefix:
        build_ids: set[str] = set()
        for p in allowed:
            if not p.startswith(build_prefix):
                continue
            tail = p[len(build_prefix):]
            parts = [s for s in tail.split("/") if s]
            if parts:
                build_ids.add(parts[0])
        if len(build_ids) > 1:
            raise ValueError(
                f"allowed_output_paths must target a single binary_id; "
                f"got multiple under {build_prefix!r}: {sorted(build_ids)!r}"
            )
    elif step_token == "validate" and validate_prefix:
        run_ids: set[str] = set()
        for p in allowed:
            if not p.startswith(validate_prefix):
                continue
            tail = p[len(validate_prefix):]
            parts = [s for s in tail.split("/") if s]
            if parts:
                run_ids.add(parts[0])
        if len(run_ids) > 1:
            raise ValueError(
                f"allowed_output_paths must target a single run_id; "
                f"got multiple under {validate_prefix!r}: {sorted(run_ids)!r}"
            )
        request_run_id = str(
            request_payload.get("run_id") or ""
        ).strip()
        if request_run_id and run_ids and request_run_id not in run_ids:
            raise ValueError(
                f"allowed_output_paths run_id={sorted(run_ids)!r} does "
                f"not match request run_id={request_run_id!r}"
            )
    # Inject only the canonical placements derived from listed paths — see
    # `_canonical_mcp_audit_log_paths` for the strict per-phase shapes.
    # `_resolved_build_system` is an internal request_payload field
    # populated by record_launch (resolved from spec.ir.yaml.impl_defaults) so the
    # helper can gate cross-phase canonical placement on `build_system=make`.
    # Tests calling this helper directly may set the field explicitly when
    # cross-phase semantics are exercised; absence simply disables
    # cross-phase auto-inject (in-phase canonical still applies).
    _bs_raw = request_payload.get("_resolved_build_system")
    _bs_norm = (
        _bs_raw.strip().lower()
        if isinstance(_bs_raw, str) and _bs_raw.strip()
        else ""
    )
    canonical_logs = _canonical_mcp_audit_log_paths(
        step_token=step_token,
        pipeline_ref=pipeline_ref,
        node_safe=node_safe,
        source_id=source_id,
        listed_paths=list(allowed),
        build_system=_bs_norm,
        substep_token=substep_token,
    )
    canonical_logs_set: set[str] = set(canonical_logs)
    for log_path in canonical_logs:
        if log_path not in allowed:
            allowed.append(log_path)

    # Mandatory build-control file pins (e.g. Make's in-source Makefile). A bare
    # `src/` directory allowlist entry covers source extensions (.f90/.c) via
    # guarded-apply-patch but NOT the extensionless `Makefile`, which is
    # intentionally excluded from the directory-allowlist source-extension set
    # (tools/hooks/common.py) and would therefore be unwritable through every
    # channel. Inject the explicit file pin so it is authorized as an output and
    # — via `_allowed_file_tool_paths_for_launch` auto-derive — Edit/Write
    # eligible. See `_mandatory_file_tool_pins_for_launch`.
    for mandatory_pin in _mandatory_file_tool_pins_for_launch(request_payload, allowed):
        if mandatory_pin not in allowed:
            allowed.append(mandatory_pin)

    # Mandatory canonical phase outputs (e.g. Validate's snapshot_schema.json)
    # whose names are not covered by a directory allowlist entry. Inject so the
    # executor's write is authorized rather than failing post_execute and forcing
    # a restart. The _matches_phase_contract pass below validates placement.
    for mandatory_output in _mandatory_phase_outputs_for_launch(request_payload, allowed):
        if mandatory_output not in allowed:
            allowed.append(mandatory_output)

    for idx, path in enumerate(allowed):
        if path in canonical_logs_set:
            # Canonical MCP-owned audit logs are pre-validated against
            # canonical phase placements (including legitimate cross-phase
            # placements like Execute's run_quality_checks log under
            # generate/<gen>/src/). Skip the capability write_roots check
            # because the cross-phase placement legitimately falls outside
            # the step's write_roots, and rely on phase contract +
            # multi-layer integrity protection instead.
            if not _matches_phase_contract(path):
                raise ValueError(
                    f"allowed_output_paths[{idx}] is outside phase contract outputs for step={step_token!r}: {path!r}"
                )
            continue
        if normalized_roots and not any(_repo_path_under_prefix(path, root) for root in normalized_roots):
            raise ValueError(
                f"allowed_output_paths[{idx}] must be under capability write_roots: {path!r}"
            )
        if not _matches_phase_contract(path):
            raise ValueError(
                f"allowed_output_paths[{idx}] is outside phase contract outputs for step={step_token!r}: {path!r}"
            )
    deduped: list[str] = []
    seen: set[str] = set()
    for path in allowed:
        if path in seen:
            continue
        deduped.append(path)
        seen.add(path)
    if not deduped:
        raise ValueError("allowed_output_paths must be non-empty for step/substep agents")
    return deduped


# Extension classification for write-path policy:
# `.json` / `.txt` outputs must go through `guarded-apply-patch` CLI for
# audit/integrity, while other artifact extensions (yaml, md, source code,
# etc.) are written via the LLM `Edit` / `Write` tools directly.
CLI_MANAGED_EXTENSIONS: frozenset[str] = frozenset({".json", ".txt"})

# Integrity-protected audit logs written exclusively by MCP tools as evidence
# of tool execution. `validate_pipeline_semantics.py` reads these files and
# trusts their JSONL records (e.g. `tool_name`, `ok`, `command`) as the source
# of truth that an MCP tool actually ran. Direct `Edit` / `Write` access by
# child agents would let them forge successful runs, so canonical placements
# (computed by `_canonical_mcp_audit_log_paths()`) are excluded from
# `allowed_file_tool_paths` and rejected from `guarded-apply-patch`.
#
# Protection is scoped to canonical placements only — a non-canonical file
# that happens to share this basename (e.g. an unrelated source asset under a
# nested subdirectory) is treated as a normal file. This avoids both
# (a) over-trusting any manifest entry whose basename happens to match
# (b) over-blocking legitimate project files with this name.
_MCP_AUDIT_LOG_BASENAME: str = "mcp_command_log.jsonl"


def _canonical_mcp_audit_log_paths(
    *,
    step_token: str,
    pipeline_ref: str,
    node_safe: str,
    listed_paths: Sequence[str],
    source_id: str = "",
    build_system: str = "",
    substep_token: str = "",
) -> list[str]:
    """Derive canonical MCP audit log paths from the listed allowed_output_paths.

    Canonical placements (per docs/workflow/phases/phase_*.md and
    skills/workflow-validate-execute/SKILL.md):
      - generate: `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl`
      - build:    `<pipeline_ref>/binary/<binary_id>/mcp_command_log.jsonl`
      - validate.execute (in-phase): `<pipeline_ref>/runs/<run_id>/<node_safe>/mcp_command_log.jsonl`
      - validate.execute (cross-phase quality_check): `<pipeline_ref>/source/<source_id>/src/mcp_command_log.jsonl`
        — `run_quality_checks` runs with `project_dir=source/<source_id>/src/`
        for `toolchain.build_system=make` + Fortran/C-family pipelines per
        `skills/workflow-validate-execute/SKILL.md`, so the MCP server's
        default `command_log_path` (resolved as
        `project_dir/mcp_command_log.jsonl`) lands in the source tree even
        though the substep is `validate.execute`.

    Only paths matching these structures are returned. A path like
    `<source_id>/src/notes/mcp_command_log.jsonl` is **not** canonical and is
    treated as a normal file (writable subject to the usual file-tool /
    apply-patch rules).
    """
    if not pipeline_ref:
        return []
    canonical: set[str] = set()
    if step_token == "generate":
        prefix = f"{pipeline_ref}/source/"
        for tok in listed_paths:
            if not isinstance(tok, str) or not tok.startswith(prefix):
                continue
            tail = tok[len(prefix):]
            parts = [p for p in tail.split("/") if p]
            # Canonical: <source_id>/src/...
            if len(parts) >= 2 and parts[1] == "src":
                canonical.add(f"{prefix}{parts[0]}/src/{_MCP_AUDIT_LOG_BASENAME}")
    elif step_token == "build":
        prefix = f"{pipeline_ref}/binary/"
        for tok in listed_paths:
            if not isinstance(tok, str) or not tok.startswith(prefix):
                continue
            tail = tok[len(prefix):]
            parts = [p for p in tail.split("/") if p]
            # Canonical (in-phase, e.g. CMake/Meson out-of-source builds with
            # project_dir=<binary_id>/): <binary_id>/mcp_command_log.jsonl
            # alongside binary_meta.json.
            if parts:
                canonical.add(f"{prefix}{parts[0]}/{_MCP_AUDIT_LOG_BASENAME}")
        # Cross-phase placement is reserved for in-source Make builds
        # (Fortran/C-family per skills/workflow-build): compile_project runs
        # with `project_dir=<pipeline>/source/<source_id>/src/` (where the
        # Makefile lives), so the MCP server's default command_log_path
        # resolves under the source tree. Gate on `build_system=make` —
        # CMake/Meson/Ninja and other out-of-source toolchains do not
        # legitimately write into the source tree, and unconditional
        # cross-phase authorization would let those phases mutate source
        # provenance.
        if source_id and build_system == "make":
            gen_prefix = f"{pipeline_ref}/source/"
            canonical.add(
                f"{gen_prefix}{source_id}/src/{_MCP_AUDIT_LOG_BASENAME}"
            )
    elif step_token == "validate" and substep_token == "execute" and node_safe:
        # Only the Validate.execute substep emits MCP command logs (run_program
        # / run_quality_checks). Validate.judge runs without MCP, so no
        # auto-injection — adding mcp_command_log.jsonl would later fail
        # phase contract validation since judge does not list it as an allowed
        # output filename.
        prefix = f"{pipeline_ref}/runs/"
        for tok in listed_paths:
            if not isinstance(tok, str) or not tok.startswith(prefix):
                continue
            tail = tok[len(prefix):]
            parts = [p for p in tail.split("/") if p]
            # Canonical (in-phase): <run_id>/<node_safe>/mcp_command_log.jsonl
            if len(parts) >= 2 and parts[1] == node_safe:
                canonical.add(
                    f"{prefix}{parts[0]}/{node_safe}/{_MCP_AUDIT_LOG_BASENAME}"
                )
        # Cross-phase quality_check log placement: derive ONLY from the
        # explicit `source_id` field AND only when the toolchain is
        # `build_system=make` (the documented Make-only exception per
        # skills/workflow-validate-execute/SKILL.md). For non-Make runs
        # (run_program against a CMake/Meson out-of-source binary), the log
        # belongs in-phase and cross-phase authorization must not be
        # granted — otherwise a child could steer MCP logging into the
        # source tree and contaminate verified provenance files.
        if source_id and build_system == "make":
            gen_prefix = f"{pipeline_ref}/source/"
            canonical.add(
                f"{gen_prefix}{source_id}/src/{_MCP_AUDIT_LOG_BASENAME}"
            )
    return sorted(canonical)


def _canonical_mcp_audit_log_paths_for_request(
    request_payload: dict[str, Any],
    allowed_output_paths: Sequence[str],
    *,
    repo_root: Path | None = None,
) -> list[str]:
    step_token = str(request_payload.get("step") or "").strip().lower()
    pipeline_ref = _normalize_rel_posix(str(request_payload.get("pipeline_ref") or ""))
    node_key = str(request_payload.get("node_key") or "").strip()
    node_safe = _node_key_to_safe(node_key) if node_key else ""
    source_id = str(request_payload.get("source_id") or "").strip()
    # Resolve toolchain.build_system, preferring (1) explicit
    # `_resolved_build_system` in request_payload (set by record_launch from
    # spec.ir.yaml.impl_defaults), then (2) reading spec.ir.yaml.impl_defaults directly
    # when repo_root is provided. Without either, build_system="" which
    # disables cross-phase canonical placement (Make-only exception).
    build_system = ""
    bs_pre = request_payload.get("_resolved_build_system")
    if isinstance(bs_pre, str) and bs_pre.strip():
        build_system = bs_pre.strip().lower()
    elif repo_root is not None:
        ir_ref = str(request_payload.get("ir_ref") or "").strip()
        if ir_ref:
            bs = _impl_resolved_build_system(repo_root, ir_ref)
            if isinstance(bs, str) and bs.strip():
                build_system = bs.strip().lower()
    substep_token = str(request_payload.get("substep") or "").strip().lower()
    return _canonical_mcp_audit_log_paths(
        step_token=step_token,
        pipeline_ref=pipeline_ref,
        node_safe=node_safe,
        source_id=source_id,
        listed_paths=list(allowed_output_paths),
        build_system=build_system,
        substep_token=substep_token,
    )


def _is_direct_write_path(rel_posix: str) -> bool:
    """Return True when the path may be written via direct Edit/Write tools.

    Paths whose extension belongs to ``CLI_MANAGED_EXTENSIONS`` (e.g. `.json`,
    `.txt`) are required to go through `guarded-apply-patch` and are therefore
    excluded from direct write. Integrity protection of canonical MCP audit
    logs is enforced separately by the caller (see
    `_canonical_mcp_audit_log_paths`); this helper is purely
    extension-classification.
    """
    token = _normalize_rel_posix(rel_posix)
    if not token:
        return False
    last = token.rsplit("/", 1)[-1]
    if "." not in last:
        return True
    ext = "." + last.rsplit(".", 1)[-1].lower()
    return ext not in CLI_MANAGED_EXTENSIONS


def _allowed_file_tool_paths_for_launch(
    *,
    request_payload: dict[str, Any],
    allowed_output_paths: Sequence[str],
) -> list[str]:
    raw = request_payload.get("allowed_file_tool_paths")
    # Exclude directory entries (trailing "/") from allowed_set: _normalize_rel_posix strips the
    # slash, which would make directory paths appear extension-free and pass _is_direct_write_path.
    allowed_set = {
        _normalize_rel_posix(str(item))
        for item in allowed_output_paths
        if isinstance(item, str) and item.strip() and not item.strip().endswith("/")
    }
    # Canonical MCP audit log paths are MCP-owned and integrity-protected:
    # exclude them from direct file-tool writes regardless of their extension.
    # Non-canonical files that happen to share the basename are treated as
    # ordinary outputs and remain Edit/Write-eligible.
    canonical_log_set = set(
        _canonical_mcp_audit_log_paths_for_request(request_payload, list(allowed_output_paths))
    )
    if raw is None:
        # Auto-derive: every output path whose extension is not CLI-managed and
        # is not a canonical MCP audit log is permitted to be written via
        # direct Edit/Write tools.
        derived = {
            path
            for path in allowed_set
            if path
            and path not in canonical_log_set
            and _is_direct_write_path(path)
        }
        result = sorted(derived)
        _assert_mandatory_file_tool_pins_present(
            request_payload=request_payload,
            allowed_output_paths=allowed_output_paths,
            allowed_file_tool_paths=result,
        )
        return result
    if not isinstance(raw, list):
        raise ValueError("allowed_file_tool_paths must be a list when provided")
    normalized: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"allowed_file_tool_paths[{idx}] must be non-empty string")
        item_token = item.strip().replace("\\", "/")
        if item_token.endswith("/"):
            raise ValueError(f"allowed_file_tool_paths[{idx}] must be file path: {item!r}")
        path = _normalize_rel_posix(item_token)
        if path in canonical_log_set:
            raise ValueError(
                f"allowed_file_tool_paths[{idx}] must not include canonical MCP audit "
                f"log path: {path!r} (written exclusively by MCP tooling)"
            )
        if not _is_direct_write_path(path):
            raise ValueError(
                f"allowed_file_tool_paths[{idx}] must not include CLI-managed extensions "
                f"{sorted(CLI_MANAGED_EXTENSIONS)!r}: {path!r}"
            )
        if path not in allowed_set:
            raise ValueError(
                f"allowed_file_tool_paths[{idx}] must be included in allowed_output_paths: {path!r}"
            )
        normalized.append(path)
    result = sorted(set(normalized))
    _assert_mandatory_file_tool_pins_present(
        request_payload=request_payload,
        allowed_output_paths=allowed_output_paths,
        allowed_file_tool_paths=result,
    )
    return result


def _mandatory_file_tool_pins_for_launch(
    request_payload: dict[str, Any],
    allowed_output_paths: Sequence[str],
) -> list[str]:
    """Build-control file pins that MUST be Edit/Write-eligible for the launch.

    For Make-based Generate launches the in-source ``Makefile`` is mandatory
    (make + Fortran/C needs the ``test`` / ``check`` targets) yet is an
    extensionless build-control name intentionally excluded from the
    directory-allowlist source-extension set (``tools/hooks/common.py``). A bare
    ``<pipeline_ref>/source/<source_id>/src/`` directory entry therefore leaves
    it unwritable through every channel, so it must be pinned explicitly.

    ``build_system`` is resolved from ``_resolved_build_system`` (populated by
    ``record_launch`` from ``spec.ir.yaml.impl_defaults``). Absence disables the
    requirement, so direct callers / non-Make toolchains are unaffected.
    """
    step_token = str(request_payload.get("step") or "").strip().lower()
    substep_token = str(request_payload.get("substep") or "").strip().lower()
    pipeline_ref = _normalize_rel_posix(str(request_payload.get("pipeline_ref") or ""))
    # Only the source-generating launch needs the Makefile. The Generate.verify
    # substep inspects src/ and writes source_meta.json — granting it Makefile
    # (build-control) write authority would let the verifier mutate the very
    # artifact it is supposed to judge, so it must be excluded.
    if step_token != "generate" or substep_token == "verify" or not pipeline_ref:
        return []
    bs_raw = request_payload.get("_resolved_build_system")
    bs_norm = bs_raw.strip().lower() if isinstance(bs_raw, str) and bs_raw.strip() else ""
    if bs_norm != "make":
        return []
    generate_prefix = f"{pipeline_ref}/source/"
    source_id = str(request_payload.get("source_id") or "").strip()
    if not source_id:
        # Single-namespace enforcement guarantees at most one source_id under
        # the generate prefix; derive it from the listed paths when the request
        # does not carry an explicit `source_id` field.
        gen_ids: set[str] = set()
        for item in allowed_output_paths:
            if not isinstance(item, str):
                continue
            tok = _normalize_rel_posix(item)
            if not tok.startswith(generate_prefix):
                continue
            tail = tok[len(generate_prefix):]
            parts = [s for s in tail.split("/") if s]
            if parts:
                gen_ids.add(parts[0])
        if len(gen_ids) == 1:
            source_id = next(iter(gen_ids))
    if not source_id:
        return []
    return [f"{generate_prefix}{source_id}/src/Makefile"]


def _mandatory_phase_outputs_for_launch(
    request_payload: dict[str, Any],
    allowed_output_paths: Sequence[str],
) -> list[str]:
    """Canonical phase outputs that MUST be pre-authorized for the launch.

    The Validate ``post_execute`` gate (``tools/validate_pipeline_semantics.py``)
    requires the snapshot schema at
    ``<pipeline_ref>/runs/<run_id>/<node_safe>/raw/state_snapshots/snapshot_schema.json``
    whenever the spec mandates state-snapshot evidence. Its ``.json`` name is not
    covered by a bare ``raw/state_snapshots/`` directory allowlist entry (the
    directory-allowlist source-extension set excludes it, same reason the
    ``Makefile`` needs an explicit pin), so an executor that writes it without
    listing it gets rejected with ``allowed_output_paths manifest violation`` and
    the orchestration restarts. Pre-authorizing the canonical path is harmless
    when the spec does not require snapshots (the file is simply never written),
    so we inject it for the Validate.execute substep and let the
    ``_matches_phase_contract`` check below confirm it stays in-contract.

    Restricted to the ``execute`` substep: only execute writes ``raw/`` evidence,
    and the ``judge`` contract rejects ``raw/`` paths — injecting there would make
    ``_matches_phase_contract`` raise. Mirrors
    ``_mandatory_file_tool_pins_for_launch``: returns paths to be merged into
    ``allowed`` only when missing; never raises.
    """
    step_token = str(request_payload.get("step") or "").strip().lower()
    substep_token = str(request_payload.get("substep") or "").strip().lower()
    pipeline_ref = _normalize_rel_posix(str(request_payload.get("pipeline_ref") or ""))
    # Generate.generate authors the pipeline ``lineage.json`` (its absence is a
    # ``post_generate`` fail). Pre-authorize the canonical ``<pipeline_ref>/lineage.json``
    # placement so an orchestration that omits it from ``allowed_output_paths`` cannot
    # block the child from writing it and stall Generate.verify on ``post_generate``
    # (audit: orch_20260615T095217Z_74450292 lost ~5 child re-launches to this). The
    # generate phase contract already permits this path; ``_matches_phase_contract``
    # confirms it stays in-contract. Restricted to the ``generate`` substep — ``verify``
    # only reads lineage.json.
    if step_token == "generate" and substep_token == "generate" and pipeline_ref:
        return [f"{pipeline_ref}/lineage.json"]
    if step_token != "validate" or substep_token != "execute" or not pipeline_ref:
        return []
    node_key = str(request_payload.get("node_key") or "").strip()
    node_safe = _node_key_to_safe(node_key) if node_key else ""
    if not node_safe:
        return []
    validate_prefix = f"{pipeline_ref}/runs/"
    run_id = str(request_payload.get("run_id") or "").strip()
    if not run_id:
        # Single-namespace enforcement guarantees at most one run_id under the
        # validate prefix; derive it from the listed paths when the request does
        # not carry an explicit `run_id` field.
        run_ids: set[str] = set()
        for item in allowed_output_paths:
            if not isinstance(item, str):
                continue
            tok = _normalize_rel_posix(item)
            if not tok.startswith(validate_prefix):
                continue
            tail = tok[len(validate_prefix):]
            parts = [s for s in tail.split("/") if s]
            if parts:
                run_ids.add(parts[0])
        if len(run_ids) == 1:
            run_id = next(iter(run_ids))
    if not run_id:
        # run_id not determinable → inject nothing and preserve the loud
        # downstream failure rather than guessing a placement.
        return []
    return [
        f"{validate_prefix}{run_id}/{node_safe}/raw/state_snapshots/snapshot_schema.json"
    ]


def _assert_mandatory_file_tool_pins_present(
    *,
    request_payload: dict[str, Any],
    allowed_output_paths: Sequence[str],
    allowed_file_tool_paths: Sequence[str],
) -> None:
    """Fail the launch (before the child spawns) when a mandatory build-control
    file pin is absent from the effective ``allowed_file_tool_paths``.

    This converts the otherwise mid-run, artifact-corrupting fail-stop (a child
    discovering it cannot write the Makefile and aborting) into a cheap,
    recoverable launch-time ``ValueError``. The common case (auto-derived
    file-tool paths) is already satisfied by the Fix-1 injection in
    ``_allowed_output_paths_for_launch``; this guard catches the case where the
    caller passes an explicit ``allowed_file_tool_paths`` list that omits the
    pin.
    """
    mandatory = _mandatory_file_tool_pins_for_launch(
        request_payload, allowed_output_paths
    )
    if not mandatory:
        return
    present = {_normalize_rel_posix(str(p)) for p in allowed_file_tool_paths}
    missing = [pin for pin in mandatory if _normalize_rel_posix(pin) not in present]
    if missing:
        raise ValueError(
            "allowed_file_tool_paths is missing mandatory build-control file "
            f"pin(s) {missing!r} required for this launch. Make-based Generate "
            "needs the in-source Makefile to be Edit/Write-eligible, but a bare "
            "src/ directory entry is insufficient (the extensionless Makefile is "
            "excluded from the directory-allowlist source-extension set). "
            "Remediation: add the Makefile path to allowed_output_paths and "
            "allowed_file_tool_paths, or omit an explicit allowed_file_tool_paths "
            "so record-launch auto-derives it."
        )


def _validate_child_write_contract_preflight(
    *,
    request_payload: dict[str, Any],
    capability_doc: dict[str, Any],
    allowed_output_paths: Sequence[str],
) -> None:
    role = str(request_payload.get("agent_role") or "").strip().lower()
    if role not in {"step", "substep"}:
        return
    cap_token = str(capability_doc.get("capability_token") or "").strip()
    if not cap_token:
        raise ValueError("child_write_contract_preflight: capability_token must be non-empty")
    roots_obj = capability_doc.get("write_roots")
    if not isinstance(roots_obj, list):
        raise ValueError("child_write_contract_preflight: capability write_roots must be list")
    roots = [_normalize_rel_posix(str(item)) for item in roots_obj if isinstance(item, str) and item.strip()]
    allowed = [_normalize_rel_posix(str(item)) for item in allowed_output_paths if isinstance(item, str) and item.strip()]
    if not allowed:
        raise ValueError("child_write_contract_preflight: allowed_output_paths must be non-empty")
    canonical_logs_set = set(
        _canonical_mcp_audit_log_paths_for_request(request_payload, allowed_output_paths)
    )
    for idx, path in enumerate(allowed):
        if path.endswith("/"):
            # Directory allowlist entry — check it is under a write root (using the dir path itself).
            if roots and not any(_repo_path_under_prefix(path, root) for root in roots):
                raise ValueError(
                    "child_write_contract_preflight: allowed_output_paths directory entry must be under "
                    f"capability write_roots: {path!r}"
                )
            continue
        if path in canonical_logs_set:
            # Canonical MCP-owned audit logs may legitimately fall outside
            # capability write_roots (e.g. Execute's run_quality_checks log
            # under generate/<gen>/src/). Phase contract pre-validates the
            # placement; multi-layer integrity protection prevents agent
            # mutation, so the cross-phase write is safe.
            continue
        if roots and not any(_repo_path_under_prefix(path, root) for root in roots):
            raise ValueError(
                "child_write_contract_preflight: allowed_output_path must be under capability write_roots: "
                f"{path!r}"
            )



def _with_trailing_slash(rel_posix: str) -> str:
    if not rel_posix:
        return ""
    return rel_posix if rel_posix.endswith("/") else rel_posix + "/"


def _repo_path_under_prefix(rel_posix: str, prefix_rel: str) -> bool:
    p = _normalize_rel_posix(rel_posix)
    base = _normalize_rel_posix(prefix_rel)
    if not base:
        return False
    return p == base or p.startswith(base + "/")


def build_access_policy_payload(
    *,
    agent_run_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the content of `access_policies/<agent_run_id>.json`."""
    node_key = request_payload.get("node_key")
    step = request_payload.get("step")
    if not isinstance(node_key, str) or not node_key.strip():
        raise ValueError("access policy requires node_key")
    if not isinstance(step, str) or not step.strip():
        raise ValueError("access policy requires step")
    ir_ref = request_payload.get("ir_ref")
    pipeline_ref = request_payload.get("pipeline_ref")
    if not isinstance(ir_ref, str) or not ir_ref.strip():
        raise ValueError("access policy requires ir_ref")
    if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
        raise ValueError("access policy requires pipeline_ref")

    allowed_read_roots = [
        "docs/",
        "spec/",
        _with_trailing_slash(_normalize_rel_posix(f"workspace/tmp/{agent_run_id.strip()}")),
        _with_trailing_slash(_normalize_rel_posix(ir_ref)),
        _with_trailing_slash(_normalize_rel_posix(pipeline_ref)),
    ]
    skill_must_read_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))
    skill_ref = request_payload.get("skill_ref")
    if isinstance(skill_ref, str) and skill_ref.strip():
        skill_must_read_refs = _merge_unique_refs([skill_ref.strip()], skill_must_read_refs)
    skill_allowed_roots = [
        _with_trailing_slash(_normalize_rel_posix(ref))
        for ref in skill_must_read_refs
        if isinstance(ref, str) and ref.strip()
    ]
    allowed_read_roots = _merge_unique_refs(allowed_read_roots, skill_allowed_roots)
    orchestration_id_val = str(request_payload.get("orchestration_id", "")).strip()
    if orchestration_id_val:
        cap_file = (
            f"workspace/orchestrations/{orchestration_id_val}"
            f"/capabilities/{agent_run_id}.json"
        )
        allowed_read_roots = _merge_unique_refs(allowed_read_roots, [cap_file])
    body: dict[str, Any] = {
        "agent_run_id": agent_run_id.strip(),
        "node_key": node_key.strip(),
        "step": step.strip().lower(),
        "allowed_read_roots": allowed_read_roots,
        "denied_read_roots": ["tools/"],
        "allowed_gate_services": list(DEFAULT_ALLOWED_GATE_SERVICES),
    }
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        body["substep"] = substep.strip().lower()
    return body


def _write_access_policy_for_launch(
    repo_root: Path,
    orchestration_id: str,
    child_agent_run_id: str,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    policy = build_access_policy_payload(agent_run_id=child_agent_run_id, request_payload=request_payload)
    out = _access_policies_dir(repo_root, orchestration_id) / f"{child_agent_run_id}.json"
    _write_json(out, policy)
    return policy


def _read_access_manifest_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> Path:
    return _read_manifests_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"


def _write_read_access_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
    allowed_read_roots: Sequence[str],
    denied_read_roots: Sequence[str],
) -> str:
    payload = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id.strip(),
        "allowed_read_roots": [
            _with_trailing_slash(_normalize_rel_posix(p))
            for p in allowed_read_roots
            if isinstance(p, str) and p.strip()
        ],
        "denied_read_roots": [
            _with_trailing_slash(_normalize_rel_posix(p))
            for p in denied_read_roots
            if isinstance(p, str) and p.strip()
        ],
        "generated_at": _utc_now_iso(),
    }
    out = _read_access_manifest_path(repo_root, orchestration_id, agent_run_id=agent_run_id)
    _write_json(out, payload)
    return f"workspace/orchestrations/{orchestration_id}/read_manifests/{agent_run_id.strip()}.json"


def _load_read_access_manifest(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> dict[str, Any]:
    path = _read_access_manifest_path(repo_root, orchestration_id, agent_run_id=agent_run_id)
    if not path.exists():
        raise FileNotFoundError(f"read access manifest not found: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"read access manifest must be object: {path}")
    return payload


def _runtime_ro_bind_paths() -> list[str]:
    runtime_paths = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
    return [p for p in runtime_paths if Path(p).exists()]


def _safe_host_env_for_child() -> dict[str, str]:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME")
    body: dict[str, str] = {}
    for key in allowed:
        value = os.environ.get(key)
        if isinstance(value, str) and value:
            body[key] = value
    body.setdefault("PATH", "/usr/bin:/bin")
    return body


def build_bwrap_profile(
    *,
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    backend_command: str,
) -> dict[str, Any]:
    read_manifest = _load_read_access_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
    )
    cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{agent_run_id}.json"
    if not cap_path.exists():
        raise ValueError(f"capability file not found: {cap_path}")
    cap_payload = _read_json(cap_path)
    if not isinstance(cap_payload, dict):
        raise ValueError(f"capability file must be object: {cap_path}")
    reads_obj = read_manifest.get("allowed_read_roots")
    if not isinstance(reads_obj, list):
        raise ValueError("read manifest must include allowed_read_roots list")
    writes_obj = cap_payload.get("write_roots")
    if not isinstance(writes_obj, list):
        raise ValueError("capability must include write_roots list")
    read_roots = sorted(
        {
            _normalize_rel_posix(str(p))
            for p in reads_obj
            if isinstance(p, str) and _normalize_rel_posix(str(p))
        }
    )
    write_roots = _load_write_roots_from_cap(writes_obj)
    resolved_repo_root = repo_root.resolve()
    created_file_pin_stubs: list[dict[str, Any]] = []
    for root_entry in write_roots:
        if root_entry.endswith("/"):
            candidate = (repo_root / root_entry.rstrip("/")).resolve()
            try:
                candidate.relative_to(resolved_repo_root)
            except ValueError:
                raise ValueError(
                    f"write_roots entry {root_entry!r} resolves outside repo_root "
                    f"({candidate} is not under {resolved_repo_root})"
                )
            candidate.mkdir(parents=True, exist_ok=True)
        else:
            # File pin: pre-create so bwrap can --bind it at file granularity.
            # File-level bind ensures bwrap cannot write to sibling files/directories —
            # the sandbox boundary is exactly the declared pin, nothing broader.
            # The stub is created empty here; _cleanup_empty_file_pin_stubs removes it
            # if the agent terminates without writing to it.
            pin_path = (repo_root / root_entry).resolve()
            try:
                pin_path.relative_to(resolved_repo_root)
            except ValueError:
                raise ValueError(
                    f"write_roots entry {root_entry!r} resolves outside repo_root "
                    f"({pin_path} is not under {resolved_repo_root})"
                )
            pin_path.parent.mkdir(parents=True, exist_ok=True)
            # Check the original (unresolved) path for symlinks — resolve() follows
            # symlinks so is_symlink() on the resolved path is always False.
            orig_pin_path = repo_root / _normalize_rel_posix(root_entry)
            if orig_pin_path.is_symlink():
                raise ValueError(
                    f"write_roots file pin {root_entry!r} is a symlink ({orig_pin_path}); "
                    f"only regular files are permitted as file pins"
                )
            if pin_path.exists():
                # Reject if the resolved path is a directory — binding it via bwrap
                # would expose the entire subtree as writable.
                if pin_path.is_dir():
                    raise ValueError(
                        f"write_roots file pin {root_entry!r} resolves to a directory ({pin_path}); "
                        f"add a trailing '/' to declare a directory write root instead"
                    )
            else:
                pin_path.touch()
                # Record path + mtime_ns so cleanup can distinguish an untouched stub
                # from a legitimately empty file written by a subprocess after touch().
                created_file_pin_stubs.append({
                    "path": _normalize_rel_posix(root_entry),
                    "mtime_ns": pin_path.stat().st_mtime_ns,
                })
    sandbox_root = _orchestration_root(repo_root, orchestration_id) / "sandboxes" / agent_run_id
    tmp_root = sandbox_root / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workspace_tmp_host = (repo_root / "workspace" / "tmp" / agent_run_id).resolve()
    workspace_tmp_host.mkdir(parents=True, exist_ok=True)
    child_env = _safe_host_env_for_child()
    child_env["TMPDIR"] = str(workspace_tmp_host)
    return {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "sandbox_runtime": "bwrap",
        "backend_command": backend_command.strip(),
        "repo_root": str(repo_root),
        "read_roots": read_roots,
        "write_roots": write_roots,
        "runtime_ro_bind_paths": _runtime_ro_bind_paths(),
        "tmp_dir": str(tmp_root),
        "workspace_tmp_rw_abs": str(workspace_tmp_host),
        "workdir": str(repo_root),
        "env": child_env,
        "generated_at": _utc_now_iso(),
        "created_file_pin_stubs": created_file_pin_stubs,
    }


def render_bwrap_command(
    *,
    profile: dict[str, Any],
    command_argv: Sequence[str],
) -> list[str]:
    if not command_argv:
        raise ValueError("command_argv must be non-empty")
    repo_root = str(profile.get("repo_root") or "").strip()
    tmp_dir = str(profile.get("tmp_dir") or "").strip()
    if not repo_root or not tmp_dir:
        raise ValueError("profile must include repo_root and tmp_dir")
    cmd: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        repo_root,
    ]
    for item in profile.get("runtime_ro_bind_paths", []):
        if isinstance(item, str) and item.strip():
            cmd.extend(["--ro-bind", item.strip(), item.strip()])
    cmd.extend(["--ro-bind", repo_root, repo_root])
    for rel in profile.get("read_roots", []):
        if not isinstance(rel, str) or not rel.strip():
            continue
        abs_path = (Path(repo_root) / _normalize_rel_posix(rel)).resolve()
        if abs_path.exists():
            abs_token = str(abs_path)
            cmd.extend(["--ro-bind", abs_token, abs_token])
    for rel in profile.get("write_roots", []):
        if not isinstance(rel, str) or not rel.strip():
            continue
        abs_path = (Path(repo_root) / _normalize_rel_posix(rel)).resolve()
        if not abs_path.exists():
            # File pins must be pre-created by build_bwrap_profile before render.
            if not rel.strip().endswith("/"):
                raise ValueError(
                    f"write_roots file pin {rel!r} does not exist; "
                    f"build_bwrap_profile must pre-create it before render_bwrap_command is called"
                )
            continue
        # File pins must be plain regular files — not directories or symlinks.
        # Binding a directory would make the entire subtree writable, not just one file.
        # Check the original (unresolved) path for symlinks: resolve() follows symlinks
        # so is_symlink() on abs_path (resolved) is always False.
        if not rel.strip().endswith("/"):
            orig_path = Path(repo_root) / _normalize_rel_posix(rel)
            if orig_path.is_symlink():
                raise ValueError(
                    f"write_roots file pin {rel!r} is a symlink ({orig_path}); "
                    f"only regular files are permitted as file pins"
                )
            if not abs_path.is_file():
                raise ValueError(
                    f"write_roots file pin {rel!r} resolves to a non-file ({abs_path}); "
                    f"add a trailing '/' to declare a directory write root instead"
                )
        abs_token = str(abs_path)
        cmd.extend(["--bind", abs_token, abs_token])
    ws_rw = str(profile.get("workspace_tmp_rw_abs") or "").strip()
    if not ws_rw:
        raise ValueError("profile must include workspace_tmp_rw_abs")
    ws_path = Path(ws_rw)
    if not ws_path.is_dir():
        raise ValueError(f"workspace_tmp_rw_abs must be existing directory: {ws_rw}")
    ws_abs = str(ws_path.resolve())
    cmd.extend(["--bind", ws_abs, ws_abs])
    cmd.extend(["--setenv", "TMPDIR", ws_abs])
    cmd.extend(["--bind", tmp_dir, tmp_dir])
    cmd.append("--")
    cmd.extend([str(part) for part in command_argv])
    return cmd


def _append_access_log_line(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    entry: dict[str, Any],
) -> None:
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    path = _access_logs_dir(repo_root, orchestration_id) / f"{agent_run_id}.jsonl"
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_orchestration_read(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    read_path: str,
) -> dict[str, Any]:
    """Read audit: on a `denied_read_roots` (`tools/`) match, record `rule_source_violation` and fail the orchestration.

    Return the body only for a permitted read.
    """
    manifest = _load_read_access_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
    )
    denied = manifest.get("denied_read_roots")
    if not isinstance(denied, list):
        denied = []
    allowed = manifest.get("allowed_read_roots")
    if not isinstance(allowed, list):
        allowed = []

    rel = _normalize_rel_posix(read_path)
    hit_denied = False
    matched_prefix: str | None = None
    for item in denied:
        if not isinstance(item, str) or not item.strip():
            continue
        prefix = _with_trailing_slash(_normalize_rel_posix(item))
        if not prefix:
            continue
        base_no_slash = prefix.rstrip("/")
        if _repo_path_under_prefix(rel, base_no_slash):
            hit_denied = True
            matched_prefix = prefix
            break

    matched_allowed_prefix: str | None = None
    hit_allowed = False
    for item in allowed:
        if not isinstance(item, str) or not item.strip():
            continue
        prefix = _with_trailing_slash(_normalize_rel_posix(item))
        if not prefix:
            continue
        base_no_slash = prefix.rstrip("/")
        if _repo_path_under_prefix(rel, base_no_slash):
            hit_allowed = True
            matched_allowed_prefix = prefix
            break

    log_entry = {
        "ts": _utc_now_iso(),
        "path": rel,
        "allowed_match": hit_allowed,
        "matched_allowed_prefix": matched_allowed_prefix,
        "denied_match": hit_denied,
        "matched_denied_prefix": matched_prefix,
    }
    _append_access_log_line(repo_root, orchestration_id, agent_run_id, log_entry)

    abs_path = (repo_root / rel).resolve()
    try:
        abs_path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes repo_root: {read_path!r}") from exc

    if hit_denied or not hit_allowed:
        _write_rule_source_violation(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id.strip(),
            read_path=rel,
            matched_prefix=matched_prefix if hit_denied else None,
        )
        try:
            update_orchestration_status(repo_root, orchestration_id, status="fail")
        except Exception:
            pass
        if hit_denied:
            raise RuntimeError(
                f"orchestration-read denied: path {rel!r} matches rule-source deny list "
                f"(prefix={matched_prefix!r}, agent_run_id={agent_run_id})"
            )
        raise RuntimeError(
            f"orchestration-read denied: path {rel!r} is outside allowed_read_roots "
            f"(agent_run_id={agent_run_id})"
        )

    content: str | None = None
    file_exists = abs_path.is_file()
    if file_exists:
        content = abs_path.read_text(encoding="utf-8")

    return {
        "read_path": rel,
        "file_exists": file_exists,
        "denied_match": False,
        "logged": True,
        "content": content,
    }


def _checkpoint_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "orchestration_checkpoint.json"


def _compute_sha256(path: Path) -> str:
    """Return the file's SHA-256 hash in the form "sha256:<hex>".

    When the file does not exist, return "sha256:missing" (not an error).
    """
    if not path.exists():
        return "sha256:missing"
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _build_artifact_hashes(
    repo_root: Path,
    output_refs: list[str],
) -> dict[str, str]:
    """Resolve each output_refs path relative to repo_root and compute the SHA-256."""
    hashes: dict[str, str] = {}
    for ref in output_refs:
        if not isinstance(ref, str) or not ref.strip():
            continue
        r = ref.strip()
        hashes[r] = _compute_sha256(repo_root / r)
    return hashes


def _run_write_baseline_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> Path:
    if agent_run_id is not None and agent_run_id.strip():
        return (
            _orchestration_root(repo_root, orchestration_id)
            / "agents"
            / agent_run_id.strip()
            / "run_write_baseline.json"
        )
    return _orchestration_root(repo_root, orchestration_id) / "orchestration_run_write_baseline.json"


def _should_ignore_runtime_snapshot_path(
    rel_posix: str,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> bool:
    token = _normalize_rel_posix(rel_posix)
    if not token or token.startswith(".git/"):
        return True
    # Ignore Claude local/runtime settings mutated by system-level hooks.
    if token.startswith(".claude/"):
        return True
    # NOTE: dated archive / backup workspaces at the repo root (`workspace_<date>/`,
    # `workspace_backup_*/`) are deliberately NOT exempted. They bloat the baseline,
    # but exempting them from BOTH the baseline and the terminal diff would blind
    # unauthorized-write validation to any child write under a `workspace_*` path
    # (the diff is the defense-in-depth backstop for an output_manifest_write_guard
    # bypass). Correctness of write validation outranks the snapshot-size win.
    # NOTE: No blanket pyc/__pycache__ exemption here.  PYTHONDONTWRITEBYTECODE=1
    # (set in run_workflow.py) is the primary protection against incidental bytecode
    # writes.  Any explicit bytecode generation (e.g. python3 -m py_compile) is an
    # agent action that SHOULD surface as an unauthorized_write_violation.
    orch_root = _normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}")
    runtime_prefixes = (
        f"{orch_root}/access_logs/",
        f"{orch_root}/access_policies/",
        # Adv-16: per-arid active-child markers managed by record_launch /
        # deactivate_child / record_agent_run terminal — orchestration runtime
        # writes only, never authored by child agents.
        f"{orch_root}/active_children/",
        # Adv-20: per-arid child-return ack markers.
        f"{orch_root}/child_returns/",
        # Adv-35: per-arid cleanup-committed markers (two-phase finalization).
        f"{orch_root}/cleanup_committed/",
        f"{orch_root}/agents/",
        f"{orch_root}/capabilities/",
        f"{orch_root}/gates/",
        f"{orch_root}/hooks/",
        f"{orch_root}/launches/",
        f"{orch_root}/output_manifests/",
        f"{orch_root}/read_manifests/",
        f"{orch_root}/sandbox_profiles/",
        f"{orch_root}/sandboxes/",
        f"{orch_root}/violations/",
        f"{orch_root}/steps/",
        f"{orch_root}/reservations/",
    )
    if any(token.startswith(prefix) for prefix in runtime_prefixes):
        return True
    # `failure_analysis.runtime.<uuid12>.json` safety-net sidecar (LEGACY: written
    # by the removed LLM-orchestrator launch path's `_write_failure_analysis`; the
    # conductor never emits it, so this exemption only matters when reading older
    # on-disk runs). It was emitted by the outer run_workflow process — never by a
    # child agent, which cannot reach the path through tool hooks
    # (`output_manifest_write_guard`). The write could land after an interrupted
    # child's launch baseline is captured but before `record-timeout` terminalizes
    # it; without this exemption the sidecar shows up in that child's terminal-diff
    # and is misattributed as an unauthorized_write_violation, dead-locking
    # terminalization. Same runtime-owned rationale as `agent_runs_invalid.jsonl`
    # below; intentionally narrow — only the UUID-suffixed runtime sidecar, NOT the
    # canonical `failure_analysis.json`.
    if re.fullmatch(
        rf"{re.escape(orch_root)}/failure_analysis\.runtime\.[0-9a-f]{{12}}\.json",
        token,
    ):
        return True
    # `launch_incident.runtime.<uuid12>.json` (LEGACY: written by the removed
    # LLM-orchestrator launch path's dangling-active_child capture; the conductor
    # never emits it, so this exemption only matters when reading older on-disk
    # runs). Same runtime-owned rationale as the failure_analysis sidecar above:
    # emitted only by the outer run_workflow process, never by a child agent, and it
    # could land while the dangling child's launch baseline is still captured —
    # without this exemption it would surface in that child's terminal diff as an
    # unauthorized_write_violation and dead-lock terminalization. Intentionally
    # narrow: only the UUID-suffixed runtime sidecar.
    if re.fullmatch(
        rf"{re.escape(orch_root)}/launch_incident\.runtime\.[0-9a-f]{{12}}\.json",
        token,
    ):
        return True
    runtime_files = {
        f"{orch_root}/agent_graph.json",
        f"{orch_root}/agent_runs.jsonl",
        # Adv-24: fcntl lock sidecar; orchestration runtime exclusively manages it.
        f"{orch_root}/agent_runs.jsonl.lock",
        # Recurrence-prevention plan (Issue 3): the invalid-payload audit log
        # is written by `record-agent-run` after terminal validation rejects a
        # payload (see `_validate_terminal_run_payload`). It lives at the
        # orchestration root and is exempted here because:
        #   (1) the runtime is the only legitimate writer (record-agent-run
        #       itself, while serializing terminal-validation failures);
        #   (2) children cannot reach the path through tool hooks
        #       (`output_manifest_write_guard` blocks any Edit/Write/Bash
        #       redirect to paths outside `allowed_file_tool_paths`), so a
        #       child entry can only land here through a hook bypass —
        #       outside the threat model this terminal-diff check addresses;
        #   (3) without the exemption, the very write that records a failed
        #       attempt contaminates the next retry's baseline diff, which
        #       was the demonstrated brick-cascade we are fixing.
        # The exemption is intentionally narrow: only the literal log file
        # and its fcntl-lock sidecar. Other files under `<orch_root>/` are
        # not blanket-exempt — see Codex review round 6 P1.
        f"{orch_root}/agent_runs_invalid.jsonl",
        f"{orch_root}/agent_runs_invalid.jsonl.lock",
        # Codex round 11 F1: fcntl lock sidecar for orchestration_meta.json
        # serialization; same runtime-managed exemption as the runs lock.
        f"{orch_root}/orchestration_meta.json.lock",
        f"{orch_root}/orchestration_meta.json",
        f"{orch_root}/orchestration_checkpoint.json",
        f"{orch_root}/active_child_agent_run_id.txt",
        f"{orch_root}/phase_state.json",
        f"{orch_root}/phase_state_log.jsonl",
        f"{orch_root}/preflight.json",
        f"{orch_root}/orchestration_run_write_baseline.json",
        f"{orch_root}/session_run_index.json",
    }
    return token in runtime_files


def _snapshot_repo_files(
    repo_root: Path,
    *,
    orchestration_id: str,
    agent_run_id: str,
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = _normalize_rel_posix(path.relative_to(repo_root).as_posix())
        if _should_ignore_runtime_snapshot_path(
            rel,
            orchestration_id=orchestration_id,
            agent_run_id=agent_run_id,
        ):
            continue
        snapshot[rel] = _compute_sha256(path)
    return snapshot


def _write_run_write_baseline(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    run_id = agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else "orchestration"
    payload = {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else None,
        "created_at": _utc_now_iso(),
        "files": _snapshot_repo_files(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=run_id,
        ),
    }
    _write_json(
        _run_write_baseline_path(repo_root, orchestration_id, agent_run_id=agent_run_id),
        payload,
    )
    return payload


def _load_run_write_baseline(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> dict[str, Any]:
    path = _run_write_baseline_path(repo_root, orchestration_id, agent_run_id=agent_run_id)
    if not path.exists():
        who = agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else "orchestration"
        raise ValueError(f"run write baseline missing for {who}: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"run write baseline must be object: {path}")
    files = payload.get("files")
    if not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ValueError(f"run write baseline files must be string map: {path}")
    return payload


def _deactivate_snapshot_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> Path:
    """Per-arid deactivate snapshot — child-authored path set captured at
    `deactivate-child` time. Lives under `agents/<arid>/` (runtime-prefix
    exempt), so writing it does not contaminate any diff."""
    return (
        _orchestration_root(repo_root, orchestration_id)
        / "agents"
        / agent_run_id.strip()
        / "deactivate_snapshot.json"
    )


def _compute_changed_paths_against_baseline(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> list[str]:
    """Raw baseline-diff computation — walks the workspace and compares
    against the per-arid write baseline. This is the canonical live-diff
    used by `_actual_changed_paths_since_baseline` for terminal write
    validation."""
    baseline = _load_run_write_baseline(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    run_id = agent_run_id.strip() if isinstance(agent_run_id, str) and agent_run_id.strip() else "orchestration"
    # Apply the runtime-snapshot ignore predicate to BOTH sides. `after` is
    # already filtered (via `_snapshot_repo_files`); filtering `before`
    # identically keeps the diff symmetric. Without this, a baseline written
    # before a path became exempt — or one that captured a pre-existing
    # runtime-owned file (e.g. a `failure_analysis.runtime.<uuid12>.json`
    # sidecar left by a prior failed run) — would show that path only in
    # `before`, so it surfaces as a spurious deletion/change and can be rejected
    # as an unauthorized write, re-wedging resumed runs with older baselines.
    before = {
        rel: str(digest)
        for path, digest in dict(baseline.get("files", {})).items()
        if not _should_ignore_runtime_snapshot_path(
            (rel := _normalize_rel_posix(str(path))),
            orchestration_id=orchestration_id,
            agent_run_id=run_id,
        )
    }
    after = _snapshot_repo_files(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=run_id,
    )
    changed = {
        rel
        for rel in set(before) | set(after)
        if before.get(rel) != after.get(rel)
    }
    return sorted(changed)


def _actual_changed_paths_since_baseline(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str | None = None,
) -> list[str]:
    # Recurrence-prevention plan (Issue 3) — Codex review P1 follow-up:
    # always perform the live baseline diff so post-deactivate filesystem
    # mutations remain visible to terminal write validation. The
    # demonstrated failure (`agent_runs_invalid.jsonl` and similar runtime
    # writes contaminating retries) is fully addressed by the
    # `runtime_files` / `runtime_prefixes` exemptions in
    # `_should_ignore_runtime_snapshot_path`, plus the existing
    # `parent_tmp_root` exclusion in `_validate_actual_write_paths`. The
    # deactivate snapshot remains available as an audit artifact (see
    # `_deactivate_snapshot_path`) but is no longer consulted for diff
    # short-circuiting — that would hide real unauthorized writes that
    # appear between deactivate and the (possibly retried) record-agent-run.
    return _compute_changed_paths_against_baseline(
        repo_root, orchestration_id, agent_run_id=agent_run_id
    )


def _normalize_rel_path_list(paths: Sequence[str]) -> list[str]:
    return sorted(
        {
            _normalize_rel_posix(str(path))
            for path in paths
            if isinstance(path, str) and path.strip()
        }
    )


def _gate_changed_paths_store_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> Path:
    return (
        _orchestration_root(repo_root, orchestration_id)
        / "agents"
        / agent_run_id.strip()
        / "gate_changed_paths.json"
    )


def _load_cumulative_gate_changed_paths_for_run(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> list[str]:
    path = _gate_changed_paths_store_path(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    if not path.exists():
        return []
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    paths_obj = payload.get("gate_changed_paths")
    if not isinstance(paths_obj, list):
        return []
    return _normalize_rel_path_list([str(item) for item in paths_obj if isinstance(item, str)])


def _update_cumulative_gate_changed_paths_for_run(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    changed_paths: Sequence[str],
) -> list[str]:
    current = _load_cumulative_gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    incoming = _normalize_rel_path_list(changed_paths)
    merged = sorted(set(current) | set(incoming))
    path = _gate_changed_paths_store_path(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    _write_json(
        path,
        {
            "orchestration_id": orchestration_id,
            "agent_run_id": agent_run_id.strip(),
            "gate_changed_paths": merged,
            "updated_at": _utc_now_iso(),
        },
    )
    return merged


def _gate_changed_paths_for_run(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> list[str]:
    cumulative = _load_cumulative_gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
    )
    if cumulative:
        return cumulative
    gate_path = _gates_dir(repo_root, orchestration_id) / agent_run_id.strip() / "apply_patch_writes.json"
    if not gate_path.exists():
        return []
    gate_doc = _read_json(gate_path)
    if not isinstance(gate_doc, dict):
        return []
    if str(gate_doc.get("status", "")).strip().lower() != "pass":
        return []
    args_json = gate_doc.get("args_json")
    if not isinstance(args_json, dict):
        return []
    changed_paths = args_json.get("changed_paths")
    if not isinstance(changed_paths, list):
        return []
    return _normalize_rel_path_list([str(item) for item in changed_paths if isinstance(item, str)])


def _declared_output_refs(payload: dict[str, Any]) -> list[str]:
    output_refs_obj = payload.get("output_refs")
    if not isinstance(output_refs_obj, list):
        return []
    return [
        _normalize_rel_posix(item)
        for item in output_refs_obj
        if isinstance(item, str) and item.strip()
    ]


def _orchestration_allowed_write_roots(orchestration_id: str) -> list[str]:
    return [
        _with_trailing_slash(_normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}")),
        _with_trailing_slash(_normalize_rel_posix(f"workspace/.pycache/{orchestration_id}")),
    ]


def _is_runtime_audit_artifact_path(orchestration_id: str, rel_path: str) -> bool:
    orch_root = _normalize_rel_posix(f"workspace/orchestrations/{orchestration_id}")
    rel = _normalize_rel_posix(rel_path)
    prefixes: tuple[str, ...] = ()
    return any(_repo_path_under_prefix(rel, prefix.rstrip("/")) for prefix in prefixes)


def _managed_write_snapshot_path(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> Path:
    return (
        _orchestration_root(repo_root, orchestration_id)
        / "agents"
        / agent_run_id.strip()
        / "managed_write_snapshot.json"
    )


def _write_managed_write_snapshot(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    declared_paths: Sequence[str],
    actual_changed_paths: Sequence[str],
) -> None:
    normalized = sorted({_normalize_rel_posix(path) for path in declared_paths if str(path).strip()})
    if not normalized:
        return
    actual_paths = sorted(
        {
            _normalize_rel_posix(path)
            for path in actual_changed_paths
            if str(path).strip()
        }
    )
    tracked_paths = sorted(
        {
            path
            for path in actual_paths
            if any(_repo_path_under_prefix(path, decl) for decl in normalized)
        }
    )
    if not tracked_paths:
        return
    files: dict[str, str] = {}
    for path in tracked_paths:
        abs_path = repo_root / path
        if abs_path.exists():
            files[path] = _compute_sha256(abs_path)
        else:
            files[path] = "__MISSING__"
    _write_json(
        _managed_write_snapshot_path(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id,
        ),
        {
            "agent_run_id": agent_run_id.strip(),
            "recorded_at": _utc_now_iso(),
            "files": files,
        },
    )


def _child_managed_paths_excludable_from_orchestration_diff(
    repo_root: Path,
    orchestration_id: str,
    *,
    current_agent_run_id: str,
    caller_holds_lock: bool = False,
) -> set[str]:
    baseline = _load_run_write_baseline(repo_root, orchestration_id)
    baseline_files_obj = baseline.get("files")
    baseline_files = (
        {
            _normalize_rel_posix(str(path)): str(digest)
            for path, digest in baseline_files_obj.items()
            if isinstance(path, str) and path.strip() and isinstance(digest, str)
        }
        if isinstance(baseline_files_obj, dict)
        else {}
    )
    excludable: set[str] = set()
    # H-FOURTH-1: forward caller_holds_lock so the orchestration-role
    # finalize path (called from inside the runs-jsonl lock) doesn't mask
    # durable corruption via self-lock contention.
    records = _load_run_records(
        _orchestration_root(repo_root, orchestration_id),
        caller_holds_lock=caller_holds_lock,
    )
    for run_id, record in records.items():
        if run_id == current_agent_run_id.strip():
            continue
        role = str(record.get("agent_role") or "").strip().lower()
        if role not in {"step", "substep"}:
            continue
        snap_path = _managed_write_snapshot_path(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
        )
        if not snap_path.exists():
            continue
        snap_doc = _read_json(snap_path)
        if not isinstance(snap_doc, dict):
            continue
        files_obj = snap_doc.get("files")
        if not isinstance(files_obj, dict):
            continue
        for path, digest in files_obj.items():
            if not isinstance(path, str) or not path.strip() or not isinstance(digest, str):
                continue
            rel = _normalize_rel_posix(path)
            current_path = repo_root / rel
            current_digest = "__MISSING__" if not current_path.exists() else _compute_sha256(current_path)
            if current_digest != digest:
                continue
            if baseline_files.get(rel) == current_digest:
                continue
            excludable.add(rel)
        manifest_path = _allowed_output_manifest_path(
            repo_root,
            orchestration_id,
            run_id,
        )
        manifest_rel = _normalize_rel_posix(str(manifest_path.relative_to(repo_root)))
        manifest_digest = (
            "__MISSING__" if not manifest_path.exists() else _compute_sha256(manifest_path)
        )
        if manifest_digest != "__MISSING__" and baseline_files.get(manifest_rel) != manifest_digest:
            excludable.add(manifest_rel)
    return excludable


def _runtime_created_pin_stub_paths(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> dict[str, int | None]:
    """Return the repo-relative file-pin stubs this run pre-created, mapped to
    the ``mtime_ns`` recorded at creation (``created_file_pin_stubs``).

    These are runtime-owned canonical placeholders (e.g. the Generate step's
    ``lineage.json``) created so bwrap can bind them at file granularity. They
    carry no agent-authored content unless the agent wrote them through the
    gate, so their collateral deletion is semantically harmless (and is even
    performed deliberately by ``_cleanup_empty_file_pin_stubs`` for untouched
    stubs). The recorded ``mtime_ns`` lets a restore reproduce the exact stub
    state so ``_cleanup_empty_file_pin_stubs`` (which matches on mtime) still
    treats the restored placeholder as an untouched stub. ``None`` indicates the
    entry recorded no usable mtime."""
    profile_path = _sandbox_profiles_dir(repo_root, orchestration_id) / f"{agent_run_id.strip()}.json"
    if not profile_path.exists():
        return {}
    try:
        profile_doc = _read_json(profile_path)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(profile_doc, dict):
        return {}
    stubs_obj = profile_doc.get("created_file_pin_stubs")
    if not isinstance(stubs_obj, list):
        return {}
    result: dict[str, int | None] = {}
    for entry in stubs_obj:
        if isinstance(entry, dict):
            p = entry.get("path")
            if isinstance(p, str) and p.strip():
                m = entry.get("mtime_ns")
                result[_normalize_rel_posix(p)] = m if isinstance(m, int) else None
    return result


def _restore_deleted_file_pin_stubs(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    skip_prefixes: Sequence[str] = (),
) -> list[str]:
    """Re-create as a 0-byte file any runtime-created file-pin stub that is
    currently absent (collaterally deleted outside the gate).

    Restoring the canonical placeholder returns the workspace to its launch
    baseline so a (possibly failed) run can be recorded instead of dead-locking
    the orchestration with a permanently unrecordable ``unauthorized_write``
    over a runtime-owned artifact. ``record-launch`` / ``record-agent-run`` /
    ``guarded-apply-patch`` all run with runtime authority (outside the child
    sandbox), so this restore is permitted where the orchestration agent's own
    canonical-path writes are not.

    Stubs covered by ``skip_prefixes`` (typically the run's
    ``gate_changed_paths`` — paths the agent legitimately mutated through
    guarded-apply-patch) are left untouched so a deliberate gate write/removal
    is never clobbered. Returns the list of restored paths."""
    stubs = _runtime_created_pin_stub_paths(
        repo_root, orchestration_id, agent_run_id=agent_run_id
    )
    if not stubs:
        return []
    resolved_root = repo_root.resolve()
    skip = [_normalize_rel_posix(str(p)) for p in skip_prefixes if str(p).strip()]
    restored: list[str] = []
    for rel in sorted(stubs):
        if any(_repo_path_under_prefix(rel, prefix) for prefix in skip):
            continue
        target = (repo_root / rel).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError:
            continue
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            # Reproduce the mtime recorded at stub creation so
            # `_cleanup_empty_file_pin_stubs` (which deletes only zero-byte
            # stubs whose mtime still matches `created_file_pin_stubs`) still
            # treats this restored placeholder as an untouched stub. Without
            # this, touch()'s fresh mtime would make the empty placeholder
            # un-cleanable and leave it lingering as canonical workspace data.
            recorded_mtime_ns = stubs.get(rel)
            if isinstance(recorded_mtime_ns, int):
                os.utime(target, ns=(recorded_mtime_ns, recorded_mtime_ns))
        except OSError:
            continue
        restored.append(rel)
    return restored


def _cleanup_empty_file_pin_stubs(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> None:
    """Remove empty file-pin stubs created by this run's build_bwrap_profile.

    Only paths recorded in the sandbox profile's `created_file_pin_stubs` are
    candidates — pre-existing files are never touched. A stub is removed iff:
    - It is listed in created_file_pin_stubs (was created as empty stub by this run)
    - It currently exists and is zero bytes
    - It was not written by the agent via guarded-apply-patch (not in gate_changed_paths)
    """
    profile_path = _sandbox_profiles_dir(repo_root, orchestration_id) / f"{agent_run_id}.json"
    if not profile_path.exists():
        return
    try:
        profile_doc = _read_json(profile_path)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(profile_doc, dict):
        return
    stubs_obj = profile_doc.get("created_file_pin_stubs")
    if not isinstance(stubs_obj, list):
        return
    # Build path → recorded_mtime_ns mapping for stubs created by this run.
    # Each entry is {"path": str, "mtime_ns": int} recorded immediately after touch().
    candidate_stubs: dict[str, int] = {}
    for entry in stubs_obj:
        if isinstance(entry, dict):
            p = entry.get("path")
            m = entry.get("mtime_ns")
            if isinstance(p, str) and p.strip() and isinstance(m, int):
                candidate_stubs[_normalize_rel_posix(p)] = m
    if not candidate_stubs:
        return
    gate_changed = {
        _normalize_rel_posix(p)
        for p in _load_cumulative_gate_changed_paths_for_run(
            repo_root, orchestration_id, agent_run_id=agent_run_id
        )
        if p
    }
    for norm, recorded_mtime_ns in candidate_stubs.items():
        if norm in gate_changed:
            continue
        stub_path = repo_root / norm
        if not stub_path.exists():
            continue
        st = stub_path.stat()
        # Only delete if the file is still zero bytes AND its mtime is unchanged since
        # our touch() — a subprocess that writes (even zero bytes) updates the mtime.
        if st.st_size == 0 and st.st_mtime_ns == recorded_mtime_ns:
            try:
                stub_path.unlink()
            except OSError:
                pass


def _cleanup_agent_tmp_root(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> bool:
    """Remove `workspace/tmp/<agent_run_id>/` recursively at terminal status.

    The per-agent tmp directory accumulates heredoc-staged patches, helper
    `*.py` scripts, request/reply payloads, etc. Without cleanup the next
    invocation of `validate_workspace_root` flags every leftover `*.py` as
    "python script under workspace/ is forbidden", failing downstream phases.

    Returns:
      True  — cleanup succeeded OR the tmp dir was already absent. The caller
              may now write the cleanup_committed marker (Adv-36).
      False — cleanup was REFUSED (ownership / collision / symlink) or the
              destructive `rmtree` raised. The committed marker MUST NOT be
              written; the run remains in cleanup-pending state so the
              validator keeps tmp scratch exempt for diagnostics.

    Safety:
    - Only deletes the exact path `workspace/tmp/<agent_run_id>/` derived from
      the agent_run_id argument. The orchestration runtime only ever calls this
      for terminal-status agents, so there is no in-flight risk.
    - Validates that the target lives under repo_root/workspace/tmp/ before
      unlinking — refuses to traverse symlinks pointing elsewhere.
    - **Adv-5 ownership guard**: only deletes if the calling orchestration owns
      the launch record for `<agent_run_id>` (i.e.,
      `workspace/orchestrations/<orchestration_id>/launches/<arid>.request.json`
      exists). The tmp namespace is shared (`workspace/tmp/<arid>/` with no
      orchestration prefix), so two orchestrations that happen to reuse the
      same `arid` would share the directory — terminating one must not delete
      the other's live scratch. Without this guard, a single record-agent-run
      could wipe a concurrent orchestration's in-flight workspace.
    - rmtree errors are not raised (cleanup must not fail the calling
      record-agent-run / record-timeout flow), but they are surfaced via
      the False return so callers can suppress the cleanup_committed marker.
    """
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        return False
    arid = agent_run_id.strip()
    # Defensive: reject path-traversal-style values.
    if "/" in arid or ".." in arid or arid in {".", ""}:
        return False
    if not isinstance(orchestration_id, str) or not orchestration_id.strip():
        return False
    # Adv-5/Adv-7: refuse to delete unless this orchestration owns the arid.
    # Two ownership proofs are accepted (covering both child agents and the
    # orchestration agent itself):
    #   (a) Step/substep agents: launches/<arid>.request.json exists.
    #   (b) Orchestration agent: orchestration_meta.json#orchestration_agent_run_id
    #       equals arid (orchestration agents are not "launched" via record-launch
    #       and have no request file, but their identity is pinned in the meta).
    # Without either proof we refuse to delete — a concurrent orchestration
    # that happens to reuse the same arid (the workspace/tmp/ namespace is flat)
    # may be using the directory as live scratch.
    orch_root_dir = repo_root / "workspace" / "orchestrations" / orchestration_id.strip()
    is_owner_via_launch = (orch_root_dir / "launches" / f"{arid}.request.json").is_file()
    is_owner_via_orchestration = False
    if not is_owner_via_launch:
        meta_path = orch_root_dir / "orchestration_meta.json"
        if meta_path.is_file():
            try:
                meta_doc = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta_doc = None
            if isinstance(meta_doc, dict):
                expected = meta_doc.get("orchestration_agent_run_id")
                if isinstance(expected, str) and expected.strip() == arid:
                    is_owner_via_orchestration = True
    if not (is_owner_via_launch or is_owner_via_orchestration):
        return False
    # Adv-11: cross-orchestration collision check. The flat workspace/tmp/<arid>/
    # namespace cannot disambiguate which orchestration owns the directory's
    # contents when two orchestrations have launched the same arid. If ANY
    # other orchestration also claims this arid (via a launch record OR as its
    # own orchestration_agent_run_id), refuse to delete — wiping the shared
    # directory would destroy the colliding orchestration's live scratch.
    # This matches the validator's collision-aware exemption (Adv-10):
    # workspace/tmp/<arid>/ is treated as ambiguous and surfaced; cleanup is
    # likewise refused.
    #
    # Codex round 20 F2: serialize the cross-orchestration scan + rmtree under
    # an fcntl lock on `workspace/tmp/<arid>.lock`. Two concurrent cleanup
    # attempts (or a cleanup race against a same-orchestration replay) cannot
    # observe stale ownership state. The lock further narrows — but does not
    # fully eliminate — the legacy M4 race where a record-launch in another
    # orchestration creates `launches/<arid>.request.json` between the scan
    # and the rmtree. With UUID4 arids (the documented norm) collision is
    # astronomically rare; namespacing `workspace/tmp/` by orchestration_id
    # is the long-term elimination (see RUNBOOK.md `tmp-namespace-redesign`).
    workspace_tmp = repo_root / "workspace" / "tmp"
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    lock_path = workspace_tmp / f"{arid}.lock"
    with _fcntl_exclusive_lock(lock_path):
        orchestrations_root = repo_root / "workspace" / "orchestrations"
        other_owners = 0
        if orchestrations_root.is_dir():
            for other_dir in orchestrations_root.iterdir():
                if not other_dir.is_dir() or other_dir.name == orchestration_id.strip():
                    continue
                if (other_dir / "launches" / f"{arid}.request.json").is_file():
                    other_owners += 1
                    continue
                other_meta_path = other_dir / "orchestration_meta.json"
                if other_meta_path.is_file():
                    try:
                        other_meta = json.loads(other_meta_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if isinstance(other_meta, dict):
                        other_arid = other_meta.get("orchestration_agent_run_id")
                        if isinstance(other_arid, str) and other_arid.strip() == arid:
                            other_owners += 1
        if other_owners > 0:
            return False
        target = workspace_tmp / arid
        if not target.exists():
            # Already absent — counts as "tmp is gone" for the committed marker.
            return True
        # Reject symlink redirection — never follow a symlink at <agent_run_id>.
        try:
            if target.is_symlink():
                return False
            # Confirm the resolved path stays under workspace/tmp/.
            resolved_target = target.resolve()
            resolved_parent = workspace_tmp.resolve()
            if resolved_target.parent != resolved_parent:
                return False
        except OSError:
            return False
        import shutil
        try:
            shutil.rmtree(target)
        except OSError:
            return False
        # Successful rmtree (or post-rmtree absence). Confirm directory is gone.
        # NOTE: the `<arid>.lock` fcntl sidecar is intentionally NOT unlinked
        # here. It is the very primitive serializing this scan + rmtree against
        # a concurrent cleanup; deleting it would let a waiter proceed on the
        # orphaned inode while a new entrant O_CREATs a fresh inode and locks
        # it without contention, breaking serialization. The sidecar is a
        # 0-byte benign file that `validate_workspace_root` tolerates, so it is
        # left in place (bounded accumulation under session-scoped scratch).
        return not target.exists()


def _write_directory_authorized_paths(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    directory_authorized_paths: list[str],
    manifest_allowed_output_dirs: list[str],
) -> None:
    out = _violations_dir(repo_root, orchestration_id).parent / "audit" / f"{agent_run_id}.directory_authorized_paths.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json(out, {
        "kind": "directory_authorized_paths",
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "recorded_at": _utc_now_iso(),
        "manifest_allowed_output_dirs": manifest_allowed_output_dirs,
        "directory_authorized_paths": directory_authorized_paths,
    })


def _write_unauthorized_write_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    actor_role: str,
    actual_changed_paths: list[str],
    unauthorized_paths: list[str],
    output_refs: list[str],
    gate_changed_paths: list[str],
    missing_from_gate_changed_paths: list[str],
    write_roots: list[str],
    manifest_file_tool_paths: list[str] | None = None,
    directory_authorized_paths: list[str] | None = None,
) -> Path:
    out = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.unauthorized_write_violation.json"
    record: dict[str, Any] = {
        "kind": "unauthorized_write_violation",
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "actor_role": actor_role,
        "detected_at": _utc_now_iso(),
        "actual_changed_paths": actual_changed_paths,
        "unauthorized_paths": unauthorized_paths,
        "output_refs": output_refs,
        "gate_changed_paths": gate_changed_paths,
        "missing_from_gate_changed_paths": missing_from_gate_changed_paths,
        "write_roots": write_roots,
    }
    if manifest_file_tool_paths is not None:
        record["manifest_file_tool_paths"] = manifest_file_tool_paths
    if directory_authorized_paths is not None:
        record["directory_authorized_paths"] = directory_authorized_paths
    # P2-B: preserve prior operator dismissal evidence.  When a re-detection
    # surfaces unauthorized paths that are NOT a subset of a previously
    # dismissed set, this function overwrites the existing violation file.
    # Without carrying it forward, the operator's prior `dismissed_at` /
    # `dismiss_reason` / `dismissed_paths` approval would be silently destroyed,
    # misleading auditors into thinking no dismissal ever happened.  Accumulate
    # the history under `prior_dismissals` so the full trail survives overwrite.
    if out.exists():
        try:
            prior = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = None
        if isinstance(prior, dict):
            # Always carry forward any accumulated history first, so a SECOND
            # consecutive re-detection (no intervening re-dismiss, hence no
            # `dismissed_at` on the prior record) does not drop the earlier
            # entries.  Then append the current dismissal if the prior record
            # was itself in a dismissed state.
            prior_history = prior.get("prior_dismissals")
            history_list = list(prior_history) if isinstance(prior_history, list) else []
            if prior.get("dismissed_at"):
                history_list.append({
                    "dismissed_at": prior.get("dismissed_at"),
                    "dismiss_reason": prior.get("dismiss_reason"),
                    "dismissed_paths": prior.get("dismissed_paths"),
                    "superseded_at": record["detected_at"],
                })
            if history_list:
                record["prior_dismissals"] = history_list
    _write_json(out, record)
    return out


def dismiss_violation(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    dismiss_reason: str,
    paths: list[str],
    operator_token: str,
) -> dict[str, Any]:
    """dismiss-violation: mark a known benign unauthorized_write_violation as operator-approved.

    Append ``dismissed_at`` / ``dismiss_reason`` / ``dismissed_paths`` to
    violations/<arid>.unauthorized_write_violation.json. On the next
    ``_validate_actual_write_paths`` call, if the detected unauthorized paths are
    a subset of dismissed_paths, skip the raise.

    Args:
        repo_root: the repository root
        orchestration_id: orchestration ID
        agent_run_id: the agent run ID to dismiss
        dismiss_reason: the dismiss reason (free-form, remains in the audit log)
        paths: the file paths to dismiss (relative to repo root)
        operator_token: the content of ~/.met-dsl/operator_tokens/<oid>.txt
    """
    # Operator-only gate via token validation.
    # The token is written to ~/.met-dsl/operator_tokens/<oid>.txt at orchestration
    # init (mode 0o600) and is NOT under any workspace/ path that agents may read.
    # Two hook layers keep it out of agent reach: (a) the orchestration agent's
    # allowed_read_roots include workspace/ but NOT ~/.met-dsl/, so the Read tool
    # is blocked by read_manifest_read_guard; (b) forbid_operator_secret_direct_read
    # blocks `cat ~/.met-dsl/...` (and $HOME/absolute spellings) via Bash.
    # Residual: a written `python3 script.py` whose body reads the token file
    # internally is not interceptable by PreToolUse hooks — this is an accepted
    # architectural limit shared by all on-disk secrets; the operator passes the
    # token from their own terminal and is not expected to materialize it on disk
    # where the agent runs.
    # This replaces the prior mutable-env-var check, which an agent could bypass
    # by clearing os.environ['METDSL_WORKFLOW_MODE'] before calling this function
    # from a tmp Python script.
    token_path = Path.home() / ".met-dsl" / "operator_tokens" / f"{orchestration_id}.txt"
    if not token_path.exists():
        raise ValueError(
            f"dismiss-violation: operator_token.txt not found at {token_path}. "
            "Re-run orchestration init to generate the token."
        )
    expected_token = token_path.read_text(encoding="utf-8").strip()
    # Reject an empty/corrupt stored token: a 0-byte file (e.g. from a crash
    # mid-write) must NEVER validate.  Without this, `compare_digest("", "")`
    # is True and `--operator-token " "` (which survives the `not` guard but
    # strips to "") would pass — letting an agent self-approve a violation.
    if not expected_token:
        raise ValueError(
            "dismiss-violation: stored operator token is empty or corrupt at "
            f"{token_path}. Re-run orchestration init to regenerate it."
        )
    _candidate = operator_token.strip() if operator_token else ""
    # Constant-time compare so the gate does not leak the token via timing.
    if not _candidate or not secrets.compare_digest(_candidate, expected_token):
        raise ValueError(
            "dismiss-violation: --operator-token does not match the stored "
            f"token in {token_path}. Read the token from that file and pass it "
            "as --operator-token."
        )
    if not dismiss_reason or not dismiss_reason.strip():
        raise ValueError("dismiss-violation: --dismiss-reason must be non-empty")
    if not paths:
        raise ValueError("dismiss-violation: --paths must contain at least one path")
    viol_path = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.unauthorized_write_violation.json"
    if not viol_path.exists():
        raise ValueError(
            f"dismiss-violation: violation file not found: {viol_path}. "
            "Run record-agent-run once to produce the violation, then dismiss."
        )
    viol_doc = _read_json(viol_path)
    if not isinstance(viol_doc, dict):
        raise ValueError(f"dismiss-violation: violation file is not a JSON object: {viol_path}")
    # Validate: every requested dismiss path must be present in the violation's
    # recorded unauthorized_paths. This prevents operators from over-broadly
    # pre-approving paths that were never in the evidence, which would create a
    # wildcard pass-gate for future unauthorized writes.
    recorded_unauthorized: set[str] = set()
    up_obj = viol_doc.get("unauthorized_paths")
    if isinstance(up_obj, list):
        recorded_unauthorized = {
            _normalize_rel_posix(str(p)) for p in up_obj if isinstance(p, str) and p.strip()
        }
    normalized_paths = sorted({_normalize_rel_posix(p) for p in paths if p.strip()})
    unknown = set(normalized_paths) - recorded_unauthorized
    if unknown:
        raise ValueError(
            f"dismiss-violation: the following paths are not in the violation's "
            f"unauthorized_paths and cannot be dismissed: {sorted(unknown)}. "
            f"Dismissable paths: {sorted(recorded_unauthorized)}"
        )
    # Allow re-dismiss with updated reason/paths; overwrite previous dismiss.
    viol_doc["dismissed_at"] = _utc_now_iso()
    viol_doc["dismiss_reason"] = dismiss_reason.strip()
    viol_doc["dismissed_paths"] = normalized_paths
    _write_json(viol_path, viol_doc)
    return {
        "dismissed": True,
        "violation_path": str(viol_path.relative_to(repo_root)),
        "dismissed_paths": normalized_paths,
        "dismissed_at": viol_doc["dismissed_at"],
        "dismiss_reason": viol_doc["dismiss_reason"],
    }


def _is_violation_dismissed(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    unauthorized_paths: list[str],
) -> bool:
    """Pass-gate of _validate_actual_write_paths: True if already dismiss-approved.

    If the violation file exists and has a ``dismissed_at`` field,
    and the ``unauthorized_paths`` detected this time are a subset of ``dismissed_paths``,
    return True (to skip the raise).
    """
    viol_path = _violations_dir(repo_root, orchestration_id) / f"{agent_run_id}.unauthorized_write_violation.json"
    if not viol_path.exists():
        return False
    try:
        viol_doc = _read_json(viol_path)
    except Exception:
        return False
    if not isinstance(viol_doc, dict):
        return False
    if not viol_doc.get("dismissed_at"):
        return False
    dismissed_set: set[str] = set()
    dp_obj = viol_doc.get("dismissed_paths")
    if isinstance(dp_obj, list):
        dismissed_set = {_normalize_rel_posix(str(p)) for p in dp_obj if isinstance(p, str) and p.strip()}
    if not dismissed_set:
        return False
    unauthorized_normalized = {_normalize_rel_posix(p) for p in unauthorized_paths}
    return unauthorized_normalized <= dismissed_set


def _validate_actual_write_paths(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
    *,
    caller_holds_lock: bool = False,
) -> None:
    role_obj = payload.get("agent_role")
    agent_run_id_obj = payload.get("agent_run_id")
    if not isinstance(role_obj, str) or not isinstance(agent_run_id_obj, str) or not agent_run_id_obj.strip():
        return
    actor_role = role_obj.strip().lower()
    if actor_role not in {"orchestration", "step", "substep"}:
        return
    status_obj = payload.get("status")
    if not isinstance(status_obj, str) or status_obj.strip().lower() not in TERMINAL_STATUSES:
        return

    run_id = agent_run_id_obj.strip()
    baseline_agent_run_id = run_id if actor_role in {"step", "substep"} else None
    if actor_role in {"step", "substep"}:
        # Fix 4 (recoverability): re-create any runtime-owned file-pin stub
        # (e.g. lineage.json) that was collaterally deleted outside the gate so
        # the baseline diff is clean and a failed run remains recordable instead
        # of dead-locking the orchestration. Stubs the agent legitimately
        # mutated through guarded-apply-patch (gate_changed_paths) are skipped.
        _restore_deleted_file_pin_stubs(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
            skip_prefixes=_gate_changed_paths_for_run(
                repo_root, orchestration_id, agent_run_id=run_id
            ),
        )
    actual_changed_paths = _actual_changed_paths_since_baseline(
        repo_root,
        orchestration_id,
        agent_run_id=baseline_agent_run_id,
    )
    output_refs = _declared_output_refs(payload)
    gate_changed_paths = _gate_changed_paths_for_run(
        repo_root,
        orchestration_id,
        agent_run_id=run_id,
    )

    if actor_role == "orchestration":
        child_excludable = _child_managed_paths_excludable_from_orchestration_diff(
            repo_root,
            orchestration_id,
            current_agent_run_id=run_id,
            caller_holds_lock=caller_holds_lock,
        )
        actual_changed_paths = [
            path
            for path in actual_changed_paths
            if path not in child_excludable
        ]
        write_roots = _orchestration_allowed_write_roots(orchestration_id)
    else:
        cap_path = _capabilities_dir(repo_root, orchestration_id) / f"{run_id}.json"
        if not cap_path.exists():
            raise ValueError(f"capability file not found for terminal write validation: {cap_path}")
        cap_doc = _read_json(cap_path)
        if not isinstance(cap_doc, dict):
            raise ValueError(f"capability must be object for terminal write validation: {cap_path}")
        roots_obj = cap_doc.get("write_roots")
        write_roots = _load_write_roots_from_cap(roots_obj)

    unauthorized: list[str] = []
    parent_tmp_root: str | None = None
    if actor_role in {"step", "substep"}:
        # Prefer the launch request file as the authoritative source for
        # parent_agent_run_id; fall back to payload for backward compatibility.
        _parent_run_id: str | None = None
        _launch_req = (
            repo_root
            / "workspace"
            / "orchestrations"
            / orchestration_id
            / "launches"
            / f"{run_id}.request.json"
        )
        if _launch_req.exists():
            try:
                _req_doc = _read_json(_launch_req)
                if isinstance(_req_doc, dict):
                    _raw = _req_doc.get("parent_agent_run_id")
                    if isinstance(_raw, str) and _raw.strip():
                        _parent_run_id = _raw.strip()
            except Exception:
                pass
        if _parent_run_id is None:
            _raw_payload = payload.get("parent_agent_run_id")
            if isinstance(_raw_payload, str) and _raw_payload.strip():
                _parent_run_id = _raw_payload.strip()
        if _parent_run_id:
            parent_tmp_root = _normalize_rel_posix(f"workspace/tmp/{_parent_run_id}")
    missing_from_gate_changed_paths = sorted(
        {
            path
            for path in actual_changed_paths
            if not any(_repo_path_under_prefix(path, gate_path) for gate_path in gate_changed_paths)
        }
    )
    manifest_file_tool_paths: set[str] = set()
    manifest_allowed_tmp_root: str | None = None
    manifest_allowed_output_dirs: list[str] = []
    manifest_integrity_protected_logs: set[str] = set()
    if actor_role == "orchestration":
        declared_paths = sorted(set(output_refs) | set(gate_changed_paths))
        exact_declared_paths = declared_paths  # orchestration: no directory entries
    else:
        # step/substep: include manifest-permitted direct write paths so that
        # `.yaml` / `.md` / source code outputs written via Edit/Write are not
        # flagged as unauthorized writes.
        try:
            manifest_doc = _load_allowed_output_manifest(
                repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=run_id,
            )
        except ValueError:
            manifest_doc = None
        if isinstance(manifest_doc, dict):
            ftp_obj = manifest_doc.get("allowed_file_tool_paths")
            if isinstance(ftp_obj, list):
                manifest_file_tool_paths = {
                    _normalize_rel_posix(str(item))
                    for item in ftp_obj
                    if isinstance(item, str) and item.strip()
                }
            aop_obj = manifest_doc.get("allowed_output_paths")
            if isinstance(aop_obj, list):
                for item in aop_obj:
                    if not isinstance(item, str) or not item.strip():
                        continue
                    raw_aop = item.strip()
                    if raw_aop.endswith("/"):
                        manifest_allowed_output_dirs.append(_normalize_rel_posix(raw_aop))
            # Canonical MCP audit logs (e.g. mcp_command_log.jsonl at the
            # phase-specific canonical placement) are written by MCP server
            # tooling without going through guarded-apply-patch and are
            # excluded from allowed_file_tool_paths so children cannot
            # Edit/Write them. Authorize them as MCP-owned outputs at
            # terminalization so a successful MCP tool run does not get
            # fail-closed for the very file it is trusted to produce.
            # Trust only the manifest's persisted `mcp_owned_audit_logs`
            # field — basename matches outside canonical placement are not
            # auto-trusted (defense against over-broad manifest entries).
            mcp_logs_obj = manifest_doc.get("mcp_owned_audit_logs")
            if isinstance(mcp_logs_obj, list):
                for item in mcp_logs_obj:
                    if isinstance(item, str) and item.strip():
                        manifest_integrity_protected_logs.add(
                            _normalize_rel_posix(item.strip())
                        )
            _tmp_raw = manifest_doc.get("allowed_tmp_root", "")
            if isinstance(_tmp_raw, str) and _tmp_raw.strip():
                _tmp_norm = _normalize_rel_posix(_tmp_raw.strip())
                _expected_tmp = _normalize_rel_posix(f"workspace/tmp/{run_id}")
                if _tmp_norm != _expected_tmp:
                    raise ValueError(
                        f"allowed_tmp_root manifest value {_tmp_norm!r} does not match "
                        f"expected per-run root {_expected_tmp!r}"
                    )
                manifest_allowed_tmp_root = _tmp_norm
        exact_declared_paths = sorted(
            set(gate_changed_paths)
            | manifest_file_tool_paths
            | manifest_integrity_protected_logs
        )
        declared_paths = sorted(set(exact_declared_paths) | set(manifest_allowed_output_dirs))
    # Use a frozenset for O(1) exact-match lookup. exact_declared_paths contains
    # concrete file paths (gate_changed_paths + manifest_file_tool_paths); prefix
    # matching would allow a directory token that leaked in to bypass extension policy.
    _exact_declared_set: frozenset[str] = frozenset(exact_declared_paths)
    directory_authorized: list[str] = []
    for path in actual_changed_paths:
        # Codex round 20 F2: cross-orchestration cleanup lock sidecar at
        # `workspace/tmp/<arid>.lock` is created/touched by `_cleanup_agent_tmp_root`
        # via fcntl during terminal status finalization. Runtime-managed; exempt.
        if (
            path.startswith("workspace/tmp/")
            and path.endswith(".lock")
            and "/" not in path[len("workspace/tmp/"):]
        ):
            continue
        if parent_tmp_root and _repo_path_under_prefix(path, parent_tmp_root):
            continue
        if manifest_allowed_tmp_root and _repo_path_under_prefix(path, manifest_allowed_tmp_root):
            continue
        if path in manifest_integrity_protected_logs:
            # Canonical MCP-owned audit logs are pre-validated against
            # canonical phase placements at launch time and protected by
            # multiple defense layers (file_tool exclusion, guarded-apply-
            # patch rejection, hook-level write block). Authorize the actual
            # write regardless of capability `write_roots` so legitimate
            # cross-phase placements (e.g. Execute's run_quality_checks log
            # under generate/<gen>/src/) are not fail-closed for the very
            # file the MCP tool produced.
            continue
        if write_roots and not _path_under_any_write_root(path, write_roots):
            unauthorized.append(path)
            continue
        if _exact_declared_set and path in _exact_declared_set:
            continue
        if manifest_allowed_output_dirs and any(_repo_path_under_prefix(path, d) for d in manifest_allowed_output_dirs):
            # All writes under a directory allowlist must have gate provenance (guarded-apply-patch).
            # Compiler byproducts (.mod, .o, .a) are also unauthorized without provenance —
            # agents must clean them up before record-agent-run to prevent unaudited binary injection.
            unauthorized.append(path)
            continue
        if not declared_paths:
            unauthorized.append(path)
            continue
        unauthorized.append(path)

    if directory_authorized:
        _write_directory_authorized_paths(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
            directory_authorized_paths=directory_authorized,
            manifest_allowed_output_dirs=manifest_allowed_output_dirs,
        )
    if unauthorized:
        # Pass-gate: a violation already approved by the operator via dismiss-violation
        # skips the raise and passes record-agent-run.
        # Confirm that the dismissed target is a strict subset of the detected unauthorized.
        if _is_violation_dismissed(
            repo_root,
            orchestration_id,
            agent_run_id=run_id,
            unauthorized_paths=unauthorized,
        ):
            # Evidence: the violation file's dismissed_at is already written by dismiss_violation(),
            # so an append indicating this pass is unnecessary (the file remains as-is).
            pass
        else:
            violation_path = _write_unauthorized_write_violation(
                repo_root,
                orchestration_id,
                agent_run_id=run_id,
                actor_role=actor_role,
                actual_changed_paths=actual_changed_paths,
                unauthorized_paths=unauthorized,
                output_refs=output_refs,
                gate_changed_paths=gate_changed_paths,
                missing_from_gate_changed_paths=missing_from_gate_changed_paths,
                write_roots=write_roots,
                manifest_file_tool_paths=sorted(manifest_file_tool_paths) if manifest_file_tool_paths else None,
                directory_authorized_paths=directory_authorized if directory_authorized else None,
            )
            if actor_role in {"step", "substep"}:
                # Cleanup runs AFTER violation is recorded so evidence is preserved for auditors.
                _cleanup_empty_file_pin_stubs(repo_root, orchestration_id, agent_run_id=run_id)
                _cleanup_agent_tmp_root(repo_root, orchestration_id, agent_run_id=run_id)
            raise ValueError(
                "terminal run has unauthorized write paths: "
                + ", ".join(unauthorized)
                + f" (violation: {violation_path})"
            )
    if actor_role in {"step", "substep"}:
        # Success path: clean up any stubs the agent never wrote to.
        # NEW-M2: tmp cleanup is DEFERRED to the post-lock end-of-function
        # phase in record_agent_run (Adv-35 two-phase commit). Doing it
        # here, before the durable terminal append at runs.jsonl, would
        # delete recovery scratch ahead of the durable state transition —
        # if a crash lands between this point and the append, the run is
        # left without a terminal entry AND without diagnostic scratch.
        # NEW-L3: wrap each cleanup helper call so an unexpected OSError
        # in one does not skip the snapshot write that follows.
        try:
            _cleanup_empty_file_pin_stubs(repo_root, orchestration_id, agent_run_id=run_id)
        except OSError:
            pass
        try:
            _write_managed_write_snapshot(
                repo_root,
                orchestration_id,
                agent_run_id=run_id,
                declared_paths=declared_paths,
                actual_changed_paths=actual_changed_paths,
            )
        except OSError:
            pass


def _load_checkpoint(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any] | None:
    """Load orchestration_checkpoint.json. Return None if it does not exist.

    Raise a RuntimeError if the JSON structure is invalid.
    """
    path = _checkpoint_path(repo_root, orchestration_id)
    if not path.exists():
        return None
    try:
        data = _read_json(path)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"orchestration_checkpoint.json is invalid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"orchestration_checkpoint.json must be object: {path}")
    if data.get("orchestration_id") != orchestration_id:
        raise RuntimeError(
            "orchestration_checkpoint.json orchestration_id mismatch: "
            f"expected {orchestration_id!r}, got {data.get('orchestration_id')!r}"
        )
    return data


def _preflight_path(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "preflight.json"


def _preflight_allows_agent_launch(payload: dict[str, Any]) -> bool:
    feature_states = payload.get("feature_states")
    if not isinstance(feature_states, dict):
        return False
    if feature_states.get("multi_agent") is not True:
        return False
    backend_token = str(payload.get("backend", "")).strip().lower()
    hooks = feature_states.get("hooks")
    if hooks is None:
        hooks = feature_states.get("codex_hooks")
    if backend_token == "codex" and hooks is not True:
        return False

    checks = payload.get("checks")
    if not isinstance(checks, list):
        return False
    multi_agent_check_pass: bool | None = None
    hooks_check_pass: bool | None = None
    codex_home_writable_check_pass: bool | None = None
    for item in checks:
        if not isinstance(item, dict):
            continue
        check_name = item.get("name")
        pass_value = item.get("pass")
        if check_name == "multi_agent_enabled" and isinstance(pass_value, bool):
            multi_agent_check_pass = pass_value
        if check_name == "hooks_enabled" and isinstance(pass_value, bool):
            hooks_check_pass = pass_value
        if (
            check_name == "codex_hooks_enabled"
            and hooks_check_pass is None
            and isinstance(pass_value, bool)
        ):
            hooks_check_pass = pass_value
        if check_name == "codex_home_writable" and isinstance(pass_value, bool):
            codex_home_writable_check_pass = pass_value

    launchable = (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
        and payload.get("sandbox_enforced") is True
        and multi_agent_check_pass is True
    )
    if backend_token == "codex":
        launchable = (
            launchable
            and hooks_check_pass is True
            and codex_home_writable_check_pass is True
        )
    return launchable


def _validate_preflight_payload(payload: dict[str, Any]) -> None:
    if (
        payload.get("can_launch_step_agents") is True
        or payload.get("can_launch_substep_agents") is True
    ) and payload.get("status") != "pass":
        raise ValueError(
            "preflight status must be pass when can_launch_step_agents/can_launch_substep_agents is true"
        )

    feature_states = payload.get("feature_states")
    backend_token = str(payload.get("backend", "")).strip().lower()
    if isinstance(feature_states, dict):
        multi_agent = feature_states.get("multi_agent")
        if isinstance(multi_agent, bool) and not multi_agent:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "feature_states.multi_agent=false is incompatible with launchable preflight"
                )
        hooks = feature_states.get("hooks")
        if hooks is None:
            hooks = feature_states.get("codex_hooks")
        if (
            backend_token == "codex"
            and hooks is not True
            and (
                payload.get("can_launch_step_agents") is True
                or payload.get("can_launch_substep_agents") is True
            )
        ):
            raise ValueError(
                "feature_states.hooks=true is required for codex launchable preflight"
            )

    checks = payload.get("checks")
    if isinstance(checks, list):
        multi_agent_check_pass: bool | None = None
        hooks_check_pass: bool | None = None
        codex_home_writable_check_pass: bool | None = None
        for item in checks:
            if not isinstance(item, dict):
                continue
            check_name = item.get("name")
            pass_value = item.get("pass")
            if check_name == "multi_agent_enabled" and isinstance(pass_value, bool):
                multi_agent_check_pass = pass_value
            if check_name == "hooks_enabled" and isinstance(pass_value, bool):
                hooks_check_pass = pass_value
            if (
                check_name == "codex_hooks_enabled"
                and hooks_check_pass is None
                and isinstance(pass_value, bool)
            ):
                hooks_check_pass = pass_value
            if check_name == "codex_home_writable" and isinstance(pass_value, bool):
                codex_home_writable_check_pass = pass_value
        if multi_agent_check_pass is False:
            if payload.get("can_launch_step_agents") is True or payload.get(
                "can_launch_substep_agents"
            ) is True:
                raise ValueError(
                    "checks.multi_agent_enabled.pass=false is incompatible with launchable preflight"
                )
        if (
            backend_token == "codex"
            and hooks_check_pass is not True
            and (
                payload.get("can_launch_step_agents") is True
                or payload.get("can_launch_substep_agents") is True
            )
        ):
            raise ValueError(
                "checks.hooks_enabled.pass=true is required for codex launchable preflight"
            )
        if (
            backend_token == "codex"
            and codex_home_writable_check_pass is not True
            and (
                payload.get("can_launch_step_agents") is True
                or payload.get("can_launch_substep_agents") is True
            )
        ):
            raise ValueError(
                "checks.codex_home_writable.pass=true is required for codex launchable preflight"
            )
    elif (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
    ):
        raise ValueError(
            "checks must include multi_agent_enabled.pass=true when preflight is launchable"
        )

    if (
        payload.get("status") == "pass"
        and payload.get("can_launch_step_agents") is True
        and payload.get("can_launch_substep_agents") is True
    ):
        if not isinstance(feature_states, dict) or feature_states.get("multi_agent") is not True:
            raise ValueError(
                "feature_states.multi_agent=true is required when preflight is launchable"
            )
        hooks = feature_states.get("hooks")
        if hooks is None:
            hooks = feature_states.get("codex_hooks")
        if backend_token == "codex" and hooks is not True:
            raise ValueError(
                "feature_states.hooks=true is required when codex preflight is launchable"
            )
        if payload.get("sandbox_enforced") is not True:
            raise ValueError("sandbox_enforced=true is required when preflight is launchable")


def _live_preflight_mode() -> str:
    """Return the operation mode from the value of METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT.

    Return value: 'never' | 'always' | 'ttl'
    - 'never' : skip the probe
    - 'always': probe every time (ignore TTL, backward compatible)
    - 'ttl'   : probe with TTL cache (default)
    """
    raw = os.environ.get("METDSL_ORCHESTRATION_ENFORCE_LIVE_PREFLIGHT", "").strip().lower()
    if raw in {"0", "false", "no"}:
        return "never"
    if raw == "1":
        return "always"
    return "ttl"


def _live_preflight_ttl_seconds() -> int:
    """Read METDSL_PREFLIGHT_TTL_SECONDS and return a non-negative integer.

    Return PREFLIGHT_TTL_DEFAULT_SECONDS when unset or an invalid value.
    """
    raw = os.environ.get("METDSL_PREFLIGHT_TTL_SECONDS", "").strip()
    if not raw:
        return PREFLIGHT_TTL_DEFAULT_SECONDS
    try:
        value = int(raw)
        return max(0, value)
    except ValueError:
        return PREFLIGHT_TTL_DEFAULT_SECONDS


def _is_within_preflight_ttl(probed_at_iso: str, ttl_seconds: int) -> bool:
    """True if the elapsed seconds from probed_at_iso is less than ttl_seconds.

    Always False when ttl_seconds == 0 (no cache).
    False on a parse failure (err on the safe side and run the probe).
    """
    if ttl_seconds <= 0:
        return False
    try:
        probed_at = datetime.fromisoformat(probed_at_iso.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - probed_at).total_seconds()
        return elapsed < ttl_seconds
    except (ValueError, TypeError):
        return False


def _live_preflight_enforced() -> bool:
    """Backward-compatibility wrapper.

    New code should use _live_preflight_mode().
    """
    return _live_preflight_mode() != "never"


def _update_preflight_probed_at(
    repo_root: Path,
    orchestration_id: str,
    probed_at_iso: str,
) -> None:
    """Update only the probed_at field of preflight.json.

    Do not change other fields (status / can_launch_* etc.).
    Do nothing when preflight.json does not exist (not an error).
    """
    path = _preflight_path(repo_root, orchestration_id)
    if not path.exists():
        return
    try:
        file_payload = _read_json(path)
    except json.JSONDecodeError:
        return
    if not isinstance(file_payload, dict):
        return
    file_payload["probed_at"] = probed_at_iso
    _write_json(path, file_payload)


def _run_live_probe_and_update(
    repo_root: Path,
    orchestration_id: str,
    cached_payload: dict[str, Any],
) -> None:
    """Run a live probe and, on success, update the probed_at of preflight.json.

    Raise a RuntimeError on failure (the caller transitions the orchestration to fail).
    """
    backend = cached_payload.get("backend")
    if not isinstance(backend, str) or backend.strip() not in SUPPORTED_BACKENDS:
        backend = "codex"
    command = cached_payload.get("probe_command")
    probe_command = command.strip() if isinstance(command, str) and command.strip() else None

    live_probe = probe_execution_platform(
        backend=backend, agent_command=probe_command, repo_root=repo_root
    )
    if not _preflight_allows_agent_launch(live_probe):
        raise RuntimeError(
            "live preflight gate failed: execution platform multi_agent must be enabled at launch time"
        )
    probed_at = live_probe.get("checked_at") or _utc_now_iso()
    _update_preflight_probed_at(repo_root, orchestration_id, probed_at)


def _require_preflight_launchable(
    repo_root: Path,
    orchestration_id: str,
    *,
    enforce_live_probe: bool = True,
) -> dict[str, Any]:
    path = _preflight_path(repo_root, orchestration_id)
    if not path.exists():
        raise RuntimeError(f"preflight missing: {path}")
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"preflight must be object: {path}")
    if not _preflight_allows_agent_launch(payload):
        raise RuntimeError(
            "preflight gate failed: launchable preflight with multi_agent=true is required"
        )

    if not enforce_live_probe:
        return payload

    mode = _live_preflight_mode()

    if mode == "never":
        return payload

    if mode == "always":
        _run_live_probe_and_update(repo_root, orchestration_id, payload)
        return payload

    ttl_seconds = _live_preflight_ttl_seconds()
    probed_at = payload.get("probed_at")

    if isinstance(probed_at, str) and _is_within_preflight_ttl(probed_at, ttl_seconds):
        return payload

    _run_live_probe_and_update(repo_root, orchestration_id, payload)
    return payload


def get_preflight_ttl_status(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """For the preflight-status command: return the details of the TTL state."""
    mode = _live_preflight_mode()
    ttl_seconds = _live_preflight_ttl_seconds()
    path = _preflight_path(repo_root, orchestration_id)

    if not path.exists():
        return {
            "orchestration_id": orchestration_id,
            "preflight_exists": False,
            "live_probe_mode": mode,
            "ttl_seconds": ttl_seconds,
            "within_ttl": None,
            "ttl_remaining_seconds": None,
            "probe_skippable": False,
        }

    try:
        file_payload = _read_json(path)
    except json.JSONDecodeError:
        file_payload = {}

    probed_at = file_payload.get("probed_at") if isinstance(file_payload, dict) else None
    checked_at = file_payload.get("checked_at") if isinstance(file_payload, dict) else None
    preflight_status = file_payload.get("status") if isinstance(file_payload, dict) else None
    backend = file_payload.get("backend") if isinstance(file_payload, dict) else None

    within_ttl: bool | None = None
    ttl_remaining: float | None = None
    if mode == "ttl" and isinstance(probed_at, str):
        within_ttl = _is_within_preflight_ttl(probed_at, ttl_seconds)
        if within_ttl and ttl_seconds > 0:
            try:
                pa = datetime.fromisoformat(probed_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - pa).total_seconds()
                ttl_remaining = max(0.0, ttl_seconds - elapsed)
            except (ValueError, TypeError):
                ttl_remaining = None

    probe_skippable = mode == "never" or (mode == "ttl" and within_ttl is True)

    return {
        "orchestration_id": orchestration_id,
        "preflight_exists": True,
        "preflight_status": preflight_status,
        "backend": backend,
        "checked_at": checked_at,
        "probed_at": probed_at,
        "live_probe_mode": mode,
        "ttl_seconds": ttl_seconds,
        "within_ttl": within_ttl,
        "ttl_remaining_seconds": ttl_remaining,
        "probe_skippable": probe_skippable,
    }


def _launch_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/launches/{agent_run_id}"
    return f"{prefix}.request.json", f"{prefix}.response.json"


def _launch_dialog_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/launches/{agent_run_id}"
    return f"{prefix}.prompt.txt", f"{prefix}.reply.txt"


def _child_launch_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/child"
    return f"{prefix}.request.json", f"{prefix}.response.json"


def _child_dialog_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/child"
    return f"{prefix}.prompt.txt", f"{prefix}.reply.txt"


def _agent_result_refs(orchestration_id: str, agent_run_id: str) -> tuple[str, str]:
    prefix = f"workspace/orchestrations/{orchestration_id}/agents/{agent_run_id}/dialogs/agent"
    return f"{prefix}.result.json", f"{prefix}.summary.txt"


def _coerce_launch_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if isinstance(value, (bool, int, float)):
        return str(value)
    return None


def _coerce_nested_launch_text(payload: dict[str, Any], path: tuple[str, ...]) -> str | None:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _coerce_launch_text(current)


def _is_placeholder_ref(value: str) -> bool:
    token = value.strip()
    if not token:
        return False
    return "agent-determined" in token or ("<" in token and ">" in token)


def _launch_prompt_template_path() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "skills"
        / "workflow-orchestration"
        / "references"
        / "launch_prompts.md"
    )


@lru_cache(maxsize=1)
def _load_launch_prompt_templates() -> dict[str, str]:
    """Load the templates and the shared boilerplate from launch_prompts.md.

    Returns:
        dict with keys "step agent", "substep agent", and "common boilerplate".
        The "step agent" / "substep agent" values contain the `{{COMMON_BOILERPLATE}}` placeholder.
        The "common boilerplate" value contains the `{{ACTOR_ROLE}}` placeholder.
    """
    text = _launch_prompt_template_path().read_text(encoding="utf-8")
    pattern = re.compile(
        r"## `(?P<name>step agent|substep agent)` launch request template\s+```text\n(?P<body>.*?)\n```",
        re.DOTALL,
    )
    templates: dict[str, str] = {}
    for match in pattern.finditer(text):
        templates[match.group("name")] = match.group("body")
    if set(templates) != {"step agent", "substep agent"}:
        raise RuntimeError("launch prompt templates must define step agent and substep agent")
    # Extract the shared boilerplate section (## Common agent contract boilerplate).
    shared_pattern = re.compile(
        r"## Common agent contract boilerplate\b.*?```text\n(?P<body>.*?)\n```",
        re.DOTALL,
    )
    shared_match = shared_pattern.search(text)
    if shared_match is None:
        raise RuntimeError(
            "launch_prompts.md must define '## Common agent contract boilerplate' "
            "with a ```text block for {{COMMON_BOILERPLATE}} expansion"
        )
    templates["common boilerplate"] = shared_match.group("body")
    return templates


def _launch_prompt_template_name(request_payload: dict[str, Any]) -> str:
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return "substep agent"
    return "step agent"


def _template_placeholder_values(request_payload: dict[str, Any]) -> dict[str, str]:
    orchestration_id = str(request_payload.get("orchestration_id", ""))
    agent_run_id = str(request_payload.get("agent_run_id", ""))
    allowed_tmp_root = f"workspace/tmp/{agent_run_id}" if agent_run_id else ""
    capability_doc_path = (
        f"workspace/orchestrations/{orchestration_id}/capabilities/{agent_run_id}.json"
        if orchestration_id and agent_run_id else ""
    )
    output_manifest_path = (
        f"workspace/orchestrations/{orchestration_id}/output_manifests/{agent_run_id}.json"
        if orchestration_id and agent_run_id else ""
    )
    read_manifest_path = (
        f"workspace/orchestrations/{orchestration_id}/read_manifests/{agent_run_id}.json"
        if orchestration_id and agent_run_id else ""
    )
    return {
        "node_key": str(request_payload.get("node_key", "")),
        "step": str(request_payload.get("step", "")),
        "substep": str(request_payload.get("substep", "")),
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "parent_agent_run_id": str(request_payload.get("parent_agent_run_id", "")),
        "workflow_mode": str(request_payload.get("workflow_mode", "")),
        "ir_ref": str(request_payload.get("ir_ref", "")),
        "pipeline_ref": str(request_payload.get("pipeline_ref", "")),
        "dependency_ref": str(request_payload.get("dependency_ref", "")),
        "skill_name": str(request_payload.get("skill_name", "")),
        "skill_ref": str(request_payload.get("skill_ref", "")),
        "skill_must_read_refs": str(request_payload.get("skill_must_read_refs", "")),
        "issue_severity": str(request_payload.get("issue_severity", "")),
        "repair_strategy": str(request_payload.get("repair_strategy", "")),
        "repair_target_agent_run_id": str(request_payload.get("repair_target_agent_run_id", "")),
        "repair_reason": str(request_payload.get("repair_reason", "")),
        # Paths injected so agents can read them directly from the launch prompt.
        "allowed_tmp_root": allowed_tmp_root,
        "capability_doc_path": capability_doc_path,
        "output_manifest_path": output_manifest_path,
        "read_manifest_path": read_manifest_path,
    }


def _render_launch_prompt_template(request_payload: dict[str, Any]) -> str:
    """Render the launch prompt template.

    Expansion order:
    1. Determine the template name (step / substep)
    2. Replace `{{ACTOR_ROLE}}` of the shared boilerplate with the role string
    3. Replace `{{COMMON_BOILERPLATE}}` of the template body with the expanded boilerplate
    4. Replace the `<key>` placeholders with the values of request_payload
    """
    templates = _load_launch_prompt_templates()
    template_name = _launch_prompt_template_name(request_payload)
    template = templates[template_name]
    # Resolve actor_role for the common boilerplate's {{ACTOR_ROLE}} placeholder.
    actor_role = "substep" if template_name == "substep agent" else "step"
    common_boilerplate = templates["common boilerplate"].replace("{{ACTOR_ROLE}}", actor_role)
    # Expand {{COMMON_BOILERPLATE}} in the template.
    rendered = template.replace("{{COMMON_BOILERPLATE}}", common_boilerplate)
    # Apply <key> placeholder substitutions from the request payload.
    for key, value in _template_placeholder_values(request_payload).items():
        rendered = rendered.replace(f"<{key}>", value)
    return rendered


def build_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    return _render_launch_prompt_template(request_payload).split("\n\n", 1)[0]


def _skill_name_for_request(request_payload: dict[str, Any]) -> str | None:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return None
    step_token = step.strip().lower()
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return f"workflow-{step_token}-{substep.strip().lower()}"
    return f"workflow-{step_token}"


def _required_verify_skill_refs(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    substep = request_payload.get("substep")
    ir_ref = request_payload.get("ir_ref")
    if (
        not isinstance(step, str)
        or step.strip().lower() not in {"compile", "generate"}
        or not isinstance(substep, str)
        or substep.strip().lower() != "verify"
        or not isinstance(ir_ref, str)
        or not ir_ref.strip()
    ):
        return []
    ir_root = ir_ref.strip().rstrip("/")
    refs = [
        f"{ir_root}/spec.ir.yaml",
    ]
    if step.strip().lower() == "generate":
        pipeline_ref = request_payload.get("pipeline_ref")
        source_id = request_payload.get("source_id")
        if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
            raise ValueError("generate verify launch request must include non-empty pipeline_ref")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("generate verify launch request must include non-empty source_id")
        pr = pipeline_ref.strip().rstrip("/")
        sid = source_id.strip()
        refs.extend(
            [
                f"{pr}/lineage.json",
                f"{pr}/source/{sid}/source_meta.json",
            ]
        )
    return refs


def _merge_unique_refs(*ref_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in ref_groups:
        for ref in group:
            token = ref.strip()
            if not token or token in seen:
                continue
            merged.append(token)
            seen.add(token)
    return merged


def _workflow_contract_refs_for_launch(request_payload: dict[str, Any]) -> list[str]:
    # The child contract is docs/AGENT_CONTRACT.md (the canonical, child-readable
    # agent contract). docs/ORCHESTRATION.md is the orchestrator/conductor design
    # spec and is NOT included here — no step/substep agent reads it (audit: 0/6
    # substeps did), so listing it only bloated every launch prompt's must_read.
    refs = [WORKFLOW_CORE_REF, "docs/AGENT_CONTRACT.md"]
    step = request_payload.get("step")
    if isinstance(step, str) and step.strip():
        phase_doc = WORKFLOW_PHASE_DOC_BY_STEP.get(step.strip().lower())
        if phase_doc:
            refs.append(phase_doc)
    return refs


def build_skill_must_read_refs(request_payload: dict[str, Any]) -> list[str]:
    skill_ref = request_payload.get("skill_ref")
    skill_refs = [skill_ref.strip()] if isinstance(skill_ref, str) and skill_ref.strip() else []
    existing_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))
    common_refs = _workflow_contract_refs_for_launch(request_payload)
    verify_refs = _required_verify_skill_refs(request_payload)
    return _merge_unique_refs(skill_refs, common_refs, existing_refs, verify_refs)


def render_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    return _render_launch_prompt_template(request_payload)


def prepare_launch_request_payload(request_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(request_payload)
    if not isinstance(payload.get("skill_name"), str) or not payload.get("skill_name", "").strip():
        skill_name = _skill_name_for_request(payload)
        if skill_name is not None:
            payload["skill_name"] = skill_name
    if not isinstance(payload.get("skill_ref"), str) or not payload.get("skill_ref", "").strip():
        skill_name = payload.get("skill_name")
        if isinstance(skill_name, str) and skill_name.strip():
            payload["skill_ref"] = f"skills/{skill_name.strip()}/SKILL.md"
    payload.setdefault("issue_severity", "none")
    payload.setdefault("workflow_mode", os.environ.get("METDSL_WORKFLOW_EXEC_MODE", "dev"))
    payload.setdefault("repair_strategy", "none")
    payload.setdefault("repair_target_agent_run_id", "none")
    payload.setdefault("repair_reason", "none")
    payload["skill_must_read_refs"] = ",".join(build_skill_must_read_refs(payload))
    explicit_prompt_present = any(
        _coerce_nested_launch_text(payload, path) is not None
        for path in (
            ("launch_prompt_full",),
            ("execution_prompt",),
            ("prompt",),
            ("task",),
            ("instruction",),
            ("instructions",),
            ("message",),
            ("spawn_request", "prompt"),
            ("spawn_request", "task"),
            ("spawn_request", "instruction"),
            ("spawn_request", "instructions"),
            ("spawn_request", "message"),
            ("launch_prompt",),
        )
    )
    if not explicit_prompt_present:
        payload["launch_prompt_full"] = render_launch_prompt_text(payload)
    return payload


def _extract_launch_prompt_text(request_payload: dict[str, Any]) -> str:
    # Prefer explicit full execution prompts, then fall back to short launch summaries.
    for path in (
        ("launch_prompt_full",),
        ("execution_prompt",),
        ("prompt",),
        ("task",),
        ("instruction",),
        ("instructions",),
        ("message",),
        ("spawn_request", "prompt"),
        ("spawn_request", "task"),
        ("spawn_request", "instruction"),
        ("spawn_request", "instructions"),
        ("spawn_request", "message"),
        ("launch_prompt",),
    ):
        text = _coerce_nested_launch_text(request_payload, path)
        if text is not None:
            return text
    return json.dumps(request_payload, ensure_ascii=False, indent=2)


def _extract_launch_reply_text(response_payload: dict[str, Any]) -> str:
    for key in ("launch_reply", "reply", "response_text", "message", "result"):
        text = _coerce_launch_text(response_payload.get(key))
        if text is not None:
            return text
    return json.dumps(response_payload, ensure_ascii=False, indent=2)


def _extract_response_agent_session_id(response_payload: dict[str, Any]) -> str | None:
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
        current: Any = response_payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
    return None


def _validate_response_agent_session_id(response_payload: dict[str, Any]) -> str:
    session_id = _extract_response_agent_session_id(response_payload)
    if session_id is None:
        raise ValueError("launch response must include child agent identifier from spawn_agent")
    if _is_placeholder_ref(session_id):
        raise ValueError("launch response child agent identifier must not contain placeholder tokens")
    return session_id


def _required_launch_prompt_markers(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
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
    substep = request_payload.get("substep")
    if isinstance(substep, str) and substep.strip():
        return [
            "You are a substep agent.",
            "Target node_key:",
            "Target step:",
            "Target substep:",
            *markers,
        ]
    return [
        "You are a step agent.",
        "Target node_key:",
        "Target step:",
        *markers,
    ]


def _required_launch_prompt_lines(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
    # Backward compatibility: workflow_mode line is recommended but not mandatory
    # for manually provided legacy launch prompts.
    return [
        line
        for line in build_launch_prompt_text(request_payload).splitlines()
        if not line.strip().startswith("workflow_mode:")
    ]


def _required_launch_prompt_constraint_lines(request_payload: dict[str, Any]) -> list[str]:
    step = request_payload.get("step")
    if not isinstance(step, str) or not step.strip():
        return []
    required_fragments = (
        "`run-gate --gate apply_patch_writes` and `apply-patch-gate`",
        "`output_manifests/",
        "/capabilities/",
        "`capability_token` is not obtained or mismatched: do not start processing and stop with fail",
        "`.json` and `.txt` output",
        "`.yaml` / `.yml` / `.md` and source code",
    )
    return [
        line
        for line in render_launch_prompt_text(request_payload).splitlines()
        if any(fragment in line for fragment in required_fragments)
        and "may be read directly" not in line
    ]


# Issue 1 of the recurrence-prevention plan: per-(step, substep) allowed
# `validate_pipeline_semantics --stage <X>` invocations. Canonical source:
# `skills/workflow-orchestration/references/launch_prompts.md` "substep ↔
# allowed validator gate correspondence table". `record-launch` rejects any launch
# prompt where an actionable invocation line targets a stage outside the
# substep's allow-set. Override with env `METDSL_ENFORCE_GATE_ALLOWLIST=0`
# for emergency rollback.
#
# Two canonical invocation forms are detected (Codex review round 2 P2):
#   (a) Direct CLI:   `python3 tools/validate_pipeline_semantics.py
#                      --stage <X> ...`
#   (b) run-gate:     `python3 tools/orchestration_runtime.py run-gate
#                      --gate validate_pipeline_semantics ...
#                      --args-json '{"stage": "<X>", ...}'`
#
# Negative-constraint prose ("do not run `validate_pipeline_semantics
# --stage compile`") is not flagged: each line is pre-filtered for
# actionable markers (`python3` / `tools/...py` / `--gate
# validate_pipeline_semantics`) before stage extraction.
# Invocation-presence detectors (do NOT capture stage). Each match marks
# the START of an actionable `validate_pipeline_semantics` invocation.
# Stage extraction is then performed in a windowed lookahead so multi-line
# commands (with `\` continuation or wrapped --args-json) are handled
# correctly (Codex review round 7 P1).
#
# The direct-CLI invocation form requires the `.py` suffix AND a
# `python3` token on the same line. The run-gate form requires the
# `--gate validate_pipeline_semantics` argument PLUS a `python3` token on
# the same line or in the immediately preceding lines (covering Bash
# backslash-continued invocations). Both checks ensure narrative
# mentions of `validate_pipeline_semantics.py` in documentation prose are
# never flagged.
_DIRECT_INVOCATION_RE = re.compile(r"validate_pipeline_semantics\.py\b")
_RUN_GATE_INVOCATION_RE = re.compile(r"--gate\s+validate_pipeline_semantics\b")
_PYTHON3_INVOCATION_TOKEN_RE = re.compile(r"python3\b")

# Stage value extractors. Each produces a single capturing group with the
# stage token. The patterns use a permissive lookahead body so they
# tolerate wrapped commands (line continuations / multiline JSON args);
# the caller bounds the search window before invoking these patterns.
_DIRECT_STAGE_RE = re.compile(
    r"validate_pipeline_semantics\.py\b.*?--stage\s+(\w+)",
    re.DOTALL,
)
# Tolerate the shell double-quoted form where JSON quotes are
# backslash-escaped (e.g. `--args-json "{\"stage\":\"compile\"}"`) in
# addition to the single-quoted form (`--args-json '{"stage":...}'`).
# The optional `\\?` before each quote captures the escape character
# when present. Codex review round 15 P1.
_RUN_GATE_STAGE_RE = re.compile(
    r"--gate\s+validate_pipeline_semantics\b.*?"
    r"\\?[\"']stage\\?[\"']\s*:\s*\\?[\"'](\w+)\\?[\"']",
    re.DOTALL,
)

# Default stage assumed when an actionable validate_pipeline_semantics
# invocation omits `--stage` / `"stage"` — mirrors the argparse default
# in `tools/validate_pipeline_semantics.py`. Used only when no stage is
# discoverable in the post-invocation window (Codex review round 4 P1).
_VALIDATE_PIPELINE_DEFAULT_STAGE = "full"

# Lookahead window (in characters) used when scanning for `--stage` /
# `"stage"` after an invocation marker. Bounded so a far-away invocation
# elsewhere in the prompt cannot accidentally satisfy a later one.
_STAGE_LOOKAHEAD_BYTES = 500

# Lookback window (in characters) used to detect that an invocation
# (direct `validate_pipeline_semantics.py` reference or `--gate
# validate_pipeline_semantics` argument) was actually launched by a
# `python3 ...` command spread over preceding lines via Bash
# backslash-continuation. Applies to both invocation forms (Codex review
# round 8 P2).
_INVOCATION_PYTHON3_LOOKBACK_BYTES = 300

# Negation markers — when any appears on the same line as an invocation
# marker the line is treated as descriptive prose (e.g. "do not run X")
# and skipped (Codex review round 7 P2).
_NEGATION_MARKERS: tuple[str, ...] = (
    "do not ",
    "don't ",
    "do not.",
    "should not ",
    "shouldn't ",
    "must not ",
    "mustn't ",
    "forbidden",
    "ng:",
)


def _line_around(text: str, position: int) -> tuple[int, int, str]:
    """Return `(line_start, line_end, line)` for the line containing
    character `position` in `text`."""
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    if line_end == -1:
        line_end = len(text)
    return line_start, line_end, text[line_start:line_end]


def _has_negation_marker(line: str) -> bool:
    lower = line.lower()
    return any(marker in lower for marker in _NEGATION_MARKERS)


def _find_stage_near_invocation(
    text: str,
    invocation_start: int,
    stage_re: re.Pattern[str],
) -> str | None:
    """Search for a stage value in the window beginning at
    `invocation_start`. The window terminates at the earliest of:
    `_STAGE_LOOKAHEAD_BYTES` chars, a paragraph break (`\\n\\n`), or the
    next invocation marker of either form. Returns the matched stage
    token or None."""
    end = min(len(text), invocation_start + _STAGE_LOOKAHEAD_BYTES)
    paragraph_break = text.find("\n\n", invocation_start)
    if paragraph_break != -1 and paragraph_break < end:
        end = paragraph_break
    for next_re in (_DIRECT_INVOCATION_RE, _RUN_GATE_INVOCATION_RE):
        m = next_re.search(text, invocation_start + 1)
        if m and m.start() < end:
            end = m.start()
    window = text[invocation_start:end]
    match = stage_re.search(window)
    if match is None:
        return None
    return match.group(1)

# Allowed `--stage` values per (step, substep). Authoritative sources:
# `tools/validate_pipeline_semantics.py` argparse choices = {`compile`,
# `post_generate`, `post_build`, `post_execute`, `pre_judge`, `full`} (see
# also `docs/CLI_REFERENCE.md`). A substep absent from this map is
# unconstrained — only substeps where the workflow explicitly forbids
# `validate_pipeline_semantics` invocation or restricts the stage are
# listed.
ALLOWED_VALIDATE_PIPELINE_STAGES: dict[tuple[str, str], frozenset[str]] = {
    # Strict per-substep mapping. Authoritative source: the "substep ↔
    # allowed validator gate correspondence table" in
    # `skills/workflow-orchestration/references/launch_prompts.md`. Each
    # substep is restricted to the single canonical `--stage` it owns;
    # cross-substep / `full` invocations are rejected at `record-launch`
    # because they widen the substep's responsibility surface beyond the
    # recurrence-prevention contract. (The broader per-step allow-set for
    # `write-step-result`'s `validation_stage` field is a separate
    # recording-layer contract; the launch-prompt layer enforced here is
    # strictly per-substep.)
    #
    # Compile.generate / Generate.generate must not invoke
    # `validate_pipeline_semantics` at all — that responsibility lies with
    # the corresponding verify substep. This was the exact pattern that
    # triggered the original `noncanonical_phase_write_attempt` failure.
    ("compile", "generate"): frozenset(),
    ("compile", "verify"): frozenset({"compile"}),
    ("generate", "generate"): frozenset(),
    ("generate", "verify"): frozenset({"post_generate"}),
    ("build", ""): frozenset({"post_build"}),
    ("validate", "execute"): frozenset({"post_execute"}),
    ("validate", "judge"): frozenset({"pre_judge"}),
}


def _iter_validate_pipeline_invocations(prompt_text: str) -> list[str]:
    """Yield the stage targeted by each actionable
    `validate_pipeline_semantics` invocation in `prompt_text`. Both
    canonical invocation forms (direct CLI and `run-gate --args-json`) are
    supported. The list ordering is unspecified.

    An invocation is recognized only when:
      - The direct-CLI marker `validate_pipeline_semantics.py` (or the
        run-gate marker `--gate validate_pipeline_semantics`) appears on
        a line that also contains `python3` (or the run-gate marker is
        preceded by `python3` within `_RUN_GATE_LOOKBACK_BYTES`, covering
        Bash backslash-continued multi-line commands), AND
      - The line containing the marker has no negation marker (e.g.
        "do not run …", "forbidden", "must not") — such lines are treated
        as documentation prose (Codex review round 7 P2).

    For each accepted invocation the stage is extracted from a bounded
    forward window so wrapped `--args-json '{"stage": ...}'` blocks are
    parsed correctly (Codex review round 7 P1). When no stage can be
    found the validator's argparse default (`full`) is reported, ensuring
    omitted-stage invocations are still subjected to the allow-set check
    (Codex review round 4 P1).
    """
    stages: list[str] = []

    def _python3_visible(marker_start: int, line_start: int, line: str) -> bool:
        """Return True when a `python3` token is on the marker's line OR
        within `_INVOCATION_PYTHON3_LOOKBACK_BYTES` chars before the
        marker (covering Bash backslash-continuation). Codex review
        round 8 P2 — same lookback applies to both invocation forms."""
        if _PYTHON3_INVOCATION_TOKEN_RE.search(line):
            return True
        lookback_start = max(0, line_start - _INVOCATION_PYTHON3_LOOKBACK_BYTES)
        preceding = prompt_text[lookback_start:marker_start]
        return _PYTHON3_INVOCATION_TOKEN_RE.search(preceding) is not None

    # Direct-CLI form: invocation marker + python3 (same line or lookback)
    # + stage extractor.
    for m in _DIRECT_INVOCATION_RE.finditer(prompt_text):
        line_start, _, line = _line_around(prompt_text, m.start())
        if not _python3_visible(m.start(), line_start, line):
            continue
        if _has_negation_marker(line):
            continue
        stage = _find_stage_near_invocation(prompt_text, m.start(), _DIRECT_STAGE_RE)
        stages.append(stage if stage else _VALIDATE_PIPELINE_DEFAULT_STAGE)

    # run-gate form: same `python3`-visibility rule (line or lookback).
    for m in _RUN_GATE_INVOCATION_RE.finditer(prompt_text):
        line_start, _, line = _line_around(prompt_text, m.start())
        if not _python3_visible(m.start(), line_start, line):
            continue
        if _has_negation_marker(line):
            continue
        stage = _find_stage_near_invocation(prompt_text, m.start(), _RUN_GATE_STAGE_RE)
        stages.append(stage if stage else _VALIDATE_PIPELINE_DEFAULT_STAGE)

    return stages


def _lint_launch_prompt_gate_allowlist(
    prompt_text: str,
    *,
    step: str,
    substep: str | None,
) -> list[str]:
    """Return a list of human-readable violation descriptions when the
    prompt contains an actionable `validate_pipeline_semantics` invocation
    targeting a stage that is not in this substep's allow-set. Empty list
    means the prompt is clean. Issue 1 of the recurrence-prevention plan.

    The scan recognizes both canonical invocation forms (direct CLI and
    `run-gate --args-json`), tolerates multi-line wrapping of long
    commands (Codex review round 7 P1), and excludes documentation prose
    that quotes the forbidden form via negation markers (Codex review
    round 7 P2)."""
    if os.environ.get("METDSL_ENFORCE_GATE_ALLOWLIST", "1").strip() == "0":
        return []
    key = (
        step.strip().lower() if isinstance(step, str) else "",
        (substep or "").strip().lower() if isinstance(substep, str) else "",
    )
    if key not in ALLOWED_VALIDATE_PIPELINE_STAGES:
        return []
    allowed_stages = ALLOWED_VALIDATE_PIPELINE_STAGES[key]
    violations: list[str] = []
    for stage in _iter_validate_pipeline_invocations(prompt_text):
        if stage.lower() not in allowed_stages:
            violations.append(
                f"validate_pipeline_semantics stage={stage!r} (allowed for "
                f"step={key[0]!r} substep={key[1]!r}: "
                f"{sorted(allowed_stages) or '(none — gate forbidden)'})"
            )
    return violations


def _validate_launch_prompt_text(request_payload: dict[str, Any], prompt_text: str) -> None:
    required_markers = _required_launch_prompt_markers(request_payload)
    if not required_markers:
        # Even when no template markers are required (e.g. orchestration
        # agent self-prompts), still apply the gate-allowlist lint when
        # step/substep are declared — this is the canonical recurrence-
        # prevention guard for Issue 1.
        step_raw = request_payload.get("step")
        substep_raw = request_payload.get("substep")
        if isinstance(step_raw, str) and step_raw.strip():
            violations = _lint_launch_prompt_gate_allowlist(
                prompt_text, step=step_raw, substep=substep_raw if isinstance(substep_raw, str) else None
            )
            if violations:
                raise ValueError(
                    f"launch prompt for step={step_raw!r} substep={substep_raw!r} "
                    f"violates substep ↔ allowed validator gate correspondence table: {violations}. "
                    f"See skills/workflow-orchestration/references/launch_prompts.md "
                    f"for the canonical allowlist."
                )
        return
    missing_markers = [marker for marker in required_markers if marker not in prompt_text]
    if missing_markers:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration template markers: "
            + ", ".join(missing_markers)
        )
    required_lines = _required_launch_prompt_lines(request_payload)
    missing_lines = [line for line in required_lines if line not in prompt_text]
    if missing_lines:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration template field values: "
            + ", ".join(missing_lines)
        )
    required_constraint_lines = _required_launch_prompt_constraint_lines(request_payload)
    missing_constraint_lines = [line for line in required_constraint_lines if line not in prompt_text]
    if missing_constraint_lines:
        raise ValueError(
            "launch prompt text must preserve workflow-orchestration shell-write constraints: "
            + ", ".join(missing_constraint_lines)
        )
    # Issue 1 of the recurrence-prevention plan: regex-based forbidden
    # validator-gate lint. Runs after marker / line / constraint checks so
    # missing-template errors retain their pre-existing precedence.
    step_raw = request_payload.get("step")
    substep_raw = request_payload.get("substep")
    if isinstance(step_raw, str) and step_raw.strip():
        hits = _lint_launch_prompt_gate_allowlist(
            prompt_text, step=step_raw, substep=substep_raw if isinstance(substep_raw, str) else None
        )
        if hits:
            raise ValueError(
                f"launch prompt for step={step_raw!r} substep={substep_raw!r} "
                f"contains forbidden gate keyword(s): {hits}. "
                f"See skills/workflow-orchestration/references/launch_prompts.md "
                f"`substep ↔ allowed validator gate correspondence table` for the canonical allowlist."
            )


def _extract_agent_summary_text(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in (
        "agent_run_id",
        "agent_role",
        "node_key",
        "step",
        "substep",
        "status",
        "agent_backend",
        "agent_model",
        "context_id",
        "agent_session_id",
        "started_at",
        "finished_at",
        "result_summary",
    ):
        value = payload.get(key)
        if value is None:
            continue
        lines.append(f"{key}: {value}")

    output_refs = payload.get("output_refs")
    if isinstance(output_refs, list) and output_refs:
        lines.append("output_refs:")
        for item in output_refs:
            if isinstance(item, str) and item.strip():
                lines.append(f"- {item.strip()}")

    if lines:
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_agent_summary_text(payload: dict[str, Any], summary_text: str) -> None:
    text = summary_text.strip()
    if not text:
        raise ValueError("agent.summary.txt must be non-empty")
    agent_role = payload.get("agent_role")
    if isinstance(agent_role, str) and agent_role.strip().lower() == "skipped_by_checkpoint":
        return

    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(non_empty_lines) < 2:
        raise ValueError("agent.summary.txt must not be single-line summary")

    status = payload.get("status")
    if isinstance(status, str) and status.strip():
        marker = f"status: {status.strip()}"
        if marker not in text:
            raise ValueError("agent.summary.txt must include final status line")

    output_refs = payload.get("output_refs")
    if isinstance(output_refs, list) and any(isinstance(item, str) and item.strip() for item in output_refs):
        if "output_refs:" not in text:
            raise ValueError("agent.summary.txt must include output_refs section for pass result")
    elif (
        isinstance(status, str)
        and status.strip().lower() in TERMINAL_STATUSES
        and not any(token in text for token in ("result_summary:", "summary:", "reason:", "failure_reason:"))
    ):
        raise ValueError("agent.summary.txt must include summary or failure reason")


def _evaluate_reply_budget(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    reply_ref: str,
) -> dict[str, Any] | None:
    """Measure a child's verbatim reply against REPLY_BUDGET_CHARS.

    Returns ``{"chars": n, "budget": REPLY_BUDGET_CHARS}`` when the reply exceeds the
    budget (so record_agent_run can surface it as telemetry), else ``None``. Always
    appends an audit entry on an over-budget reply. With METDSL_ENFORCE_REPLY_BUDGET=1
    it raises instead of returning, turning the soft warning into a hard fail (the
    orchestration must then re-launch the child with a terse final message). A missing /
    unreadable reply file is treated as under budget (never blocks on absence).
    """
    path = repo_root / reply_ref
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    n = len(text)
    if n <= REPLY_BUDGET_CHARS:
        return None
    info = {"chars": n, "budget": REPLY_BUDGET_CHARS}
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="reply_over_budget",
        status="warn",
        detail={"agent_run_id": agent_run_id, **info},
    )
    if os.environ.get("METDSL_ENFORCE_REPLY_BUDGET") == "1":
        raise ValueError(
            f"record-agent-run: child reply is {n} chars, over the {REPLY_BUDGET_CHARS}-char budget "
            f"(METDSL_ENFORCE_REPLY_BUDGET=1). Re-launch the child with a terse final message — a status "
            f"line, output_refs, and a few lines of rationale; full detail belongs in the child's artifacts."
        )
    return info


def _split_skill_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        refs: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                refs.append(item.strip())
        return refs
    return []


def _node_key_to_safe(node_key: str) -> str:
    spec_kind, spec_id, spec_version = _parse_node_key_strict(node_key)
    return f"{spec_kind}__{spec_id}__{spec_version}"


def update_checkpoint(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """After write_step_result completes, append/update a completion entry to the checkpoint.

    Record only when status=pass. Otherwise return immediately.

    Overwrite when an entry for the same (node_key, step) already exists.
    """
    status = result.get("status")
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return {}

    node_safe = _node_key_to_safe(node_key)
    output_refs: list[str] = []
    required = result.get("required_outputs")
    if isinstance(required, list) and required:
        output_refs = [r.strip() for r in required if isinstance(r, str) and r.strip()]
    if not output_refs:
        raw = result.get("output_refs")
        if isinstance(raw, list):
            output_refs = [r.strip() for r in raw if isinstance(r, str) and r.strip()]

    ir_ref = str(result.get("ir_ref") or "")
    pipeline_ref = str(result.get("pipeline_ref") or "")

    if not ir_ref or not pipeline_ref:
        lr_ref = result.get("launch_request_ref")
        if isinstance(lr_ref, str) and lr_ref.strip():
            lr_path = repo_root / lr_ref.strip()
            if lr_path.exists():
                try:
                    lr_data = _read_json(lr_path)
                    if isinstance(lr_data, dict):
                        ir_ref = ir_ref or str(lr_data.get("ir_ref") or "")
                        pipeline_ref = pipeline_ref or str(
                            lr_data.get("pipeline_ref") or ""
                        )
                except json.JSONDecodeError:
                    pass

    artifact_hashes = _build_artifact_hashes(repo_root, output_refs)

    entry: dict[str, Any] = {
        "node_key": node_key.strip(),
        "node_key_safe": node_safe,
        "step": step.strip().lower(),
        "agent_run_id": agent_run_id.strip(),
        "status": "pass",
        "completed_at": _utc_now_iso(),
        "ir_ref": ir_ref.strip(),
        "pipeline_ref": pipeline_ref.strip(),
        "output_refs": output_refs,
        "artifact_hashes": artifact_hashes,
    }

    path = _checkpoint_path(repo_root, orchestration_id)
    checkpoint = _load_checkpoint(repo_root, orchestration_id) or {
        "orchestration_id": orchestration_id,
        "schema_version": "1",
        "completed_steps": [],
    }

    steps: list[dict[str, Any]] = list(checkpoint.get("completed_steps", []))
    steps = [
        s
        for s in steps
        if not (s.get("node_key") == entry["node_key"] and s.get("step") == entry["step"])
    ]
    steps.append(entry)
    checkpoint["completed_steps"] = steps
    checkpoint["last_updated_at"] = _utc_now_iso()

    _write_json(path, checkpoint)
    return entry


def _guard_checkpoint_read_requires_resume(repo_root: Path, orchestration_id: str) -> None:
    ck_path = _checkpoint_path(repo_root, orchestration_id)
    if not ck_path.is_file():
        return
    try:
        ck = _read_json(ck_path)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(ck, dict):
        return
    steps = ck.get("completed_steps")
    if not isinstance(steps, list) or not steps:
        return
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.is_file():
        return
    try:
        meta = _read_json(meta_path)
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(meta, dict) and meta.get("resume_enabled") is True:
        return
    raise RuntimeError(
        "read_checkpoint forbidden unless orchestration_meta.resume_enabled is true "
        f"(orchestration_id={orchestration_id!r})"
    )


def read_checkpoint(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any] | None:
    """Read and return orchestration_checkpoint.json. None if it does not exist."""
    _guard_checkpoint_read_requires_resume(repo_root, orchestration_id)
    return _load_checkpoint(repo_root, orchestration_id)


def verify_checkpoint_integrity(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """Recompute all artifact hashes of the checkpoint and verify the integrity."""
    checkpoint = _load_checkpoint(repo_root, orchestration_id)
    if checkpoint is None:
        return {
            "orchestration_id": orchestration_id,
            "valid": False,
            "error": "orchestration_checkpoint.json not found",
            "steps": [],
        }

    step_results: list[dict[str, Any]] = []
    all_ok = True

    for entry in checkpoint.get("completed_steps", []):
        node_key = entry.get("node_key", "")
        step = entry.get("step", "")
        stored_hashes: dict[str, str] = entry.get("artifact_hashes", {})
        if not isinstance(stored_hashes, dict):
            stored_hashes = {}
        mismatches: list[dict[str, str]] = []
        missing: list[str] = []

        for ref, expected_hash in stored_hashes.items():
            if not isinstance(ref, str):
                continue
            if not isinstance(expected_hash, str):
                continue
            if expected_hash == "sha256:missing":
                missing.append(ref)
                continue
            actual_hash = _compute_sha256(repo_root / ref)
            if actual_hash != expected_hash:
                mismatches.append(
                    {
                        "ref": ref,
                        "expected": expected_hash,
                        "actual": actual_hash,
                    }
                )

        if missing:
            integrity = "missing_artifacts"
            all_ok = False
        elif mismatches:
            integrity = "stale"
            all_ok = False
        else:
            integrity = "ok"

        step_results.append(
            {
                "node_key": node_key,
                "step": step,
                "integrity": integrity,
                "mismatches": mismatches,
                "missing_artifacts": missing,
            }
        )

    return {
        "orchestration_id": orchestration_id,
        "valid": all_ok,
        "steps": step_results,
    }


def check_step_completed(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    verify_integrity: bool = True,
) -> dict[str, Any] | None:
    """Return the completion status of the specified (node_key, step).

    Return None when incomplete or when the checkpoint does not exist.
    When verify_integrity=True, perform hash verification and return None if stale.
    """
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if meta_path.exists():
        try:
            meta = _read_json(meta_path)
            if isinstance(meta, dict) and not meta.get("resume_enabled"):
                return None
        except json.JSONDecodeError:
            return None
    else:
        return None

    checkpoint = _load_checkpoint(repo_root, orchestration_id)
    if checkpoint is None:
        return None

    node_key_norm = node_key.strip()
    step_norm = step.strip().lower()

    entry = next(
        (
            s
            for s in checkpoint.get("completed_steps", [])
            if s.get("node_key") == node_key_norm and s.get("step") == step_norm
        ),
        None,
    )
    if entry is None:
        return None

    if verify_integrity:
        stored_hashes: dict[str, str] = entry.get("artifact_hashes", {})
        if not isinstance(stored_hashes, dict):
            return None
        for ref, expected_hash in stored_hashes.items():
            if not isinstance(ref, str) or not isinstance(expected_hash, str):
                return None
            if expected_hash == "sha256:missing":
                continue
            actual_hash = _compute_sha256(repo_root / ref)
            if actual_hash != expected_hash:
                return None

    return {
        "node_key": entry.get("node_key"),
        "step": entry.get("step"),
        "agent_run_id": entry.get("agent_run_id"),
        "ir_ref": entry.get("ir_ref"),
        "pipeline_ref": entry.get("pipeline_ref"),
        "output_refs": entry.get("output_refs", []),
        "completed_at": entry.get("completed_at"),
        "integrity": "ok",
    }


def _derive_resume_directive(
    repo_root: Path,
    orchestration_id: str,
    reason_code: str | None,
) -> dict[str, Any] | None:
    """Build a `resume_directive` for a cross-phase Compile retry on resume.

    A terminal `*_ir` attribution (e.g. `validate_judge_structural_violation_ir`)
    routes the retry to Compile, but the resumed run cannot proceed until the
    checkpointed-pass Compile/Generate/Build phases are reopened (see `reopen_phase`).
    Reading `failure_analysis.json#original_finding`, this records the parameters the
    resumed orchestration agent feeds to `reopen-phase --from-phase compile` so the
    resume is deterministic and does not re-derive the dead end (token-cost saver).
    Returns None when the reason does not map to a Compile reopen or the finding
    evidence is incomplete — the agent then falls back to the decision table.
    """
    # Gate on the CURRENT terminal reason, not just `failure_analysis.json`. The
    # attribution=ir reason codes carry the `_ir` suffix (e.g.
    # `validate_judge_structural_violation_ir`); a non-ir resume must not emit a
    # directive off a stale ir `original_finding` left in failure_analysis.json.
    if not isinstance(reason_code, str) or not reason_code.strip().lower().endswith("_ir"):
        return None
    fa_path = _orchestration_root(repo_root, orchestration_id) / "failure_analysis.json"
    if not fa_path.exists():
        return None
    try:
        fa = _read_json(fa_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(fa, dict):
        return None
    finding = fa.get("original_finding")
    if not isinstance(finding, dict):
        return None
    if str(finding.get("attribution") or "").strip().lower() != "ir":
        return None
    node_key = fa.get("node_key")
    trigger = finding.get("failed_substep_agent_run_id")
    if not (isinstance(node_key, str) and node_key.strip()):
        return None
    if not (isinstance(trigger, str) and trigger.strip()):
        return None
    return {
        "reopen_from": "compile",
        "node_key": node_key.strip(),
        "trigger_agent_run_id": trigger.strip(),
        "finding_id": finding.get("finding_id"),
        "reason_code": reason_code.strip(),
        "source": "failure_analysis.original_finding",
    }


def _attribution_phase_tokens(value: object) -> list[str]:
    """Return the workflow phase tokens (compile/generate/build) named in a
    free-form attribution string, in canonical phase order. `validate` is
    excluded — it is never a reopen *target* (an unauthorized write attributed
    to Validate's own execution is not an upstream-phase defect)."""
    if not isinstance(value, str) or not value.strip():
        return []
    low = value.lower()
    return [tok for tok in ("compile", "generate", "build") if tok in low]


def _derive_unauthorized_write_resume_directive(
    repo_root: Path,
    orchestration_id: str,
    reason_code: str | None,
) -> dict[str, Any] | None:
    """Build a `resume_directive` when the prior terminal failure was an
    unauthorized write attributed to an upstream phase.

    Such a run never reached `agent_runs.jsonl` (`record_agent_run` diverted its
    terminal `fail` to `agent_runs_invalid.jsonl`); the resumed agent must feed
    that diverted arid to `reopen-phase` so the attributed upstream phase is
    invalidated and re-run — `reopen_phase` accepts it because a matching
    `unauthorized_write_violation.json` exists. This makes the resume
    deterministic instead of re-deriving the `resume_reopen_no_valid_trigger`
    dead end (mirrors `_derive_resume_directive` for the `_ir` case).

    Conservative: returns None unless (a) the CURRENT terminal failure is the
    unauthorized-write reason code (gated like the `_ir` path's `reason_code`
    check — without this, a resume that failed for an unrelated reason could emit a
    stale directive off a leftover invalid-log row + `failure_analysis.json`
    attribution), (b) exactly one *not-yet-consumed* node's downstream run in the
    invalid log is backed by a violation file, and (c) `failure_analysis.json`
    attributes it to a single upstream phase strictly above the run's own phase.
    On None the agent falls back to the decision table — `reopen_phase` still
    accepts the invalid-log trigger if the agent supplies it.
    """
    # Gate on the CURRENT terminal reason. An unauthorized-write fail_closed is
    # recorded as `noncanonical_phase_write_attempt` (the nearest FAIL_CLOSED enum
    # fit); any other reason means this resume is not recovering an unauthorized
    # write, so a leftover violation-backed invalid row must not drive a reopen.
    if not isinstance(reason_code, str) or reason_code.strip() != _UNAUTHORIZED_WRITE_FAIL_REASON:
        return None
    root = _orchestration_root(repo_root, orchestration_id)
    invalid_runs = _load_invalid_run_records(root)
    if not invalid_runs:
        return None
    # Already-superseded invalid IDs are prior attempts a previous reopen already
    # consumed; excluding them is what keeps a REPEATED unauthorized-write retry
    # deterministic. Without it, the second failure leaves two violation-backed rows
    # in the invalid log (the consumed one + the new one), the uniqueness check below
    # suppresses the directive, and resume can re-derive the consumed trigger
    # (`reopen-phase` -> noop) — the `resume_reopen_no_valid_trigger` dead end again.
    superseded_run_ids = _load_superseded_run_ids(repo_root, orchestration_id)
    # An invalid-log arid that now also has a canonical `agent_runs.jsonl` row was
    # retried to success with the same arid (the documented same-arid retry path, e.g.
    # after `dismiss-violation`); its stale invalid row + violation file linger but are
    # NOT the current failure — and `reopen_phase` would reject it anyway (it accepts a
    # trigger only when absent from `agent_runs.jsonl`). Exclude such recovered IDs so
    # they do not inflate the candidate count and suppress the directive.
    recovered_run_ids = set(_load_run_records(root).keys())
    violations_dir = _violations_dir(repo_root, orchestration_id)
    # Candidate failing runs: step/substep, non-pass, not yet consumed by a prior
    # reopen, not recovered via same-arid retry, backed by an unauthorized-write
    # violation file, with a known phase.
    candidates: list[tuple[str, dict[str, Any]]] = []
    for arid, rec in invalid_runs.items():
        if arid in superseded_run_ids or arid in recovered_run_ids:
            continue
        if str(rec.get("agent_role") or "").strip().lower() not in {"step", "substep"}:
            continue
        if str(rec.get("status") or "").strip().lower() == "pass":
            continue
        step = str(rec.get("step") or "").strip().lower()
        if step not in STEP_KEYS_FOR_NODE_STATE:
            continue
        if not (violations_dir / f"{arid}.unauthorized_write_violation.json").is_file():
            continue
        candidates.append((arid, rec))
    if len(candidates) != 1:
        return None
    trigger_arid, rec = candidates[0]
    node_key = str(rec.get("node_key") or "").strip()
    if not node_key:
        return None
    trig_idx = STEP_KEYS_FOR_NODE_STATE.index(str(rec.get("step")).strip().lower())

    fa_path = root / "failure_analysis.json"
    if not fa_path.exists():
        return None
    try:
        fa = _read_json(fa_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(fa, dict):
        return None
    # Gather attribution strings from the agent-authored failure_analysis. The
    # schema is conventional (not enforced), so probe the well-known locations.
    attribution_values: list[object] = []
    for container_key in ("original_finding", "root_cause", "secondary_known_defect"):
        container = fa.get(container_key)
        if isinstance(container, dict):
            attribution_values.append(container.get("attribution"))
    attribution_values.append(fa.get("attribution"))
    phase_tokens: list[str] = []
    for val in attribution_values:
        for tok in _attribution_phase_tokens(val):
            if tok not in phase_tokens:
                phase_tokens.append(tok)
    # Require a single upstream phase strictly above the failing run's phase.
    upstream = [tok for tok in phase_tokens if STEP_KEYS_FOR_NODE_STATE.index(tok) < trig_idx]
    if len(upstream) != 1:
        return None
    reopen_from = upstream[0]
    return {
        "reopen_from": reopen_from,
        "node_key": node_key,
        "trigger_agent_run_id": trigger_arid,
        "source": "agent_runs_invalid.unauthorized_write",
    }


def enable_checkpoint_resume(
    repo_root: Path,
    orchestration_id: str,
    *,
    spec_ref: str | None = None,
    source_dependency_ref: str | None = None,
) -> dict[str, Any]:
    """Set resume_enabled=true in orchestration_meta.json.

    Update the meta only when `spec_ref` / `source_dependency_ref` is specified
    (to reflect the value overridden via the CLI at resume time into the meta so that
    the restoration source of the next resume does not become stale). When unspecified, keep the existing meta.

    When resuming an orchestration already terminated with a terminal status
    (pass / fail / fail_closed / blocked / timeout / cancel), return the live status to `running`.
    Because `update_orchestration_status` rejects a terminal → other status transition except `fail` → `fail_closed`,
    without a reset the resumed agent could not record `pass` even if it completed,
    and the resume would not hold. The prior terminal narrative is saved to `resumed_from_*`,
    and the history remains in failure_analysis.json / phase_state_log.

    Raise a RuntimeError when the orchestration does not exist.
    """
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        raise RuntimeError(
            f"orchestration not found: {orchestration_id}. "
            "Run 'init' before enabling checkpoint resume."
        )
    try:
        meta = _read_json(meta_path)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"orchestration_meta.json is invalid: {meta_path}") from exc
    if not isinstance(meta, dict):
        raise RuntimeError(f"orchestration_meta.json is invalid: {meta_path}")
    meta["resume_enabled"] = True
    meta["resumed_at"] = _utc_now_iso()
    if isinstance(spec_ref, str) and spec_ref.strip():
        meta["spec_ref"] = spec_ref.strip()
    if isinstance(source_dependency_ref, str) and source_dependency_ref.strip():
        meta["source_dependency_ref"] = source_dependency_ref.strip()
    prior_status = meta.get("status")
    terminal_reset = (
        isinstance(prior_status, str) and prior_status in IDEMPOTENT_TERMINAL_STATUSES
    )
    if terminal_reset:
        # Archive the prior terminal narrative, then hand the resumed run a fresh
        # in-progress lifecycle so its eventual set-status(pass/fail) is a valid
        # forward transition from `running` rather than a rejected terminal-to-terminal one.
        meta["resumed_from_status"] = prior_status
        prior_reason_code = meta.get("reason_code")
        for live_field, archive_field in (
            ("reason_code", "resumed_from_reason_code"),
            ("reason_detail", "resumed_from_reason_detail"),
            ("blocking_policy_scope", "resumed_from_blocking_policy_scope"),
        ):
            if meta.get(live_field) is not None:
                meta[archive_field] = meta.get(live_field)
                meta.pop(live_field, None)
        # When the prior failure was a cross-phase Compile retry (attribution=ir),
        # record the parameters the resumed agent feeds to `reopen-phase` so the
        # checkpointed-pass upstream phases are reopened deterministically rather
        # than the resume re-running only Validate and reproducing the same fail.
        directive = _derive_resume_directive(
            repo_root,
            orchestration_id,
            prior_reason_code if isinstance(prior_reason_code, str) else None,
        )
        # When the prior failure was an unauthorized write attributed to an
        # upstream phase, the failing run is only in `agent_runs_invalid.jsonl`;
        # point the resumed agent at it so `reopen-phase` (which now accepts a
        # violation-backed invalid-log trigger) invalidates the attributed phase
        # rather than re-deriving the `resume_reopen_no_valid_trigger` dead end.
        if directive is None:
            directive = _derive_unauthorized_write_resume_directive(
                repo_root,
                orchestration_id,
                prior_reason_code if isinstance(prior_reason_code, str) else None,
            )
        if directive is not None:
            meta["resume_directive"] = directive
        else:
            # Drop a directive left by a prior IR resume; the resumed agent is told
            # to honor `resume_directive` first, so a stale node/trigger would make an
            # unrelated later resume reopen Compile with the wrong parameters.
            meta.pop("resume_directive", None)
        meta.pop("finished_at", None)
        meta.pop("detected_at", None)
        meta["status"] = "running"
        # Drop the prior terminalization's cleanup_committed marker so the resumed
        # run's eventual terminalization performs a clean two-phase commit and the
        # validator does not treat reused orch tmp as already revoked.
        orch_arid = meta.get("orchestration_agent_run_id")
        if isinstance(orch_arid, str) and orch_arid.strip():
            marker = _cleanup_committed_marker_path(
                repo_root, orchestration_id, orch_arid.strip()
            )
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
        # Re-open the orchestration's own agent_runs row (terminalized in place by
        # the prior set-status) so the resumed run's row is `running` again, its
        # eventual set-status can re-terminalize it, and validate_workspace_root
        # does not age the live tmp root out as cleanup-pending. Done BEFORE the
        # meta commit below so the still-terminal on-disk meta gates recovery: if
        # this is interrupted, a retry re-enters `terminal_reset` and re-runs it
        # (idempotent — the helper no-ops once the row is already `running`).
        _reopen_orchestration_run_row(repo_root, orchestration_id)
        # A host that died mid-launch leaves the active_child window open (no live
        # agent ran deactivate-child / record-timeout). The terminal status proves
        # no child is actually running, so clear the stale markers here — otherwise
        # the resumed agent's first record-launch hits the Claude-backend sequential
        # check and is rejected, permanently wedging recovery for
        # launch_incomplete_active_child / llm_launch_interrupted. Captured before the
        # meta commit so a retry re-enters terminal_reset and re-runs it (idempotent).
        cleared_active_child = _clear_stale_active_child_markers(repo_root, orchestration_id)
        # The abandoned child's agent_graph edge was written by record_launch before
        # the marker and never gets a terminal agent_runs row; prune it so the
        # resumed run's eventual set-status(pass) is not rejected by the orphan-edge
        # check in _validate_orchestration_completion_for_pass. Idempotent.
        pruned_graph_children = _prune_orphan_agent_graph_edges(repo_root, orchestration_id)
        # Drop stale `child_running` authority for the abandoned launch — the phase
        # gates (apply-patch / MCP / run-gate) authorize child work on that state, and
        # a terminal status proves no child is live. merge_phase_state_for_resume below
        # preserves node_states, so this reset survives into the resumed run.
        reset_child_running = _reset_stale_child_running_node_steps(repo_root, orchestration_id)
        # Tombstone the abandoned launches' residual artifacts so a later manual
        # inspection / audit can tell them apart from a genuine protocol violation.
        # _write_orphan_launch_tombstones self-derives candidates from the durable
        # launches/*.request.json artifacts (resilient to an interrupted resume retry
        # where the cleared/pruned lists below would be empty) and filters to GENUINE
        # orphans (launched ∧ not in _protected_child_arids). The pruned/cleared lists
        # are passed only as a supplementary hint.
        orphan_tombstones = _write_orphan_launch_tombstones(
            repo_root,
            orchestration_id,
            list(pruned_graph_children) + list(cleared_active_child),
        )
    _write_json(meta_path, meta)
    merge_phase_state_for_resume(repo_root, orchestration_id)
    if terminal_reset:
        _append_phase_state_log(
            repo_root,
            orchestration_id,
            {
                "ts": _utc_now_iso(),
                "event": "resume_status_reset",
                "from": prior_status,
                "to": "running",
                "note": "terminal status reset to running for checkpoint resume",
            },
        )
        if pruned_graph_children:
            _append_phase_state_log(
                repo_root,
                orchestration_id,
                {
                    "ts": _utc_now_iso(),
                    "event": "resume_pruned_orphan_graph_edges",
                    "note": "orphan agent_graph edges (no agent_runs row) pruned for checkpoint resume",
                    "pruned_child_agent_run_ids": pruned_graph_children,
                },
            )
        if cleared_active_child:
            _append_phase_state_log(
                repo_root,
                orchestration_id,
                {
                    "ts": _utc_now_iso(),
                    "event": "resume_cleared_stale_active_child",
                    "from": "child_running",
                    "to": "not_started",
                    "note": "stale active_child markers cleared for checkpoint resume",
                    "cleared_active_child_arids": cleared_active_child,
                },
            )
        if reset_child_running:
            _append_phase_state_log(
                repo_root,
                orchestration_id,
                {
                    "ts": _utc_now_iso(),
                    "event": "resume_reset_stale_child_running",
                    "from": "child_running",
                    "to": "not_started",
                    "note": "stale child_running node/step authority reset for checkpoint resume",
                    "reset_node_steps": reset_child_running,
                },
            )
        if orphan_tombstones:
            _append_phase_state_log(
                repo_root,
                orchestration_id,
                {
                    "ts": _utc_now_iso(),
                    "event": "resume_tombstoned_orphan_launches",
                    "note": "orphan launch artifacts marked with launches/<arid>.pruned.json for checkpoint resume",
                    "tombstoned_agent_run_ids": orphan_tombstones,
                },
            )
    return meta


def _load_substep_parent_map(root: Path) -> dict[str, str]:
    """Map each substep agent_run_id -> its step's executor_agent_run_id.

    Built from every steps/*/*/*/step_result.json. This is the authoritative
    source for the pre_judge substep-linkage check (which requires a substep's
    parent_agent_run_id to equal the listing step's executor_agent_run_id), so
    backfilling parent_agent_run_id from here satisfies that gate exactly.
    """
    mapping: dict[str, str] = {}
    steps_dir = root / "steps"
    if not steps_dir.is_dir():
        return mapping
    for result_path in steps_dir.glob("*/*/*/step_result.json"):
        try:
            doc = _read_json(result_path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        executor = doc.get("executor_agent_run_id")
        subs = doc.get("substep_agent_run_ids")
        if not isinstance(executor, str) or not executor.strip():
            continue
        if not isinstance(subs, list):
            continue
        for sid in subs:
            if isinstance(sid, str) and sid.strip():
                mapping.setdefault(sid.strip(), executor.strip())
    return mapping


def _load_agent_graph_parent_map(root: Path) -> dict[str, str]:
    """Map child_agent_run_id -> parent_agent_run_id from agent_graph.json edges."""
    mapping: dict[str, str] = {}
    graph_path = root / "agent_graph.json"
    if not graph_path.is_file():
        return mapping
    try:
        doc = _read_json(graph_path)
    except (OSError, json.JSONDecodeError):
        return mapping
    edges = doc.get("edges") if isinstance(doc, dict) else None
    if not isinstance(edges, list):
        return mapping
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        child = edge.get("child_agent_run_id")
        parent = edge.get("parent_agent_run_id")
        if (
            isinstance(child, str)
            and child.strip()
            and isinstance(parent, str)
            and parent.strip()
        ):
            mapping.setdefault(child.strip(), parent.strip())
    return mapping


def repair_legacy_agent_runs(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_model: str | None = None,
) -> dict[str, Any]:
    """Backfill parent_agent_run_id / agent_model on pre-fix agent_runs.jsonl lines.

    step/substep records written before commit caa10ab (which made agent_model
    required and added forward backfill) lack `parent_agent_run_id` and
    `agent_model`. Because agent_runs.jsonl is append-only and duplicate
    record-agent-run is rejected, those values cannot be supplied forward — yet
    the pre_judge gate scans every line and the substep-linkage check, so the
    legacy lines permanently fail the gate and make resume impossible.

    The orchestration row (written by init_orchestration, which has no launch and
    thus no record-launch agent_model) is also covered for `agent_model` only —
    it is the graph root and legitimately has no parent_agent_run_id, so the
    parent backfill is skipped for it. This fixes the top-level cost-attribution
    blind spot (audit orch_20260617T010118Z_f5a9577c: every child row carried a
    model, the orchestration row carried none).

    This repair reconstructs the missing values from existing artifacts and fills
    ONLY missing fields (never overwriting a present non-empty value):

    - parent_agent_run_id (step/substep only): substep -> step_result.json
      executor_agent_run_id; step -> orchestration_meta.json
      orchestration_agent_run_id. Both are cross-checked against agent_graph.json
      child->parent edges; a disagreement marks the line unrepairable rather than
      guessing.
    - agent_model: runtime non-derivable by design (orchestration_runtime: the
      LLM that produced a child's artifacts is not knowable from the runtime), so
      it is taken from an explicit `agent_model` override or, failing that, from
      the unique non-empty agent_model already present on sibling entries of the
      same orchestration. If neither yields a value, the line is left missing and
      status is `needs_manual`.

    Filled lines gain a `backfilled` provenance object and an audit entry is
    appended to record_repairs.jsonl. The original lines' missing state is thereby
    preserved in the audit log. Idempotent: a re-run with nothing missing is a
    no-op. Serialized under the agent_runs.jsonl exclusive lock.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    runs_path = root / "agent_runs.jsonl"
    base_result: dict[str, Any] = {
        "status": "noop",
        "orchestration_id": orchestration_id,
        "agent_model": None,
        "agent_model_source": None,
        "repaired_lines": [],
        "remaining_missing": [],
        "unrepairable": [],
    }
    if not runs_path.is_file():
        base_result["reason"] = "agent_runs.jsonl absent"
        return base_result

    orchestration_arid: str | None = None
    meta_path = root / "orchestration_meta.json"
    try:
        meta = _read_json(meta_path)
    except (OSError, json.JSONDecodeError):
        meta = None
    if isinstance(meta, dict):
        v = meta.get("orchestration_agent_run_id")
        if isinstance(v, str) and v.strip():
            orchestration_arid = v.strip()

    repaired: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    unrepairable: list[dict[str, Any]] = []
    chosen_model: str | None = None
    model_source: str | None = None
    changed = False

    with _runs_jsonl_exclusive_lock(repo_root, orchestration_id):
        raw = runs_path.read_text(encoding="utf-8")
        ends_nl = raw.endswith("\n")
        raw_lines = raw.splitlines()

        parsed: list[dict[str, Any] | None] = []
        models: set[str] = set()
        for line in raw_lines:
            s = line.strip()
            if not s:
                parsed.append(None)
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                parsed.append(None)
                continue
            if not isinstance(obj, dict):
                parsed.append(None)
                continue
            parsed.append(obj)
            m = obj.get("agent_model")
            if isinstance(m, str) and m.strip():
                models.add(m.strip())

        if isinstance(agent_model, str) and agent_model.strip():
            chosen_model = agent_model.strip()
            model_source = "override"
        elif len(models) == 1:
            chosen_model = next(iter(models))
            model_source = "sibling_uniform"

        substep_parent = _load_substep_parent_map(root)
        graph_parent = _load_agent_graph_parent_map(root)

        audit_entries: list[dict[str, Any]] = []
        for idx, obj in enumerate(parsed):
            if obj is None:
                continue
            role = obj.get("agent_role")
            role_token = (
                role.strip().lower() if isinstance(role, str) and role.strip() else None
            )
            if role_token not in {"orchestration", "step", "substep"}:
                continue
            arid_val = obj.get("agent_run_id")
            arid = arid_val.strip() if isinstance(arid_val, str) and arid_val.strip() else None

            fields_filled: list[str] = []
            line_missing: list[str] = []
            parent_src_used: str | None = None

            # The orchestration agent is the graph root — it has no
            # parent_agent_run_id, so only agent_model is backfillable for it.
            # step/substep rows still get the parent backfill below.
            cur_parent = obj.get("parent_agent_run_id")
            if role_token in {"step", "substep"} and not (
                isinstance(cur_parent, str) and cur_parent.strip()
            ):
                derived: str | None = None
                parent_source: str | None = None
                if role_token == "substep" and arid in substep_parent:
                    derived = substep_parent[arid]
                    parent_source = "step_result_executor"
                elif role_token == "step":
                    derived = orchestration_arid
                    parent_source = "orchestration_meta"
                graph_val = graph_parent.get(arid) if arid else None
                if derived is None and graph_val:
                    derived = graph_val
                    parent_source = "agent_graph"
                if derived is not None and graph_val is not None and graph_val != derived:
                    unrepairable.append(
                        {
                            "agent_run_id": arid,
                            "line": idx + 1,
                            "field": "parent_agent_run_id",
                            "reason": (
                                f"agent_graph parent {graph_val} disagrees with "
                                f"derived {derived}"
                            ),
                        }
                    )
                    line_missing.append("parent_agent_run_id")
                elif derived:
                    obj["parent_agent_run_id"] = derived
                    fields_filled.append("parent_agent_run_id")
                    parent_src_used = parent_source
                else:
                    unrepairable.append(
                        {
                            "agent_run_id": arid,
                            "line": idx + 1,
                            "field": "parent_agent_run_id",
                            "reason": "no authoritative source",
                        }
                    )
                    line_missing.append("parent_agent_run_id")

            cur_model = obj.get("agent_model")
            if not (isinstance(cur_model, str) and cur_model.strip()):
                if chosen_model:
                    obj["agent_model"] = chosen_model
                    fields_filled.append("agent_model")
                else:
                    line_missing.append("agent_model")

            if fields_filled:
                changed = True
                prov: dict[str, Any] = {
                    "at": _utc_now_iso(),
                    "reason": "pre_caa10ab legacy record backfill (repair-agent-runs)",
                    "fields": list(fields_filled),
                }
                if "parent_agent_run_id" in fields_filled:
                    prov["parent_source"] = parent_src_used
                if "agent_model" in fields_filled:
                    prov["agent_model_source"] = model_source
                obj["backfilled"] = prov
                raw_lines[idx] = json.dumps(obj, ensure_ascii=False)
                repaired.append(
                    {"agent_run_id": arid, "line": idx + 1, "fields": list(fields_filled)}
                )
                audit_entries.append(
                    {"agent_run_id": arid, "line": idx + 1, **prov}
                )

            if line_missing:
                remaining.append(
                    {"agent_run_id": arid, "line": idx + 1, "missing": line_missing}
                )

        if changed:
            body = "\n".join(raw_lines)
            if ends_nl:
                body += "\n"
            _atomic_write_text(runs_path, body)
            repairs_path = root / "record_repairs.jsonl"
            with repairs_path.open("a", encoding="utf-8") as fh:
                for entry in audit_entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if remaining:
        status = "needs_manual"
    elif changed:
        status = "repaired"
    else:
        status = "noop"

    return {
        "status": status,
        "orchestration_id": orchestration_id,
        "agent_model": chosen_model,
        "agent_model_source": model_source,
        "repaired_lines": repaired,
        "remaining_missing": remaining,
        "unrepairable": unrepairable,
    }


def _validate_canonical_workspace_root_ref(
    *,
    ref: str,
    node_safe: str,
    kind: str,
    label: str,
) -> None:
    """Require ref == workspace/{kind}/{node_safe}/{root_id} with no extra path segments."""
    token = ref.strip().strip("/")
    parts = token.split("/")
    if len(parts) != 4:
        raise ValueError(
            f"launch request {label} must be exactly workspace/{kind}/<node_key_safe>/<id> "
            f"(directory root only); got {ref!r}"
        )
    if parts[0] != "workspace" or parts[1] != kind:
        raise ValueError(f"launch request {label} must be under workspace/{kind}/; got {ref!r}")
    seg_node = parts[2]
    root_id = parts[3]
    if seg_node != node_safe:
        raise ValueError(
            f"launch request {label} node directory must be {node_safe!r}; got {ref!r}"
        )
    if not _NODE_KEY_SAFE_PATTERN.match(seg_node):
        raise ValueError(f"launch request {label} has invalid node_key_safe segment: {ref!r}")
    if not _SLUG_DATE_SEQ3_PATTERN.match(root_id):
        raise ValueError(
            f"launch request {label} root id must match <slug>_<YYYYMMDD>_<seq3>; got {ref!r}"
        )


def _workspace_path_is_under_ref(path: str, base: str) -> bool:
    p = path.strip().rstrip("/")
    b = base.strip().rstrip("/")
    return p == b or p.startswith(b + "/")


def _validate_pass_output_refs_against_launch(
    repo_root: Path,
    payload: dict[str, Any],
) -> None:
    """Require each output_ref to lie under ir_ref or pipeline_ref from the saved launch request.

    Only applies to ``step`` / ``substep`` runs that have a launch request on disk.
    ``orchestration`` and other roles do not set ``launch_request_ref``; skip validation.
    """
    role = payload.get("agent_role")
    if not isinstance(role, str) or role.strip().lower() not in {"step", "substep"}:
        return

    output_refs = payload.get("output_refs")
    if not isinstance(output_refs, list) or not output_refs:
        return

    launch_request_ref = payload.get("launch_request_ref")
    if not isinstance(launch_request_ref, str) or not launch_request_ref.strip():
        raise ValueError("launch_request_ref must be non-empty string for pass output_refs validation")
    launch_path = repo_root / launch_request_ref.strip()
    if not launch_path.exists():
        raise ValueError(f"launch_request_ref target not found: {launch_request_ref}")
    try:
        launch_payload = _read_json(launch_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"launch_request_ref must be valid json: {launch_request_ref}") from exc
    if not isinstance(launch_payload, dict):
        raise ValueError(f"launch request must be object: {launch_request_ref}")

    ir_ref = launch_payload.get("ir_ref")
    pipeline_ref = launch_payload.get("pipeline_ref")
    if not isinstance(ir_ref, str) or not ir_ref.strip():
        raise ValueError("launch request ir_ref missing for output_refs validation")
    if not isinstance(pipeline_ref, str) or not pipeline_ref.strip():
        raise ValueError("launch request pipeline_ref missing for output_refs validation")

    ir_root = ir_ref.strip().rstrip("/")
    pipe_root = pipeline_ref.strip().rstrip("/")

    for idx, ref in enumerate(output_refs):
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"output_refs[{idx}] must be non-empty string")
        r = ref.strip()
        if not r.startswith("workspace/"):
            raise ValueError(f"output_refs[{idx}] must start with workspace/: {r!r}")
        if _workspace_path_is_under_ref(r, ir_root) or _workspace_path_is_under_ref(r, pipe_root):
            continue
        raise ValueError(
            f"output_refs[{idx}] must be under ir_ref or pipeline_ref root "
            f"({ir_root!r} or {pipe_root!r}); got {r!r}"
        )


def _validate_launch_request_payload(request_payload: dict[str, Any]) -> None:
    node_key = request_payload.get("node_key")
    step = request_payload.get("step")
    substep = request_payload.get("substep")
    if not isinstance(node_key, str) or not node_key.strip():
        raise ValueError("launch request must include non-empty node_key")
    if not isinstance(step, str) or not step.strip():
        raise ValueError("launch request must include non-empty step")
    # agent_model identifies the LLM that produced the child's artifacts; it is
    # not derivable by the runtime (e.g. Claude Code has no runtime-knowable
    # model), so the orchestration agent must supply it at launch. record-launch
    # persists it into the request, and record_agent_run backfills it onto the
    # agent_runs entry, satisfying the pre_judge step/substep requirement.
    agent_model = request_payload.get("agent_model")
    if not isinstance(agent_model, str) or not agent_model.strip():
        raise ValueError("launch request must include non-empty agent_model")
    if isinstance(node_key, str) and node_key.strip():
        node_safe = _node_key_to_safe(node_key.strip())
    else:
        node_safe = None

    for key in ("ir_ref", "pipeline_ref", "dependency_ref"):
        value = request_payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"launch request must include non-empty {key}")
        if _is_placeholder_ref(value):
            raise ValueError(f"launch request {key} must not contain placeholder tokens")

    ir_ref = request_payload.get("ir_ref")
    pipeline_ref = request_payload.get("pipeline_ref")
    dependency_ref = request_payload.get("dependency_ref")
    if node_safe is not None:
        if isinstance(ir_ref, str) and ir_ref.strip():
            _validate_canonical_workspace_root_ref(
                ref=ir_ref,
                node_safe=node_safe,
                kind="ir",
                label="ir_ref",
            )
        if isinstance(pipeline_ref, str) and pipeline_ref.strip():
            _validate_canonical_workspace_root_ref(
                ref=pipeline_ref,
                node_safe=node_safe,
                kind="pipelines",
                label="pipeline_ref",
            )
    if isinstance(dependency_ref, str) and _is_placeholder_ref(dependency_ref):
        raise ValueError("launch request dependency_ref must not contain placeholder tokens")
    step_val = str(request_payload.get("step", "")).strip().lower()
    if step_val == "compile" and isinstance(dependency_ref, str) and dependency_ref.strip():
        dep_norm = _normalize_rel_posix(dependency_ref.strip())
        if not (dep_norm.startswith("spec/") and dep_norm.endswith("/deps.yaml")):
            raise ValueError(
                "record-launch: Compile step dependency_ref must be spec/.../deps.yaml, "
                f"got {dependency_ref!r}. "
                "Both generate and verify substeps must receive the spec path, "
                "not workspace/ir/."
            )

    # value validation of repair_strategy / issue_severity
    repair_strategy = str(request_payload.get("repair_strategy", "none")).strip()
    if repair_strategy not in VALID_REPAIR_STRATEGIES:
        raise ValueError(
            f"launch request repair_strategy must be one of {sorted(VALID_REPAIR_STRATEGIES)}; "
            f"got {repair_strategy!r}"
        )

    issue_severity = str(request_payload.get("issue_severity", "none")).strip()
    if issue_severity not in VALID_ISSUE_SEVERITIES:
        raise ValueError(
            f"launch request issue_severity must be one of {sorted(VALID_ISSUE_SEVERITIES)}; "
            f"got {issue_severity!r}"
        )

    # require the repair fields when repair_strategy is reuse/restart
    if repair_strategy in {"reuse", "restart"}:
        repair_target = str(request_payload.get("repair_target_agent_run_id", "none")).strip()
        if not repair_target or repair_target == "none":
            raise ValueError(
                "repair launch request requires non-empty repair_target_agent_run_id "
                f"(repair_strategy={repair_strategy!r})"
            )
        repair_reason = str(request_payload.get("repair_reason", "none")).strip()
        if not repair_reason or repair_reason == "none":
            raise ValueError(
                "repair launch request requires non-empty repair_reason "
                f"(repair_strategy={repair_strategy!r})"
            )

    is_verify_substep = (
        isinstance(step, str)
        and step.strip().lower() in {"compile", "generate"}
        and isinstance(substep, str)
        and substep.strip().lower() == "verify"
    )
    # Validate source_id format for any generate-step launch (both generate and
    # verify substeps).  The orchestration agent must supply source_id in the
    # launch request when step=generate; validate format here so a mis-formatted
    # source_id (e.g. using the ir_id slug format instead of `src_YYYYMMDD_seq3`)
    # is rejected before the child agent runs, rather than discovered later by
    # generate.verify after a full substep execution.
    if isinstance(step, str) and step.strip().lower() == "generate":
        gen_id = request_payload.get("source_id")
        if not isinstance(gen_id, str) or not gen_id.strip():
            raise ValueError(
                "generate step launch request must include non-empty source_id "
                "(format: src_<YYYYMMDD>_<seq3>, e.g. src_20260511_001)"
            )
        if not _SOURCE_ID_RE.match(gen_id.strip()):
            raise ValueError(
                f"generate step launch request source_id={gen_id!r} does not match "
                "required format src_<YYYYMMDD>_<seq3> (e.g. src_20260511_001). "
                "source_id must start with literal 'src_' prefix followed by 8-digit "
                "date and 3-digit sequence; do not reuse the ir_id / pipeline_id slug format."
            )
    if is_verify_substep and isinstance(step, str) and step.strip().lower() == "generate":
        # source_id presence and format already validated above; keep this branch
        # for the additional non-empty guard that predates the format check.
        gen_id = request_payload.get("source_id")
        if not isinstance(gen_id, str) or not gen_id.strip():
            raise ValueError("generate verify launch request must include non-empty source_id")

    if not is_verify_substep:
        return

    skill_name = request_payload.get("skill_name")
    skill_ref = request_payload.get("skill_ref")
    skill_must_read_refs = _split_skill_refs(request_payload.get("skill_must_read_refs"))

    if not isinstance(skill_name, str) or not skill_name.strip():
        raise ValueError("verify launch request must include non-empty skill_name")
    if not isinstance(skill_ref, str) or not skill_ref.strip():
        raise ValueError("verify launch request must include non-empty skill_ref")
    if not skill_must_read_refs:
        raise ValueError("verify launch request must include non-empty skill_must_read_refs")

    required_refs = _required_verify_skill_refs(request_payload)

    missing_refs = [ref for ref in required_refs if ref not in skill_must_read_refs]
    if missing_refs:
        raise ValueError(
            "request payload skill_must_read_refs missing required verify inputs: "
            + ", ".join(missing_refs)
        )


def _load_run_records(
    orchestration_root: Path,
    *,
    caller_holds_lock: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load `agent_runs.jsonl` into a dict keyed by agent_run_id.

    M-NEW-1: shares the same parse-resilience contract as
    `_read_existing_run_ids` — Adv-21 trailing-line tolerance combined with
    Adv-40 in-flight detection. A truncated last line whose corruption is
    plausibly a concurrent appender's mid-write is silently skipped; any
    other malformed line raises `RuntimeError("...durable corruption...")`.
    Without this, callers like `_validate_orchestration_completion_for_pass`
    would crash on `JSONDecodeError` whenever `record_agent_run` is appending
    concurrently.

    H-FOURTH-1: caller_holds_lock mirrors `_read_existing_run_ids`. When the
    caller already holds the runs-jsonl fcntl lock (typically via
    `_runs_jsonl_exclusive_lock` inside `record_agent_run`), the
    writer-active probe would self-contend and falsely report in-flight,
    silently masking durable trailing-line corruption. Pass True to force
    every malformed line to surface.
    """
    runs_path = orchestration_root / "agent_runs.jsonl"
    records: dict[str, dict[str, Any]] = {}
    if not runs_path.exists():
        return records
    non_empty_lines = [s for s in (raw.strip() for raw in runs_path.read_text(encoding="utf-8").splitlines()) if s]
    n_lines = len(non_empty_lines)
    for idx, line in enumerate(non_empty_lines):
        is_last = idx == n_lines - 1
        in_flight = (
            is_last
            and not caller_holds_lock
            and _agent_runs_writer_active(runs_path)
        )
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            if in_flight:
                continue
            raise RuntimeError(
                f"agent_runs.jsonl has malformed JSON at line {idx + 1}: {exc} "
                f"(no active writer detected — durable corruption; quarantine "
                f"the ledger and roll forward explicitly)"
                if is_last else
                f"agent_runs.jsonl has malformed non-trailing JSON at line {idx + 1}: {exc}"
            ) from exc
        if not isinstance(item, dict):
            if in_flight:
                continue
            raise RuntimeError(
                f"agent_runs.jsonl line {idx + 1} is not a JSON object: {item!r}"
            )
        run_id = item.get("agent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            records[run_id.strip()] = item
    return records


def _load_invalid_run_records(
    orchestration_root: Path,
) -> dict[str, dict[str, Any]]:
    """Load `agent_runs_invalid.jsonl` into a dict keyed by agent_run_id.

    Mirrors `_load_run_records` but reads the SEPARATE invalid log that
    `record_agent_run` diverts terminal payloads to when terminal-write
    validation rejects them (e.g. an unauthorized write — see the `except
    ValueError` handler in `record_agent_run`). Unlike the canonical log this
    file is not the durable agent ledger, so a malformed line is skipped rather
    than raised: it is a best-effort recovery source consulted only when a run
    is absent from `agent_runs.jsonl`. On a duplicate arid the LAST entry wins
    (a re-rejected retry overwrites the earlier diagnostic row).
    """
    invalid_path = orchestration_root / "agent_runs_invalid.jsonl"
    records: dict[str, dict[str, Any]] = {}
    if not invalid_path.is_file():
        return records
    try:
        lines = invalid_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for raw in lines:
        token = raw.strip()
        if not token:
            continue
        try:
            item = json.loads(token)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        run_id = item.get("agent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            records[run_id.strip()] = item
    return records


def _validate_terminal_run_payload(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
    *,
    caller_holds_lock: bool = False,
) -> None:
    role = payload.get("agent_role")
    status = payload.get("status")
    if not isinstance(role, str):
        return
    role_token = role.strip().lower()
    if role_token not in {"orchestration", "step", "substep"}:
        return
    # H-FOURTH-1: forward caller_holds_lock so the orchestration-role
    # _load_run_records call within _validate_actual_write_paths surfaces
    # durable corruption rather than masking it via self-lock contention.
    _validate_actual_write_paths(
        repo_root, orchestration_id, payload, caller_holds_lock=caller_holds_lock
    )
    if not isinstance(status, str) or status.strip().lower() != "pass":
        return

    output_refs = payload.get("output_refs")
    if role_token in {"step", "substep"}:
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError("pass status for step/substep requires non-empty output_refs")
        for idx, item in enumerate(output_refs):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"output_refs[{idx}] must be non-empty string")
        _validate_pass_output_refs_against_launch(repo_root, payload)
        _validate_paths_against_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=str(payload.get("agent_run_id") or ""),
            paths=[str(item) for item in output_refs if isinstance(item, str)],
        )
        _validate_apply_patch_gate_coverage(
            repo_root, orchestration_id, payload,
            caller_holds_lock=caller_holds_lock,
        )
        return

    if not isinstance(output_refs, list) or not output_refs:
        return
    for idx, item in enumerate(output_refs):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"output_refs[{idx}] must be non-empty string")
    _validate_apply_patch_gate_coverage(
        repo_root, orchestration_id, payload,
        caller_holds_lock=caller_holds_lock,
    )


def _read_launch_request_payload(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> dict[str, Any] | None:
    """Return the parsed `launches/<arid>.request.json` payload, or None
    when absent / malformed."""
    request_path = (
        _orchestration_root(repo_root, orchestration_id)
        / "launches"
        / f"{agent_run_id.strip()}.request.json"
    )
    if not request_path.exists():
        return None
    try:
        payload = _read_json(request_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _read_repair_fields_from_launch_request(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> tuple[str, str]:
    """Return (repair_strategy, repair_target_agent_run_id) from the per-arid
    launch request, or empty strings when either field is absent / malformed.
    Used by `_maybe_inherit_apply_patch_gate` (Issue 2 of the recurrence-
    prevention plan)."""
    payload = _read_launch_request_payload(
        repo_root, orchestration_id, agent_run_id=agent_run_id
    )
    if payload is None:
        return "", ""
    strategy = str(payload.get("repair_strategy") or "").strip().lower()
    target = str(payload.get("repair_target_agent_run_id") or "").strip()
    return strategy, target


def _launch_identity_tuple(payload: dict[str, Any]) -> tuple[str, str, str]:
    """Return the (node_key, step, substep) tuple from a launch request
    payload, normalized to lower-case strings. Used for reuse-target
    identity verification (Issue 2 + Codex review round 3 P1)."""
    return (
        str(payload.get("node_key") or "").strip().lower(),
        str(payload.get("step") or "").strip().lower(),
        str(payload.get("substep") or "").strip().lower(),
    )


def _record_gate_evidence_inheritance(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    inherited_from: str,
    gate: str,
) -> None:
    """Write the audit sidecar that documents a gate-evidence inheritance.
    Issue 2 of the recurrence-prevention plan.

    Lives under `<orch_root>/agents/<arid>/audit/gate_inheritance.json`
    (per-arid runtime sidecar). The `<orch_root>/agents/` prefix is already
    runtime-exempt in `_should_ignore_runtime_snapshot_path` (see also
    `deactivate_snapshot.json` and `gate_changed_paths.json` siblings),
    so the path is invisible to child-side baseline diffs without
    needing a separate `audit/` prefix carve-out (Codex review round 6
    follow-up — avoids blanket-hiding a directory a child could
    theoretically touch via hook bypass)."""
    audit_path = (
        _orchestration_root(repo_root, orchestration_id)
        / "agents"
        / agent_run_id.strip()
        / "audit"
        / "gate_inheritance.json"
    )
    _write_json(
        audit_path,
        {
            "kind": "gate_evidence_inheritance",
            "agent_run_id": agent_run_id.strip(),
            "inherited_from": inherited_from.strip(),
            "gate": gate,
            "recorded_at": _utc_now_iso(),
        },
    )


def _read_gate_inheritance_audit(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
) -> str | None:
    """Return the `inherited_from` arid recorded in the per-arid gate-
    inheritance audit sidecar, or None when the sidecar is absent /
    malformed. Used by `_maybe_inherit_apply_patch_gate` to follow the
    inheritance chain across chained reuse retries (Codex review round 8
    P1)."""
    audit_path = (
        _orchestration_root(repo_root, orchestration_id)
        / "agents"
        / agent_run_id.strip()
        / "audit"
        / "gate_inheritance.json"
    )
    if not audit_path.exists():
        return None
    try:
        payload = _read_json(audit_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("gate") or "").strip() != "apply_patch_writes":
        return None
    inherited_from = str(payload.get("inherited_from") or "").strip()
    return inherited_from or None


def _collect_inherited_apply_patch_gates(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    caller_holds_lock: bool = False,
) -> list[tuple[Path, str]]:
    """Walk the inheritance chain starting from the current run's
    `repair_target_agent_run_id` and return EVERY ancestor's
    `apply_patch_writes.json` (gate_path, arid) tuple discovered along
    the way. Empty list when no inheritance applies. Issue 2 of the
    recurrence-prevention plan, refined per Codex review round 9.

    Two contracts differ from earlier iterations:

    1. Aggregation over the chain (Codex round 9 P1 #1) — partial-reuse
       chains like A→B→C, where B patched a subset and inherited the
       rest from A, must surface BOTH B's and A's `changed_paths` when
       C validates. Earlier implementations stopped at the first
       ancestor that had a gate file, breaking transitive coverage.

    2. Chain traversal mechanism (Codex review rounds 9 P1 #2 + 11 P1):
       the chain advances first via the per-arid audit sidecar
       `agents/<cursor>/audit/gate_inheritance.json` (recorded only on a
       successful coverage check, i.e. `status=pass` ancestors); when
       the audit is absent, it falls back to
       `repair_target_agent_run_id` from the cursor's launch request,
       BUT only because the runs.jsonl attestation below already
       guarantees the cursor's terminal validation succeeded. The
       fallback is what preserves transitive coverage across
       intermediate `status=fail` reuse retries — those runs never have
       audit sidecars (because `_validate_apply_patch_gate_coverage`
       only runs for `status=pass`) but their workspace state is still
       validated by `_validate_actual_write_paths`.

    3. agent_runs.jsonl attestation gates each hop's OWN gate, not the
       walk itself (Codex review rounds 10 + 14) — `guarded-apply-patch`
       writes `gates/<arid>/apply_patch_writes.json` BEFORE `record-
       agent-run` runs, so a run whose terminal validation was rejected
       (entry only in `agent_runs_invalid.jsonl`) still leaves its gate
       file behind. Inheriting that gate would launder unvalidated
       evidence (round 10). However, the rejected cursor's launch
       request still records a valid `repair_target_agent_run_id`
       pointer to an earlier ancestor whose evidence WAS validated; the
       walk can safely traverse past the rejected cursor to that
       ancestor (round 14). Concretely: a cursor that is not in
       `agent_runs.jsonl` does NOT contribute its own gate to
       `results`, but the walk still follows its audit / request-chain
       pointer to the next hop. The next hop is then subjected to the
       same attestation re-check.

    Identity is re-verified at every chain step so a forged audit
    sidecar cannot bridge mismatched `(node_key, step, substep)`
    identities."""
    current_payload = _read_launch_request_payload(
        repo_root, orchestration_id, agent_run_id=agent_run_id
    )
    if current_payload is None:
        return []
    strategy = str(current_payload.get("repair_strategy") or "").strip().lower()
    target = str(current_payload.get("repair_target_agent_run_id") or "").strip()
    if strategy != "reuse" or not target:
        return []

    current_identity = _launch_identity_tuple(current_payload)
    if not current_identity[0] or not current_identity[1]:
        return []

    orch_root = _orchestration_root(repo_root, orchestration_id)
    run_records = _load_run_records(orch_root, caller_holds_lock=caller_holds_lock)

    # The walk terminates because arids are unique and the `visited` set
    # grows monotonically. There is no fixed depth cap (Codex review
    # round 12 P2): valid `repair_strategy=reuse` chains can exceed any
    # arbitrary limit, especially with partial-patch retries where each
    # hop only owns a subset of `changed_paths`. Cycle detection alone
    # is the correct termination criterion.
    results: list[tuple[Path, str]] = []
    visited: set[str] = {agent_run_id.strip()}
    cursor: str | None = target
    while True:
        if not cursor or cursor in visited:
            break
        visited.add(cursor)

        cursor_payload = _read_launch_request_payload(
            repo_root, orchestration_id, agent_run_id=cursor
        )
        if cursor_payload is None:
            # Broken chain — refuse all inheritance rather than partial.
            return []
        if _launch_identity_tuple(cursor_payload) != current_identity:
            # Mis-targeted hop — refuse the entire chain.
            return []

        # Attestation: a cursor not in agent_runs.jsonl was rejected at
        # terminal validation. Its OWN gate evidence is unvalidated and
        # must not be inherited (Codex round 10 P1), but the walk can
        # still traverse past it via its launch request's repair_target
        # to reach a validated ancestor (Codex round 14 P1). Together
        # these prevent laundering of the rejected cursor's evidence
        # while preserving transitive chains across rejected
        # intermediates.
        cursor_validated = cursor in run_records
        if cursor_validated:
            gate = (
                _gates_dir(repo_root, orchestration_id)
                / cursor
                / "apply_patch_writes.json"
            )
            if gate.exists():
                results.append((gate, cursor))

        # Chain traversal: audit first (success-attested), then request
        # chain (validated by the runs.jsonl attestation re-checked at
        # the next iteration). Codex review round 11 P1 — request-chain
        # fallback is re-enabled now that round 10's runs.jsonl check
        # blocks rejected ancestors from being walked.
        next_arid = _read_gate_inheritance_audit(
            repo_root, orchestration_id, agent_run_id=cursor
        )
        if not next_arid:
            cursor_strategy = (
                str(cursor_payload.get("repair_strategy") or "").strip().lower()
            )
            cursor_target = (
                str(cursor_payload.get("repair_target_agent_run_id") or "").strip()
            )
            if cursor_strategy == "reuse" and cursor_target:
                next_arid = cursor_target
        if not next_arid:
            break
        cursor = next_arid

    return results


def _validate_apply_patch_gate_doc(
    gate_path: Path,
    *,
    actor_role: str,
) -> None:
    """Structural validation of an `apply_patch_writes.json` artifact.
    Raises ValueError with a descriptive message on any mismatch. Issue 2
    of the recurrence-prevention plan — extracted so both the primary and
    the inherited gate can be validated symmetrically when partial reuse
    merges evidence from both runs."""
    gate_doc = _read_json(gate_path)
    if not isinstance(gate_doc, dict):
        raise ValueError(f"apply_patch_writes gate artifact must be object: {gate_path}")
    if str(gate_doc.get("status", "")).strip().lower() != "pass":
        raise ValueError(f"apply_patch_writes gate must pass before terminal run record: {gate_path}")
    args_json = gate_doc.get("args_json")
    if not isinstance(args_json, dict):
        raise ValueError(f"apply_patch_writes gate args_json must be object: {gate_path}")
    gate_actor_role = args_json.get("actor_role")
    if not isinstance(gate_actor_role, str) or gate_actor_role.strip().lower() != actor_role:
        raise ValueError(
            "apply_patch_writes gate actor_role mismatch: "
            f"expected={actor_role!r} got={gate_actor_role!r} ({gate_path})"
        )
    changed_paths_obj = args_json.get("changed_paths")
    if not isinstance(changed_paths_obj, list) or not all(isinstance(x, str) for x in changed_paths_obj):
        raise ValueError(f"apply_patch_writes gate changed_paths must be string array: {gate_path}")


def _validate_apply_patch_gate_coverage(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
    *,
    caller_holds_lock: bool = False,
) -> None:
    """Enforce the gate execution evidence of the `apply_patch` write path at terminalization."""
    role = payload.get("agent_role")
    if not isinstance(role, str):
        return
    actor_role = role.strip().lower()
    if actor_role not in {"orchestration", "step", "substep"}:
        return

    agent_run_id = payload.get("agent_run_id")
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("agent_run_id must be non-empty string for apply_patch gate coverage")
    run_id = agent_run_id.strip()

    output_refs_obj = payload.get("output_refs")
    output_refs = (
        [str(item).strip() for item in output_refs_obj if isinstance(item, str) and item.strip()]
        if isinstance(output_refs_obj, list)
        else []
    )
    if not output_refs:
        return

    # Canonical MCP audit logs are written by MCP server tooling without gate
    # provenance and are exempt from this coverage check. Only canonical
    # placements recorded in the manifest's `mcp_owned_audit_logs` field
    # qualify; basename matches at non-canonical paths still require coverage.
    mcp_owned_logs: set[str] = set()
    try:
        manifest_doc_for_coverage = _load_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=run_id,
        )
    except ValueError:
        manifest_doc_for_coverage = None
    if isinstance(manifest_doc_for_coverage, dict):
        mcp_logs_obj = manifest_doc_for_coverage.get("mcp_owned_audit_logs")
        if isinstance(mcp_logs_obj, list):
            for item in mcp_logs_obj:
                if isinstance(item, str) and item.strip():
                    mcp_owned_logs.add(_normalize_rel_posix(item.strip()))
    # Direct-write extensions (e.g. .yaml / .md / source code) are written via
    # `Edit`/`Write` tools and are exempt from `apply_patch_writes` gate
    # coverage. Only `.json` / `.txt` outputs (CLI-managed extensions) require
    # gate evidence. Canonical MCP audit logs are likewise exempt because the
    # only legitimate writer is the MCP server itself.
    cli_required_refs = [
        ref
        for ref in output_refs
        if not _is_direct_write_path(ref)
        and _normalize_rel_posix(ref) not in mcp_owned_logs
    ]
    if not cli_required_refs:
        return

    # Issue 2 of the recurrence-prevention plan (with Codex review
    # rounds 8 + 9 follow-ups): `repair_strategy=reuse` retries inherit
    # gate evidence from successful ancestors so they don't need to
    # re-invoke `guarded-apply-patch` just to satisfy the coverage
    # check. Coverage aggregates the union of:
    #
    #   - the current run's own gate (when present, e.g. partial
    #     reuse that patched a subset of outputs), and
    #   - every audit-attested ancestor's gate, walked transitively via
    #     `agents/<arid>/audit/gate_inheritance.json`.
    #
    # The audit-only walk (Codex round 9 P1 #2) ensures only previously-
    # validated ancestor evidence can be inherited; rejected ancestors
    # have no audit and break the chain. Transitive aggregation (Codex
    # round 9 P1 #1) preserves coverage across partial-reuse chains
    # (A→B→C where B patched a subset and inherited the rest from A).
    current_gate_path = _gates_dir(repo_root, orchestration_id) / run_id / "apply_patch_writes.json"
    inherited_chain = _collect_inherited_apply_patch_gates(
        repo_root, orchestration_id, agent_run_id=run_id,
        caller_holds_lock=caller_holds_lock,
    )

    evidences: list[tuple[Path, str]] = []
    if current_gate_path.exists():
        evidences.append((current_gate_path, run_id))
    evidences.extend(inherited_chain)

    if not evidences:
        raise ValueError(
            f"pass status for {actor_role} requires apply_patch_writes gate evidence: "
            f"{current_gate_path}"
        )

    # Validate each gate doc structurally (status / actor_role /
    # changed_paths shape) before trusting its evidence.
    for gate_path, _arid in evidences:
        _validate_apply_patch_gate_doc(gate_path, actor_role=actor_role)

    effective_changed_paths: set[str] = set()
    for _gate_path, arid in evidences:
        effective_changed_paths.update(
            _gate_changed_paths_for_run(
                repo_root,
                orchestration_id,
                agent_run_id=arid,
            )
        )
    if not effective_changed_paths:
        raise ValueError(
            f"apply_patch_writes gate changed_paths must be non-empty: {evidences[0][0]}"
        )

    uncovered: list[str] = []
    for output_ref in cli_required_refs:
        rel = _normalize_rel_posix(output_ref)
        if not any(_repo_path_under_prefix(rel, cp) for cp in effective_changed_paths):
            uncovered.append(output_ref)
    if uncovered:
        raise ValueError(
            "apply_patch_writes gate does not cover terminal output_refs: "
            + ", ".join(uncovered)
        )

    # Audit: record inheritance against the immediate `repair_target`
    # (per C's launch request), regardless of how many chain hops were
    # actually traversed to reach a gate file. The next run that retries
    # against this run can then follow the audit one hop at a time —
    # `_collect_inherited_apply_patch_gates` walks the chain by reading
    # each per-arid audit sidecar.
    if inherited_chain:
        _, immediate_target = _read_repair_fields_from_launch_request(
            repo_root, orchestration_id, agent_run_id=run_id
        )
        if immediate_target:
            _record_gate_evidence_inheritance(
                repo_root,
                orchestration_id,
                agent_run_id=run_id,
                inherited_from=immediate_target,
                gate="apply_patch_writes",
            )


def _validate_step_or_substep_launch_refs(repo_root: Path, payload: dict[str, Any]) -> None:
    for key in (
        "launch_request_ref",
        "launch_response_ref",
        "launch_prompt_ref",
        "launch_reply_ref",
    ):
        ref = payload.get(key)
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f"{key} must be non-empty string")
        target = repo_root / ref.strip()
        if not target.exists():
            raise ValueError(f"{key} target not found: {ref}")
        if key in {"launch_prompt_ref", "launch_reply_ref"}:
            text = target.read_text(encoding="utf-8", errors="ignore")
            if not text.strip():
                raise ValueError(f"{key} target must be non-empty: {ref}")


def _iter_step_result_paths(root: Path) -> list[Path]:
    steps_root = root / "steps"
    if not steps_root.exists():
        return []
    return sorted(steps_root.glob("*/*/*/step_result.json"))


def _validate_orchestration_completion_for_pass(
    repo_root: Path,
    orchestration_id: str,
) -> None:
    root = _orchestration_root(repo_root, orchestration_id)
    graph_path = root / "agent_graph.json"
    runs = _load_run_records(root)
    if not runs:
        raise RuntimeError("cannot mark orchestration pass without agent_runs.jsonl records")

    orchestration_runs = [
        payload
        for payload in runs.values()
        if isinstance(payload.get("agent_role"), str)
        and payload.get("agent_role") == "orchestration"
    ]
    if not orchestration_runs:
        raise RuntimeError("cannot mark orchestration pass without orchestration agent run record")

    graph = _load_graph(graph_path)
    edges = graph.get("edges")
    if not isinstance(edges, list) or not edges:
        raise RuntimeError("cannot mark orchestration pass without agent_graph edges")

    step_result_refs_by_substep: dict[str, Path] = {}
    for result_path in _iter_step_result_paths(root):
        try:
            result = _read_json(result_path)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid step_result.json: {result_path}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"step_result.json must be object: {result_path}")
        executor_run_id = result.get("executor_agent_run_id")
        if not isinstance(executor_run_id, str) or not executor_run_id.strip():
            raise RuntimeError(f"executor_agent_run_id missing: {result_path}")
        substep_run_ids = result.get("substep_agent_run_ids")
        if not isinstance(substep_run_ids, list):
            raise RuntimeError(f"substep_agent_run_ids must be list: {result_path}")
        for substep_run_id in substep_run_ids:
            if isinstance(substep_run_id, str) and substep_run_id.strip():
                step_result_refs_by_substep[substep_run_id.strip()] = result_path

    # Runs tombstoned by a `reopen-phase` cross-phase retry are prior attempts for a
    # reopened phase: their step_result.json was archived aside and a fresh attempt
    # now vouches the phase. Exempt them from the terminal/vouch requirements below —
    # otherwise the orphaned superseded substep rows would block the resumed pass.
    # Loaded BEFORE the edge check because a reopen trigger may be a downstream child
    # whose failure mode was an unauthorized write: that run was diverted to
    # `agent_runs_invalid.jsonl` (never `agent_runs.jsonl`), but its launch edge in
    # `agent_graph.json` is deliberately KEPT by `_prune_orphan_agent_graph_edges`.
    # Once `reopen-phase` has consumed and superseded it, that edge must not block pass.
    superseded_run_ids = _load_superseded_run_ids(repo_root, orchestration_id)
    invalid_runs = _load_invalid_run_records(root)

    for idx, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise RuntimeError(f"agent_graph edge must be object: index={idx}")
        parent_id = edge.get("parent_agent_run_id")
        child_id = edge.get("child_agent_run_id")
        if not isinstance(parent_id, str) or not parent_id.strip() or parent_id.strip() not in runs:
            raise RuntimeError(
                f"agent_graph edge parent_agent_run_id missing from agent_runs.jsonl: index={idx}"
            )
        child_norm = child_id.strip() if isinstance(child_id, str) and child_id.strip() else None
        if child_norm is None or (
            child_norm not in runs
            # A reopen-consumed unauthorized-write trigger lives only in the invalid
            # log; tolerate its kept edge. Tightly gated (superseded AND in the invalid
            # log) so an UN-consumed invalid terminal attempt still blocks pass — that
            # is the safety the kept edge exists to enforce.
            and not (child_norm in superseded_run_ids and child_norm in invalid_runs)
        ):
            raise RuntimeError(
                f"agent_graph edge child_agent_run_id missing from agent_runs.jsonl: index={idx}"
            )

    # The (node_key, phase) pairs a reopen invalidated, derived from the tombstoned
    # runs' own records. Each must regain at least one fresh (non-superseded) terminal
    # run below before pass — otherwise an orchestration could be marked pass right
    # after `reopen-phase` archived the only evidence and reset phase_state, with no
    # replacement attempt yet.
    reopened_node_phases: set[tuple[str, str]] = set()
    for sid in superseded_run_ids:
        # A superseded invalid-log trigger is absent from `runs`; fall back to its
        # diverted record so its (node, phase) still demands a fresh replacement.
        rec = runs.get(sid) or invalid_runs.get(sid)
        if not isinstance(rec, dict):
            continue
        nk = rec.get("node_key")
        st = rec.get("step")
        if isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip():
            reopened_node_phases.add((nk.strip(), st.strip().lower()))
    fresh_node_phases: set[tuple[str, str]] = set()

    for run_id, payload in runs.items():
        role = payload.get("agent_role")
        if not isinstance(role, str) or role not in {"step", "substep"}:
            continue
        if run_id in superseded_run_ids:
            continue
        status = payload.get("status")
        if not isinstance(status, str) or status.strip().lower() not in TERMINAL_STATUSES:
            raise RuntimeError(f"{role} agent_run_id must be terminal before pass: {run_id}")
        _validate_step_or_substep_launch_refs(repo_root, payload)
        node_key = payload.get("node_key")
        step = payload.get("step")
        if not isinstance(node_key, str) or not node_key.strip():
            raise RuntimeError(f"{role} node_key missing: {run_id}")
        if not isinstance(step, str) or not step.strip():
            raise RuntimeError(f"{role} step missing: {run_id}")
        node_safe = _node_key_to_safe(node_key.strip())
        step_token = step.strip().lower()
        if role == "step":
            result_path = root / "steps" / node_safe / step_token / run_id / "step_result.json"
            if not result_path.exists():
                raise RuntimeError(f"step_result.json missing for step agent_run_id={run_id}")
        else:
            if run_id not in step_result_refs_by_substep:
                raise RuntimeError(
                    f"step_result.json missing substep_agent_run_ids entry for substep agent_run_id={run_id}"
                )
        fresh_node_phases.add((node_key.strip(), step_token))

    # A reopened phase must have a replacement: at least one fresh (non-superseded)
    # terminal run vouched above. Without this, the superseded-run exemption would let
    # pass succeed on reopened phases whose evidence was archived and not yet rebuilt.
    missing_replacements = sorted(reopened_node_phases - fresh_node_phases)
    if missing_replacements:
        detail = ", ".join(f"{nk}:{ph}" for nk, ph in missing_replacements)
        raise RuntimeError(
            "cannot mark orchestration pass: reopened phase(s) have no fresh (non-superseded) "
            f"run after reopen-phase — re-run them before pass: {detail}"
        )


_STEP_META_FILENAME = STAGE_META_FILENAME_BY_STEP

STEP_REQUIRED_VALIDATION_STAGES: dict[str, frozenset[str]] = {
    "compile": frozenset({"compile", "full"}),
    # Generate accepts only `post_generate` (the validator stage that
    # checks generation outputs) and `full`. `post_build` is Build's
    # responsibility — its semantics are "validation after the build
    # artifact exists" and applying it to a Generate terminal status
    # would conflate phase boundaries. No production code or test
    # exercises `generate + post_build`; the previous permissive set was
    # a tolerance with no legitimate use case.
    "generate": frozenset({"post_generate", "full"}),
    "build": frozenset({"post_build", "full"}),
    "validate": frozenset({"post_execute", "pre_judge", "full"}),
}

_RETRY_DECISION_REQUIRED_KEYS: tuple[str, ...] = (
    "issue_severity",
    "repair_strategy",
    "repair_target_agent_run_id",
    "new_agent_run_id",
    "repair_reason",
)


def _validate_lint_command_ref(meta_data: dict[str, Any], *, meta_filename: str, meta_ref: str) -> None:
    lint_command_ref = meta_data.get("lint_command_ref")
    if meta_filename != "source_meta.json":
        return
    status = str(meta_data.get("verification_status", "")).strip().lower()
    if status != "pass":
        return
    if not isinstance(lint_command_ref, dict):
        raise ValueError(
            f"{meta_filename} missing lint_command_ref when verification_status=pass: {meta_ref}"
        )
    run_linter = lint_command_ref.get("run_linter")
    if not isinstance(run_linter, list) or not run_linter:
        raise ValueError(f"{meta_filename} lint_command_ref.run_linter must be non-empty list: {meta_ref}")
    for idx, item in enumerate(run_linter):
        if not isinstance(item, dict):
            raise ValueError(
                f"{meta_filename} lint_command_ref.run_linter[{idx}] must be object: {meta_ref}"
            )
        for key in ("command_id", "command_log_ref", "preset"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{meta_filename} lint_command_ref.run_linter[{idx}].{key} must be non-empty string: {meta_ref}"
                )


def _validate_step_meta_payload(meta_data: dict[str, Any], *, step_token: str, meta_ref: str) -> None:
    meta_filename = _STEP_META_FILENAME[step_token]
    missing_keys = missing_required_meta_keys(meta_data, step_token=step_token)
    if missing_keys:
        raise ValueError(
            f"{meta_filename} missing required keys: {missing_keys} "
            f"(phase={step_token} substep=verify ref={meta_ref})"
        )
    context_isolated = meta_data.get("context_isolated")
    if not isinstance(context_isolated, bool):
        raise ValueError(f"{meta_filename} context_isolated must be boolean: {meta_ref}")
    if not isinstance(meta_data.get("debug_mode"), bool):
        raise ValueError(f"{meta_filename} debug_mode must be boolean: {meta_ref}")
    if not isinstance(meta_data.get("attempt_count"), int):
        raise ValueError(f"{meta_filename} attempt_count must be integer: {meta_ref}")
    verification_status = meta_data.get("verification_status")
    if not isinstance(verification_status, str) or not verification_status.strip():
        raise ValueError(f"{meta_filename} verification_status must be non-empty string: {meta_ref}")
    last_fail_reason = meta_data.get("last_fail_reason")
    if last_fail_reason is not None and not isinstance(last_fail_reason, str):
        raise ValueError(f"{meta_filename} last_fail_reason must be string or null: {meta_ref}")
    if context_isolated is False:
        constraint_reason = meta_data.get("constraint_reason")
        if not isinstance(constraint_reason, str) or not constraint_reason.strip():
            raise ValueError(
                f"{meta_filename} requires non-empty constraint_reason when context_isolated=false: {meta_ref}"
            )
    _validate_lint_command_ref(meta_data, meta_filename=meta_filename, meta_ref=meta_ref)


def _effective_pass_substep_run_ids(
    payload: dict[str, Any],
    *,
    repo_root: Path,
    orchestration_id: str,
    run_records: dict[str, dict[str, Any]],
    node_key: str,
    step_token: str,
) -> tuple[list[str], dict[str, set[str]]]:
    substep_run_ids = payload.get("substep_agent_run_ids")
    if not isinstance(substep_run_ids, list) or not substep_run_ids:
        raise ValueError(f"pass step_result for {step_token} requires non-empty substep_agent_run_ids")

    listed_run_ids: list[str] = []
    listed_run_id_set: set[str] = set()
    for idx, substep_run_id in enumerate(substep_run_ids):
        if not isinstance(substep_run_id, str) or not substep_run_id.strip():
            raise ValueError(f"substep_agent_run_ids[{idx}] must be non-empty string")
        token = substep_run_id.strip()
        if token in listed_run_id_set:
            raise ValueError(f"substep_agent_run_ids must not contain duplicates: {token}")
        listed_run_ids.append(token)
        listed_run_id_set.add(token)

        substep_record = run_records.get(token)
        if not isinstance(substep_record, dict):
            raise ValueError(f"missing substep run record: {token}")
        role = str(substep_record.get("agent_role") or "").strip().lower()
        if role != "substep":
            raise ValueError(f"listed run must be substep role: {token}")
        record_node_key = str(substep_record.get("node_key") or "").strip()
        if record_node_key != node_key:
            raise ValueError(f"listed substep run node_key mismatch: {token}")
        record_step = str(substep_record.get("step") or "").strip().lower()
        if record_step != step_token:
            raise ValueError(f"listed substep run step mismatch: {token}")

    failed_substeps = payload.get("failed_substeps", [])
    if not isinstance(failed_substeps, list):
        raise ValueError("step_result.failed_substeps must be list")
    explicit_failed_run_ids: set[str] = set()
    for idx, failed_run_id in enumerate(failed_substeps):
        if not isinstance(failed_run_id, str) or not failed_run_id.strip():
            raise ValueError(f"failed_substeps[{idx}] must be non-empty string")
        token = failed_run_id.strip()
        if token not in listed_run_id_set:
            raise ValueError(f"failed_substeps[{idx}] must be listed in substep_agent_run_ids: {token}")
        failed_status = str(run_records[token].get("status") or "").strip().lower()
        if failed_status == "pass":
            raise ValueError(f"failed_substeps[{idx}] must reference actual non-pass run: {token}")
        explicit_failed_run_ids.add(token)

    retry_decisions = payload.get("retry_decisions", [])
    if retry_decisions is None:
        retry_decisions = []
    if not isinstance(retry_decisions, list):
        raise ValueError("step_result.retry_decisions must be list when provided")
    replaced_run_ids: set[str] = set()
    adopted_run_ids: set[str] = set()
    for idx, item in enumerate(retry_decisions):
        if not isinstance(item, dict):
            raise ValueError(f"retry_decisions[{idx}] must be object")
        missing_keys = [
            key for key in _RETRY_DECISION_REQUIRED_KEYS
            if not isinstance(item.get(key), str) or not str(item.get(key)).strip()
        ]
        if missing_keys:
            raise ValueError(
                f"retry_decisions[{idx}] missing required string keys: {missing_keys}"
            )
        repair_target = str(item["repair_target_agent_run_id"]).strip()
        new_run_id = str(item["new_agent_run_id"]).strip()
        repair_strategy = str(item.get("repair_strategy") or "").strip().lower()
        repair_reason = str(item.get("repair_reason") or "").strip().lower()
        if repair_target not in listed_run_id_set:
            raise ValueError(
                f"retry_decisions[{idx}].repair_target_agent_run_id must be listed in substep_agent_run_ids: {repair_target}"
            )
        if new_run_id not in listed_run_id_set:
            raise ValueError(
                f"retry_decisions[{idx}].new_agent_run_id must be listed in substep_agent_run_ids: {new_run_id}"
            )
        if repair_target == new_run_id:
            raise ValueError(f"retry_decisions[{idx}] must replace a different run_id")
        if repair_target in replaced_run_ids:
            raise ValueError(f"retry_decisions must not replace the same run twice: {repair_target}")
        repair_target_status = str(run_records[repair_target].get("status") or "").strip().lower()
        if repair_target_status == "pass":
            raise ValueError(
                f"retry_decisions[{idx}].repair_target_agent_run_id must reference actual non-pass run: {repair_target}"
            )
        violation_path = (
            _violations_dir(repo_root, orchestration_id)
            / f"{repair_target}.noncanonical_phase_write_attempt.json"
        )
        has_noncanonical_violation = violation_path.exists()
        if (has_noncanonical_violation or "noncanonical_phase_write_attempt" in repair_reason) and repair_strategy != "restart":
            raise ValueError(
                f"retry_decisions[{idx}] must use repair_strategy='restart' for noncanonical_phase_write_attempt"
            )
        replaced_run_ids.add(repair_target)
        adopted_run_ids.add(new_run_id)

    effective_run_ids: list[str] = []
    for run_id in listed_run_ids:
        if run_id in replaced_run_ids or run_id in explicit_failed_run_ids:
            continue
        substep_record = run_records[run_id]
        substep_status = str(substep_record.get("status") or "").strip().lower()
        if substep_status != "pass":
            raise ValueError(
                f"non-pass substep {run_id} must be excluded by failed_substeps or retry_decisions before step_result can pass"
            )
        effective_run_ids.append(run_id)

    for run_id in adopted_run_ids:
        substep_status = str(run_records[run_id].get("status") or "").strip().lower()
        if substep_status != "pass":
            raise ValueError(f"retry_decisions new_agent_run_id must be pass for step_result pass: {run_id}")
    if not effective_run_ids:
        raise ValueError(f"pass step_result for {step_token} requires at least one effective pass substep")

    return effective_run_ids, {
        "listed_run_ids": listed_run_id_set,
        "explicit_failed_run_ids": explicit_failed_run_ids,
        "replaced_run_ids": replaced_run_ids,
        "adopted_run_ids": adopted_run_ids,
    }


def _pre_phase_complete_judge_checks(
    repo_root: Path,
    *,
    node_key: str,
    status_token: str,
    payload: dict[str, Any],
) -> None:
    lr_ref = payload.get("launch_request_ref")
    if not isinstance(lr_ref, str) or not lr_ref.strip():
        raise ValueError("judge step_result requires launch_request_ref for pre_phase_complete hook")
    lr_path = repo_root / lr_ref.strip()
    if not lr_path.is_file():
        raise ValueError(f"judge launch_request_ref not found: {lr_ref}")
    try:
        lr = _read_json(lr_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge launch_request_ref invalid json: {lr_ref}") from exc
    if not isinstance(lr, dict):
        raise ValueError(f"judge launch_request must be object: {lr_ref}")
    pr = lr.get("pipeline_ref")
    if not isinstance(pr, str) or not pr.strip():
        raise ValueError("judge launch_request missing pipeline_ref")
    base, err = _resolve_judge_execution_dir(
        repo_root,
        pipeline_ref=pr.strip(),
        node_key=node_key,
        launch_request=lr,
    )
    if base is None:
        raise ValueError(f"judge execution directory not resolved: {err}")
    if status_token in JUDGE_SEMANTIC_REVIEW_SKIPPED_STATUSES:
        return
    sem = base / "semantic_review.json"
    if not sem.is_file():
        raise ValueError("pre_phase_complete: judge requires semantic_review.json")
    try:
        sdoc = _read_json(sem)
    except json.JSONDecodeError as exc:
        raise ValueError("semantic_review.json must be valid json") from exc
    if not isinstance(sdoc, dict):
        raise ValueError("semantic_review.json must be a json object")
    dec = sdoc.get("decision")
    if dec is None or (isinstance(dec, str) and not str(dec).strip()):
        raise ValueError("semantic_review.json decision missing (completion forbidden)")
    dec_norm = str(dec).strip().lower()
    if dec_norm == "fail" and status_token == "pass":
        raise ValueError("semantic_review.json decision=fail cannot accompany pass step_result")
    if dec_norm == "pass" and status_token == "fail":
        raise ValueError("semantic_review.json decision=pass cannot accompany fail step_result")
    # NOTE: `blocked` is intentionally decoupled from `decision`. A `pass`
    # semantic_review reflects node-physics correctness, whereas `blocked` means
    # the node could not be CERTIFIED due to a non-physics blocker (e.g. an
    # orchestration-record integrity gate failure that is unrecoverable in the
    # current run). Coupling those two axes previously deadlocked terminalization
    # — write-step-result rejected `fail`/`blocked` while semantic_review=pass yet
    # `pass` was impossible without a finalized verdict — forcing fail_closed as
    # the only escape. A node whose physics passed may still honestly terminalize
    # as `blocked`; the additional `blocked` artifact requirements below remain
    # enforced so the verdict is not merely asserted.
    if status_token == "blocked":
        for name in ("aggregate_verdict.json", "summary.json", "trial_meta.json"):
            if not (base / name).is_file():
                raise ValueError(f"blocked judge requires {name} under execution directory")


def post_phase_complete(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
) -> None:
    from tools.validate_workspace_root import _validate_write_scope_from_baseline

    step_token = step.strip().lower()
    violations: list[str] = []
    baseline_ref = payload.get("write_scope_baseline_ref")
    if isinstance(baseline_ref, str) and baseline_ref.strip():
        bp = repo_root / _normalize_rel_posix(baseline_ref.strip())
        if bp.is_file():
            violations.extend(
                _validate_write_scope_from_baseline(
                    repo_root=repo_root,
                    workspace_root="workspace/",
                    baseline_path=bp,
                )
            )
    orch_root = _orchestration_root(repo_root, orchestration_id)
    resp_path = orch_root / "launches" / f"{agent_run_id.strip()}.response.json"
    req_path = orch_root / "launches" / f"{agent_run_id.strip()}.request.json"
    if resp_path.is_file() and req_path.is_file():
        try:
            rsp = _read_json(resp_path)
            req = _read_json(req_path)
        except (OSError, json.JSONDecodeError):
            rsp = {}
            req = {}
        if isinstance(req, dict) and isinstance(rsp, dict):
            rq_sid = req.get("agent_session_id")
            rs_sid = rsp.get("agent_session_id")
            if (
                isinstance(rq_sid, str)
                and rq_sid.strip()
                and isinstance(rs_sid, str)
                and rs_sid.strip()
                and rq_sid.strip() != rs_sid.strip()
            ):
                violations.append(
                    "post_phase_complete: launch response agent_session_id mismatch vs request"
                )
    if violations:
        _append_workflow_hook_log(
            repo_root,
            orchestration_id,
            hook_name="post_phase_complete",
            status="deny",
            detail={"violations": violations, "step": step_token},
        )
        raise RuntimeError("post_phase_complete denied: " + "; ".join(violations))
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="post_phase_complete",
        status="allow",
        detail={"step": step_token, "agent_run_id": agent_run_id},
    )


def _validate_step_result_payload(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
) -> None:
    step_token = step.strip().lower()
    status = payload.get("status")
    status_token = status.strip().lower() if isinstance(status, str) else ""

    # validation_stage check (at terminal of generate/build/execute/judge)
    if step_token in STEP_REQUIRED_VALIDATION_STAGES and status_token in TERMINAL_STATUSES:
        allowed = STEP_REQUIRED_VALIDATION_STAGES[step_token]
        validation_stage = payload.get("validation_stage")
        if not isinstance(validation_stage, str) or validation_stage.strip() not in allowed:
            raise ValueError(
                f"terminal step_result for {step_token} requires validation_stage in "
                f"{sorted(allowed)}; status={status_token!r} validation_stage={validation_stage!r}"
            )
        _append_workflow_hook_log(
            repo_root,
            orchestration_id,
            hook_name="pre_phase_complete",
            status="allow",
            detail={"step": step_token, "status": status_token, "validation_stage": validation_stage},
        )

    if step_token == "validate" and status_token in TERMINAL_STATUSES:
        _pre_phase_complete_judge_checks(
            repo_root,
            node_key=node_key,
            status_token=status_token,
            payload=payload,
        )

    # the following is the existing substep verification (compile/generate/validate only)
    if step_token not in SUBSTEP_AWARE_STEPS:
        return
    if status_token != "pass":
        return

    run_records = _load_run_records(_orchestration_root(repo_root, orchestration_id))
    effective_run_ids, _ = _effective_pass_substep_run_ids(
        payload,
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        run_records=run_records,
        node_key=node_key,
        step_token=step_token,
    )
    required_outputs = payload.get("required_outputs")
    if not isinstance(required_outputs, list):
        raise ValueError("step_result.required_outputs must be list")
    declared_outputs = {item.strip() for item in required_outputs if isinstance(item, str) and item.strip()}

    substep_outputs: set[str] = set()
    for substep_run_id in effective_run_ids:
        substep_record = run_records[substep_run_id]
        output_refs = substep_record.get("output_refs")
        if not isinstance(output_refs, list) or not output_refs:
            raise ValueError(f"substep {substep_run_id} must publish non-empty output_refs")
        for output_ref in output_refs:
            if isinstance(output_ref, str) and output_ref.strip():
                substep_outputs.add(output_ref.strip())

    # meta file verification (only on pass of plan/generate)
    if step_token in _STEP_META_FILENAME:
        meta_filename = _STEP_META_FILENAME[step_token]
        meta_refs = [ref for ref in declared_outputs if ref.endswith(meta_filename)]
        if not meta_refs:
            raise ValueError(
                f"pass step_result for {step_token} requires required_outputs to include final {meta_filename}"
            )
        if len(meta_refs) != 1:
            raise ValueError(
                f"pass step_result for {step_token} requires exactly one final {meta_filename} in required_outputs"
            )
        meta_ref = meta_refs[0]
        if meta_ref not in substep_outputs:
            raise ValueError(
                f"step_result.required_outputs must reference final {meta_filename} from effective substep output_refs: {meta_ref}"
            )
        meta_path = repo_root / meta_ref
        if not meta_path.exists():
            raise ValueError(
                f"{meta_filename} not found at output_ref: {meta_ref}"
            )
        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{meta_filename} is not valid JSON: {meta_ref}"
            ) from exc
        if not isinstance(meta_data, dict):
            raise ValueError(f"{meta_filename} must be a JSON object: {meta_ref}")
        _validate_step_meta_payload(
            meta_data,
            step_token=step_token,
            meta_ref=meta_ref,
        )

    missing_outputs = sorted(ref for ref in declared_outputs if ref not in substep_outputs)
    if missing_outputs:
        raise ValueError(
            "step_result.required_outputs must be satisfied by substep output_refs: "
            + ", ".join(missing_outputs)
        )


def parse_feature_list(raw: str) -> dict[str, bool]:
    features: dict[str, bool] = {}
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        enabled = parts[-1].lower()
        if enabled not in {"true", "false"}:
            continue
        feature_name = parts[0].strip()
        if feature_name:
            features[feature_name] = enabled == "true"
    return features


def _probe_existing_directory_writable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path} does not exist"
    if not path.is_dir():
        return False, f"{path} is not a directory"
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(path),
            prefix=".codex-orchestration-preflight-",
            delete=True,
        ) as handle:
            handle.write(b"probe")
            handle.flush()
    except OSError as exc:
        return False, f"{path}: {exc}"
    return True, str(path)


def _probe_codex_home_writable() -> dict[str, Any]:
    raw = os.environ.get("METDSL_HOME")
    source = "env:METDSL_HOME" if isinstance(raw, str) and raw.strip() else "default:~/.codex"
    codex_home = (
        Path(raw).expanduser()
        if isinstance(raw, str) and raw.strip()
        else (Path.home() / ".codex")
    )
    if codex_home.exists():
        ok, detail = _probe_existing_directory_writable(codex_home)
        return {
            "name": "codex_home_writable",
            "pass": ok,
            "detail": f"{source} path={codex_home} detail={detail}",
        }
    parent = codex_home.parent
    ok, detail = _probe_existing_directory_writable(parent)
    detail_text = (
        f"{source} path={codex_home} parent={parent} "
        + ("parent writable; codex_home can be created" if ok else f"parent not writable: {detail}")
    )
    return {"name": "codex_home_writable", "pass": ok, "detail": detail_text}


def _probe_bwrap_sandbox() -> tuple[list[dict[str, Any]], bool]:
    checks: list[dict[str, Any]] = []
    assume = os.environ.get("METDSL_ORCHESTRATION_ASSUME_BWRAP", "").strip().lower()
    if assume in {"1", "true", "yes"}:
        checks.extend(
            [
                {"name": "sandbox_bwrap_available", "pass": True, "detail": "assumed via env override"},
                {"name": "sandbox_bwrap_userns", "pass": True, "detail": "assumed via env override"},
                {"name": "sandbox_bwrap_exec", "pass": True, "detail": "assumed via env override"},
            ]
        )
        return checks, True

    bwrap_path = shutil.which("bwrap")
    bwrap_available = bool(bwrap_path)
    checks.append(
        {
            "name": "sandbox_bwrap_available",
            "pass": bwrap_available,
            "detail": bwrap_path if bwrap_path else "bwrap not found in PATH",
        }
    )
    if not bwrap_available:
        checks.append(
            {
                "name": "sandbox_bwrap_userns",
                "pass": False,
                "detail": "skipped because bwrap is unavailable",
            }
        )
        checks.append(
            {
                "name": "sandbox_bwrap_exec",
                "pass": False,
                "detail": "skipped because bwrap is unavailable",
            }
        )
        return checks, False

    proc = subprocess.run(["bwrap", "--version"], text=True, capture_output=True, check=False)
    userns_ok = proc.returncode == 0
    checks.append(
        {
            "name": "sandbox_bwrap_userns",
            "pass": userns_ok,
            "detail": (proc.stdout.strip() or proc.stderr.strip() or f"exit={proc.returncode}"),
        }
    )
    dry_run = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--", "sh", "-lc", "true"],
        text=True,
        capture_output=True,
        check=False,
    )
    checks.append(
        {
            "name": "sandbox_bwrap_exec",
            "pass": dry_run.returncode == 0,
            "detail": (dry_run.stdout.strip() or dry_run.stderr.strip() or f"exit={dry_run.returncode}"),
        }
    )
    required_names = {"sandbox_bwrap_available", "sandbox_bwrap_userns", "sandbox_bwrap_exec"}
    by_name = {
        str(item.get("name")): item.get("pass")
        for item in checks
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    sandbox_enforced = all(by_name.get(name) is True for name in required_names)
    return checks, sandbox_enforced


def _probe_codex_backend(
    backend_token: str,
    command: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[list[dict[str, Any]], dict[str, bool], bool, str]:
    """Run the codex backend probe and return (checks, features, multi_agent_enabled, agent_version)."""
    version_proc = runner([command, "--version"], text=True, capture_output=True, check=False)
    features_proc = runner([command, "features", "list"], text=True, capture_output=True, check=False)
    features: dict[str, bool] = {}
    features_list_available = features_proc.returncode == 0
    multi_agent_enabled = False
    if features_proc.returncode == 0:
        features = parse_feature_list(features_proc.stdout)
        multi_agent_enabled = features.get("multi_agent") is True
    features_list_detail = features_proc.stdout.strip() or features_proc.stderr.strip()
    checks = [
        {
            "name": f"{backend_token}_version_available",
            "pass": version_proc.returncode == 0,
            "detail": version_proc.stdout.strip() or version_proc.stderr.strip(),
        },
        {
            "name": f"{backend_token}_features_list_available",
            "pass": features_list_available,
            "detail": features_list_detail,
        },
        {
            "name": "multi_agent_enabled",
            "pass": multi_agent_enabled,
            "detail": f"multi_agent={features.get('multi_agent')}",
        },
    ]
    return checks, features, multi_agent_enabled, version_proc.stdout.strip()


def _pass_values_by_check_name(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Make each check's `pass` lookupable by name. `pass` is bool or None (skipped, not run)."""
    by_name: dict[str, Any] = {}
    for item in checks:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            by_name[name] = item.get("pass")
    return by_name


def _can_launch_from_help_fallback_checks(
    backend_token: str, checks: list[dict[str, Any]]
) -> bool:
    """For claude. Even without `features list`, treat it as launchable if `--help` passes."""
    passes = _pass_values_by_check_name(checks)
    version_ok = passes.get(f"{backend_token}_version_available") is True
    features_list_ok = passes.get(f"{backend_token}_features_list_available") is True
    help_pass = passes.get(f"{backend_token}_help_probe_available")
    multi_ok = passes.get("multi_agent_enabled") is True
    # When `pass` is None, --help was not run (multi_agent already determined by features list).
    # In that case, delegate to `features_list_ok` and do not silently treat `None` as False-equivalent.
    help_confirms_launch = help_pass is True
    return version_ok and multi_ok and (features_list_ok or help_confirms_launch)


def _all_strict_boolean_probe_checks_pass(checks: list[dict[str, Any]]) -> bool:
    """For codex etc. The `pass` key is required. A check with value None is excluded from evaluation as an unrun probe.

    An explicit False `pass` is a failure. At least 1 non-None `pass` must exist,
    and all of them must be True. Even if a check column from the help fallback is mistakenly passed,
    do not silently fail on only `pass: None`.
    """
    evaluated_any = False
    for item in checks:
        if not isinstance(item, dict):
            return False
        if "pass" not in item:
            return False
        p = item["pass"]
        if p is None:
            continue
        evaluated_any = True
        if p is not True:
            return False
    return evaluated_any


def _probe_claude_backend(
    backend_token: str,
    command: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[list[dict[str, Any]], dict[str, bool], bool, str]:
    """A probe specific to the claude backend.

    `claude features list` is a subcommand that does not exist in the Claude Code CLI,
    and running it makes claude return its chat response as-is to stdout (exit 0).
    This response mixes into `features_list_available.detail` and contaminates preflight.json
    (observed in orch_20260610T130256Z_ebe96a51).

    Fix: on the claude backend, do not run `features list` and treat it as advisory,
    and use the `--help` liveness probe for the best-effort detection of multi_agent.
    Because Claude Code's child agent is launched not by a CLI subcommand but by the `Agent` tool,
    no `agents` subcommand appears in the `--help` output (the old docstring's claim is wrong).
    This probe requires (a) `--help` to exit 0 and (b) stdout to return help text,
    preventing a false-pass of an empty-output binary impersonating `claude`. The authoritative gate of `multi_agent`
    remains on the launch-time live preflight side of `record_launch`.
    """
    version_proc = runner([command, "--version"], text=True, capture_output=True, check=False)
    # Skip `features list` for claude: the subcommand does not exist in Claude Code CLI
    # and would result in a full chat session response being captured as the probe output,
    # contaminating preflight.json with assistant text.  Mark as advisory (pass=None).
    features_list_pass: bool | None = None
    features_list_detail = (
        "skipped for claude backend: 'claude features list' is not a structured CLI "
        "subcommand; Claude Code responds with a chat reply which is not machine-parseable. "
        "multi_agent detection uses --help probe instead."
    )

    help_proc = runner([command, "--help"], text=True, capture_output=True, check=False)
    help_stdout = help_proc.stdout.strip()
    # P2-C: require BOTH exit 0 AND non-empty help stdout.  Exit-code alone is a
    # weak proxy — any binary named `claude` that exits 0 (even with no output)
    # would otherwise pass.  Requiring help text on stdout rules out broken or
    # substitute binaries.  This is still a best-effort liveness signal; the
    # authoritative multi_agent gate is the launch-time live preflight.
    multi_agent_enabled = help_proc.returncode == 0 and bool(help_stdout)
    features: dict[str, bool] = {"multi_agent": multi_agent_enabled}
    help_probe_pass = multi_agent_enabled
    help_detail = help_stdout or help_proc.stderr.strip()
    help_probe_detail = help_detail if help_detail else "(no stdout/stderr from --help)"

    checks = [
        {
            "name": f"{backend_token}_version_available",
            "pass": version_proc.returncode == 0,
            "detail": version_proc.stdout.strip() or version_proc.stderr.strip(),
        },
        {
            "name": f"{backend_token}_features_list_available",
            "pass": features_list_pass,
            "detail": features_list_detail,
        },
        {
            "name": f"{backend_token}_help_probe_available",
            "pass": help_probe_pass,
            "detail": help_probe_detail,
        },
        {
            "name": "multi_agent_enabled",
            "pass": multi_agent_enabled,
            "detail": f"multi_agent={features.get('multi_agent')}",
        },
    ]
    return checks, features, multi_agent_enabled, version_proc.stdout.strip()


_BACKEND_PROBERS: dict[
    str,
    Callable[
        [str, str, Callable[..., subprocess.CompletedProcess[str]]],
        tuple[list[dict[str, Any]], dict[str, bool], bool, str],
    ],
] = {
    "codex": _probe_codex_backend,
    "claude": _probe_claude_backend,
}


_CLAUDE_MCP_BUILD_RUNTIME_SERVER_RELPATH = "mcp_servers/build_runtime_server.py"
_CLAUDE_MCP_BUILD_RUNTIME_NAME_TOKENS = ("build-runtime", "build_runtime")
# The canonical source for enablement is the project settings committed to the repo. `~/.claude.json`
# (per-user / per-machine trust history) is intentionally not referenced — because it would cause the
# preflight result to vary per machine (reproducibility first).
_CLAUDE_PROJECT_SETTINGS_RELPATH = ".claude/settings.json"
_CLAUDE_PROJECT_LOCAL_SETTINGS_RELPATH = ".claude/settings.local.json"
_MCP_JSON_RELPATH = ".mcp.json"
_CLAUDE_MCP_REMEDIATION = (
    "build-runtime MCP server is not enabled for this project via the repo-committed "
    "`.claude/settings.json`. Required tools (run_linter, compile_project, run_program, "
    "run_quality_checks) are needed by Generate/Build/Validate phases (detect_build_system "
    "is advisory — provided by the server but not gated). "
    "Remediation: add `\"enabledMcpjsonServers\": [\"build-runtime\"]` (or "
    "`\"enableAllProjectMcpServers\": true`) to the top level of the committed "
    "`.claude/settings.json`, and ensure no `disabledMcpjsonServers` entry for build-runtime "
    "exists in `.claude/settings.json` / `.claude/settings.local.json`. "
    "Reference: `mcp_servers/README.md`."
)
# The canonical form of the permission rule string. Because Claude Code's permission rule does not
# interpret a wildcard in the MCP tool name part (`mcp__build-runtime__*`), a server-level grant covering all tools is the proper approach.
# The canonical (hyphen) token for displaying the remediation message.
_CLAUDE_MCP_SERVER_PERMISSION_TOKEN = "mcp__build-runtime"
# The individual tool names required for the granted decision. detect_build_system is advisory (not
# included in the granted decision) — only the 4 tools that Generate/Build/Validate require are gated.
_CLAUDE_MCP_REQUIRED_TOOL_NAMES = (
    "run_linter",
    "compile_project",
    "run_program",
    "run_quality_checks",
)
# The canonical (hyphen) token for displaying the remediation message.
_CLAUDE_MCP_REQUIRED_TOOL_PERMISSION_TOKENS = tuple(
    f"mcp__build-runtime__{name}" for name in _CLAUDE_MCP_REQUIRED_TOOL_NAMES
)
# The permission token accepted in the decision is derived from the server alias actually enabled in
# registration (see _evaluate_build_runtime_tool_permission below). When the enabled alias (e.g. `build-runtime`)
# and the permission's alias (e.g. `mcp__build_runtime`) diverge, Claude keys the permission by the actual
# server name, so the child Agent cannot call the tool — an unconditional cross-alias accept is a source of false-pass.
_CLAUDE_MCP_PERMISSION_REMEDIATION = (
    "build-runtime MCP tools are registered/connected but not permission-granted to the "
    "orchestration's spawned child Agent sessions. Add the server-level grant "
    "`\"mcp__build-runtime\"` to `permissions.allow` in the repo-committed "
    "`.claude/settings.json` (this grants all build-runtime tools — run_linter, "
    "compile_project, run_program, run_quality_checks, detect_build_system). Claude Code "
    "permission rules do NOT support a tool-name wildcard (`mcp__build-runtime__*`), so use "
    "the server-level token; to grant individually, list "
    "`mcp__build-runtime__run_linter` / `__compile_project` / `__run_program` / "
    "`__run_quality_checks`. Ensure no matching `permissions.deny` entry exists in "
    "`.claude/settings.json` / `.claude/settings.local.json`. Reference: `mcp_servers/README.md`."
)


def _load_json_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read a JSON object from path. Return value (obj, error).

    If it does not exist, (None, None) (the caller can handle "none"). On a read/parse failure or
    a non-object, return (None, error_string).
    """
    if not path.exists():
        return None, None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"read_error ({path}): {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, f"not_object ({path})"
    return data, None


def _read_repo_enabled_mcp_servers(
    repo_root: Path,
    *,
    settings_path: Path | None = None,
    local_settings_path: Path | None = None,
    mcp_json_path: Path | None = None,
) -> tuple[set[str] | None, str]:
    """Read the set of enabled MCP server names from the project settings committed to the repo.

    The canonical source is `<repo>/.claude/settings.json` (flat key). `~/.claude.json`
    (per-user / per-machine trust history) is intentionally not referenced — to avoid the result
    varying per machine and ensure reproducibility. The set is obtained by the following operation:
        (.claude/settings.json: enabledMcpjsonServers)
        ∪ (all mcpServers keys of .mcp.json when enableAllProjectMcpServers=true)
        − (.claude/settings.json: disabledMcpjsonServers)
        − (.claude/settings.local.json: disabledMcpjsonServers)   # personal opt-out detection

    When `.claude/settings.json` is absent / invalid JSON / a non-object, return (None, explanation).
    None means "undetermined" and is treated as a gate fail (the repo is not configured).

    Known trade-off: because the opt-out via disabledMcpjsonServers of `~/.claude.json` is not seen,
    a user who disabled it via that path can false-pass (allowed, reproducibility first).
    """
    settings = (
        settings_path
        if settings_path is not None
        else repo_root / _CLAUDE_PROJECT_SETTINGS_RELPATH
    )
    local_settings = (
        local_settings_path
        if local_settings_path is not None
        else repo_root / _CLAUDE_PROJECT_LOCAL_SETTINGS_RELPATH
    )
    mcp_json = (
        mcp_json_path if mcp_json_path is not None else repo_root / _MCP_JSON_RELPATH
    )

    data, err = _load_json_object(settings)
    if data is None:
        if err is not None:
            return None, f"project_settings_{err}"
        return None, f"project_settings_missing ({settings})"

    enabled: set[str] = set()
    enabled_list = data.get("enabledMcpjsonServers")
    if isinstance(enabled_list, list):
        enabled.update(str(x) for x in enabled_list if isinstance(x, str))

    enable_all = data.get("enableAllProjectMcpServers") is True
    if enable_all:
        mcp_data, _mcp_err = _load_json_object(mcp_json)
        if isinstance(mcp_data, dict):
            mcp_servers = mcp_data.get("mcpServers")
            if isinstance(mcp_servers, dict):
                enabled.update(str(k) for k in mcp_servers.keys() if isinstance(k, str))

    disabled_proj = data.get("disabledMcpjsonServers")
    if isinstance(disabled_proj, list):
        for x in disabled_proj:
            if isinstance(x, str):
                enabled.discard(x)

    local_disabled: set[str] = set()
    local_data, _local_err = _load_json_object(local_settings)
    if isinstance(local_data, dict):
        local_disabled_list = local_data.get("disabledMcpjsonServers")
        if isinstance(local_disabled_list, list):
            local_disabled.update(
                str(x) for x in local_disabled_list if isinstance(x, str)
            )
            enabled.difference_update(local_disabled)

    detail = (
        f"settings={settings}; enable_all={enable_all}; "
        f"enabled_servers={sorted(enabled)}; "
        f"local_disabled={sorted(local_disabled)}"
    )
    return enabled, detail


def _read_repo_mcp_tool_permissions(
    repo_root: Path,
    *,
    settings_path: Path | None = None,
    local_settings_path: Path | None = None,
) -> tuple[set[str], set[str], str | None, str]:
    """Read the state of MCP tool permission from the project settings committed to the repo.

    The canonical source is the `permissions` section of `<repo>/.claude/settings.json`.
    The `permissions.allow` (personal opt-in addition) and `permissions.deny` (personal opt-out)
    of `.claude/settings.local.json` are also combined/subtracted. `~/.claude.json` is not
    referenced for the same reason as the enablement check (machine-dependent, reproducibility).

    Return value `(allow_set, deny_set, default_mode, detail)`:
      - allow_set: the string set of project + local `permissions.allow`
      - deny_set:  the string set of project + local `permissions.deny`
      - default_mode: `permissions.defaultMode` (project first, else local, else None)
      - detail: a diagnostic string
    """
    settings = (
        settings_path
        if settings_path is not None
        else repo_root / _CLAUDE_PROJECT_SETTINGS_RELPATH
    )
    local_settings = (
        local_settings_path
        if local_settings_path is not None
        else repo_root / _CLAUDE_PROJECT_LOCAL_SETTINGS_RELPATH
    )

    def _collect(path: Path) -> tuple[set[str], set[str], str | None]:
        data, _err = _load_json_object(path)
        if not isinstance(data, dict):
            return set(), set(), None
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            return set(), set(), None
        # Ignore a non-list (null / str / int etc.) and treat it as an empty set. Do `for x in value`
        # only after confirming a list, to prevent a TypeError (preflight abort) on a malformed value.
        allow_raw = perms.get("allow")
        allow = (
            {str(x) for x in allow_raw if isinstance(x, str)}
            if isinstance(allow_raw, list)
            else set()
        )
        deny_raw = perms.get("deny")
        deny = (
            {str(x) for x in deny_raw if isinstance(x, str)}
            if isinstance(deny_raw, list)
            else set()
        )
        mode = perms.get("defaultMode")
        return allow, deny, (mode if isinstance(mode, str) else None)

    proj_allow, proj_deny, proj_mode = _collect(settings)
    local_allow, local_deny, local_mode = _collect(local_settings)

    allow_set = proj_allow | local_allow
    deny_set = proj_deny | local_deny
    default_mode = proj_mode if proj_mode is not None else local_mode

    detail = (
        f"settings={settings}; allow={sorted(allow_set)}; "
        f"deny={sorted(deny_set)}; default_mode={default_mode}"
    )
    return allow_set, deny_set, default_mode, detail


def _evaluate_build_runtime_tool_permission(
    repo_root: Path,
    *,
    enabled_aliases: set[str] | None = None,
    settings_path: Path | None = None,
    local_settings_path: Path | None = None,
) -> tuple[bool, str]:
    """Determine whether the build-runtime MCP tool is permission-granted to the child Agent session.

    `enabled_aliases` is the set of build-runtime server aliases actually enabled in registration
    (`{"build-runtime"}` / `{"build_runtime"}` etc.). The permission token is derived **only from this
    enabled alias**. Because Claude keys the permission by the actual server name,
    when the enabled name and the permission alias diverge, preflight passes (false-pass) even though
    the child Agent cannot call the tool. When `enabled_aliases` is empty/None, the
    registration AND-gate fails separately, so for diagnostic purposes the whole known alias set is used as a fallback.

    granted condition (any of):
      - `permissions.defaultMode == "bypassPermissions"` (unconditional permission for all tools)
      - a server-level grant (`mcp__<enabled-alias>`) is in allow, and there is neither a server-level deny
        nor an individual deny of a required tool
      - the required 4 tools (`run_linter` / `compile_project` / `run_program` /
        `run_quality_checks`) each "have at least 1 allow of an enabled alias and are not denied"
        (and there is no server-level deny)
    Claude Code's deny rule takes precedence over allow. Because a tool-specific deny cancels a server-level allow,
    and a server-level deny cancels an individual tool allow, granted=false if either deny exists
    (false-pass prevention). `detect_build_system` is advisory (out of gate scope because no phase invokes it).
    """
    allow_set, deny_set, default_mode, perms_detail = _read_repo_mcp_tool_permissions(
        repo_root,
        settings_path=settings_path,
        local_settings_path=local_settings_path,
    )

    if default_mode == "bypassPermissions":
        return True, f"granted via defaultMode=bypassPermissions | {perms_detail}"

    # Accept only the enabled alias (preserving canonical order). When unspecified/empty, registration
    # fails separately, so use the whole known alias set as a fallback.
    aliases = tuple(
        a for a in _CLAUDE_MCP_BUILD_RUNTIME_NAME_TOKENS if a in (enabled_aliases or set())
    ) or _CLAUDE_MCP_BUILD_RUNTIME_NAME_TOKENS

    server_tokens = {f"mcp__{alias}" for alias in aliases}
    required_tool_aliases = tuple(
        tuple(f"mcp__{alias}__{name}" for alias in aliases)
        for name in _CLAUDE_MCP_REQUIRED_TOOL_NAMES
    )

    server_allowed = bool(server_tokens & allow_set)
    server_denied = bool(server_tokens & deny_set)

    def _tool_allowed(alias_tokens: tuple[str, ...]) -> bool:
        # true if the tool has at least 1 allow alias and that alias is not denied
        return any(tok in allow_set and tok not in deny_set for tok in alias_tokens)

    def _tool_denied(alias_tokens: tuple[str, ...]) -> bool:
        return any(tok in deny_set for tok in alias_tokens)

    # if any required tool is individually denied, cancel the server-level grant
    required_tool_denied = any(_tool_denied(toks) for toks in required_tool_aliases)
    server_granted = server_allowed and not server_denied and not required_tool_denied

    # because a server-level deny blocks all tools of the server, granted is impossible even with an individual allow.
    tools_granted = (not server_denied) and all(
        _tool_allowed(toks) for toks in required_tool_aliases
    )

    granted = server_granted or tools_granted
    detail_parts = [
        f"granted={granted}",
        f"accepted_aliases={list(aliases)}",
        f"server_grant={server_granted}",
        f"server_allowed={server_allowed}",
        f"server_denied={server_denied}",
        f"required_tool_denied={required_tool_denied}",
        f"all_tool_grants={tools_granted}",
        perms_detail,
    ]
    if not granted:
        detail_parts.append(_CLAUDE_MCP_PERMISSION_REMEDIATION)
    return granted, " | ".join(detail_parts)


def _probe_claude_mcp_registry(
    command: str,
    repo_root: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    settings_path: Path | None = None,
    local_settings_path: Path | None = None,
    mcp_json_path: Path | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Determine whether the Claude session exposes the build-runtime MCP tool in the target repo.

    The canonical signal is the `<repo>/.claude/settings.json` committed to the repo
    (`enabledMcpjsonServers` / `enableAllProjectMcpServers` − `disabledMcpjsonServers`,
    and subtracting the disable of `.claude/settings.local.json`). `~/.claude.json` (per-user /
    per-machine trust history) is not referenced — because it would cause the preflight result
    to vary per machine (reproducibility first).
    Because `claude mcp list` skips the trust dialog and spawns the stdio server, it causes a
    false-positive (returns `✓ Connected` even for an untrusted workspace) — it is treated as an auxiliary
    diagnostic and is not used for the preflight gate (Codex review P1). The timeout of `mcp list` is
    advisory only and does not block the Claude orchestration (P2).

    When `repo_root` is None (mainly in unit tests), return only the advisory-only check.
    """
    if repo_root is None:
        return (
            [
                {
                    "name": "claude_mcp_build_runtime_registered",
                    "pass": None,
                    "detail": (
                        "skipped; probe_execution_platform was called without repo_root "
                        "(advisory only — production calls via cmd_preflight always pass repo_root)"
                    ),
                },
                {
                    # The contract evaluates registered and permission always as a pair (see docs/RUNBOOK.md §0-2).
                    # Even on the advisory-only path without repo_root, include the permission check as skipped rather than omitting it.
                    "name": "claude_mcp_build_runtime_permission_granted",
                    "pass": None,
                    "detail": (
                        "skipped; probe_execution_platform was called without repo_root "
                        "(advisory only — production calls via cmd_preflight always pass repo_root)"
                    ),
                },
            ],
            True,
        )

    enabled_servers, settings_detail = _read_repo_enabled_mcp_servers(
        repo_root,
        settings_path=settings_path,
        local_settings_path=local_settings_path,
        mcp_json_path=mcp_json_path,
    )
    build_runtime_token_set = {tok for tok in _CLAUDE_MCP_BUILD_RUNTIME_NAME_TOKENS}
    if enabled_servers is None:
        session_enabled = False
        enabled_build_runtime_aliases: set[str] = set()
    else:
        enabled_build_runtime_aliases = enabled_servers & build_runtime_token_set
        session_enabled = bool(enabled_build_runtime_aliases)

    # Call `claude mcp list` lightly as an advisory diagnostic. The timeout does not gate.
    try:
        proc = runner(
            [command, "mcp", "list"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
            cwd=str(repo_root),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        list_available: bool | None = None  # advisory: timeout / spawn error is inconclusive
        list_stdout = ""
        list_stderr = f"{type(exc).__name__}: {exc}"
        returncode = -1
    else:
        list_available = proc.returncode == 0
        list_stdout = proc.stdout or ""
        list_stderr = proc.stderr or ""
        returncode = proc.returncode

    name_listed = False
    healthy = False
    matched_line = ""
    for raw_line in list_stdout.splitlines():
        head = raw_line.strip().split(":", 1)[0].strip().lower()
        if not head:
            continue
        normalized = head.replace("_", "-")
        if any(
            normalized == tok or normalized.startswith(tok + " ")
            for tok in (t.replace("_", "-") for t in _CLAUDE_MCP_BUILD_RUNTIME_NAME_TOKENS)
        ):
            name_listed = True
            matched_line = raw_line.strip()
            lower_line = matched_line.lower()
            if "connected" in lower_line and "disconnected" not in lower_line:
                healthy = True
            break

    server_path = repo_root / _CLAUDE_MCP_BUILD_RUNTIME_SERVER_RELPATH
    server_present = server_path.is_file()

    registered_pass = bool(session_enabled)

    list_detail_parts: list[str] = [f"returncode={returncode}"]
    if list_stdout.strip():
        list_detail_parts.append(f"stdout={list_stdout.strip()[:512]}")
    if list_stderr.strip():
        list_detail_parts.append(f"stderr={list_stderr.strip()[:512]}")
    list_detail = "; ".join(list_detail_parts)

    registered_detail_parts: list[str] = [
        f"session_enabled={session_enabled}",
        f"repo_settings_signal={settings_detail}",
        f"mcp_list_advisory: in_list={name_listed}, healthy={healthy}",
        f"server_file_present={server_present} ({_CLAUDE_MCP_BUILD_RUNTIME_SERVER_RELPATH}; diagnostic only)",
    ]
    if matched_line:
        registered_detail_parts.append(f"matched_line={matched_line[:256]}")
    if not registered_pass:
        registered_detail_parts.append(_CLAUDE_MCP_REMEDIATION)
    registered_detail = " | ".join(registered_detail_parts)

    permission_granted, permission_detail = _evaluate_build_runtime_tool_permission(
        repo_root,
        enabled_aliases=enabled_build_runtime_aliases,
        settings_path=settings_path,
        local_settings_path=local_settings_path,
    )

    checks = [
        {
            "name": "claude_mcp_list_available",
            "pass": list_available,
            "detail": list_detail,
        },
        {
            "name": "claude_mcp_build_runtime_registered",
            "pass": registered_pass,
            "detail": registered_detail,
        },
        {
            "name": "claude_mcp_build_runtime_permission_granted",
            "pass": permission_granted,
            "detail": permission_detail,
        },
    ]
    # The AND of registration and permission is the gate signal. Even with registered=true, if the tool
    # is not permission-granted to the child Agent session, the launch is not permitted.
    mcp_ok = registered_pass and permission_granted
    return checks, mcp_ok


def probe_execution_platform(
    *,
    backend: str,
    agent_command: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    backend_token = backend.strip().lower()
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(f"unsupported backend: {backend}")

    default_command = DEFAULT_BACKEND_COMMANDS[backend_token]
    command = (
        agent_command.strip()
        if isinstance(agent_command, str) and agent_command.strip()
        else default_command
    )
    for known_backend, known_command in DEFAULT_BACKEND_COMMANDS.items():
        if command != known_command:
            continue
        if known_backend != backend_token:
            raise ValueError(
                f"agent_command/backend mismatch: backend={backend_token} requires "
                f"{DEFAULT_BACKEND_COMMANDS[backend_token]} (or custom command), got {command}"
            )
        break

    prober = _BACKEND_PROBERS[backend_token]
    checks, features, multi_agent_enabled, agent_version = prober(backend_token, command, runner)

    if backend_token == "claude":
        can_launch_agents = _can_launch_from_help_fallback_checks(backend_token, checks)
        mcp_checks, mcp_ok = _probe_claude_mcp_registry(command, repo_root, runner)
        checks.extend(mcp_checks)
        can_launch_agents = can_launch_agents and mcp_ok
    else:
        can_launch_agents = _all_strict_boolean_probe_checks_pass(checks)
        hooks_enabled = features.get("hooks") is True
        checks.append(
            {
                "name": "hooks_enabled",
                "pass": hooks_enabled,
                "detail": f"hooks={features.get('hooks')}",
            }
        )
        can_launch_agents = can_launch_agents and hooks_enabled
        codex_home_check = _probe_codex_home_writable()
        checks.append(codex_home_check)
        can_launch_agents = can_launch_agents and (codex_home_check.get("pass") is True)
    sandbox_checks, sandbox_enforced = _probe_bwrap_sandbox()
    checks.extend(sandbox_checks)
    can_launch_agents = can_launch_agents and sandbox_enforced
    session_policy = {
        "allow_step_agent_launch": os.environ.get("METDSL_ALLOW_STEP_AGENT_LAUNCH", "1").strip().lower()
        not in {"0", "false", "no"},
        "allow_substep_agent_launch": os.environ.get(
            "METDSL_ALLOW_SUBSTEP_AGENT_LAUNCH", "1"
        ).strip().lower()
        not in {"0", "false", "no"},
    }
    return {
        "checked_at": _utc_now_iso(),
        "backend": backend_token,
        "probe_command": command,
        "agent_version": agent_version,
        "feature_states": features,
        "checks": checks,
        "sandbox_runtime": "bwrap",
        "sandbox_enforced": sandbox_enforced,
        "hooks_enabled": (features.get("hooks") is True) if backend_token == "codex" else None,
        "can_launch_step_agents": can_launch_agents,
        "can_launch_substep_agents": can_launch_agents,
        "session_policy": session_policy,
        "session_policy_launchable": (
            bool(session_policy["allow_step_agent_launch"])
            and bool(session_policy["allow_substep_agent_launch"])
        ),
        "status": "pass" if can_launch_agents else "fail",
    }


def probe_codex_cli(
    codex_command: str = "codex",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    return probe_execution_platform(
        backend="codex",
        agent_command=codex_command,
        runner=runner,
    )


def init_orchestration(
    repo_root: Path,
    orchestration_id: str,
    *,
    spec_ref: str | None = None,
    source_dependency_ref: str | None = None,
    status: str = "running",
    agent_backend: str = "codex",
    agent_model: str | None = None,
) -> dict[str, Any]:
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    (repo_root / "workspace" / "tmp").mkdir(parents=True, exist_ok=True)
    (root / "launches").mkdir(parents=True, exist_ok=True)
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "steps").mkdir(parents=True, exist_ok=True)
    _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
    init_phase_state_json(repo_root, orchestration_id, reason="init_orchestration")

    meta = {
        "orchestration_id": orchestration_id,
        "status": status,
        "started_at": _utc_now_iso(),
    }
    if spec_ref:
        meta["spec_ref"] = spec_ref
    if source_dependency_ref:
        meta["source_dependency_ref"] = source_dependency_ref
    meta_path = root / "orchestration_meta.json"
    orchestration_agent_run_id: str | None = None
    existing: dict[str, Any] | None = None
    if meta_path.is_file():
        try:
            existing = _read_json(meta_path)
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            for key in ("parallel_nodes_explicit", "parallel_nodes_policy"):
                if key in existing:
                    meta.setdefault(key, existing[key])
            existing_run_id = existing.get("orchestration_agent_run_id")
            if isinstance(existing_run_id, str) and existing_run_id.strip():
                orchestration_agent_run_id = existing_run_id.strip()
    if not orchestration_agent_run_id:
        orchestration_agent_run_id = str(uuid.uuid4())
    backend_token = str(agent_backend).strip().lower()
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"agent_backend must be one of {sorted(SUPPORTED_BACKENDS)}; got {agent_backend!r}"
        )
    meta["orchestration_agent_run_id"] = orchestration_agent_run_id
    _write_json(meta_path, meta)
    # Operator token: written once at init to ~/.met-dsl/operator_tokens/<oid>.txt
    # (mode 0o600), never overwritten on resume so the same token remains valid
    # across restarts.  Stored OUTSIDE workspace/ so the orchestration agent's
    # allowed_read_roots (which include workspace/) cannot reach it via the Read
    # tool, and forbid_operator_secret_direct_read blocks `cat ~/.met-dsl/...` via
    # Bash.  dismiss-violation requires this token to prevent agents from calling
    # the function programmatically (e.g. from a tmp Python script) to self-approve
    # their own unauthorized_write_violations.
    operator_token_path = Path.home() / ".met-dsl" / "operator_tokens" / f"{orchestration_id}.txt"
    operator_token_path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict the directory to the owner so other local users on a shared host
    # cannot enumerate or read operator tokens.
    try:
        operator_token_path.parent.chmod(0o700)
    except OSError:
        pass
    # Create the token atomically with mode 0o600.  Keep a VALID existing token
    # unchanged (so the same token survives across resume), but REPAIR a 0-byte
    # or whitespace-only file left by a crash mid-write — otherwise that broken
    # token is permanent and (combined with the dismiss_violation guard) would
    # be a self-approval hole.  temp-file + os.replace makes the write atomic:
    # the file is either absent or fully populated, never a 0-byte window, and
    # mode 0o600 avoids the umask-default (0o644) world-readable window that
    # write_text()-then-chmod() would leave.
    _existing_token = ""
    if operator_token_path.exists():
        try:
            _existing_token = operator_token_path.read_text(encoding="utf-8").strip()
        except OSError:
            _existing_token = ""
    if not _existing_token:
        _tok_fd, _tok_tmp = tempfile.mkstemp(
            dir=str(operator_token_path.parent), prefix=".operator_token."
        )
        try:
            os.fchmod(_tok_fd, 0o600)
            os.write(_tok_fd, str(uuid.uuid4()).encode("utf-8"))
            os.close(_tok_fd)
            os.replace(_tok_tmp, operator_token_path)
        except OSError:
            # Do not leave a .operator_token.* temp file (holding an unused UUID)
            # littering the dir on failure between mkstemp and replace.
            try:
                os.close(_tok_fd)
            except OSError:
                pass
            try:
                os.unlink(_tok_tmp)
            except OSError:
                pass
            raise
    _write_read_access_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=orchestration_agent_run_id,
        allowed_read_roots=["docs/", "spec/", "skills/", "workspace/"],
        denied_read_roots=["tools/"],
    )
    _write_allowed_output_manifest(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=orchestration_agent_run_id,
        allowed_output_paths=[
            f"workspace/orchestrations/{orchestration_id}/failure_analysis.json",
        ],
        allowed_file_tool_paths=[
            f"workspace/orchestrations/{orchestration_id}/failure_analysis.json",
        ],
        agent_role="orchestration",
        allowed_tmp_root=f"workspace/tmp/{orchestration_agent_run_id}",
    )
    (repo_root / "workspace" / "tmp" / orchestration_agent_run_id).mkdir(parents=True, exist_ok=True)

    graph_path = root / "agent_graph.json"
    if not graph_path.exists():
        _write_json(graph_path, {"edges": []})

    runs_path = root / "agent_runs.jsonl"
    if not runs_path.exists():
        runs_path.write_text("", encoding="utf-8")
    # H-NEW-1: serialize the read+append against any concurrent
    # record_agent_run finalizer. Without this lock, init_orchestration's
    # plain `open("a")` could interleave bytes with a finalizer's locked
    # append, defeating Adv-24 serialization. The lock protects both the
    # idempotency scan (read existing entries) and the running-entry
    # append.
    with _runs_jsonl_exclusive_lock(repo_root, orchestration_id):
        has_orchestration_running_entry = False
        with runs_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                if (
                    str(item.get("agent_run_id", "")).strip() == orchestration_agent_run_id
                    and str(item.get("agent_role", "")).strip() == "orchestration"
                    and str(item.get("status", "")).strip() == "running"
                ):
                    has_orchestration_running_entry = True
                    break
        if not has_orchestration_running_entry:
            orchestration_row: dict[str, Any] = {
                "agent_run_id": orchestration_agent_run_id,
                "agent_role": "orchestration",
                "agent_backend": backend_token,
                "status": "running",
                "started_at": _utc_now_iso(),
            }
            # Record the orchestration agent's own model when supplied so the
            # top-level row is not a blind spot for cost attribution / repro.
            # Children record agent_model at record-launch; the orchestration row
            # has no launch, so it is threaded in here (legacy/omitted rows are
            # backfilled by repair-agent-runs from sibling_uniform).
            if isinstance(agent_model, str) and agent_model.strip():
                orchestration_row["agent_model"] = agent_model.strip()
            with runs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(orchestration_row, ensure_ascii=False) + "\n")
    _append_session_run_index_entry(
        repo_root,
        orchestration_id,
        agent_run_id=orchestration_agent_run_id,
        agent_session_id=orchestration_agent_run_id,
        context_id=orchestration_agent_run_id,
        agent_role="orchestration",
        status="running",
    )

    pre_orchestration_start(repo_root, orchestration_id, event="init")
    _write_run_write_baseline(repo_root, orchestration_id)
    return meta


def write_preflight(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
    *,
    host_session_id: str | None = None,
) -> dict[str, Any]:
    _validate_preflight_payload(payload)
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    pre_orchestration_start(repo_root, orchestration_id, event="preflight")

    stored = dict(payload)
    if not isinstance(stored.get("session_policy"), dict):
        allow_step = stored.get("can_launch_step_agents") is True
        allow_substep = stored.get("can_launch_substep_agents") is True
        stored["session_policy"] = {
            "allow_step_agent_launch": allow_step,
            "allow_substep_agent_launch": allow_substep,
        }
    if not isinstance(stored.get("session_policy_launchable"), bool):
        policy = stored.get("session_policy")
        allow_step = bool(policy.get("allow_step_agent_launch")) if isinstance(policy, dict) else True
        allow_substep = (
            bool(policy.get("allow_substep_agent_launch")) if isinstance(policy, dict) else True
        )
        stored["session_policy_launchable"] = allow_step and allow_substep
    if "probed_at" not in stored:
        stored["probed_at"] = stored.get("checked_at") or _utc_now_iso()

    _write_json(root / "preflight.json", stored)
    meta_path = root / "orchestration_meta.json"
    if meta_path.exists():
        # Codex round 11 F1: the orchestration_meta.json read-check-write region
        # here mutates the same file mark_dependency_readiness() guards under
        # _orchestration_meta_exclusive_lock. Without acquiring the same lock,
        # a concurrent mark-dependency-readiness call can have its verified
        # `true` flags overwritten by this preflight pass with a stale snapshot.
        with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
            meta = _read_json(meta_path)
            if isinstance(meta, dict):
                # Codex round 6 F1 fix: tie dependency_readiness to a fingerprint of
                # (spec_ref + deps.yaml bytes). When that fingerprint changes —
                # e.g. spec_ref repointed from a leaf to a non-leaf, or deps.yaml
                # edited — any previously persisted "true" flags refer to a stale
                # dependency set and MUST be invalidated. Branches:
                #
                #   - existing missing            → initialize from computed.
                #   - fingerprint mismatch        → reset to computed (the new
                #                                    canonical initial state, which
                #                                    is fail-closed for non-leaf
                #                                    and trivial-true for leaf).
                #   - fingerprint match + leaf    → idempotent refresh from computed.
                #   - fingerprint match + non-leaf→ preserve CLI-verified flags.
                existing = meta.get("dependency_readiness")
                # Codex round 33 F1: when PyYAML is unavailable we cannot
                # recompute the full fingerprint that a previously-verified
                # non-leaf record was built with. Synthesizing a byte-only
                # fallback (round 32) caused a guaranteed fingerprint
                # MISMATCH and overwrote verified state with fail-closed —
                # a transient packaging issue on the controller erased
                # every non-leaf orchestration's verified state. Skip the
                # invalidation block entirely on PyYAML outage:
                #   - existing verified record → preserved (degraded mode)
                #   - no existing record → write minimal fail-closed inline
                #     so the gate refuses launches until PyYAML is restored.
                try:
                    computed = _compute_initial_dependency_readiness(
                        repo_root, meta.get("spec_ref")
                    )
                except SpecCatalogCorruption:
                    # Codex round 34 F2: catalog corruption / missing.
                    # Same treatment as PyYAML outage: preserve existing
                    # verified state, or write fail-closed if no prior
                    # record. mark-dependency-readiness will surface the
                    # specific reason loudly when next invoked.
                    if isinstance(existing, dict):
                        _append_phase_state_log(
                            repo_root, orchestration_id,
                            {
                                "ts": _utc_now_iso(),
                                "event": "dependency_readiness_preserved_spec_catalog_unavailable",
                                "reason": "spec_catalog_corrupt_at_preflight",
                            },
                        )
                        computed = None  # type: ignore[assignment]
                    else:
                        meta["dependency_readiness"] = {
                            "direct_dependency_compile_readiness": False,
                            "direct_dependency_execution_readiness": False,
                            "detail": {
                                "ir_ref_verified": False,
                                "pipeline_ref_verified": False,
                                "aggregate_verdict_verified": False,
                            },
                        }
                        _write_json(meta_path, meta)
                        _append_phase_state_log(
                            repo_root, orchestration_id,
                            {
                                "ts": _utc_now_iso(),
                                "event": "dependency_readiness_degraded_init",
                                "reason": "spec_catalog_corrupt_at_preflight",
                            },
                        )
                        computed = None  # type: ignore[assignment]
                except RuntimeError as exc:
                    if "PyYAML" not in str(exc):
                        raise
                    if isinstance(existing, dict):
                        # Preserve existing readiness; record degraded mode
                        # in the phase state log so operators can correlate.
                        _append_phase_state_log(
                            repo_root, orchestration_id,
                            {
                                "ts": _utc_now_iso(),
                                "event": "dependency_readiness_preserved_pyyaml_unavailable",
                                "reason": "pyyaml_unavailable_at_preflight",
                            },
                        )
                        computed = None  # type: ignore[assignment]
                    else:
                        # No prior record — write a minimal fail-closed
                        # payload (no fingerprint we can compute, no
                        # certified_deps) so the gate refuses launch.
                        meta["dependency_readiness"] = {
                            "direct_dependency_compile_readiness": False,
                            "direct_dependency_execution_readiness": False,
                            "detail": {
                                "ir_ref_verified": False,
                                "pipeline_ref_verified": False,
                                "aggregate_verdict_verified": False,
                            },
                        }
                        _write_json(meta_path, meta)
                        _append_phase_state_log(
                            repo_root, orchestration_id,
                            {
                                "ts": _utc_now_iso(),
                                "event": "dependency_readiness_degraded_init",
                                "reason": "pyyaml_unavailable_at_preflight",
                            },
                        )
                        computed = None  # type: ignore[assignment]
                if computed is None:
                    # PyYAML-degraded mode handled above; skip the rest of
                    # the invalidation/refresh logic.
                    pass
                else:
                    is_leaf = computed.get("direct_dependency_compile_readiness") is True
                    current_fp = computed.get("dep_set_fingerprint")
                    existing_fp = existing.get("dep_set_fingerprint") if isinstance(existing, dict) else None
                    if not isinstance(existing, dict):
                        meta["dependency_readiness"] = computed
                        _write_json(meta_path, meta)
                    elif existing_fp != current_fp:
                        meta["dependency_readiness"] = computed
                        _write_json(meta_path, meta)
                        _append_phase_state_log(
                            repo_root, orchestration_id,
                            {
                                "ts": _utc_now_iso(),
                                "event": "dependency_readiness_invalidated",
                                "reason": "dep_set_fingerprint_mismatch",
                                "previous_fingerprint": existing_fp,
                                "new_fingerprint": current_fp,
                            },
                        )
                    elif is_leaf and existing != computed:
                        meta["dependency_readiness"] = computed
                        _write_json(meta_path, meta)
    if _preflight_allows_agent_launch(stored):
        # Record the host session id ONLY when preflight is launchable — i.e. when the
        # caller will actually start the backend session. Recording it at init (before
        # preflight) would leave a failed-preflight orchestration, or a resume whose
        # preflight then fails, pointing meta.host_session_id at a session that never
        # launched (Codex review). orchestration_meta.json is runtime-owned and exempt
        # from the agent write-baseline check (_should_ignore_runtime_snapshot_path),
        # so this post-init meta write is safe.
        if isinstance(host_session_id, str) and host_session_id.strip():
            with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
                hs_meta = _read_json(meta_path)
                if isinstance(hs_meta, dict):
                    hs_meta["host_session_id"] = host_session_id.strip()
                    _write_json(meta_path, hs_meta)
        _transition_phase_state(
            repo_root,
            orchestration_id,
            new_state="preflight_passed",
            event="preflight_written",
        )
    else:
        _ensure_orchestration_audit_dirs(repo_root, orchestration_id)
        _append_phase_state_log(
            repo_root,
            orchestration_id,
            {
                "ts": _utc_now_iso(),
                "event": "preflight_written_not_launchable",
                "from": None,
                "to": None,
            },
        )
    return stored


def _load_graph(graph_path: Path) -> dict[str, Any]:
    if graph_path.exists():
        graph = _read_json(graph_path)
        if isinstance(graph, dict) and isinstance(graph.get("edges"), list):
            return graph
    return {"edges": []}


def record_launch(
    repo_root: Path,
    orchestration_id: str,
    *,
    parent_agent_run_id: str,
    child_agent_run_id: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    relation_type: str = "launch",
) -> dict[str, Any]:
    if not isinstance(parent_agent_run_id, str) or not parent_agent_run_id.strip():
        raise ValueError("record-launch requires non-empty parent_agent_run_id")
    parent_agent_run_id = parent_agent_run_id.strip()
    if not _AGENT_RUN_ID_RE.match(parent_agent_run_id):
        raise ValueError(
            f"record-launch: parent_agent_run_id contains invalid characters "
            f"(got {parent_agent_run_id!r}); only alphanumerics, hyphens, and underscores are allowed"
        )
    if not isinstance(child_agent_run_id, str) or not child_agent_run_id.strip():
        raise ValueError("record-launch requires non-empty child_agent_run_id")
    child_agent_run_id = child_agent_run_id.strip()
    if not _AGENT_RUN_ID_RE.match(child_agent_run_id):
        raise ValueError(
            f"record-launch: child_agent_run_id contains invalid characters "
            f"(got {child_agent_run_id!r}); only alphanumerics, hyphens, and underscores are allowed"
        )
    preflight_payload: dict[str, Any] | None = None
    try:
        preflight_payload = _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=True,
        )
    except RuntimeError:
        # Launch gate failure must terminate orchestration immediately.
        try:
            update_orchestration_status(
                repo_root,
                orchestration_id,
                status="fail",
            )
        except Exception:
            pass
        raise
    preflight_backend = (
        str(preflight_payload.get("backend", "")).strip().lower()
        if isinstance(preflight_payload, dict)
        else ""
    )
    backend_token = preflight_backend if preflight_backend in SUPPORTED_BACKENDS else "codex"

    # The Claude backend enforces sequential child launch via the active file.
    if backend_token == "claude":
        active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
        if active_path.exists():
            existing_id = active_path.read_text(encoding="utf-8").strip()
            try:
                update_orchestration_status(
                    repo_root,
                    orchestration_id,
                    status="fail_closed",
                    reason_code="parallel_nodes_not_explicitly_allowed",
                )
            except Exception:
                pass
            raise RuntimeError(
                "Claude backend sequential violation: "
                f"active child agent {existing_id!r} is still running. "
                "Parallel child agent launch is not permitted on Claude backend."
            )

    step_raw = request_payload.get("step")
    node_key_raw = request_payload.get("node_key")
    # Recurrence guard: Build is the only step-only phase (one child == one
    # step_result). Launching the next build while a prior terminal build agent
    # still lacks a step_result means that result was silently skipped — the exact
    # original gap that later deadlocked completion. Fail-closed and require the
    # missing step_result be written first. The condition is checked against the
    # actual step_result files (not the `child_finished` phase-state proxy): a
    # crash between write-step-result writing the file and advancing the phase can
    # leave a stale `child_finished` even though the result is present, and that
    # must NOT wedge recovery (both write paths refuse to overwrite an existing
    # result). Substep phases (compile/generate/validate) legitimately revisit
    # `child_finished` between substeps, so this stays scoped to build. This runs
    # BEFORE any durable launch/session/graph mutation, so a blocked relaunch
    # leaves no dangling agent_graph edge or session-index row.
    if (
        isinstance(step_raw, str)
        and step_raw.strip().lower() == "build"
        and isinstance(node_key_raw, str)
        and node_key_raw.strip()
    ):
        missing_build_step_results = _build_step_agents_missing_step_result(
            repo_root, orchestration_id, node_key=node_key_raw.strip()
        )
        if missing_build_step_results:
            raise RuntimeError(
                f"record-launch: prior build agent(s) for {node_key_raw.strip()}/build finished "
                f"without a step_result ({', '.join(sorted(missing_build_step_results))}); write it "
                "with `write-step-result` (or `write-step-result --backfill`) before launching another build"
            )
    if isinstance(step_raw, str) and step_raw.strip() and isinstance(node_key_raw, str) and node_key_raw.strip():
        required = _required_child_agent_kind(step_raw)
        launch_ctx = dict(request_payload)
        check = pre_phase_launch(
            repo_root,
            orchestration_id=orchestration_id,
            node_key=node_key_raw.strip(),
            step=step_raw.strip(),
            backend=backend_token,
            require_child_agent=required,
            launch_request=launch_ctx,
        )
        if check.get("status") == "fail_closed":
            reason_code = str(check.get("reason_code") or "child_agent_unavailable_on_execution_platform")
            try:
                update_orchestration_status(
                    repo_root,
                    orchestration_id,
                    status="fail_closed",
                    reason_code=reason_code,
                    reason_detail=str(check.get("reason_detail") or ""),
                    blocking_policy_scope=str(check.get("blocking_policy_scope") or ""),
                )
            except Exception:
                pass
            raise RuntimeError(
                "record-launch blocked by pre_phase_launch / workflow-launch-check: "
                f"reason_code={reason_code}"
            )
    backend_command = "codex"
    if isinstance(preflight_payload, dict):
        probe_command = preflight_payload.get("probe_command")
        if isinstance(probe_command, str) and probe_command.strip():
            backend_command = probe_command.strip()
    root = _orchestration_root(repo_root, orchestration_id)
    launches_root = root / "launches"
    launches_root.mkdir(parents=True, exist_ok=True)
    child_dialog_root = root / "agents" / child_agent_run_id / "dialogs"
    child_dialog_root.mkdir(parents=True, exist_ok=True)

    request_payload = dict(request_payload)
    request_payload.setdefault("orchestration_id", orchestration_id)
    request_payload.setdefault("agent_run_id", child_agent_run_id)
    request_payload.setdefault("parent_agent_run_id", parent_agent_run_id)
    request_payload = prepare_launch_request_payload(request_payload)
    response_payload = dict(response_payload)
    response_agent_session_id = _validate_response_agent_session_id(response_payload)
    response_payload.setdefault("agent_session_id", response_agent_session_id)
    launch_role_obj = request_payload.get("agent_role")
    if isinstance(launch_role_obj, str) and launch_role_obj.strip():
        launch_role = launch_role_obj.strip().lower()
    else:
        try:
            launch_role = _required_child_agent_kind(str(request_payload.get("step", "") or ""))
        except ValueError:
            launch_role = "unknown"
    context_obj = request_payload.get("context_id")
    launch_context_id = context_obj.strip() if isinstance(context_obj, str) and context_obj.strip() else None

    _validate_launch_request_payload(request_payload)
    _append_session_run_index_entry(
        repo_root,
        orchestration_id,
        agent_run_id=child_agent_run_id,
        agent_session_id=response_agent_session_id,
        context_id=launch_context_id,
        agent_role=launch_role,
        status="running",
    )

    prompt_text = _extract_launch_prompt_text(request_payload)
    reply_text = _extract_launch_reply_text(response_payload)
    if not prompt_text.strip():
        raise ValueError("launch prompt text must be non-empty")
    if not reply_text.strip():
        raise ValueError("launch reply text must be non-empty")
    _validate_launch_prompt_text(request_payload, prompt_text)
    _append_workflow_hook_log(
        repo_root,
        orchestration_id,
        hook_name="pre_agent_launch",
        status="allow",
        detail={"child_agent_run_id": child_agent_run_id},
    )

    request_ref, response_ref = _launch_refs(orchestration_id, child_agent_run_id)
    prompt_ref, reply_ref = _launch_dialog_refs(orchestration_id, child_agent_run_id)
    child_request_ref, child_response_ref = _child_launch_refs(orchestration_id, child_agent_run_id)
    child_prompt_ref, child_reply_ref = _child_dialog_refs(orchestration_id, child_agent_run_id)
    request_payload.setdefault("launch_prompt_ref", prompt_ref)
    request_payload.setdefault("child_launch_request_ref", child_request_ref)
    request_payload.setdefault("child_launch_prompt_ref", child_prompt_ref)
    response_payload.setdefault("launch_reply_ref", reply_ref)
    response_payload.setdefault("child_launch_response_ref", child_response_ref)
    response_payload.setdefault("child_launch_reply_ref", child_reply_ref)

    request_path = launches_root / f"{child_agent_run_id}.request.json"
    response_path = launches_root / f"{child_agent_run_id}.response.json"
    prompt_path = launches_root / f"{child_agent_run_id}.prompt.txt"
    reply_path = launches_root / f"{child_agent_run_id}.reply.txt"
    child_request_path = child_dialog_root / "child.request.json"
    child_response_path = child_dialog_root / "child.response.json"
    child_prompt_path = child_dialog_root / "child.prompt.txt"
    child_reply_path = child_dialog_root / "child.reply.txt"

    graph_path = root / "agent_graph.json"
    graph = _load_graph(graph_path)
    edge = {
        "parent_agent_run_id": parent_agent_run_id,
        "child_agent_run_id": child_agent_run_id,
        "relation_type": relation_type,
    }
    if edge not in graph["edges"]:
        graph["edges"].append(edge)
    _write_json(graph_path, graph)

    nk = request_payload.get("node_key")
    st = request_payload.get("step")
    out_refs: dict[str, Any] = {
        "launch_request_ref": request_ref,
        "launch_response_ref": response_ref,
        "launch_prompt_ref": prompt_ref,
        "launch_reply_ref": reply_ref,
        "child_launch_request_ref": child_request_ref,
        "child_launch_response_ref": child_response_ref,
        "child_launch_prompt_ref": child_prompt_ref,
        "child_launch_reply_ref": child_reply_ref,
        # The exact prompt text record-launch rendered and wrote to
        # launches/<child_arid>.prompt.txt. Returned so the orchestration agent
        # can pass it verbatim to the Agent tool WITHOUT reading the template
        # (launch_prompts.md) or the written prompt file (both blocked for the
        # orchestration). The Agent-tool prompt is then identical in content to
        # the recorded artifact by construction (audit 1-to-1); the .prompt.txt
        # file only differs by a trailing newline the text writer appends.
        # Retained in terse output.
        "launch_prompt_text": prompt_text,
    }
    if not (isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip()):
        raise ValueError("record-launch requires non-empty node_key and step for sandbox-enforced launch")
    if isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip():
        _write_access_policy_for_launch(
            repo_root,
            orchestration_id,
            child_agent_run_id,
            request_payload,
        )
        policy_doc = _read_json(
            _access_policies_dir(repo_root, orchestration_id) / f"{child_agent_run_id}.json"
        )
        if not isinstance(policy_doc, dict):
            raise ValueError("access policy must be object for read manifest generation")
        allowed_read_roots_obj = policy_doc.get("allowed_read_roots")
        denied_read_roots_obj = policy_doc.get("denied_read_roots")
        allowed_read_roots = (
            [str(item) for item in allowed_read_roots_obj]
            if isinstance(allowed_read_roots_obj, list)
            else []
        )
        denied_read_roots = (
            [str(item) for item in denied_read_roots_obj]
            if isinstance(denied_read_roots_obj, list)
            else []
        )
        read_manifest_ref = _write_read_access_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=child_agent_run_id,
            allowed_read_roots=allowed_read_roots,
            denied_read_roots=denied_read_roots,
        )
        out_refs["read_access_manifest_ref"] = read_manifest_ref
        cap_doc = _write_capability_for_launch(
            repo_root,
            orchestration_id,
            child_agent_run_id,
            request_payload,
        )
        cap_rel = f"workspace/orchestrations/{orchestration_id}/capabilities/{child_agent_run_id}.json"
        out_refs["capability_ref"] = cap_rel
        out_refs["capability_token"] = cap_doc.get("capability_token", "")
        write_roots_obj = cap_doc.get("write_roots")
        write_roots = [str(item) for item in write_roots_obj] if isinstance(write_roots_obj, list) else []
        # Resolve toolchain.build_system from spec.ir.yaml.impl_defaults so the
        # canonical-placement helper can gate cross-phase auto-inject on
        # `build_system=make` (the documented Make-only exception).
        _ir_ref_for_bs = str(request_payload.get("ir_ref") or "").strip()
        if _ir_ref_for_bs:
            _bs_resolved = _impl_resolved_build_system(repo_root, _ir_ref_for_bs)
            if isinstance(_bs_resolved, str) and _bs_resolved.strip():
                request_payload = dict(request_payload)
                request_payload["_resolved_build_system"] = _bs_resolved.strip().lower()
        allowed_output_paths = _allowed_output_paths_for_launch(
            request_payload=request_payload,
            write_roots=write_roots,
        )
        allowed_file_tool_paths = _allowed_file_tool_paths_for_launch(
            request_payload=request_payload,
            allowed_output_paths=allowed_output_paths,
        )
        _validate_child_write_contract_preflight(
            request_payload=request_payload,
            capability_doc=cap_doc,
            allowed_output_paths=allowed_output_paths,
        )
        (repo_root / "workspace" / "tmp" / child_agent_run_id).mkdir(parents=True, exist_ok=True)
        # Execute step lineage bind (mandatory for ALL execute launches, not
        # only when cross-phase quality_check log is authorized): every
        # execute run must declare `source_build_id` in the launch request,
        # and the referenced build's `binary_meta.json` must record
        # `source_source_id` matching the request's `source_id`.
        # Without this binding, an execute could run binaries from build A
        # while attributing evidence (e.g. trial_meta) to a different
        # sibling build's generation — a mixed-build forge that purely
        # in-phase logging would not catch elsewhere.
        _step_token_for_bind = str(request_payload.get("step") or "").strip().lower()
        _pipe_ref_for_bind = _normalize_rel_posix(
            str(request_payload.get("pipeline_ref") or "")
        )
        _substep_token_for_bind = str(request_payload.get("substep") or "").strip().lower()
        if (
            _step_token_for_bind == "validate"
            and _substep_token_for_bind == "execute"
            and _pipe_ref_for_bind
        ):
            _gen_id_for_bind = str(
                request_payload.get("source_id") or ""
            ).strip()
            _source_binary_id = str(
                request_payload.get("source_binary_id") or ""
            ).strip()
            if not _gen_id_for_bind:
                raise ValueError(
                    "validate.execute launch requires `source_id` in the launch "
                    "request to bind provenance to a specific source."
                )
            if not _source_binary_id:
                raise ValueError(
                    "validate.execute launch requires `source_binary_id` in the launch "
                    "request to bind provenance to a specific build. "
                    "Without this binding, evidence could be forged across "
                    "sibling builds even when in-phase logging is used."
                )
            _bm_path = (
                repo_root / _pipe_ref_for_bind / "binary" / _source_binary_id / "binary_meta.json"
            )
            if not _bm_path.is_file():
                raise ValueError(
                    f"validate.execute launch source_binary_id={_source_binary_id!r} "
                    f"does not resolve to an existing binary_meta.json at "
                    f"{_bm_path!s}. The referenced build must exist on disk "
                    "before validate.execute can attribute provenance to it."
                )
            try:
                _bm_doc = _read_json(_bm_path)
            except (OSError, json.JSONDecodeError):
                _bm_doc = None
            if not isinstance(_bm_doc, dict):
                raise ValueError(
                    f"validate.execute launch source_binary_id={_source_binary_id!r}: "
                    "binary_meta.json could not be parsed as a JSON object."
                )
            _bm_src_gen = _bm_doc.get("source_source_id")
            if (
                not isinstance(_bm_src_gen, str)
                or not _bm_src_gen.strip()
            ):
                raise ValueError(
                    f"validate.execute launch source_binary_id={_source_binary_id!r}: "
                    "binary_meta.json must record `source_source_id` to "
                    "bind validate.execute provenance. Migrate the build's metadata "
                    "before launching validate.execute against it."
                )
            if _bm_src_gen.strip() != _gen_id_for_bind:
                raise ValueError(
                    f"validate.execute launch source_id={_gen_id_for_bind!r} "
                    f"does not match build {_source_binary_id!r}'s "
                    f"source_source_id={_bm_src_gen.strip()!r}. Validate.execute "
                    "must run against the binary produced by the source "
                    "it claims provenance for."
                )
        canonical_audit_logs = _canonical_mcp_audit_log_paths_for_request(
            request_payload, allowed_output_paths, repo_root=repo_root
        )
        # Validate cross-phase canonical placements:
        #   - Execute → generate/<gen>/ for run_quality_checks
        #   - Build → generate/<gen>/ for in-source compile_project
        #     (Make for Fortran/C-family runs project_dir=<gen>/src/)
        # The `source_id` from the request payload is otherwise free-form
        # and could authorize writes to an unrelated generation's audit log
        # under the same pipeline. Require the referenced generate run to
        # actually exist on disk (source_meta.json must be present) and to
        # have reached pass state before granting cross-phase write authority.
        _step_token_xpv = str(request_payload.get("step") or "").strip().lower()
        _pipe_ref_xpv = _normalize_rel_posix(
            str(request_payload.get("pipeline_ref") or "")
        )
        _substep_xpv = str(request_payload.get("substep") or "").strip().lower()
        _xpv_is_validate_execute = (
            _step_token_xpv == "validate" and _substep_xpv == "execute"
        )
        if (_xpv_is_validate_execute or _step_token_xpv == "build") and _pipe_ref_xpv:
            _gen_prefix_xpv = f"{_pipe_ref_xpv}/source/"
            for _log_path in canonical_audit_logs:
                if not _log_path.startswith(_gen_prefix_xpv):
                    continue
                _tail_xpv = _log_path[len(_gen_prefix_xpv):]
                _parts_xpv = [p for p in _tail_xpv.split("/") if p]
                if not _parts_xpv:
                    continue
                _gen_id_xpv = _parts_xpv[0]
                _gen_meta = (
                    repo_root
                    / _gen_prefix_xpv
                    / _gen_id_xpv
                    / "source_meta.json"
                )
                if not _gen_meta.exists():
                    raise ValueError(
                        f"{_step_token_xpv} launch references unknown "
                        f"cross-phase source_id={_gen_id_xpv!r}: "
                        f"source_meta.json not found at {_gen_meta!s}. "
                        "Cross-phase MCP audit log authorization requires the "
                        "referenced generation to have actually run."
                    )
                # Verify the generation reached pass state BEFORE granting
                # write authority into its tree. Authorizing writes against a
                # failed/stale generation would let an Execute run mutate or
                # append to provenance files that later validators trust,
                # contaminating cross-phase artifacts irreversibly.
                try:
                    _gen_meta_doc = _read_json(_gen_meta)
                except (OSError, json.JSONDecodeError):
                    _gen_meta_doc = None
                _gen_status_raw = (
                    _gen_meta_doc.get("verification_status")
                    if isinstance(_gen_meta_doc, dict)
                    else None
                )
                _gen_status = (
                    _gen_status_raw.strip().lower()
                    if isinstance(_gen_status_raw, str)
                    else None
                )
                if _gen_status != "pass":
                    raise ValueError(
                        f"{_step_token_xpv} launch references cross-phase "
                        f"source_id={_gen_id_xpv!r} with "
                        f"verification_status={_gen_status!r} (expected "
                        "'pass'). Cannot grant MCP-owned write authority to "
                        "a failed/stale generation tree; this would "
                        "contaminate provenance files trusted by later "
                        "validators."
                    )
                # NOTE: execute step source_build_id / source_id lineage
                # bind is enforced unconditionally above (see "Execute step
                # lineage bind" block before canonical_audit_logs). The
                # cross-phase loop here only handles existence + pass-state
                # of the generation referenced from the cross-phase audit
                # log path (build step's Make-only path).
        manifest_ref = _write_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=child_agent_run_id,
            allowed_output_paths=allowed_output_paths,
            allowed_file_tool_paths=allowed_file_tool_paths,
            allowed_tmp_root=f"workspace/tmp/{child_agent_run_id}",
            mcp_owned_audit_logs=canonical_audit_logs,
        )
        out_refs["allowed_output_manifest_ref"] = manifest_ref
        try:
            profile = build_bwrap_profile(
                repo_root=repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=child_agent_run_id,
                backend_command=backend_command,
            )
            command_argv = [backend_command]
            rendered = render_bwrap_command(profile=profile, command_argv=command_argv)
            profile["rendered_command"] = rendered
            profile_path = _sandbox_profiles_dir(
                repo_root,
                orchestration_id,
            ) / f"{child_agent_run_id}.json"
            _write_json(profile_path, profile)
            sandbox_ref = (
                f"workspace/orchestrations/{orchestration_id}/sandbox_profiles/{child_agent_run_id}.json"
            )
            out_refs["sandbox_profile_ref"] = sandbox_ref
            request_payload.setdefault("sandbox_profile_ref", sandbox_ref)
            response_payload.setdefault("sandbox_runtime", "bwrap")
            response_payload.setdefault("sandbox_enforced", True)
            response_payload.setdefault("sandbox_profile_ref", sandbox_ref)
            response_payload.setdefault("sandbox_command", rendered)
        except Exception as exc:
            _write_sandbox_enforcement_violation(
                repo_root,
                orchestration_id,
                agent_run_id=child_agent_run_id,
                reason="sandbox_profile_build_failed",
                detail={"error": str(exc)},
            )
            update_orchestration_status(
                repo_root,
                orchestration_id,
                status="fail_closed",
                reason_code="sandbox_enforcement_violation",
                reason_detail=str(exc),
                blocking_policy_scope="sandbox",
            )
            raise RuntimeError(f"record-launch sandbox enforcement failed: {exc}") from exc
    _write_json(request_path, request_payload)
    _write_json(response_path, response_payload)
    _write_text(prompt_path, prompt_text)
    _write_text(reply_path, reply_text)
    _write_json(child_request_path, request_payload)
    _write_json(child_response_path, response_payload)
    _write_text(child_prompt_path, prompt_text)
    _write_text(child_reply_path, reply_text)
    if isinstance(nk, str) and nk.strip() and isinstance(st, str) and st.strip():
        step_tok = st.strip().lower()
        _transition_node_step_phase_state(
            repo_root,
            orchestration_id,
            node_key=nk.strip(),
            step=step_tok,
            new_state="launch_recorded",
            event="record_launch",
            agent_run_id=child_agent_run_id,
        )
        _transition_node_step_phase_state(
            repo_root,
            orchestration_id,
            node_key=nk.strip(),
            step=step_tok,
            new_state="child_running",
            event="child_launched",
            agent_run_id=child_agent_run_id,
        )
    _write_run_write_baseline(
        repo_root,
        orchestration_id,
        agent_run_id=child_agent_run_id,
    )
    # NEW-M1: write parent_return_token FIRST so it is durably present
    # before the active_children marker (Adv-16) appears. record_child_return
    # checks the marker before the token: if a crash interrupts launch
    # writing, this ordering guarantees we never observe "marker exists but
    # token missing" — the recoverable invariant is "token may exist
    # without marker (launch incomplete)" rather than "marker exists
    # without token (permanent record_child_return failure)".
    # Adv-30: per-arid parent-bound token; record-child-return requires it
    # to construct a valid ack. Stored in launches/<arid>.parent_return_token
    # (parent-only via read manifests). Atomic write (M1/Adv-27) so
    # concurrent readers never observe partial content.
    parent_return_token = secrets.token_hex(32)
    _atomic_write_text(
        _parent_return_token_path(repo_root, orchestration_id, child_agent_run_id),
        parent_return_token,
    )

    # L-NEW-2: route active-child marker writes through _atomic_write_text
    # for consistency with Adv-27. The marker file is queried only for
    # existence (not contents) by record_child_return, but a concurrent
    # reader observing a partial write would still log a confusing
    # half-empty file; atomic writes eliminate that observability gap.
    if backend_token == "claude":
        _atomic_write_text(
            _active_child_agent_run_id_path(repo_root, orchestration_id),
            child_agent_run_id,
        )
    # Adv-16: backend-neutral per-arid active-child marker. Codex lack
    # the single-active-child constraint that the Claude marker enforces, but
    # they still need a "the Agent tool actually returned" handshake before
    # record-timeout may finalize a run. Marker is created here for ALL
    # backends (LAST per NEW-M1 ordering above) and removed by
    # deactivate-child / record-agent-run terminal.
    marker_dir = _active_children_dir(repo_root, orchestration_id)
    marker_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        _active_child_marker_path(repo_root, orchestration_id, child_agent_run_id),
        child_agent_run_id,
    )

    return out_refs


def _agent_runs_writer_active(runs_path: Path) -> bool:
    """Adv-40: probe whether a writer currently holds the agent_runs.jsonl
    fcntl lock. True = lock contended (writer is appending), False = lock
    free (no active writer). Used to distinguish an in-flight concurrent
    append (which legitimately leaves the trailing line truncated) from
    durable crash corruption that masquerades as the same shape.

    On non-POSIX platforms (no fcntl) we conservatively return True so that
    legacy single-process workflows keep tolerating partial reads.
    """
    if _fcntl is None:  # pragma: no cover — non-POSIX
        return True
    lock_path = runs_path.parent / (runs_path.name + ".lock")
    if not lock_path.exists():
        # No lock file means no record_agent_run has ever held the lock here.
        # A truncated tail is therefore not an in-flight append.
        return False
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError:
        # Permission / race; cannot determine — fall back to "active" so we
        # do not raise on a possibly-recoverable in-flight append.
        return True
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            # Contention — writer is active.
            return True
        # We acquired the lock → no writer was holding it. Release.
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_existing_run_ids(path: Path, *, caller_holds_lock: bool = False) -> set[str]:
    """Return the set of agent_run_ids recorded in agent_runs.jsonl.

    Adv-21: writers append without holding a Python-level lock per write
    line, so a reader that lands mid-write can observe a truncated trailing
    line. Tolerate exactly one JSONDecodeError on the LAST non-empty line
    when it is plausibly an in-flight append.
    Adv-40: distinguish in-flight from durable corruption by probing the
    record_agent_run fcntl lock. If no writer is currently holding the
    lock, a malformed trailing line is durable corruption and must surface
    as a controlled RuntimeError rather than be silently swallowed (which
    could let a duplicate terminal entry slip past `record_agent_run`'s
    duplicate-id check).
    NEW-H1: when the caller already holds the fcntl lock (the locked
    re-check inside record_agent_run), the writer-active probe would
    self-contend and falsely return True — masking durable corruption.
    `caller_holds_lock=True` skips the heuristic and treats every
    malformed line as durable corruption, which is the correct behavior
    when no other writer can be active by definition.
    """
    if not path.exists():
        return set()
    run_ids: set[str] = set()
    non_empty_lines = [s for s in (raw.strip() for raw in path.read_text(encoding="utf-8").splitlines()) if s]
    n_lines = len(non_empty_lines)
    for idx, line in enumerate(non_empty_lines):
        is_last = idx == n_lines - 1
        # NEW-H1: when the caller holds the fcntl lock, no other writer can
        # be active — a malformed line is unconditionally durable corruption.
        in_flight = (
            is_last
            and not caller_holds_lock
            and _agent_runs_writer_active(path)
        )
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            if in_flight:
                continue
            raise RuntimeError(
                f"agent_runs.jsonl has malformed JSON at line {idx + 1}: {exc} "
                f"(no active writer detected — durable corruption; quarantine "
                f"the ledger and roll forward explicitly)"
                if is_last else
                f"agent_runs.jsonl has malformed non-trailing JSON at line {idx + 1}: {exc}"
            ) from exc
        if not isinstance(item, dict):
            if in_flight:
                continue
            raise RuntimeError(
                f"agent_runs.jsonl line {idx + 1} is not a JSON object: {item!r}"
            )
        run_id = item.get("agent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            run_ids.add(run_id.strip())
    return run_ids


def _validate_skipped_by_checkpoint_payload(payload: dict[str, Any]) -> None:
    for key in ("node_key", "step", "skipped_step", "reason", "checkpoint_agent_run_id"):
        val = payload.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{key} must be non-empty string for skipped_by_checkpoint")
    status = payload.get("status")
    if not isinstance(status, str) or status.strip().lower() != "skipped":
        raise ValueError("skipped_by_checkpoint requires status=skipped")
    if payload["step"].strip().lower() != payload["skipped_step"].strip().lower():
        raise ValueError("skipped_step must match step for skipped_by_checkpoint")


def record_timeout(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    reason: str,
    force_reason: str | None = None,
) -> dict[str, Any]:
    """Canonical recovery for substep/step API stream idle timeout.

    Normal path: orchestration agent observes Agent tool returning, calls
    record-child-return → deactivate-child → record-timeout. The Adv-14/16/20
    guards refuse to finalize while liveness markers are still present.

    Adv-26 escape hatch: pass `force_reason` (or --force-reason on CLI) to
    bypass the active-children/legacy-marker guards for genuinely wedged
    children where deactivate-child is unreachable (Agent tool process killed
    before the parent observed any return). The bypass clears both markers
    on the operator's responsibility; force_reason is appended to the audit
    trail (timeout_reason) and recorded as `forced=True` in the run payload.

    Without --force-reason, a wedged child is a permanent dead end because
    record-child-return → deactivate-child → record-timeout cannot make
    progress. With it, operators retain a controlled finalization path.
    """
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("record-timeout requires non-empty --agent-run-id")
    arid = agent_run_id.strip()
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("record-timeout requires non-empty --reason")
    reason_text = reason.strip()
    forced = isinstance(force_reason, str) and force_reason.strip() != ""
    forced_reason_text = force_reason.strip() if forced else None

    orch_root = _orchestration_root(repo_root, orchestration_id)
    req_path = orch_root / "launches" / f"{arid}.request.json"
    resp_path = orch_root / "launches" / f"{arid}.response.json"
    if not req_path.exists():
        raise ValueError(
            f"record-timeout cannot find launch request: {req_path}. "
            "Ensure record-launch was executed before the Agent tool call."
        )
    if not resp_path.exists():
        raise ValueError(
            f"record-timeout cannot find launch response: {resp_path}."
        )
    try:
        req_doc = _read_json(req_path)
        resp_doc = _read_json(resp_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"record-timeout failed to read launch artifacts: {exc}") from exc
    if not isinstance(req_doc, dict) or not isinstance(resp_doc, dict):
        raise ValueError("record-timeout: launch request/response must be JSON objects")

    role_token = ""
    role_obj = req_doc.get("agent_role")
    if isinstance(role_obj, str) and role_obj.strip():
        role_token = role_obj.strip().lower()
    if role_token not in {"step", "substep"}:
        raise ValueError(
            f"record-timeout only supports step/substep agents; agent_role={role_token!r}. "
            "Use set-status for orchestration timeouts."
        )

    backend_obj = resp_doc.get("backend")
    backend_token = backend_obj.strip().lower() if isinstance(backend_obj, str) and backend_obj.strip() else ""
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"record-timeout: launch response backend={backend_token!r} not in {sorted(SUPPORTED_BACKENDS)}"
        )

    # Adv-14: liveness/ownership guards before fabricating a terminal record
    # and wiping workspace/tmp/<arid>/. record-timeout cannot directly verify
    # that the child process is dead, but it MUST refuse obvious mis-fires
    # (duplicate timer, delayed callback, mis-routed retry) that would
    # finalize a still-active run and destroy its scratch.
    runs_path = orch_root / "agent_runs.jsonl"
    # H3: this early read is unlocked. record_agent_run runs another check
    # under fcntl.LOCK_EX (Adv-24) so a race between this read and a
    # concurrent in-progress record_agent_run for the same arid is caught
    # there. Distinguish the two cases in the error message:
    #   - "already finalized": the entry was durably present when we read.
    #   - "raced concurrent finalizer": the locked re-check at
    #     record_agent_run will surface this with its own message; this
    #     unlocked path errs on the side of failing fast with the safer
    #     wording that matches the most common cause.
    if runs_path.is_file() and not _agent_runs_writer_active(runs_path):
        # No writer holds the lock at this instant → the existing entry, if
        # any, is durable rather than mid-write. Safe to claim "already
        # finalized" without ambiguity.
        existing_run_ids = _read_existing_run_ids(runs_path)
        if arid in existing_run_ids:
            raise ValueError(
                f"record-timeout: agent_run_id={arid!r} already has a durable "
                f"entry in agent_runs.jsonl (the run was already finalized). "
                f"Refusing to add a duplicate terminal record — investigate "
                f"why the timeout path was re-entered."
            )
    elif runs_path.is_file():
        # Writer active: defer to record_agent_run's locked re-check, which
        # will raise "race detected on locked re-check" with precise wording
        # if and only if the racing writer commits an entry for this arid.
        pass
    # session_run_index status check — the index is updated to the terminal
    # status on successful record-agent-run, so any non-running status here
    # signals that another finalization path already won the race.
    index_doc = _read_session_run_index(repo_root, orchestration_id)
    for _entry in index_doc.get("entries", []) or []:
        if isinstance(_entry, dict) and _entry.get("agent_run_id") == arid:
            _sess_status = _entry.get("status")
            if isinstance(_sess_status, str) and _sess_status.strip().lower() != "running":
                raise ValueError(
                    f"record-timeout: session_run_index for {arid!r} shows "
                    f"status={_sess_status!r}, not 'running'. Refusing — the "
                    f"run has already terminated or was never launched."
                )
            break
    # Adv-16: backend-neutral active-child marker check. record-launch creates
    # `active_children/<arid>.txt` for ALL backends; deactivate-child (the
    # documented signal that the Agent tool returned) removes it. While the
    # marker exists, the orchestration agent has not yet acknowledged that the
    # child finished — firing record-timeout in that state would race the
    # still-pending leaf return on Codex as well as Claude.
    # Adv-26: forced=True skips this guard for genuinely-wedged children
    # (operator override; force_reason is recorded in the audit trail).
    # Adv-37: forced bypass NO LONGER unlinks markers up-front. If
    # record_agent_run() below fails (validation, locked re-check, etc.)
    # the run must remain protected by its existing markers so a retry is
    # possible. Marker removal happens AFTER record_agent_run succeeds.
    marker_path = _active_child_marker_path(repo_root, orchestration_id, arid)
    forced_marker_to_remove: list[Path] = []
    if marker_path.is_file():
        if not forced:
            raise ValueError(
                f"record-timeout: active-child marker {marker_path.name} still exists "
                f"under workspace/orchestrations/{orchestration_id}/active_children/. "
                f"Run `deactivate-child --child-run-id {arid}` first to confirm the "
                f"Agent tool actually returned, then retry record-timeout. "
                f"For genuinely-wedged children where deactivate-child is "
                f"unreachable, use --force-reason '<text>' to bypass."
            )
        # Forced bypass: queue the marker for removal AFTER record_agent_run
        # commits the durable terminal entry.
        forced_marker_to_remove.append(marker_path)
    # Defensive: also check the legacy Claude single-file marker for backward
    # compat (record-launch keeps writing it for sequential-child enforcement).
    if backend_token == "claude":
        active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
        if active_path.is_file():
            try:
                active_arid = active_path.read_text(encoding="utf-8").strip()
            except OSError:
                active_arid = ""
            if active_arid == arid:
                if not forced:
                    raise ValueError(
                        f"record-timeout: active_child_agent_run_id.txt still points "
                        f"to {arid!r}. Run `deactivate-child --child-run-id {arid}` "
                        f"first to confirm the Agent tool actually returned, then "
                        f"retry record-timeout. For genuinely-wedged children, "
                        f"use --force-reason '<text>' to bypass."
                    )
                forced_marker_to_remove.append(active_path)
    if forced:
        # Adv-37: defer this too. The ack is part of the liveness story;
        # removing it before terminal commit would also lose retry safety.
        forced_marker_to_remove.append(
            _child_return_marker_path(repo_root, orchestration_id, arid)
        )

    started_at = resp_doc.get("started_at")
    if not isinstance(started_at, str) or not started_at.strip():
        started_at = _utc_now_iso()

    # Resolve identity fields from authoritative sources rather than synthesising them.
    # Order of precedence (Adv-3):
    #   1. session_run_index.json — written by record-launch with the actual launched
    #      identity (agent_session_id may differ from agent_run_id for Codex backend
    #      and may be reused across runs under repair_strategy=reuse).
    #   2. launches/<arid>.response.json (record-launch persists the response payload).
    #   3. launches/<arid>.request.json for fields the response does not carry
    #      (parent_agent_run_id, context_id when explicitly set in request).
    #   4. arid itself, as last-resort default for Claude Code where
    #      agent_session_id == context_id == agent_run_id by contract.
    # index_doc was loaded above for the Adv-14 liveness guards.
    index_entry: dict[str, Any] = {}
    for entry in index_doc.get("entries", []) or []:
        if isinstance(entry, dict) and entry.get("agent_run_id") == arid:
            index_entry = entry
            break

    def _first_str(*candidates: Any) -> str | None:
        for c in candidates:
            if isinstance(c, str) and c.strip():
                return c.strip()
        return None

    session_value = _first_str(
        index_entry.get("agent_session_id"),
        resp_doc.get("agent_session_id"),
        arid,
    ) or arid
    context_value = _first_str(
        index_entry.get("context_id"),
        req_doc.get("context_id"),
        resp_doc.get("context_id"),
        # Claude Code contract: context_id == agent_run_id.
        arid if backend_token == "claude" else None,
        session_value,
    ) or session_value
    parent_value = _first_str(req_doc.get("parent_agent_run_id"))

    composed_reason = reason_text
    if forced:
        composed_reason = f"{reason_text} | FORCED: {forced_reason_text}"
    payload: dict[str, Any] = {
        "agent_run_id": arid,
        "agent_role": role_token,
        "agent_backend": backend_token,
        "status": "timeout",
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "agent_session_id": session_value,
        "context_id": context_value,
        "context_isolated": True,
        "timeout_reason": composed_reason,
        # _extract_agent_summary_text scans for keys ending in "summary"/"reason";
        # surface the timeout reason as result_summary so agent.summary.txt
        # validation passes without requiring callers to duplicate the field.
        "result_summary": f"timeout: {composed_reason}",
    }
    if forced:
        payload["forced"] = True
        payload["forced_reason"] = forced_reason_text
    if parent_value is not None:
        payload["parent_agent_run_id"] = parent_value
    for field in ("node_key", "step", "substep", "agent_model"):
        val = req_doc.get(field)
        if isinstance(val, str) and val.strip():
            payload[field] = val.strip()

    result = record_agent_run(
        repo_root=repo_root,
        orchestration_id=orchestration_id,
        payload=payload,
    )
    # Adv-37: only after the durable terminal record committed do we touch
    # forced-bypass markers. record_agent_run terminal already unlinks
    # active_child / per-arid / parent_return_token markers, so the only
    # remaining cleanup here is the child_return ack (if any) — which
    # deactivate_child would normally have removed but the forced path
    # bypassed.
    for stale_marker in forced_marker_to_remove:
        try:
            stale_marker.unlink(missing_ok=True)
        except OSError:
            pass
    return result


def record_agent_run(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    root = _orchestration_root(repo_root, orchestration_id)
    root.mkdir(parents=True, exist_ok=True)
    runs_path = root / "agent_runs.jsonl"

    agent_run_id = payload.get("agent_run_id")
    if not isinstance(agent_run_id, str) or not agent_run_id.strip():
        raise ValueError("agent_run_id must be non-empty string")
    agent_run_id = agent_run_id.strip()

    role = payload.get("agent_role") or payload.get("agent_type") or payload.get("role")
    role_token = role.strip().lower() if isinstance(role, str) and role.strip() else None
    if role_token is None:
        raise ValueError("agent_role must be non-empty string")
    if role_token == "skipped_by_checkpoint":
        _validate_skipped_by_checkpoint_payload(payload)
    elif role_token in {"step", "substep"}:
        _require_preflight_launchable(
            repo_root,
            orchestration_id,
            enforce_live_probe=False,
        )

    agent_backend = payload.get("agent_backend")
    if not isinstance(agent_backend, str) or not agent_backend.strip():
        raise ValueError("agent_backend must be non-empty string")
    backend_token = agent_backend.strip().lower()
    if backend_token not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"agent_backend must be one of {sorted(SUPPORTED_BACKENDS)}; got {agent_backend!r}"
        )
    payload["agent_backend"] = backend_token

    existing = _read_existing_run_ids(runs_path)
    if agent_run_id in existing:
        raise ValueError(f"duplicate agent_run_id: {agent_run_id}")

    payload = dict(payload)
    payload["agent_run_id"] = agent_run_id
    payload["agent_role"] = role_token
    payload.setdefault("started_at", _utc_now_iso())

    reply_budget_info: dict[str, Any] | None = None
    if role_token in {"step", "substep"}:
        payload.setdefault("context_isolated", True)
        request_ref, response_ref = _launch_refs(orchestration_id, agent_run_id)
        prompt_ref, reply_ref = _launch_dialog_refs(orchestration_id, agent_run_id)
        payload.setdefault("launch_request_ref", request_ref)
        payload.setdefault("launch_response_ref", response_ref)
        payload.setdefault("launch_prompt_ref", prompt_ref)
        payload.setdefault("launch_reply_ref", reply_ref)
        _validate_step_or_substep_launch_refs(repo_root, payload)
        # Budget the child's verbatim reply (telemetry by default; hard fail under
        # METDSL_ENFORCE_REPLY_BUDGET=1). Evaluated before the terminal append so a
        # hard fail leaves no durable record — the orchestration re-launches terser.
        reply_budget_info = _evaluate_reply_budget(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id,
            reply_ref=str(payload["launch_reply_ref"]),
        )

        # Auto-derive identity fields the orchestration agent need not re-supply.
        # `parent_agent_run_id` and `agent_model` are recorded into the launch
        # request by record-launch (parent unconditionally; agent_model required
        # for step/substep launches), so we backfill them here rather than rely
        # on the caller's record-agent-run payload — which the canonical field
        # tables historically omitted, leaving every step/substep entry without
        # them and failing pre_judge. Mirrors the record_timeout path which
        # already pulls both from the launch request. Read defensively: if the
        # request is missing/unreadable, leave the fields absent so the terminal
        # validator and pre_judge still fail loud rather than silently passing.
        try:
            _launch_req = _read_json(repo_root / str(payload["launch_request_ref"]))
        except (OSError, json.JSONDecodeError, KeyError):
            _launch_req = None
        if isinstance(_launch_req, dict):
            for _field in ("parent_agent_run_id", "agent_model"):
                _val = _launch_req.get(_field)
                if isinstance(_val, str) and _val.strip():
                    payload.setdefault(_field, _val.strip())

    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in TERMINAL_STATUSES:
        payload.setdefault("finished_at", _utc_now_iso())

    # NEW-H2: sandbox checks moved into the locked region below so two
    # concurrent finalizers cannot race on `_write_sandbox_enforcement_violation`
    # writes, and so failures are recorded in agent_runs_invalid.jsonl
    # alongside other terminal-validation failures rather than only as
    # violation sidecars.

    dialogs_root = root / "agents" / agent_run_id / "dialogs"
    dialogs_root.mkdir(parents=True, exist_ok=True)
    result_ref, summary_ref = _agent_result_refs(orchestration_id, agent_run_id)
    payload.setdefault("agent_result_ref", result_ref)
    payload.setdefault("agent_summary_ref", summary_ref)

    # Adv-24/H2: serialize the duplicate-recheck + terminal validation +
    # dialog/append commit phase against any concurrent finalizer for the
    # same orchestration. _validate_terminal_run_payload runs inside the
    # lock (H2 fix) because its violation path calls
    # _cleanup_empty_file_pin_stubs / _cleanup_agent_tmp_root — destructive
    # operations that two unsynchronized finalizers must not race.
    with _runs_jsonl_exclusive_lock(repo_root, orchestration_id):
        # NEW-H1: caller holds the lock, so writer-active probe would
        # self-contend. Pass the flag so durable corruption is surfaced
        # rather than masked.
        existing_locked = _read_existing_run_ids(runs_path, caller_holds_lock=True)
        if agent_run_id in existing_locked:
            raise ValueError(
                f"duplicate agent_run_id: {agent_run_id} "
                f"(race detected on locked re-check)"
            )
        # L-NEW-1: capture sandbox-violation reason so the invalid-log entry
        # records the specific failure (e.g. sandbox_runtime_not_bwrap)
        # rather than the generic "terminal_payload_validation_error".
        sandbox_fail_reason: str | None = None
        try:
            # NEW-H2: sandbox checks run under the lock and route violations
            # through the same invalid-entry log as _validate_terminal_run_payload.
            if role_token in {"step", "substep"}:
                launch_response_path = repo_root / payload["launch_response_ref"]
                launch_response_payload = _read_json(launch_response_path)
                if not isinstance(launch_response_payload, dict):
                    sandbox_fail_reason = "launch_response_not_object"
                    raise ValueError("launch response must be json object")
                # M-FOURTH-1: pre-set the reason so a ValueError raised by
                # the helper carries the specific identity-failure tag into
                # agent_runs_invalid.jsonl rather than the generic
                # "terminal_payload_validation_error" bucket.
                sandbox_fail_reason = "launch_response_session_id_invalid"
                response_agent_session_id = _validate_response_agent_session_id(launch_response_payload)
                # Helper accepted the response → reset to None so the next
                # check's failure (if any) records its own specific reason.
                sandbox_fail_reason = None
                payload_agent_session_id = payload.get("agent_session_id")
                if not isinstance(payload_agent_session_id, str) or not payload_agent_session_id.strip():
                    sandbox_fail_reason = "agent_session_id_empty"
                    raise ValueError("agent_session_id must be non-empty string")
                if payload_agent_session_id.strip() != response_agent_session_id:
                    sandbox_fail_reason = "agent_session_id_mismatch"
                    raise ValueError(
                        "agent_session_id must match child agent identifier in launch response"
                    )
                sandbox_ref = launch_response_payload.get("sandbox_profile_ref")
                if launch_response_payload.get("sandbox_runtime") != "bwrap":
                    _write_sandbox_enforcement_violation(
                        repo_root,
                        orchestration_id,
                        agent_run_id=agent_run_id,
                        reason="sandbox_runtime_not_bwrap",
                        detail={"launch_response_ref": payload["launch_response_ref"]},
                    )
                    sandbox_fail_reason = "sandbox_runtime_not_bwrap"
                    raise ValueError("launch response must record sandbox_runtime=bwrap")
                if launch_response_payload.get("sandbox_enforced") is not True:
                    _write_sandbox_enforcement_violation(
                        repo_root,
                        orchestration_id,
                        agent_run_id=agent_run_id,
                        reason="sandbox_not_enforced",
                        detail={"launch_response_ref": payload["launch_response_ref"]},
                    )
                    sandbox_fail_reason = "sandbox_not_enforced"
                    raise ValueError("launch response must record sandbox_enforced=true")
                if not isinstance(sandbox_ref, str) or not sandbox_ref.strip():
                    _write_sandbox_enforcement_violation(
                        repo_root,
                        orchestration_id,
                        agent_run_id=agent_run_id,
                        reason="sandbox_profile_missing",
                        detail={"launch_response_ref": payload["launch_response_ref"]},
                    )
                    sandbox_fail_reason = "sandbox_profile_missing"
                    raise ValueError("launch response must include sandbox_profile_ref")
                sandbox_path = repo_root / str(sandbox_ref).strip()
                if not sandbox_path.exists():
                    _write_sandbox_enforcement_violation(
                        repo_root,
                        orchestration_id,
                        agent_run_id=agent_run_id,
                        reason="sandbox_profile_not_found",
                        detail={"sandbox_profile_ref": sandbox_ref},
                    )
                    sandbox_fail_reason = "sandbox_profile_not_found"
                    raise ValueError(f"sandbox_profile_ref target not found: {sandbox_ref}")
                payload.setdefault("sandbox_runtime", "bwrap")
                payload.setdefault("sandbox_enforced", True)
                payload.setdefault("sandbox_profile_ref", str(sandbox_ref).strip())
            _validate_terminal_run_payload(
                repo_root, orchestration_id, payload, caller_holds_lock=True,
            )
        except ValueError as exc:
            # Unauthorized write or sandbox violation detected during terminal
            # validation.  Persist a fail entry to a SEPARATE log so audit
            # tools can see that this agent_run_id reached record-agent-run
            # with an invalid payload — without polluting agent_runs.jsonl
            # (which the duplicate-detection check would otherwise refuse on
            # retry). The retry path (typically with the same agent_run_id
            # after fixing the payload) therefore remains operational.
            invalid_path = root / "agent_runs_invalid.jsonl"
            fail_entry: dict[str, Any] = dict(payload)
            fail_entry["status"] = "fail"
            fail_entry["finished_at"] = _utc_now_iso()
            # L-NEW-1: prefer the specific sandbox/identity reason; fall
            # back to the generic terminal-validation tag for everything
            # else (output manifest violations, etc., raised by
            # _validate_terminal_run_payload itself).
            fail_entry["fail_reason"] = sandbox_fail_reason or "terminal_payload_validation_error"
            fail_entry["fail_message"] = str(exc)
            with invalid_path.open("a", encoding="utf-8") as _h:
                _h.write(json.dumps(fail_entry, ensure_ascii=False) + "\n")
            raise
        # Summary text needs the (possibly-mutated) payload.
        summary_text = _extract_agent_summary_text(payload)
        _validate_agent_summary_text(payload, summary_text)
        _write_json(dialogs_root / "agent.result.json", payload)
        _write_text(dialogs_root / "agent.summary.txt", summary_text)
        # Adv-35: append the durable terminal record FIRST. The destructive
        # tmp cleanup runs after this append (outside the lock) and only
        # then is the cleanup_committed marker written. Validator treats an
        # arid as truly terminated only when BOTH the terminal entry AND
        # the committed marker exist — so a partial failure (cleanup fails
        # silently, or process dies between append and commit) preserves
        # the recovery scratch under workspace/tmp/<arid>/ for diagnostics
        # rather than losing it ahead of a durable state transition.
        with runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    status_token = str(payload.get("status", "")).strip().lower()
    if status_token in TERMINAL_STATUSES:
        session_obj = payload.get("agent_session_id")
        session_value = session_obj.strip() if isinstance(session_obj, str) and session_obj.strip() else agent_run_id
        context_obj = payload.get("context_id")
        context_value = context_obj.strip() if isinstance(context_obj, str) and context_obj.strip() else None
        _append_session_run_index_entry(
            repo_root,
            orchestration_id,
            agent_run_id=agent_run_id,
            agent_session_id=session_value,
            context_id=context_value,
            agent_role=role_token,
            status=status_token,
        )

    if role_token in {"step", "substep"}:
        status_raw = payload.get("status")
        status_lower = status_raw.strip().lower() if isinstance(status_raw, str) else ""
        if status_lower in TERMINAL_STATUSES:
            nk_done = payload.get("node_key")
            st_done = payload.get("step")
            if isinstance(nk_done, str) and nk_done.strip() and isinstance(st_done, str) and st_done.strip():
                _transition_node_step_phase_state(
                    repo_root,
                    orchestration_id,
                    node_key=nk_done.strip(),
                    step=st_done.strip().lower(),
                    new_state="child_finished",
                    event="record_agent_run_terminal",
                    agent_run_id=agent_run_id,
                )
            if backend_token == "claude":
                _active_child_agent_run_id_path(repo_root, orchestration_id).unlink(missing_ok=True)
            # Adv-16: also clear backend-neutral per-arid marker.
            _active_child_marker_path(
                repo_root, orchestration_id, agent_run_id
            ).unlink(missing_ok=True)
            # Adv-30: clear the parent return token sidecar; the run is done
            # and the token must not be reusable.
            _parent_return_token_path(
                repo_root, orchestration_id, agent_run_id
            ).unlink(missing_ok=True)

    # Adv-35/36: terminal record is now durable in agent_runs.jsonl. Run the
    # destructive tmp cleanup, then write the cleanup_committed marker ONLY
    # IF the cleanup actually completed (rmtree succeeded or tmp was already
    # absent). Adv-36: a refusal (ownership / collision / symlink) or a
    # silent rmtree OSError must NOT publish the committed marker —
    # otherwise validator would revoke exemption while leftover scratch
    # remains, exactly the "broken two-phase invariant" Codex flagged.
    final_status = str(payload.get("status", "")).strip().lower()
    if final_status in TERMINAL_STATUSES or final_status == "fail_closed":
        cleanup_done = _cleanup_agent_tmp_root(
            repo_root, orchestration_id, agent_run_id=agent_run_id
        )
        if cleanup_done:
            _write_cleanup_committed_marker(
                repo_root, orchestration_id, agent_run_id=agent_run_id
            )

    # Surface an over-budget reply in the CLI result (soft mode) without persisting it
    # into the durable agent_runs.jsonl record. _TERSE_ALWAYS_KEEP retains it through the
    # terse projection so the orchestration always sees the warning.
    if reply_budget_info is not None:
        return {**payload, "reply_over_budget": reply_budget_info}
    return payload


def deactivate_child_agent(
    repo_root: Path,
    orchestration_id: str,
    *,
    child_run_id: str,
) -> dict[str, Any]:
    """Clear active-child markers for a SUBSTEP / STEP child run.

    **M3 design note**: This function is intentionally asymmetric vs the
    orchestration agent's own arid. The orch agent has no `record-launch`
    handshake (no per-arid `launches/<arid>.request.json`), so it has no
    `record-child-return` ack to validate against and is finalized via
    `update_orchestration_status` directly. The Adv-30 parent-bound token
    and Adv-20 ack file therefore protect ONLY child runs (where the
    "Agent tool returned" signal is the actual security boundary). The
    orch's own scratch is cleaned at terminal `set-status` time and is
    guarded by the `is_owner_via_orchestration` proof in
    `_cleanup_agent_tmp_root` (orchestration_meta.json identity).
    """
    # Adv-18: validate-first, unlink-second. The previous implementation
    # unlinked the per-arid backend-neutral marker (Adv-16) BEFORE checking
    # the legacy Claude marker, which meant a stray/misrouted deactivate-child
    # call could clear a still-running child's record-timeout liveness guard
    # and enable a downstream record-timeout to wipe live tmp scratch.
    # Both marker checks now run before any state mutation.
    active_path = _active_child_agent_run_id_path(repo_root, orchestration_id)
    legacy_marker_present = active_path.is_file()
    if legacy_marker_present:
        active_value = active_path.read_text(encoding="utf-8").strip()
        if active_value != child_run_id:
            raise ValueError(
                "active child run mismatch: "
                f"expected={child_run_id!r}, actual={active_value!r}"
            )
    per_arid_marker = _active_child_marker_path(
        repo_root, orchestration_id, child_run_id
    )
    per_arid_present = per_arid_marker.is_file()
    if not legacy_marker_present and not per_arid_present:
        # Idempotent: already deactivated (or never launched).
        return {
            "deactivated_child_run_id": child_run_id,
            "orchestration_id": orchestration_id,
            "deactivated_at": _utc_now_iso(),
            "already_inactive": True,
        }
    # Adv-20: require explicit "Agent tool returned" ack. The orchestration
    # agent must call record-child-return before deactivate-child. Without
    # this gate, a misrouted deactivate-child for a still-running Codex
    # child clears its only liveness guard (active_children marker) and lets
    # record-timeout finalize and wipe its scratch.
    return_ack = _child_return_marker_path(repo_root, orchestration_id, child_run_id)
    if not return_ack.is_file():
        raise ValueError(
            f"deactivate-child: child_returns/{child_run_id}.txt is missing — "
            f"call `record-child-return --agent-run-id {child_run_id}` AFTER "
            f"observing the Agent tool actually return, BEFORE deactivate-child."
        )
    # Adv-30: re-verify the parent-bound token at unlink time. Even if the
    # ack file was created legitimately, an attacker who later edits the file
    # cannot set a token they don't know.
    expected_parent_token = _read_parent_return_token(
        repo_root, orchestration_id, child_run_id
    )
    # Token-missing bug fix: `record_child_return` requires the
    # parent_return_token to exist before writing the ack file. If we now
    # see the ack present but the token absent, the invariant has been
    # violated (manual tampering, partial cleanup race, or a code-path
    # bug). Silently skipping verification would let any caller forge an
    # ack by deleting the token file first — defeating the Adv-30 binding.
    # Refuse to clear liveness markers and surface the inconsistency.
    if expected_parent_token is None:
        raise ValueError(
            f"deactivate-child: child_returns/{child_run_id}.txt exists but "
            f"launches/{child_run_id}.parent_return_token is missing. "
            f"record-child-return requires both files together, so the "
            f"absence of the parent token indicates corruption or external "
            f"tampering of the launch artifacts. Refusing to clear liveness "
            f"markers — investigate (re-launch may be required)."
        )
    try:
        ack_doc = json.loads(return_ack.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        ack_doc = None
    ack_token = (
        ack_doc.get("return_token") if isinstance(ack_doc, dict) else None
    )
    if not isinstance(ack_token, str) or not secrets.compare_digest(
        ack_token, expected_parent_token
    ):
        raise ValueError(
            f"deactivate-child: child_returns/{child_run_id}.txt does not "
            f"contain a valid parent return token. The ack appears to have "
            f"been tampered with or constructed without the per-arid token "
            f"from launches/{child_run_id}.parent_return_token."
        )
    # Recurrence-prevention plan (Issue 3): capture the child-authored path
    # set NOW, before any subsequent parent / runtime write can contaminate
    # the live baseline diff. This freezes what the child wrote during its
    # active window so `record-agent-run` retries (which may run after
    # post-fail infrastructure writes like `agent_runs_invalid.jsonl`) can
    # compute the diff against the snapshot rather than re-walking the
    # workspace. Idempotent: subsequent deactivate calls preserve the first
    # captured snapshot.
    snap_path = _deactivate_snapshot_path(
        repo_root, orchestration_id, agent_run_id=child_run_id
    )
    if not snap_path.exists():
        try:
            child_authored = _compute_changed_paths_against_baseline(
                repo_root,
                orchestration_id,
                agent_run_id=child_run_id,
            )
            _write_json(
                snap_path,
                {
                    "kind": "deactivate_snapshot",
                    "agent_run_id": child_run_id,
                    "orchestration_id": orchestration_id,
                    "child_authored_paths": child_authored,
                    "captured_at": _utc_now_iso(),
                },
            )
        except (OSError, ValueError):
            # If baseline is missing (legacy orchestration without per-arid
            # baseline tracking) or write fails, fall through to the
            # tree-walk fallback in `_actual_changed_paths_since_baseline`.
            pass
    # Validation passed and at least one marker exists — atomic unlink phase.
    per_arid_marker.unlink(missing_ok=True)
    active_path.unlink(missing_ok=True)
    # Consume the ack: it must be re-issued for any future relaunch of the
    # same arid (defensive — no current code path reuses arids, but this
    # keeps the invariant that ack proves a one-time return event).
    return_ack.unlink(missing_ok=True)
    return {
        "deactivated_child_run_id": child_run_id,
        "orchestration_id": orchestration_id,
        "deactivated_at": _utc_now_iso(),
        "already_inactive": False,
    }


def record_reply_text(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    reply_text: str,
) -> dict[str, Any]:
    if not isinstance(reply_text, str) or not reply_text.strip():
        raise ValueError("record-reply requires non-empty reply_text")
    _, reply_ref = _launch_dialog_refs(orchestration_id, agent_run_id)
    reply_path = repo_root / reply_ref
    _write_text(reply_path, reply_text)
    return {
        "orchestration_id": orchestration_id,
        "agent_run_id": agent_run_id,
        "reply_ref": reply_ref,
        "recorded_at": _utc_now_iso(),
    }


def _collect_child_usage_for_finalize(
    repo_root: Path, orchestration_id: str, agent_run_id: str
) -> dict[str, Any]:
    """Best-effort token usage for a just-returned child, for durable in-repo persistence.

    Child ``Agent`` subagents carry no usage in ``agent_runs.jsonl`` and are not
    sidechains in the host transcript, so child cost (empirically the majority of
    a node's total) is invisible once the machine-local, ephemeral ``~/.claude``
    transcript is cleaned. Capturing it at finalize time — when the child has just
    returned and its transcript is on disk — persists the numbers in-repo so later
    audits aren't blind. Never raises and never blocks finalization: any failure
    yields a ``status: "unavailable"`` marker. The post-hoc reconstruction path is
    ``tools/audit_orchestration.py`` (same aggregator).
    """
    try:
        try:  # script run: tools/ on sys.path ; package import: repo root on path
            from orchestration_diagnostics import aggregate_child_usage
        except ImportError:  # pragma: no cover - import-path shim
            from tools.orchestration_diagnostics import aggregate_child_usage
        # The just-returned child is under the current host session, so hint that
        # session to avoid scanning every other session's subagents on each finalize.
        meta = _read_json(
            _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
        )
        host_session_id = None
        if isinstance(meta, dict):
            hs = meta.get("host_session_id")
            if isinstance(hs, str) and hs.strip():
                host_session_id = hs.strip()
        agg = aggregate_child_usage(
            repo_root, [agent_run_id], host_session_id=host_session_id
        )
        usage = (agg.get("per_child") or {}).get(agent_run_id)
        if isinstance(usage, dict) and usage:
            return usage
        return {
            "status": "unavailable",
            "reason": agg.get("reason") or "no locatable child transcript",
        }
    except Exception as exc:  # noqa: BLE001 - usage capture must never break finalize
        return {"status": "unavailable", "reason": f"usage capture error: {exc}"}


def finalize_child(
    repo_root: Path,
    orchestration_id: str,
    *,
    agent_run_id: str,
    return_token: str,
    reply_text: str,
    agent_run_payload: dict[str, Any],
    reply_excerpt: str | None = None,
) -> dict[str, Any]:
    """One-call child finalization: collapse the 4 finalize CLI round-trips into one.

    Performs, in the mandated order and in a single process,
    `record_child_return` -> `deactivate_child_agent` -> `record_reply_text` ->
    `record_agent_run`, reusing those functions unchanged so every guard is preserved
    (Adv-30 return-token verification, the Adv-20 ack-file precondition for deactivate,
    the active_child ordering, and the reply budget guard which reads the reply written
    by step 3). Cutting the per-child finalize from 4 Bash round-trips to 1 removes the
    bulk of the per-child command+output that otherwise stays resident in the
    orchestration transcript (a driver of the quadratic cache_read cost).

    `agent_run_payload` is the `record-agent-run` payload; its `agent_run_id` must match
    the `--agent-run-id` argument (a mismatch is a caller error). The reply excerpt
    defaults to the first non-empty line of `reply_text` when not supplied.
    """
    arid = agent_run_id.strip() if isinstance(agent_run_id, str) else ""
    if not arid:
        raise ValueError("finalize-child requires non-empty --agent-run-id")
    if not isinstance(reply_text, str) or not reply_text.strip():
        raise ValueError("finalize-child requires non-empty --reply-text")
    if not isinstance(agent_run_payload, dict):
        raise ValueError("finalize-child requires --agent-run-json to be a JSON object")
    payload_arid = agent_run_payload.get("agent_run_id")
    if not (isinstance(payload_arid, str) and payload_arid.strip() == arid):
        raise ValueError(
            f"finalize-child: --agent-run-json agent_run_id ({payload_arid!r}) must equal "
            f"--agent-run-id ({arid!r})"
        )
    if reply_excerpt is None:
        first_line = next((ln.strip() for ln in reply_text.splitlines() if ln.strip()), "")
        reply_excerpt = first_line or None

    # Hard reply-budget gate runs BEFORE any state-consuming side effect. Without this,
    # record_agent_run's hard check (METDSL_ENFORCE_REPLY_BUDGET=1) would fire only after
    # record-child-return + deactivate-child have already consumed the ack / active marker /
    # parent_return_token, leaving the run un-retriable via finalize-child. The soft path
    # (telemetry) is still handled by record_agent_run below, which reads the written reply.
    # Measure the EXACT persisted form: record_reply_text -> _write_text appends a trailing
    # newline when absent, so an exact-boundary reply (== budget, no newline) becomes
    # budget+1 on disk; counting the persisted length keeps this precheck consistent with the
    # downstream record_agent_run check and avoids a boundary leak past the side effects.
    persisted_reply = reply_text if reply_text.endswith("\n") else reply_text + "\n"
    if (
        len(persisted_reply) > REPLY_BUDGET_CHARS
        and os.environ.get("METDSL_ENFORCE_REPLY_BUDGET") == "1"
    ):
        # Audit the reject before raising — mirrors _evaluate_reply_budget's hook entry on
        # the direct record-agent-run path, so finalize-child hard failures stay visible in
        # hooks/workflow_hooks.jsonl (operators diagnose budget failures from that log).
        _append_workflow_hook_log(
            repo_root,
            orchestration_id,
            hook_name="reply_over_budget",
            status="reject",
            detail={"agent_run_id": arid, "chars": len(persisted_reply), "budget": REPLY_BUDGET_CHARS},
        )
        raise ValueError(
            f"finalize-child: child reply is {len(persisted_reply)} chars, over the {REPLY_BUDGET_CHARS}-char "
            f"budget (METDSL_ENFORCE_REPLY_BUDGET=1). Re-launch the child with a terse final message — a "
            f"status line, output_refs, and a few lines of rationale; full detail belongs in the artifacts."
        )

    child_return = record_child_return(
        repo_root,
        orchestration_id,
        agent_run_id=arid,
        return_token=return_token,
        reply_excerpt=reply_excerpt,
    )
    deactivation = deactivate_child_agent(
        repo_root,
        orchestration_id,
        child_run_id=arid,
    )
    reply = record_reply_text(
        repo_root,
        orchestration_id,
        agent_run_id=arid,
        reply_text=reply_text,
    )
    # Persist child token usage in-repo (resolves the measurement blind spot for
    # later audits, since ~/.claude transcripts are ephemeral). Best-effort and
    # additive: never overrides a caller-supplied value, never blocks finalize.
    if "usage" not in agent_run_payload:
        agent_run_payload = dict(agent_run_payload)
        agent_run_payload["usage"] = _collect_child_usage_for_finalize(
            repo_root, orchestration_id, arid
        )
    run_record = record_agent_run(
        repo_root,
        orchestration_id,
        agent_run_payload,
    )

    result: dict[str, Any] = {
        "orchestration_id": orchestration_id,
        "agent_run_id": arid,
        "status": run_record.get("status"),
        "deactivated_child_run_id": deactivation.get("deactivated_child_run_id", arid),
        "child_return_recorded_at": child_return.get("recorded_at"),
        "reply_ref": reply.get("reply_ref"),
        "finalized_at": _utc_now_iso(),
    }
    if isinstance(run_record, dict) and run_record.get("reply_over_budget"):
        result["reply_over_budget"] = run_record["reply_over_budget"]
    return result


def write_step_result(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
    agent_run_id: str,
    payload: dict[str, Any],
    backfill: bool = False,
) -> dict[str, Any]:
    _require_preflight_launchable(
        repo_root,
        orchestration_id,
        enforce_live_probe=False,
    )
    node_safe = _node_key_to_safe(node_key)
    step_token = step.strip().lower()
    root = _orchestration_root(repo_root, orchestration_id)
    result_path = root / "steps" / node_safe / step_token / agent_run_id / "step_result.json"

    if backfill:
        # Backfill writes a step_result for an already-terminal step agent that
        # never received one (e.g. an original-run gap stranded by a checkpoint
        # resume that reset the build phase out of `child_finished`). It bypasses
        # the live `child_finished` gate and does NOT advance the phase state, so
        # it adds no new step agent and is net-negative on the missing-step_result
        # count — the only way to break the completion-check deadlock without
        # launching net-new step agents. Guards keep the completion invariant
        # honest: gap-fill only, recorded run must be a terminal step agent for
        # this node/step, and the payload status must match the recorded status.
        if result_path.exists():
            raise RuntimeError(
                f"write_step_result --backfill: step_result already exists for "
                f"agent_run_id={agent_run_id} (backfill only fills a genuine gap, never overwrites)"
            )
        runs = _load_run_records(root)
        record = runs.get(agent_run_id.strip())
        if not isinstance(record, dict):
            raise RuntimeError(
                f"write_step_result --backfill: no agent_runs.jsonl record for agent_run_id={agent_run_id}"
            )
        # The recorded run must be a `step` agent for exactly this node/step.
        # result_path is built from the caller-supplied node_key/step, so without
        # this the command could write a step_result into the wrong directory
        # (mistyped node_key/step, or a substep/other run id) while the genuinely
        # stranded step stays uncovered — completion would still be blocked.
        recorded_role = record.get("agent_role")
        if not (isinstance(recorded_role, str) and recorded_role.strip().lower() == "step"):
            raise RuntimeError(
                f"write_step_result --backfill: agent_run_id={agent_run_id} is not a step agent "
                f"(agent_role={recorded_role!r}); only a step agent can be backfilled"
            )
        recorded_node_key = record.get("node_key")
        if not (isinstance(recorded_node_key, str) and recorded_node_key.strip() == node_key.strip()):
            raise RuntimeError(
                f"write_step_result --backfill: node_key mismatch for agent_run_id={agent_run_id} "
                f"(recorded={recorded_node_key!r}, requested={node_key!r})"
            )
        recorded_step = record.get("step")
        recorded_step_token = recorded_step.strip().lower() if isinstance(recorded_step, str) else ""
        if recorded_step_token != step_token:
            raise RuntimeError(
                f"write_step_result --backfill: step mismatch for agent_run_id={agent_run_id} "
                f"(recorded={recorded_step!r}, requested={step!r})"
            )
        recorded_status = record.get("status")
        recorded_token = recorded_status.strip().lower() if isinstance(recorded_status, str) else ""
        if recorded_token not in TERMINAL_STATUSES:
            raise RuntimeError(
                f"write_step_result --backfill: agent_run_id={agent_run_id} is not terminal "
                f"(status={recorded_status!r}); only a terminated step agent can be backfilled"
            )
        # A `pass` is backfillable too: a build child can record terminal `pass`
        # (its outputs validated by record-agent-run) yet lose its `child_finished`
        # authority before write-step-result ran, leaving it stranded with no
        # recovery path otherwise (the relaunch guard would block, and the normal
        # write path needs the lost `child_finished`). The status-match check below
        # is what prevents fabricating a pass — backfill can only mirror the
        # authoritative recorded status, never invent a better one.
        payload_status = payload.get("status")
        payload_token = payload_status.strip().lower() if isinstance(payload_status, str) else ""
        if payload_token != recorded_token:
            raise RuntimeError(
                f"write_step_result --backfill: payload status={payload_status!r} must match the "
                f"recorded run status={recorded_status!r} for agent_run_id={agent_run_id}"
            )
    else:
        _phase_state_allows_write_step_result(
            repo_root,
            orchestration_id,
            node_key=node_key,
            step=step,
        )
        # Fail-fast executor-role guard. For substep-aware phases the executor must be
        # the orchestration agent (the substeps' parent), and for the no-substep Build
        # phase it must be the step agent. Without this, a wrong --agent-run-id (e.g. a
        # verify-substep arid) is only caught downstream at the Validate pre_judge gate
        # (validate_pipeline_semantics.py), by which point the phase is locked at
        # step_result_written with no public reset. Raising here — before _write_json and
        # the phase transition below — leaves the phase at child_finished so the agent can
        # simply re-run with the correct arid.
        # Only enforce when the executor's role is resolvable from agent_runs.jsonl. An
        # absent record (unresolved role) is left to the downstream validator's
        # "parent directory must match existing executor agent_run_id" check; in a real
        # run the executor is always recorded (orchestration row at init, step agent via
        # record-agent-run), so the recurrence case — a recorded wrong-role arid — is
        # fully covered here.
        run_records = _load_run_records(_orchestration_root(repo_root, orchestration_id))
        executor_role = str(run_records.get(agent_run_id.strip(), {}).get("agent_role") or "").strip().lower()
        if executor_role:
            if step_token in SUBSTEP_AWARE_STEPS:
                if executor_role != "orchestration":
                    raise RuntimeError(
                        f"write_step_result: step {step_token!r} is substep-aware; --agent-run-id must be "
                        f"the orchestration agent_run_id (role=orchestration), got role={executor_role!r} "
                        f"for {agent_run_id}. The phase stays child_finished — re-run write-step-result with the "
                        f"orchestration arid as both --agent-run-id and executor_agent_run_id."
                    )
            elif executor_role != "step":
                raise RuntimeError(
                    f"write_step_result: step {step_token!r} is a no-substep phase; --agent-run-id must be the "
                    f"step agent_run_id (role=step), got role={executor_role!r} for {agent_run_id}."
                )
        explicit_executor = payload.get("executor_agent_run_id")
        if isinstance(explicit_executor, str) and explicit_executor.strip() and explicit_executor.strip() != agent_run_id.strip():
            raise RuntimeError(
                f"write_step_result: executor_agent_run_id ({explicit_executor.strip()}) must equal "
                f"--agent-run-id ({agent_run_id.strip()})."
            )

    result = dict(payload)
    result.setdefault("executor_agent_run_id", agent_run_id)
    result.setdefault("required_outputs", [])
    result.setdefault("failed_substeps", [])

    _validate_step_result_payload(
        repo_root,
        orchestration_id,
        node_key=node_key,
        step=step,
        agent_run_id=agent_run_id,
        payload=result,
    )

    _write_json(result_path, result)

    try:
        post_phase_complete(
            repo_root,
            orchestration_id,
            node_key=node_key,
            step=step,
            agent_run_id=agent_run_id,
            payload=result,
        )
    except RuntimeError:
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # Backfill never advances the phase state — it accounts for a terminal agent
    # whose `child_finished` authority is gone, so consuming a transition would
    # either corrupt the live phase or be impossible. The completion check keys
    # solely on the per-agent step_result file existing, which is now written.
    if not backfill:
        _transition_node_step_phase_state(
            repo_root,
            orchestration_id,
            node_key=node_key,
            step=step_token,
            new_state="step_result_written",
            event="write_step_result",
            agent_run_id=agent_run_id,
        )

    if not backfill and result.get("status", "").strip().lower() == "pass":
        try:
            update_checkpoint(
                repo_root,
                orchestration_id,
                node_key=node_key,
                step=step,
                agent_run_id=agent_run_id,
                result=result,
            )
        except Exception:
            print(
                f"[WARN] checkpoint update failed for {node_key}/{step}: "
                + traceback.format_exc(),
                file=sys.stderr,
            )

    return result


def repair_step_result_executor(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    step: str,
) -> dict[str, Any]:
    """Repair a substep-aware step_result.json whose executor arid is a substep.

    Failure mode (orch_20260618T111853Z_17038507): a compile/generate/validate
    `step_result.json` was written under a verify-substep arid directory with its
    `executor_agent_run_id` field set to that same substep arid, instead of the
    orchestration arid. For substep-aware phases the executor MUST be the
    orchestration agent (the substeps' parent). The Validate `pre_judge` gate
    rejects the mismatch, the phase is locked at `step_result_written`, and there
    was no public repair path: `--backfill` is keyed to a terminal step agent +
    status match, `repair-agent-runs` only touches agent_runs.jsonl rows, and the
    forward guard in `write_step_result` only blocks a *new* bad write. The only
    remedy was a fresh orchestration (re-paying the whole node cost).

    This relocates the file to `steps/<node>/<step>/<orch_arid>/step_result.json`,
    rewrites `executor_agent_run_id` to the orchestration arid, and appends the old
    (substep) arid to `substep_agent_run_ids` if absent — preserving the
    substep→step_result linkage the gate checks. It refuses (leaving the file
    untouched) unless: the step is substep-aware; the corrupt directory arid is a
    recorded `substep` for this exact node/step (never inventing linkage); the
    target arid is the orchestration row; no legitimate step_result already exists
    at the target; and the corrected payload passes `_validate_step_result_payload`.
    Idempotent: a re-run with the file already correct is a `noop`. An audit entry
    is appended to record_repairs.jsonl.
    """
    step_token = step.strip().lower()
    if step_token not in SUBSTEP_AWARE_STEPS:
        raise RuntimeError(
            f"repair-step-result-executor: step {step_token!r} is not substep-aware; this repair "
            f"only applies to {sorted(SUBSTEP_AWARE_STEPS)} (no-substep Build uses write-step-result)."
        )
    node_safe = _node_key_to_safe(node_key.strip())
    root = _orchestration_root(repo_root, orchestration_id)

    runs = _load_run_records(root)
    orch_arids = [
        arid
        for arid, rec in runs.items()
        if isinstance(rec, dict) and str(rec.get("agent_role") or "").strip().lower() == "orchestration"
    ]
    # orchestration_meta.json is the authoritative source; cross-check against runs.
    meta_arid: str | None = None
    try:
        meta = _read_json(root / "orchestration_meta.json")
    except (OSError, json.JSONDecodeError):
        meta = None
    if isinstance(meta, dict):
        cand = meta.get("orchestration_agent_run_id")
        if isinstance(cand, str) and cand.strip():
            meta_arid = cand.strip()
    if meta_arid is not None:
        orch_arid = meta_arid
    elif len(orch_arids) == 1:
        orch_arid = orch_arids[0]
    else:
        raise RuntimeError(
            "repair-step-result-executor: cannot resolve a single orchestration arid "
            f"(meta orchestration_agent_run_id missing; agent_runs orchestration rows={orch_arids})"
        )

    step_dir = root / "steps" / node_safe / step_token
    result_paths = sorted(step_dir.glob("*/step_result.json")) if step_dir.exists() else []
    if not result_paths:
        raise RuntimeError(
            f"repair-step-result-executor: no step_result.json under {step_dir} "
            f"(node_key={node_key!r}, step={step_token!r})"
        )

    target_path = step_dir / orch_arid / "step_result.json"
    corrupt_paths = [p for p in result_paths if p.parent.name != orch_arid]

    if not corrupt_paths:
        # Already at the orchestration arid dir; confirm the field agrees.
        doc = _read_json(target_path)
        field = doc.get("executor_agent_run_id") if isinstance(doc, dict) else None
        if isinstance(field, str) and field.strip() == orch_arid:
            return {
                "status": "noop",
                "orchestration_id": orchestration_id,
                "node_key": node_key,
                "step": step_token,
                "executor_agent_run_id": orch_arid,
                "reason": "step_result already keyed to the orchestration arid",
            }
        # Dir is correct but the field is wrong — rewrite the field in place.
        corrupt_paths = [target_path]

    if len(corrupt_paths) > 1:
        raise RuntimeError(
            "repair-step-result-executor: multiple non-orchestration step_result directories "
            f"({[p.parent.name for p in corrupt_paths]}); refuse to guess — resolve manually."
        )

    corrupt_path = corrupt_paths[0]
    corrupt_arid = corrupt_path.parent.name
    doc = _read_json(corrupt_path)
    if not isinstance(doc, dict):
        raise RuntimeError(f"repair-step-result-executor: step_result.json must be object: {corrupt_path}")
    original_executor = str(doc.get("executor_agent_run_id") or "").strip()

    relocate = corrupt_arid != orch_arid

    # The wrong executor arid(s) that must stay linked as substeps: the directory
    # key (relocate case, where the file sat under the substep arid) AND the
    # original `executor_agent_run_id` field (field-only case, where the file sits
    # under the orchestration dir but the field points at the verify substep). Both
    # represent a real substep whose substep→step_result linkage the pre_judge /
    # completion check requires; overwriting the field to the orchestration arid
    # without preserving these would drop the linkage.
    wrong_substeps: list[str] = []
    for cand in ((corrupt_arid if relocate else ""), original_executor):
        c = cand.strip()
        if c and c != orch_arid and c not in wrong_substeps:
            wrong_substeps.append(c)

    # Each wrong executor arid must be a recorded substep for this exact node/step —
    # never invent linkage for an arbitrary arid, and refuse rather than silently
    # dropping an unknown one.
    for arid in wrong_substeps:
        rec = runs.get(arid)
        rec_role = str(rec.get("agent_role") or "").strip().lower() if isinstance(rec, dict) else ""
        rec_node = str(rec.get("node_key") or "").strip() if isinstance(rec, dict) else ""
        rec_step = str(rec.get("step") or "").strip().lower() if isinstance(rec, dict) else ""
        if rec_role != "substep" or rec_node != node_key.strip() or rec_step != step_token:
            raise RuntimeError(
                f"repair-step-result-executor: executor arid {arid} is not a recorded substep "
                f"for node {node_key!r} step {step_token!r} (role={rec_role!r}, node={rec_node!r}, "
                f"step={rec_step!r}); refuse to rewrite."
            )

    if relocate and target_path.exists():
        raise RuntimeError(
            f"repair-step-result-executor: a step_result already exists at the orchestration dir "
            f"{target_path}; refuse to overwrite (resolve manually)."
        )

    corrected = dict(doc)
    corrected["executor_agent_run_id"] = orch_arid
    substeps = corrected.get("substep_agent_run_ids")
    substep_list = [s for s in substeps if isinstance(s, str) and s.strip()] if isinstance(substeps, list) else []
    for arid in wrong_substeps:
        if arid not in substep_list:
            substep_list.append(arid)
    corrected["substep_agent_run_ids"] = substep_list

    # Validate the corrected payload exactly as the write path would; refuse and
    # leave the original untouched if it would not pass (e.g. broken linkage).
    _validate_step_result_payload(
        repo_root,
        orchestration_id,
        node_key=node_key,
        step=step,
        agent_run_id=orch_arid,
        payload=corrected,
    )

    _write_json(target_path, corrected)
    if relocate:
        try:
            corrupt_path.unlink(missing_ok=True)
            corrupt_path.parent.rmdir()
        except OSError:
            pass

    from_executor = original_executor or corrupt_arid
    prov = {
        "at": _utc_now_iso(),
        "reason": "step_result executor arid repair (repair-step-result-executor)",
        "node_key": node_key,
        "step": step_token,
        "from_executor_agent_run_id": from_executor,
        "to_executor_agent_run_id": orch_arid,
        "relocated": relocate,
        "linked_substeps": wrong_substeps,
    }
    repairs_path = root / "record_repairs.jsonl"
    with repairs_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(prov, ensure_ascii=False) + "\n")

    return {
        "status": "repaired",
        "orchestration_id": orchestration_id,
        "node_key": node_key,
        "step": step_token,
        "executor_agent_run_id": orch_arid,
        "from_executor_agent_run_id": from_executor,
        "relocated": relocate,
        "step_result_ref": str(target_path.relative_to(repo_root)) if target_path.is_relative_to(repo_root) else str(target_path),
    }


def _reopen_dir(repo_root: Path, orchestration_id: str) -> Path:
    return _orchestration_root(repo_root, orchestration_id) / "reopen"


def _superseded_runs_path(repo_root: Path, orchestration_id: str) -> Path:
    return _reopen_dir(repo_root, orchestration_id) / "superseded_runs.json"


def _reopen_log_path(repo_root: Path, orchestration_id: str) -> Path:
    return _reopen_dir(repo_root, orchestration_id) / "reopen_log.jsonl"


def _load_superseded_run_ids(repo_root: Path, orchestration_id: str) -> set[str]:
    """The set of step/substep agent_run_ids tombstoned by a `reopen-phase` call.

    A superseded run is a prior cross-phase-retry attempt for a reopened phase: it
    is exempt from the `_validate_orchestration_completion_for_pass` terminal/vouch
    requirements because its `step_result.json` was archived aside and a fresh
    attempt now vouches the phase. Returns an empty set when no reopen has occurred
    (tolerant of a missing / malformed file — a corrupt tombstone must never wedge
    the completion check, only widen the vouch requirement back to every run).
    """
    path = _superseded_runs_path(repo_root, orchestration_id)
    if not path.exists():
        return set()
    try:
        data = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, dict):
        ids = data.get("superseded_agent_run_ids")
    else:
        ids = data
    if not isinstance(ids, list):
        return set()
    return {s.strip() for s in ids if isinstance(s, str) and s.strip()}


def reopen_phase(
    repo_root: Path,
    orchestration_id: str,
    *,
    node_key: str,
    from_phase: str,
    reason: str,
    trigger_agent_run_id: str,
    finding_id: str | None = None,
) -> dict[str, Any]:
    """Reopen a checkpointed-pass phase and everything downstream for a node.

    Makes the decision table's cross-phase retry (`structural_violation`/`ir` ->
    Compile, or `Generate.verify` `ir_inconsistency` -> Compile) executable in
    place. Once Compile has produced a `pass` step_result + checkpoint entry, the
    retry cannot be expressed: `check_step_completed` keys "done" on artifact-hash
    integrity (a stale-but-intact IR reads `integrity=ok`), the phase sits at
    `step_result_written` (not the `child_finished` the write path needs), and
    `retry_decisions` only models within-step substep retries. This invalidates the
    `from_phase` and all downstream phases so the orchestration agent re-runs
    `Compile -> Generate -> Build -> Validate` against a corrected IR.

    Temporal-cut supersede model (mirrors the orphan-tombstone precedent): every
    step/substep run recorded for the reopened phases *before* this call is snapshot
    into `reopen/superseded_runs.json` and exempted from the completion-vouch
    requirement; every run created afterwards is the new attempt, vouched normally.

    Operations (idempotent): (1) validate the trigger is a recorded terminal
    non-pass substep/step strictly downstream of `from_phase`; (2) snapshot the
    superseded runs + append `reopen/reopen_log.jsonl`; (3) archive each affected
    `step_result.json` to `step_result.superseded.<seq>.json` (drops it from the
    `_iter_step_result_paths` glob and frees the deterministic executor path);
    (4) drop the affected `completed_steps` checkpoint entries; (5) reset the
    affected `phase_state` node_states to `not_started`.
    """
    _require_preflight_launchable(repo_root, orchestration_id, enforce_live_probe=False)

    from_token = from_phase.strip().lower()
    if from_token not in STEP_KEYS_FOR_NODE_STATE:
        raise RuntimeError(
            f"reopen-phase: unsupported --from-phase {from_phase!r}; expected one of "
            f"{list(STEP_KEYS_FOR_NODE_STATE)}"
        )
    from_idx = STEP_KEYS_FOR_NODE_STATE.index(from_token)
    affected_phases = list(STEP_KEYS_FOR_NODE_STATE[from_idx:])

    node_key_norm = node_key.strip()
    node_safe = _node_key_to_safe(node_key_norm)
    root = _orchestration_root(repo_root, orchestration_id)
    runs = _load_run_records(root)

    # Validate the trigger: a real terminal non-pass step/substep run for this node,
    # whose phase is strictly downstream of `from_phase`. This is the anti-abuse gate
    # — reopen must never erase a genuinely-passing pipeline; it can only follow a
    # downstream phase that actually failed and attributed back to `from_phase`
    # (mirrors the Compile-retry launch contract in phase_04_validate.md).
    trigger_arid = trigger_agent_run_id.strip()
    trigger = runs.get(trigger_arid)
    trigger_from_invalid_log = False
    if not isinstance(trigger, dict):
        # Recovery path: a downstream phase whose failure mode *is* an
        # unauthorized write never reaches `agent_runs.jsonl` — `record_agent_run`
        # diverts its terminal `fail` payload to `agent_runs_invalid.jsonl` and
        # re-raises (so the orchestration fail_closes). Without a usable trigger
        # the defective upstream phase can never be invalidated and resume
        # dead-locks (`resume_reopen_no_valid_trigger`). Accept the diverted entry
        # — but ONLY when a `violations/<arid>.unauthorized_write_violation.json`
        # exists, which is the authoritative proof the run terminally failed on an
        # unauthorized write (not a sandbox/identity reject, where reopening an
        # upstream phase would be wrong). This keeps the anti-abuse property: reopen
        # still only follows a genuinely-failed downstream run backed by hard evidence.
        invalid_runs = _load_invalid_run_records(root)
        candidate = invalid_runs.get(trigger_arid)
        violation_path = (
            _violations_dir(repo_root, orchestration_id)
            / f"{trigger_arid}.unauthorized_write_violation.json"
        )
        if isinstance(candidate, dict) and violation_path.is_file():
            trigger = candidate
            trigger_from_invalid_log = True
    if not isinstance(trigger, dict):
        raise RuntimeError(
            f"reopen-phase: --trigger-agent-run-id {trigger_agent_run_id!r} not found in "
            f"agent_runs.jsonl (nor as an unauthorized-write reject in agent_runs_invalid.jsonl "
            f"with a matching violation file)"
        )
    trig_role = str(trigger.get("agent_role") or "").strip().lower()
    if trig_role not in {"step", "substep"}:
        raise RuntimeError(
            f"reopen-phase: trigger {trigger_agent_run_id!r} must be a step/substep run (role={trig_role!r})"
        )
    trig_node = str(trigger.get("node_key") or "").strip()
    if trig_node != node_key_norm:
        raise RuntimeError(
            f"reopen-phase: trigger node_key {trig_node!r} does not match --node-key {node_key_norm!r}"
        )
    trig_step = str(trigger.get("step") or "").strip().lower()
    if trig_step not in STEP_KEYS_FOR_NODE_STATE or STEP_KEYS_FOR_NODE_STATE.index(trig_step) <= from_idx:
        raise RuntimeError(
            f"reopen-phase: trigger phase {trig_step!r} must be strictly downstream of "
            f"--from-phase {from_token!r}"
        )
    trig_status = str(trigger.get("status") or "").strip().lower()
    if trig_status not in TERMINAL_STATUSES or trig_status == "pass":
        raise RuntimeError(
            f"reopen-phase: trigger {trigger_agent_run_id!r} must be a terminal non-pass run "
            f"(status={trig_status!r}); refuse to reopen a passing pipeline"
        )

    existing = _load_superseded_run_ids(repo_root, orchestration_id)

    # Idempotency guard. A redundant re-invocation carries a trigger that a prior
    # reopen already superseded; re-snapshotting would tombstone the in-progress
    # fresh attempt's runs (recorded after that reopen) and archive their new
    # `step_result.json`, discarding retry progress. No-op in that case. A genuine
    # *subsequent* reopen — the fresh attempt itself failed again and attributed
    # back to `from_phase` — carries a NEW, not-yet-superseded trigger and proceeds
    # (correctly superseding the fresh attempt and starting another).
    # `superseded_runs.json` is written LAST, as the atomic commit marker: the
    # trigger appears in the snapshot (it is a downstream substep of an affected
    # phase), so `trigger in existing` holds only once a prior reopen fully
    # completed. A reopen interrupted before the marker (checkpoint still stale,
    # phase_state not reset) therefore leaves the trigger NOT superseded, so this
    # guard does not fire and the retry re-runs the remaining (idempotent) cleanup.
    if trigger_agent_run_id.strip() in existing:
        return {
            "status": "noop",
            "orchestration_id": orchestration_id,
            "node_key": node_key_norm,
            "from_phase": from_token,
            "affected_phases": affected_phases,
            "reason": "trigger already superseded by a fully-applied prior reopen; nothing to reopen",
            "superseded_run_count": len(existing),
        }

    # (1) Snapshot every step/substep run for this node in an affected phase. The
    # temporal cut: these become superseded; anything recorded after this call is
    # the new attempt.
    superseded_set = {
        arid
        for arid, rec in runs.items()
        if isinstance(rec, dict)
        and str(rec.get("agent_role") or "").strip().lower() in {"step", "substep"}
        and str(rec.get("node_key") or "").strip() == node_key_norm
        and str(rec.get("step") or "").strip().lower() in affected_phases
    }
    # An invalid-log trigger lives in `agent_runs_invalid.jsonl`, not `runs`, so the
    # snapshot above never captures it. Add it explicitly: otherwise the idempotency
    # guard (`trigger in existing`) could never fire for this trigger, and a
    # redundant re-invocation would re-archive the in-progress fresh attempt's
    # step_result — the very tombstoning the guard exists to prevent.
    if trigger_from_invalid_log:
        superseded_set.add(trigger_arid)
    superseded_now = sorted(superseded_set)

    log_path = _reopen_log_path(repo_root, orchestration_id)
    prior_reopens = 0
    if log_path.exists():
        prior_reopens = sum(
            1 for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    reopen_seq = prior_reopens + 1

    # (2) Archive the affected step_results aside so they drop out of the vouch glob
    # and free the deterministic executor path for the new write. Idempotent on a
    # post-crash retry: the canonical files are already renamed, so the glob is empty.
    archived: list[str] = []
    for phase in affected_phases:
        phase_dir = root / "steps" / node_safe / phase
        if not phase_dir.exists():
            continue
        for result_path in sorted(phase_dir.glob("*/step_result.json")):
            archived_path = result_path.with_name(f"step_result.superseded.{reopen_seq}.json")
            result_path.rename(archived_path)
            archived.append(
                str(archived_path.relative_to(repo_root))
                if archived_path.is_relative_to(repo_root)
                else str(archived_path)
            )

    # (3) Drop the affected checkpoint entries so check_step_completed re-runs them.
    checkpoint = _load_checkpoint(repo_root, orchestration_id)
    dropped_checkpoint_steps: list[str] = []
    if isinstance(checkpoint, dict):
        steps = checkpoint.get("completed_steps")
        if isinstance(steps, list):
            kept = []
            for entry in steps:
                if (
                    isinstance(entry, dict)
                    and entry.get("node_key") == node_key_norm
                    and str(entry.get("step") or "").strip().lower() in affected_phases
                ):
                    dropped_checkpoint_steps.append(str(entry.get("step")))
                    continue
                kept.append(entry)
            if len(kept) != len(steps):
                checkpoint["completed_steps"] = kept
                checkpoint["last_updated_at"] = _utc_now_iso()
                _write_json(_checkpoint_path(repo_root, orchestration_id), checkpoint)

    # (4) Reset the affected phase_state node_states so the phases are re-runnable.
    for phase in affected_phases:
        _transition_node_step_phase_state(
            repo_root,
            orchestration_id,
            node_key=node_key_norm,
            step=phase,
            new_state="not_started",
            event="reopen_phase",
            agent_run_id=trigger_agent_run_id.strip(),
        )

    # (5) Append the audit log, then commit by writing the superseded set LAST. Only
    # after this does `trigger in existing` hold, gating the no-op above on full
    # completion of steps (1)-(4).
    log_record = {
        "at": _utc_now_iso(),
        "reopen_seq": reopen_seq,
        "node_key": node_key_norm,
        "from_phase": from_token,
        "affected_phases": affected_phases,
        "reason": reason,
        "trigger_agent_run_id": trigger_agent_run_id.strip(),
        "trigger_source": "agent_runs_invalid" if trigger_from_invalid_log else "agent_runs",
        "finding_id": finding_id,
        "superseded_agent_run_ids": superseded_now,
        "archived_step_results": archived,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(log_record, ensure_ascii=False) + "\n")

    merged_ids = sorted(existing | set(superseded_now))
    _write_json(
        _superseded_runs_path(repo_root, orchestration_id),
        {
            "orchestration_id": orchestration_id,
            "superseded_agent_run_ids": merged_ids,
        },
    )

    return {
        "status": "reopened",
        "orchestration_id": orchestration_id,
        "node_key": node_key_norm,
        "from_phase": from_token,
        "affected_phases": affected_phases,
        "reopen_seq": reopen_seq,
        "trigger_source": "agent_runs_invalid" if trigger_from_invalid_log else "agent_runs",
        "superseded_run_count": len(superseded_now),
        "archived_step_results": archived,
        "dropped_checkpoint_steps": dropped_checkpoint_steps,
        "next_action": f"relaunch from {from_token}",
    }


def repair_all_step_result_executors(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """Best-effort resume self-heal: repair every corrupt substep-aware step_result.

    Scans all `step_result.json` under substep-aware phases and, for each whose
    executor arid is not the orchestration arid (either the directory key or the
    `executor_agent_run_id` field), invokes `repair_step_result_executor`. Used by
    `init --resume-from-checkpoint` so a resume self-heals the
    `validate_pre_judge_step_result_executor_integrity` lock instead of dead-ending
    on a fresh-orchestration requirement. Idempotent and non-fatal: a repair that
    cannot be safely performed is recorded under `skipped`, never raised.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    runs = _load_run_records(root)
    orch_arid: str | None = None
    try:
        meta = _read_json(root / "orchestration_meta.json")
    except (OSError, json.JSONDecodeError):
        meta = None
    if isinstance(meta, dict):
        cand = meta.get("orchestration_agent_run_id")
        if isinstance(cand, str) and cand.strip():
            orch_arid = cand.strip()
    repaired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if orch_arid is None:
        return {"status": "noop", "reason": "orchestration arid unresolved", "repaired": [], "skipped": []}

    # node_safe -> node_key, from any run row carrying a node_key. The corrupt
    # directory arid is not a reliable node_key source (a field-only mismatch has
    # the orchestration arid as the dir, and the orchestration row has no
    # node_key), so resolve the node from the path's node_safe instead.
    node_key_by_safe: dict[str, str] = {}
    for rec in runs.values():
        if not isinstance(rec, dict):
            continue
        nk = str(rec.get("node_key") or "").strip()
        if not nk:
            continue
        try:
            node_key_by_safe.setdefault(_node_key_to_safe(nk), nk)
        except (ValueError, RuntimeError):
            continue

    seen: set[tuple[str, str]] = set()
    for path in _iter_step_result_paths(root):
        # steps/<node_safe>/<step>/<arid>/step_result.json
        dir_arid = path.parent.name
        step_token = path.parent.parent.name
        node_safe = path.parent.parent.parent.name
        if step_token not in SUBSTEP_AWARE_STEPS:
            continue
        corrupt = dir_arid != orch_arid
        doc: Any = None
        if not corrupt:
            try:
                doc = _read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            field = doc.get("executor_agent_run_id") if isinstance(doc, dict) else None
            corrupt = not (isinstance(field, str) and field.strip() == orch_arid)
        if not corrupt:
            continue
        # Resolve node_key from the path's node_safe; fall back to a substep listed
        # in the corrupt step_result (covers the field-only case where dir_arid is
        # the orchestration arid). Never rely on the dir_arid run row alone.
        node_key = node_key_by_safe.get(node_safe, "")
        if not node_key:
            if doc is None:
                try:
                    doc = _read_json(path)
                except (OSError, json.JSONDecodeError):
                    doc = None
            if isinstance(doc, dict):
                for sid in doc.get("substep_agent_run_ids") or []:
                    srec = runs.get(sid) if isinstance(sid, str) else None
                    cand = str(srec.get("node_key") or "").strip() if isinstance(srec, dict) else ""
                    if cand:
                        node_key = cand
                        break
        if not node_key:
            skipped.append({"step": step_token, "node_safe": node_safe, "dir_arid": dir_arid, "error": "node_key unresolved"})
            continue
        key = (node_key, step_token)
        if key in seen:
            continue
        seen.add(key)
        try:
            repaired.append(
                repair_step_result_executor(
                    repo_root,
                    orchestration_id,
                    node_key=node_key,
                    step=step_token,
                )
            )
        except (ValueError, RuntimeError) as exc:
            skipped.append({"node_key": node_key, "step": step_token, "error": str(exc)})

    actionable = [r for r in repaired if r.get("status") == "repaired"]
    return {
        "status": "repaired" if actionable else "noop",
        "repaired": repaired,
        "skipped": skipped,
    }


def _rewrite_orchestration_run_row(
    repo_root: Path,
    orchestration_id: str,
    *,
    should_rewrite: Callable[[dict[str, Any]], bool],
    apply_mutation: Callable[[dict[str, Any]], None],
) -> bool:
    """Rewrite the orchestration's own agent_runs.jsonl row in place.

    `init` appends a `{agent_role:orchestration, status:running}` row but
    agent_runs.jsonl is append-only and a second `record-agent-run` for the same
    arid is rejected as `duplicate agent_run_id`, so there is no forward path to
    update it. This helper rewrites the single row matching the meta's
    `orchestration_agent_run_id` with `agent_role == "orchestration"` IN PLACE
    (not append) under the agent_runs.jsonl exclusive lock, mirroring
    `repair_legacy_agent_runs`' atomic rewrite, so it never trips the duplicate
    guard. `should_rewrite` decides whether the matched row needs changing (for
    idempotency); `apply_mutation` mutates it. Returns True iff a row was rewritten.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    runs_path = root / "agent_runs.jsonl"
    if not runs_path.is_file():
        return False

    orch_arid: str | None = None
    try:
        meta = _read_json(root / "orchestration_meta.json")
    except (OSError, json.JSONDecodeError):
        meta = None
    if isinstance(meta, dict):
        v = meta.get("orchestration_agent_run_id")
        if isinstance(v, str) and v.strip():
            orch_arid = v.strip()
    if orch_arid is None:
        return False

    changed = False
    with _runs_jsonl_exclusive_lock(repo_root, orchestration_id):
        raw = runs_path.read_text(encoding="utf-8")
        ends_nl = raw.endswith("\n")
        raw_lines = raw.splitlines()
        for idx, line in enumerate(raw_lines):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if (
                str(obj.get("agent_run_id", "")).strip() == orch_arid
                and str(obj.get("agent_role", "")).strip() == "orchestration"
                and should_rewrite(obj)
            ):
                apply_mutation(obj)
                raw_lines[idx] = json.dumps(obj, ensure_ascii=False)
                changed = True
                break
        if changed:
            body = "\n".join(raw_lines)
            if ends_nl:
                body += "\n"
            _atomic_write_text(runs_path, body)
    return changed


def _sync_orchestration_session_index_status(
    repo_root: Path,
    orchestration_id: str,
    status: str,
) -> bool:
    """Reconcile the orchestration's own session_run_index.json row to `status`.

    The session index orchestration row is seeded `running` by `init` and — unlike
    the agent_runs.jsonl row, which `_finalize_orchestration_run_row` rewrites — had
    no forward path to a terminal status, leaving session-index-based audits seeing
    the orchestration `running` even after `orchestration_meta.json` reached
    `pass`/`fail`/`fail_closed`. Called alongside `_finalize_orchestration_run_row`
    (terminal) and `_reopen_orchestration_run_row` (`running`, resume inverse) so both
    audit surfaces stay in sync at every call site. Idempotent: a no-op once the row
    already equals `status`, so same-terminal set-status replays do not rewrite it
    (preserving `updated_at`). Returns True iff the row was updated.
    """
    root = _orchestration_root(repo_root, orchestration_id)
    orch_arid: str | None = None
    try:
        meta = _read_json(root / "orchestration_meta.json")
    except (OSError, json.JSONDecodeError):
        meta = None
    if isinstance(meta, dict):
        v = meta.get("orchestration_agent_run_id")
        if isinstance(v, str) and v.strip():
            orch_arid = v.strip()
    if orch_arid is None:
        return False
    target = status.strip().lower()
    doc = _read_session_run_index(repo_root, orchestration_id)
    entries = doc.get("entries")
    if isinstance(entries, list):
        for item in entries:
            if (
                isinstance(item, dict)
                and str(item.get("agent_run_id", "")).strip() == orch_arid
                and str(item.get("status", "")).strip().lower() == target
            ):
                return False
    _append_session_run_index_entry(
        repo_root,
        orchestration_id,
        agent_run_id=orch_arid,
        agent_session_id=orch_arid,
        context_id=orch_arid,
        agent_role="orchestration",
        status=status,
    )
    return True


def _finalize_orchestration_run_row(
    repo_root: Path,
    orchestration_id: str,
    *,
    status: str,
    finished_at: str | None = None,
) -> bool:
    """Terminalize the orchestration's own agent_runs.jsonl row.

    Called on terminal `set-status` so agent_runs-based audits and
    `validate_workspace_root` no longer see the orchestration row stuck `running`
    even after `orchestration_meta.json` reaches `pass`/`fail`/`fail_closed`.
    Rewrites whenever the row's status differs from `status`, so it (a)
    terminalizes a `running` row, (b) follows the permitted `fail -> fail_closed`
    promotion (the row was already rewritten to `fail` by the first set-status),
    and (c) repairs a row left `running` by a half-committed prior finalize on
    replay. Idempotent: a no-op once the row already equals `status`, so
    same-terminal set-status replays do not rewrite it (preserving `finished_at`).
    Only invoked after update_orchestration_status' own transition guard accepts
    the meta status change, so a non-`running`→terminal rewrite here only ever
    reflects the allowed `fail -> fail_closed` case. Returns True iff the row was
    transitioned. See `_reopen_orchestration_run_row` for the resume inverse.
    """
    finished = (
        finished_at if isinstance(finished_at, str) and finished_at.strip() else _utc_now_iso()
    )

    def _mutate(obj: dict[str, Any]) -> None:
        obj["status"] = status
        obj["finished_at"] = finished

    row_changed = _rewrite_orchestration_run_row(
        repo_root,
        orchestration_id,
        should_rewrite=lambda obj: str(obj.get("status", "")).strip() != status,
        apply_mutation=_mutate,
    )
    # Mirror the terminalization into session_run_index.json (idempotent), so
    # session-index-based audits do not see the orchestration row stuck `running`.
    _sync_orchestration_session_index_status(repo_root, orchestration_id, status)
    return row_changed


def _reopen_orchestration_run_row(
    repo_root: Path,
    orchestration_id: str,
) -> bool:
    """Re-open the orchestration's own agent_runs.jsonl row for a resumed run.

    Inverse of `_finalize_orchestration_run_row`, symmetric with
    `enable_checkpoint_resume` resetting `orchestration_meta.status` to `running`
    and dropping the cleanup_committed marker. When a terminal orchestration is
    resumed, its agent_runs row (terminalized in place by the prior set-status)
    must also be reset to `running` with `finished_at` cleared. Otherwise the live
    resumed run carries a stale terminal row, which (a) lets
    `validate_workspace_root` treat the live orchestration tmp root as
    cleanup-pending/terminated after the TTL, and (b) leaves the resumed run's
    eventual set-status unable to re-terminalize it (the finalizer only matches
    `running` rows). Targets only a non-`running` row (idempotent). Returns True
    iff the row was reset.
    """

    def _mutate(obj: dict[str, Any]) -> None:
        obj["status"] = "running"
        obj.pop("finished_at", None)

    row_changed = _rewrite_orchestration_run_row(
        repo_root,
        orchestration_id,
        should_rewrite=lambda obj: str(obj.get("status", "")).strip() != "running",
        apply_mutation=_mutate,
    )
    # Symmetric reset of the session_run_index.json orchestration row (idempotent).
    _sync_orchestration_session_index_status(repo_root, orchestration_id, "running")
    return row_changed


def update_orchestration_status(
    repo_root: Path,
    orchestration_id: str,
    *,
    status: str,
    reason_code: str | None = None,
    reason_detail: str | None = None,
    blocking_policy_scope: str | None = None,
) -> dict[str, Any]:
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(meta_path)
    # F3: serialize the full read-check-write-cleanup-marker section. Without this
    # lock, two concurrent terminalizers can both observe a non-terminal status,
    # both pass the same-terminal guard below, and race the meta write — the
    # final reason_code/reason_detail becomes nondeterministic.
    with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
        meta = _read_json(meta_path)
        if not isinstance(meta, dict):
            raise ValueError(f"invalid orchestration_meta.json: {meta_path}")
        current_status_pre = meta.get("status")
        is_same_terminal_replay = (
            isinstance(current_status_pre, str)
            and current_status_pre in IDEMPOTENT_TERMINAL_STATUSES
            and status == current_status_pre
        )
        # Codex round 15 F1: status-specific preconditions (pass needs
        # preflight launchability + completion validation; fail_closed needs
        # reason_code) MUST run only for FORWARD transitions. A same-terminal
        # replay reflects a defensive retry after the first call already
        # committed — re-running pass validation can fail on expired preflight
        # or drifted workspace even though the orchestration is already done.
        if not is_same_terminal_replay:
            if status == "pass":
                _require_preflight_launchable(
                    repo_root,
                    orchestration_id,
                    enforce_live_probe=False,
                )
                _validate_orchestration_completion_for_pass(repo_root, orchestration_id)
            if status == "fail_closed":
                if not isinstance(reason_code, str) or not reason_code.strip():
                    raise ValueError("set-status fail_closed requires non-empty reason_code")
                if reason_code.strip() not in FAIL_CLOSED_REASON_CODES:
                    raise ValueError(
                        "set-status fail_closed reason_code must be one of "
                        f"{sorted(FAIL_CLOSED_REASON_CODES)}"
                    )
        current_status = meta.get("status")
        orch_arid_obj = meta.get("orchestration_agent_run_id")
        orch_arid = (
            orch_arid_obj.strip()
            if isinstance(orch_arid_obj, str) and orch_arid_obj.strip()
            else None
        )
        if isinstance(current_status, str) and current_status in IDEMPOTENT_TERMINAL_STATUSES:
            if status == current_status:
                # F2: same-terminal re-invocation is the only built-in recovery
                # path for a half-committed terminalization. Two-phase commit:
                # (a) terminal meta written, (b) tmp cleaned, (c) committed
                # marker. If (b) or (c) failed previously, the cleanup_committed
                # marker is missing — accept the call as a cleanup-retry that
                # touches ONLY the cleanup/marker phase. Narrative fields stay
                # frozen at their first-write values; failure_analysis.json is
                # the canonical place for additional context.
                marker_present = False
                if orch_arid is not None:
                    marker_present = _cleanup_committed_marker_path(
                        repo_root, orchestration_id, orch_arid
                    ).is_file()
                if marker_present:
                    # F1 (Codex round 4): fully-committed same-terminal replay is a
                    # safe no-op. Defensive retries after ambiguous network/IO
                    # failures (the caller may have lost the response even though
                    # the first call succeeded) must not error — the system is
                    # already in the requested state. Narrative updates still
                    # belong in failure_analysis.json (orchestration agent's
                    # allowed_file_tool_path), but reissuing set-status with the
                    # same status returns the existing meta unchanged.
                    #
                    # Codex round 18 F2: if the canonical `set_status` audit
                    # event for this terminal status is missing (e.g., the
                    # original forward-transition call committed meta + marker
                    # but its log append failed), backfill it from the persisted
                    # meta state before recording the no-op replay. Otherwise
                    # the canonical transition record stays lost forever.
                    if not _phase_state_log_has_set_status(
                        repo_root, orchestration_id, status,
                    ):
                        _append_phase_state_log(
                            repo_root,
                            orchestration_id,
                            {
                                "ts": _utc_now_iso(),
                                "event": "set_status",
                                "to": status,
                                "reason_code": meta.get("reason_code"),
                                "reason_detail": meta.get("reason_detail"),
                                "blocking_policy_scope": meta.get("blocking_policy_scope"),
                                "detected_at": meta.get("detected_at"),
                                "backfilled": True,
                                "backfill_reason": "missing_canonical_set_status_event",
                            },
                        )
                    # Idempotent backfill: terminalize the orchestration's
                    # agent_runs row if a pre-fix run left it `running`.
                    _finalize_orchestration_run_row(
                        repo_root,
                        orchestration_id,
                        status=status,
                        finished_at=meta.get("finished_at"),
                    )
                    _append_phase_state_log(
                        repo_root,
                        orchestration_id,
                        {
                            "ts": _utc_now_iso(),
                            "event": "set_status_noop_replay",
                            "to": status,
                        },
                    )
                    return meta
                # Cleanup retry path: re-run only the destructive cleanup and
                # marker write. Do NOT mutate meta narrative fields.
                # Codex round 18 F2: also backfill the canonical set_status
                # event if it's missing (original call may have failed before
                # logging).
                _finalize_orchestration_run_row(
                    repo_root,
                    orchestration_id,
                    status=status,
                    finished_at=meta.get("finished_at"),
                )
                if orch_arid is not None:
                    cleanup_done = _cleanup_agent_tmp_root(
                        repo_root,
                        orchestration_id,
                        agent_run_id=orch_arid,
                    )
                    if cleanup_done:
                        _write_cleanup_committed_marker(
                            repo_root, orchestration_id, agent_run_id=orch_arid,
                        )
                if not _phase_state_log_has_set_status(
                    repo_root, orchestration_id, status,
                ):
                    _append_phase_state_log(
                        repo_root,
                        orchestration_id,
                        {
                            "ts": _utc_now_iso(),
                            "event": "set_status",
                            "to": status,
                            "reason_code": meta.get("reason_code"),
                            "reason_detail": meta.get("reason_detail"),
                            "blocking_policy_scope": meta.get("blocking_policy_scope"),
                            "detected_at": meta.get("detected_at"),
                            "backfilled": True,
                            "backfill_reason": "missing_canonical_set_status_event",
                        },
                    )
                _append_phase_state_log(
                    repo_root,
                    orchestration_id,
                    {
                        "ts": _utc_now_iso(),
                        "event": "set_status_cleanup_retry",
                        "to": status,
                    },
                )
                return meta
            if not (current_status == "fail" and status == "fail_closed"):
                raise ValueError(
                    "terminal-to-terminal status transition rejected: "
                    f"'{current_status}' -> '{status}'. Only 'fail' -> 'fail_closed' is permitted."
                )
        meta["status"] = status
        if isinstance(reason_code, str) and reason_code.strip():
            meta["reason_code"] = reason_code.strip()
        if isinstance(reason_detail, str) and reason_detail.strip():
            meta["reason_detail"] = reason_detail.strip()
        if isinstance(blocking_policy_scope, str) and blocking_policy_scope.strip():
            meta["blocking_policy_scope"] = blocking_policy_scope.strip()
        if status == "fail_closed":
            meta["detected_at"] = _utc_now_iso()
        if status in TERMINAL_STATUSES:
            meta["finished_at"] = _utc_now_iso()
        if status == "fail_closed":
            meta["finished_at"] = _utc_now_iso()
        # Adv-35: write the durable terminal status FIRST, then cleanup tmp,
        # then write the cleanup_committed marker. Validator's exemption-revoke
        # decision requires BOTH the terminal meta status AND the committed
        # marker (Adv-35 two-phase commit), so a partial failure between meta
        # write and cleanup keeps orch tmp scratch exempt for diagnostics
        # rather than orphaning recovery artifacts.
        _write_json(meta_path, meta)
        if status in TERMINAL_STATUSES or status == "fail_closed":
            # Terminalize the orchestration's own agent_runs.jsonl row in place so
            # agent_runs-based audits no longer see it stuck `running`. Done before
            # tmp cleanup; the append-only duplicate guard is bypassed by rewrite.
            _finalize_orchestration_run_row(
                repo_root,
                orchestration_id,
                status=status,
                finished_at=meta.get("finished_at"),
            )
            if orch_arid is not None:
                cleanup_done = _cleanup_agent_tmp_root(
                    repo_root,
                    orchestration_id,
                    agent_run_id=orch_arid,
                )
                # Adv-36: only publish committed marker on confirmed cleanup.
                if cleanup_done:
                    _write_cleanup_committed_marker(
                        repo_root, orchestration_id, agent_run_id=orch_arid,
                    )
        # Codex round 21 F1: build the audit event from the COMMITTED meta
        # rather than raw call arguments. Raw args bypass the .strip()
        # normalization and the "preserve existing if unchanged" logic above,
        # so the log could record a different reason_code/reason_detail than
        # the persisted state. Reading back from `meta` ensures the audit
        # trail matches the canonical orchestration_meta.json and that the
        # forward write has the same shape as the replay backfill (which
        # also sources from meta).
        _append_phase_state_log(
            repo_root,
            orchestration_id,
            {
                "ts": _utc_now_iso(),
                "event": "set_status",
                "to": meta.get("status"),
                "reason_code": meta.get("reason_code"),
                "reason_detail": meta.get("reason_detail"),
                "blocking_policy_scope": meta.get("blocking_policy_scope"),
                "detected_at": meta.get("detected_at"),
            },
        )
        return meta


def mark_dependency_readiness(
    repo_root: Path,
    orchestration_id: str,
) -> dict[str, Any]:
    """Re-verify `orchestration_meta.dependency_readiness` from real workspace
    artifacts and overwrite ALL detail flags with the freshly computed values.

    Codex round 6 F2 fix: every call performs a full re-verification of all
    three stages. Selective updates would let stale `true` flags survive
    dependency regressions (an older `ir_ref_verified=true` could persist
    even after a newer ir_meta.json went `verification_status=fail`). By
    overwriting all flags from current artifact state, the persisted booleans
    that `_dependency_ready` consumes always reflect the live workspace.

    The runtime resolves every direct dependency in `<spec_ref>/deps.yaml`
    (via `spec/registry/spec_catalog.yaml` + version_constraint matching),
    then inspects the LATEST (mtime) workspace artifact per stage:

      - ir_ref: latest `workspace/ir/<dep_safe>/*/ir_meta.json` has verification_status=pass
      - pipeline_ref: latest `workspace/pipelines/<dep_safe>/*/binary/*/binary_meta.json` has verification_status=pass
      - aggregate_verdict: latest `workspace/pipelines/<dep_safe>/**/aggregate_verdict.json` has top-level value ∈ {pass, xfail}

    A stage flag is True only when EVERY direct dep passes its per-stage check
    (empty deps → trivially true). Top-level flags derived:
      compile_readiness   = ir_ref_verified
      execution_readiness = ir_ref_verified AND pipeline_ref_verified AND aggregate_verdict_verified

    Also refreshes `dep_set_fingerprint` so subsequent `write_preflight` calls
    can detect deps.yaml / spec_ref churn.

    The CLI is NOT a caller assertion — it triggers runtime artifact
    verification. A caller cannot mark deps "verified" unless the workspace
    actually contains passing artifacts.
    """
    meta_path = _orchestration_root(repo_root, orchestration_id) / "orchestration_meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(meta_path)
    with _orchestration_meta_exclusive_lock(repo_root, orchestration_id):
        meta = _read_json(meta_path)
        if not isinstance(meta, dict):
            raise ValueError(f"invalid orchestration_meta.json: {meta_path}")
        spec_ref = meta.get("spec_ref")
        # Codex round 16 F1: single-pass read so persisted booleans and
        # fingerprint derive from the SAME artifact byte snapshots. Otherwise
        # a producer mutation between a separate verify() and fingerprint()
        # call would persist inconsistent state.
        # Codex round 17 F1+F2: also persist certified_deps (one canonical
        # version per dep) so downstream consumers see the same version that
        # satisfied readiness, and the fingerprint narrows to those versions
        # only (historical-version churn does not invalidate).
        verified, fingerprint_hex, certified_deps, fail_reason = _compute_dep_readiness_and_fingerprint(
            repo_root, spec_ref
        )
        if verified is None:
            # Codex round 8 F2 + round 21 F2: persist fail-closed BEFORE
            # raising and record the SPECIFIC fail_reason so observability
            # tooling can differentiate `deps_yaml_missing_or_unparseable`
            # (file absent / YAML parse failed) from
            # `deps_yaml_malformed_schema` (file parses but schema invalid).
            fail_closed_payload: dict[str, Any] = {
                "direct_dependency_compile_readiness": False,
                "direct_dependency_execution_readiness": False,
                "detail": {
                    "ir_ref_verified": False,
                    "pipeline_ref_verified": False,
                    "aggregate_verdict_verified": False,
                },
                "dep_set_fingerprint": fingerprint_hex,
            }
            meta["dependency_readiness"] = fail_closed_payload
            _write_json(meta_path, meta)
            _append_phase_state_log(
                repo_root,
                orchestration_id,
                {
                    "ts": _utc_now_iso(),
                    "event": "mark_dependency_readiness_failed",
                    "reason": fail_reason or "deps_yaml_missing_or_unparseable",
                    "spec_ref": spec_ref,
                },
            )
            if fail_reason == "deps_yaml_malformed_schema":
                raise ValueError(
                    "mark-dependency-readiness: deps.yaml schema is malformed "
                    f"for spec_ref={spec_ref!r}. Persisted dependency_readiness "
                    "reset to fail-closed. Inspect dependencies.components / "
                    "dependencies.profiles for unknown keys, missing canonical "
                    "lists, or malformed list items, then re-run "
                    "mark-dependency-readiness."
                )
            if fail_reason == "spec_catalog_corrupt":
                # Codex round 33 F2: surface catalog corruption distinctly
                # so wrappers can route this to a repo-wide outage alert
                # rather than treating it like an ordinary dep miss.
                raise ValueError(
                    "mark-dependency-readiness: spec_catalog.yaml is "
                    "corrupt or missing the canonical schema. Persisted "
                    f"dependency_readiness for spec_ref={spec_ref!r} reset "
                    "to fail-closed. This is a repository-wide outage, "
                    "not a normal dependency miss — fix the catalog file "
                    "(top-level `specs:` list) and re-run."
                )
            raise ValueError(
                "mark-dependency-readiness: cannot verify readiness — "
                f"deps.yaml missing or unparseable for spec_ref={spec_ref!r}. "
                "Persisted dependency_readiness reset to fail-closed. "
                "Populate spec_ref in orchestration_meta and ensure deps.yaml exists, "
                "then re-run mark-dependency-readiness."
            )
        detail = {
            "ir_ref_verified": bool(verified.get("ir_ref_verified", False)),
            "pipeline_ref_verified": bool(verified.get("pipeline_ref_verified", False)),
            "aggregate_verdict_verified": bool(verified.get("aggregate_verdict_verified", False)),
        }
        compile_ok = detail["ir_ref_verified"]
        execution_ok = (
            detail["ir_ref_verified"]
            and detail["pipeline_ref_verified"]
            and detail["aggregate_verdict_verified"]
        )
        new_readiness: dict[str, Any] = {
            "direct_dependency_compile_readiness": compile_ok,
            "direct_dependency_execution_readiness": execution_ok,
            "detail": detail,
            "dep_set_fingerprint": fingerprint_hex,
            # Codex round 17 F1: persist the per-dep certified version so
            # downstream lineage / launch-request templating uses the SAME
            # version that satisfied readiness, not a separately-resolved
            # "highest matching version".
            "certified_deps": certified_deps,
        }
        meta["dependency_readiness"] = new_readiness
        _write_json(meta_path, meta)
        _append_phase_state_log(
            repo_root,
            orchestration_id,
            {
                "ts": _utc_now_iso(),
                "event": "mark_dependency_readiness",
                "verified": verified,
                "detail": detail,
                "direct_dependency_compile_readiness": compile_ok,
                "direct_dependency_execution_readiness": execution_ok,
            },
        )
        return new_readiness


def reserve_phase_root(
    repo_root: Path,
    *,
    orchestration_id: str,
    node_key: str,
    step: str,
    reserved_id: str,
    reserved_by_agent_run_id: str,
) -> dict[str, Any]:
    step_key = step.strip().lower()
    _required_child_agent_kind(step_key)
    # Codex round 32 F2: validate `reserved_id` against `_SLUG_DATE_SEQ3_PATTERN`
    # at the canonical reservation entrypoint. Round-31 tightened the
    # freshness reader to this same grammar but left the writer side
    # unenforced — a workflow could reserve a non-canonical id, write
    # valid artifacts under it, and then have downstream
    # `dependency_readiness` silently ignore them. Enforcing the same
    # grammar here means the trust boundary is symmetric: only IDs that
    # readers will honor can be issued in the first place.
    reserved_id_clean = reserved_id.strip()
    if not _SLUG_DATE_SEQ3_PATTERN.match(reserved_id_clean):
        raise ValueError(
            f"reserve-phase-root: reserved_id={reserved_id_clean!r} must match "
            f"<slug>_<YYYYMMDD>_<seq3> (lowercase hyphen-separated slug + "
            "8-digit date + 3-digit sequence). The freshness selector "
            "rejects non-canonical IDs, so an artifact written under this "
            "id would be invisible to downstream dependency_readiness."
        )
    node_safe = _node_key_to_safe(node_key.strip())
    out = (
        _orchestration_root(repo_root, orchestration_id)
        / "reservations"
        / node_safe
        / f"{step_key}.json"
    )
    payload = {
        "node_key": node_key.strip(),
        "step": step_key,
        "reserved_ir_id": reserved_id_clean,
        "reserved_by_agent_run_id": reserved_by_agent_run_id.strip(),
        "status": "reserved",
        "reserved_at": _utc_now_iso(),
    }
    _write_json(out, payload)
    return payload


def _json_arg(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("json payload must be object")
    return value


def _json_string_list_arg(raw: str) -> list[str]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise argparse.ArgumentTypeError("json payload must be array of strings")
    return [x for x in value if x.strip()]


def _select_patch_strip(
    repo_root: Path,
    patch_text: str,
    normalized_paths: list[str],
) -> tuple[int, list[str]]:
    """Determine the correct -p<strip> level using changed_paths as the disambiguation oracle.

    Runs 'git apply --numstat' with strip=1 (standard git format) then strip=0 (bare paths),
    selecting the first level whose numstat targets are all covered by normalized_paths.

    Heuristic pattern-matching on 'diff --git a/<X> b/<X>' headers is fundamentally ambiguous:
    those headers are identical whether the patch is git-prefix format (target=<X>, strip=1)
    or a bare rename between real directories 'a/' and 'b/' (target='b/<X>', strip=0).
    Using changed_paths as the oracle resolves this without false assumptions.

    Returns (strip, numstat_targets). Raises RuntimeError if neither strip level produces
    targets covered by changed_paths (includes mixed-prefix and out-of-scope patches).
    """
    # Collect, per strip level, the actual numstat outcome so a rejection tells the
    # author exactly what git would touch vs. what was declared — turning a blind
    # re-author loop (observed costing many turns in validate substeps) into a
    # one-shot fix.
    diagnostics: list[str] = []
    for strip in (1, 0):
        try:
            numstat = _numstat_targets(repo_root, patch_text, strip)
        except RuntimeError as exc:
            diagnostics.append(f"-p{strip}: numstat rejected the patch ({exc})")
            continue
        if not numstat:
            diagnostics.append(f"-p{strip}: numstat produced no targets")
            continue
        uncovered = [
            p
            for p in numstat
            if not any(_repo_path_under_prefix(p, cp) for cp in normalized_paths)
        ]
        if not uncovered:
            return strip, numstat
        diagnostics.append(
            f"-p{strip}: git would touch {numstat}; not covered by changed_paths: {uncovered}"
        )
    raise RuntimeError(
        "guarded-apply-patch: cannot determine patch strip level — "
        "neither -p1 nor -p0 produces targets covered by declared changed_paths "
        "(patch may have mixed prefixes or targets outside changed_paths). "
        f"declared changed_paths={normalized_paths}. "
        + " | ".join(diagnostics)
        + ". Fix: make each patch's '+++ b/<path>' header (after -p1 strip) exactly "
        "match a declared changed_paths entry, declare every touched path, and keep "
        "one file per patch."
    )


def _extract_patch_target_paths(patch_text: str, strip: int = 1) -> list[str]:
    targets: list[str] = []
    for raw in patch_text.splitlines():
        line = raw.strip()
        if not line.startswith("+++ "):
            continue
        token = line[4:].strip()
        if token == "/dev/null":
            continue
        if strip == 1 and token.startswith("b/"):
            token = token[2:]
        if ".." in token.split("/"):
            raise RuntimeError(
                f"guarded-apply-patch: patch path traversal detected: {token!r}"
            )
        norm = _normalize_rel_posix(token)
        if norm:
            targets.append(norm)
    return sorted(set(targets))


def _extract_rename_sources(patch_text: str) -> list[str]:
    """Return the source paths of all 'rename from' directives in the patch.

    Rename operations delete the source file, which is a destructive side-effect that
    must be authorized independently from the destination path.  'rename from' lines
    use raw repo-relative paths (no a/b/ prefix) regardless of the strip level, so no
    strip logic is needed here.  Copy sources are intentionally excluded: 'copy from'
    leaves the source intact and only creates a new destination file.
    """
    sources: list[str] = []
    for raw in patch_text.splitlines():
        line = raw.strip()
        if not line.startswith("rename from "):
            continue
        token = line[len("rename from "):].strip()
        if not token:
            continue
        norm = _normalize_rel_posix(token)
        if norm:
            sources.append(norm)
    return sorted(set(sources))


def _numstat_targets(repo_root: Path, patch_text: str, strip: int) -> list[str]:
    """Dry-run git apply --numstat -z to enumerate the paths git will actually touch.

    Uses -z so that git outputs NUL-terminated raw byte paths instead of quoting/escaping
    filenames that contain tabs, newlines, double-quotes, or backslashes.  With -z, each
    record is '<added>\\t<deleted>\\t<dest-path>\\0'.  For renames git outputs the
    destination path only (the file that will exist after apply), which is what we need.
    """
    proc = subprocess.run(
        ["git", "apply", "--numstat", "-z", "--check", f"-p{strip}", "-"],
        cwd=str(repo_root),
        input=patch_text.encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raw_err = proc.stderr or proc.stdout or b""
        if isinstance(raw_err, str):
            raw_err = raw_err.encode()
        msg = raw_err.decode(errors="replace").strip()
        raise RuntimeError(f"guarded-apply-patch: pre-apply numstat failed: {msg}")
    raw_out = proc.stdout if isinstance(proc.stdout, bytes) else (proc.stdout or "").encode()
    targets: list[str] = []
    for record in raw_out.split(b"\0"):
        if not record:
            continue
        parts = record.split(b"\t", 2)
        if len(parts) < 3:
            continue
        path_bytes = parts[2]
        try:
            path_str = path_bytes.decode()
        except UnicodeDecodeError:
            path_str = path_bytes.decode(errors="replace")
        norm = _normalize_rel_posix(path_str)
        if norm:
            targets.append(norm)
    return sorted(set(targets))


def guarded_apply_patch(
    repo_root: Path,
    *,
    orchestration_id: str,
    actor_role: str,
    agent_run_id: str,
    changed_paths: Sequence[str],
    patch_text: str,
    capability_token: str,
) -> dict[str, Any]:
    if not patch_text.strip():
        raise ValueError("patch_text must be non-empty")
    normalized_paths = [_normalize_rel_posix(p) for p in changed_paths if str(p).strip()]
    if not normalized_paths:
        raise ValueError("changed_paths must be non-empty")

    strip, numstat_targets = _select_patch_strip(repo_root, patch_text, normalized_paths)
    # numstat_targets is the authoritative write set: already verified to be covered by
    # changed_paths inside _select_patch_strip.  It correctly handles mode-only patches
    # and pure renames that have no '+++ ' lines and would produce an empty patch_targets.
    patch_targets = _extract_patch_target_paths(patch_text, strip=strip)
    # Security check: if the +++ header parser claims a path that git's numstat does NOT
    # include, the patch text may be trying to deceive the gate.  We only reject for
    # parser-exclusive paths; numstat-exclusive paths (mode-only, renames) are fine.
    numstat_set = set(numstat_targets)
    parser_exclusive = [p for p in patch_targets if p not in numstat_set]
    if parser_exclusive:
        raise RuntimeError(
            "guarded-apply-patch: +++ headers declare paths absent from git-apply numstat "
            f"(strip={strip}); suspicious_paths={parser_exclusive} numstat={numstat_targets}"
        )
    # Defense-in-depth: re-verify all git-resolved targets are within declared changed_paths.
    not_covered = [
        p for p in numstat_targets
        if not any(_repo_path_under_prefix(p, cp) for cp in normalized_paths)
    ]
    if not_covered:
        raise RuntimeError(
            "guarded-apply-patch: patch targets are not covered by changed_paths: "
            + ", ".join(not_covered)
        )
    # Rename-source check: 'rename from' deletes the source file, which is a destructive
    # side-effect that must also be authorized.  numstat only reports the destination, so
    # we parse 'rename from' lines directly and require each source to be in changed_paths.
    rename_sources = _extract_rename_sources(patch_text)
    uncovered_sources = [
        p for p in rename_sources
        if not any(_repo_path_under_prefix(p, cp) for cp in normalized_paths)
    ]
    if uncovered_sources:
        raise RuntimeError(
            "guarded-apply-patch: rename source paths are not covered by changed_paths "
            "(rename deletes the source file, which must be explicitly authorized): "
            + ", ".join(uncovered_sources)
        )
    # Canonical MCP audit logs are written exclusively by MCP server tooling
    # and trusted by validate_pipeline_semantics.py as proof of tool execution.
    # Although the path is auto-injected into allowed_output_paths so MCP-side
    # writes pass record-agent-run's terminal validation, child agents must
    # not be able to mutate the log via this CLI. Reject if any path the patch
    # would touch (declared, git-resolved, or rename source) targets a path
    # listed in the manifest's `mcp_owned_audit_logs` field. Non-canonical
    # paths that happen to share the basename remain mutable through normal
    # apply-patch rules.
    mcp_owned_logs_for_patch: set[str] = set()
    actor_role_token = actor_role.strip().lower()
    if actor_role_token in {"step", "substep"}:
        try:
            manifest_doc_for_patch = _load_allowed_output_manifest(
                repo_root,
                orchestration_id=orchestration_id,
                agent_run_id=agent_run_id,
            )
        except ValueError:
            manifest_doc_for_patch = None
        if isinstance(manifest_doc_for_patch, dict):
            mcp_logs_obj = manifest_doc_for_patch.get("mcp_owned_audit_logs")
            if isinstance(mcp_logs_obj, list):
                for item in mcp_logs_obj:
                    if isinstance(item, str) and item.strip():
                        mcp_owned_logs_for_patch.add(
                            _normalize_rel_posix(item.strip())
                        )
    if mcp_owned_logs_for_patch:
        all_touched_paths = (
            set(normalized_paths)
            | set(numstat_targets)
            | set(patch_targets)
            | set(rename_sources)
        )
        forbidden_logs = sorted(
            p for p in all_touched_paths if p in mcp_owned_logs_for_patch
        )
        if forbidden_logs:
            raise RuntimeError(
                "guarded-apply-patch: cannot mutate MCP-owned audit logs "
                "(written exclusively by MCP tooling): "
                + ", ".join(forbidden_logs)
            )
    if actor_role_token in {"step", "substep"}:
        _validate_paths_against_allowed_output_manifest(
            repo_root,
            orchestration_id=orchestration_id,
            agent_run_id=agent_run_id,
            paths=normalized_paths,
        )

    gate_result = gate_apply_patch_writes(
        repo_root,
        orchestration_id=orchestration_id,
        actor_role=actor_role,
        changed_paths=normalized_paths,
        agent_run_id=agent_run_id,
        capability_token=capability_token,
    )
    proc = subprocess.run(
        ["git", "apply", "--recount", "--whitespace=nowarn", f"-p{strip}", "-"],
        cwd=str(repo_root),
        text=True,
        input=patch_text,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"guarded-apply-patch: git apply failed: {msg}")
    # Fix 3 (probing-deletion safety): defense-in-depth — re-create any
    # runtime-owned file-pin stub (e.g. lineage.json) that this apply removed
    # without it being declared in changed_paths. `not_covered` above already
    # rejects out-of-scope targets, so a placeholder outside changed_paths must
    # not have been a legitimate target; restoring the 0-byte canonical
    # placeholder guarantees guarded-apply-patch never leaves it deleted
    # out-of-band (which would otherwise surface as an unrecordable
    # unauthorized_write at terminalization).
    _restore_deleted_file_pin_stubs(
        repo_root,
        orchestration_id,
        agent_run_id=agent_run_id,
        skip_prefixes=normalized_paths,
    )
    gate_ref = _write_apply_patch_gate_evidence(
        repo_root,
        orchestration_id=orchestration_id,
        agent_run_id=agent_run_id,
        actor_role=actor_role,
        changed_paths=normalized_paths,
        result_payload=gate_result,
    )

    return {
        "applied": True,
        "changed_paths": normalized_paths,
        "patch_targets": numstat_targets,
        "gate_result_ref": gate_ref,
    }


def _validate_record_launch_response_fields(payload: dict[str, Any]) -> None:
    """Validate the required fields of record-launch --response-json at CLI dispatch time."""
    label = "record-launch --response-json"
    for key in ("agent_run_id", "agent_session_id", "started_at", "backend"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"{label}: required field {key!r} is missing or empty. "
                f"For Claude Code use: {{\"agent_run_id\": \"<uuid>\", "
                f"\"agent_session_id\": \"<same uuid>\", "
                f"\"started_at\": \"<ISO8601>\", \"backend\": \"claude\"}}"
            )
    backend = payload["backend"].strip()
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(
            f"{label}: 'backend' must be one of {sorted(SUPPORTED_BACKENDS)}; got {backend!r}"
        )


def _validate_record_agent_run_fields(payload: dict[str, Any]) -> None:
    """Validate the required fields of record-agent-run --agent-run-json at CLI dispatch time."""
    label = "record-agent-run --agent-run-json"
    for key in ("agent_run_id", "agent_backend", "status"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}: required field {key!r} is missing or empty")
    role_raw = payload.get("agent_role") or payload.get("agent_type") or payload.get("role")
    if not isinstance(role_raw, str) or not role_raw.strip():
        raise ValueError(f"{label}: required field 'agent_role' is missing or empty")
    role_token = role_raw.strip().lower()
    if role_token in {"step", "substep"}:
        for key in ("node_key", "agent_session_id"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{label}: field {key!r} is required for agent_role={role_token!r}"
                )


def _validate_write_step_result_fields(payload: dict[str, Any], step: str) -> None:
    """Validate the required fields of write-step-result --result-json at CLI dispatch time."""
    label = "write-step-result --result-json"
    status_raw = payload.get("status")
    if not isinstance(status_raw, str) or not status_raw.strip():
        raise ValueError(f"{label}: required field 'status' is missing or empty")
    status_token = status_raw.strip().lower()
    substep_ids = payload.get("substep_agent_run_ids")
    if not isinstance(substep_ids, list):
        type_name = type(substep_ids).__name__ if substep_ids is not None else "missing"
        raise ValueError(
            f"{label}: required field 'substep_agent_run_ids' must be a list (got {type_name}). "
            "Use an empty list [] for step-only phases (build)."
        )
    step_token = step.strip().lower()
    if step_token in STEP_REQUIRED_VALIDATION_STAGES and status_token in TERMINAL_STATUSES:
        allowed = STEP_REQUIRED_VALIDATION_STAGES[step_token]
        validation_stage = payload.get("validation_stage")
        if not isinstance(validation_stage, str) or validation_stage.strip() not in allowed:
            raise ValueError(
                f"{label}: step={step_token!r} with terminal status={status_token!r} requires "
                f"'validation_stage' to be one of {sorted(allowed)}; "
                f"got {validation_stage!r}"
            )


# Default (terse) result projections for the high-frequency bookkeeping
# subcommands. Each maps a subcommand to the result fields the orchestration
# agent actually consumes downstream. Anything not listed is dropped from the
# default stdout to keep the orchestration's resident context small (its
# cache-read cost scales with context size times turn count). The full payload
# is still written to its canonical artifact files and recoverable with
# --verbose. Commands absent from this map are emitted unprojected.
_TERSE_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "record-launch": (
        "capability_token",
        "capability_ref",
        "read_access_manifest_ref",
        "allowed_output_manifest_ref",
        "sandbox_profile_ref",
        "launch_prompt_ref",
        # The rendered prompt text the orchestration must pass verbatim to the
        # Agent tool (it cannot read the template or the written prompt file).
        "launch_prompt_text",
    ),
    # record_agent_run returns the run record; it carries started_at/finished_at,
    # not a `recorded_at` field.
    "record-agent-run": ("agent_run_id", "status", "started_at", "finished_at"),
    # finalize-child composes record-child-return/deactivate/record-reply/record-agent-run;
    # the orchestration only needs the terminal status + the deactivation confirmation.
    "finalize-child": (
        "agent_run_id",
        "status",
        "deactivated_child_run_id",
        "reply_ref",
        "finalized_at",
    ),
    "record-child-return": ("agent_run_id", "recorded_at", "return_token"),
    "deactivate-child": (
        "deactivated_child_run_id",
        "orchestration_id",
        "deactivated_at",
        "already_inactive",
    ),
    "record-reply": ("orchestration_id", "agent_run_id", "reply_ref", "recorded_at"),
    # `step` is a CLI arg, not part of the result payload; the status/executor/
    # failed_substeps fields are what the orchestration consumes.
    "write-step-result": ("status", "executor_agent_run_id", "failed_substeps"),
    # run_gate's stdout result contains only violations/gate_result_ref/result
    # (gate/status live in the persisted gate doc + stderr summary). `result`
    # carries the orchestration_read content (the only path child agents may use
    # for those reads), so it must survive the terse projection.
    "run-gate": ("violations", "gate_result_ref", "result"),
}

# Fields preserved in a terse result whenever present and non-empty, even if not
# in the per-command list above, so a terse success can never silently hide a
# soft-failure signal carried in the payload.
_TERSE_ALWAYS_KEEP: tuple[str, ...] = ("violations", "error", "errors", "warning", "warnings", "reply_over_budget")


def _project_terse_result(command: str, result: Any) -> Any:
    """Project a bookkeeping subcommand result to its terse field subset.

    Returns ``result`` unchanged when the command has no projection or the
    result is not a dict. Hard failures already exit non-zero via stderr before
    reaching here; this only trims successful-path payloads.
    """
    fields = _TERSE_RESULT_FIELDS.get(command)
    if fields is None or not isinstance(result, dict):
        return result
    projected: dict[str, Any] = {key: result[key] for key in fields if key in result}
    for key in _TERSE_ALWAYS_KEEP:
        if key not in projected and result.get(key):
            projected[key] = result[key]
    return projected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--repo-root", required=True)
    init_parser.add_argument("--orchestration-id", required=True)
    init_parser.add_argument("--spec-ref")
    init_parser.add_argument("--source-dependency-ref")
    init_parser.add_argument("--status", default="running")
    init_parser.add_argument("--agent-backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
    init_parser.add_argument(
        "--agent-model",
        default=None,
        help="Model id of the orchestration agent itself (e.g. claude-opus-4-8); recorded on the orchestration agent_runs row for cost attribution / reproducibility.",
    )
    init_parser.add_argument(
        "--resume-from-checkpoint",
        action="store_true",
        help="Enable checkpoint resume on an existing orchestration (sets resume_enabled; resets a terminal status back to running).",
    )

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--repo-root", required=True)
    preflight_parser.add_argument("--orchestration-id", required=True)
    preflight_parser.add_argument("--backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
    preflight_parser.add_argument("--agent-command")
    preflight_parser.add_argument("--codex-command", default="codex")
    preflight_parser.add_argument("--claude-command", default="claude")
    preflight_parser.add_argument(
        "--host-session-id",
        help=(
            "The real backend session id the orchestration agent will run inside (e.g. "
            "the Claude Code session UUID pinned via `claude --session-id`). Recorded in "
            "orchestration_meta.json#host_session_id ONLY when preflight is launchable, "
            "so a failed/non-launchable preflight does not point meta at a session that "
            "never started."
        ),
    )

    preflight_status_parser = subparsers.add_parser("preflight-status")
    preflight_status_parser.add_argument("--repo-root", required=True)
    preflight_status_parser.add_argument("--orchestration-id", required=True)

    _NODE_KEY_HELP = (
        "node_key in '<spec_kind>/<spec_id>@<spec_version>' format "
        "(e.g. 'component/dynamics_shallow_water_flux_2d_rusanov_p0@0.1.0'). "
        "Derived from deps.yaml (spec_kind, spec_id) and controlled_spec.md (spec_version). "
        "NOT a filesystem path."
    )
    _RECORD_LAUNCH_REQUEST_HELP = (
        "JSON object with launch parameters. Required fields: "
        "agent_role ('step'|'substep'), node_key (<spec_kind>/<spec_id>@<spec_version>), "
        "step, substep (for substep agents), orchestration_id, agent_run_id, "
        "parent_agent_run_id, workflow_mode ('dev'|'prod'), "
        "ir_ref (workspace/ir/<node_key_safe>/<ir_id>), "
        "pipeline_ref (workspace/pipelines/<node_key_safe>/<pipeline_id> -- required for ALL "
        "phases including Plan; reserve via reserve-phase-root --step generate if not yet created), "
        "dependency_ref (phase rule: Plan => spec/.../deps.yaml; Generate+ => workspace phase root), "
        "skill_name, skill_ref. "
        "For step/substep launch, one of allowed_output_paths|required_outputs|output_refs must be provided "
        "as file-path list; runtime validates each path against phase contract outputs and capability write_roots. "
        "allowed_file_tool_paths is optional and, when provided, must be a file-path list included in allowed_output_paths. "
        "ir_id/pipeline_id format: <slug>_<YYYYMMDD>_<seq3> where slug uses hyphens only "
        "(e.g. 'flux-rsn-p0_20260425_001'; underscores in slug are invalid). "
        "Execute step extra-required fields: run_id (single exec_id pinned for this launch), "
        "source_id (the gen_id whose <gen>/src/ run_quality_checks uses; record_launch verifies "
        "verification_status=pass), and source_build_id (the binary_id whose binary execute uses; "
        "record_launch reads <pipeline>/build/<source_build_id>/binary_meta.json and verifies "
        "source_source_id == request.source_id to prevent mixed-build forge). "
        "Cross-phase MCP audit log auto-inject (`<gen>/src/mcp_command_log.jsonl`) only fires when "
        "spec.ir.yaml.impl_defaults records `toolchain.build_system: make` (Fortran/C-family in-source builds). "
        "Generate substep extra-required: source_id matches the listed paths' single <gen_id>. "
        "Build step listed paths must use a single <binary_id>; cross-phase Make builds also accept "
        "source_id-derived `<gen>/src/mcp_command_log.jsonl` placement."
    )
    _RECORD_LAUNCH_RESPONSE_HELP = (
        "JSON object with child agent response. For Claude Code backend use: "
        '{"agent_run_id": "<uuid>", "agent_session_id": "<same uuid>", '
        '"started_at": "<ISO8601>", "backend": "claude"}. '
        "sandbox_runtime/sandbox_enforced/sandbox_profile_ref are added automatically. "
        "Call record-launch BEFORE Agent tool so capability_token is available to the child agent; "
        "then overwrite launches/<child_agent_run_id>.reply.txt with the actual Agent tool response."
    )
    _RUN_GATE_ARGS_HELP = (
        "JSON object for gate-specific arguments. Allowed gates and minimal args_json schema: "
        "orchestration_read => {'read_path': 'docs/...'}; "
        "validate_workspace_root => {'paths': ['workspace']} (optional, defaults to repo workspace); "
        "check_artifact_syntax => {'expect_top': 'object', 'paths': ['workspace/.../file.yaml', ...]}; "
        "validate_pipeline_semantics => {'stage': 'plan|post_generate|post_build|post_execute|pre_judge|full', "
        "'ir_ref': 'workspace/ir/...'(plan stage), "
        "'pipeline_root': 'workspace/pipelines/...' or ['workspace/pipelines/...', ...], "
        "'source_id': '<id>' (optional)}. "
        "Keys are converted to CLI flags (e.g. pipeline_root -> --pipeline-root)."
    )
    _STEP_RESULT_HELP = (
        "JSON object for step_result. Required: status, required_outputs (list[str]), "
        "executor_agent_run_id, substep_agent_run_ids (list[str], empty list allowed for step-only phases), "
        "failed_substeps (list[str], optional), retry_decisions (list[object], optional). "
        "retry_decisions items require: issue_severity, repair_strategy, repair_target_agent_run_id, "
        "new_agent_run_id, repair_reason. "
        "When step in {compile,generate,build,validate} and status is terminal "
        "(pass/fail/blocked/timeout/cancel), validation_stage is required: "
        "compile=>compile|full, generate=>post_generate|full, "
        "build=>post_build|full, validate=>post_execute|pre_judge|full. "
        "For compile/generate pass, required_outputs must be covered by effective substep output_refs."
    )

    launch_parser = subparsers.add_parser(
        "record-launch",
        description=(
            "Record a child agent launch: runs live preflight, generates capability_token, "
            "sandbox profile, output/read manifests, and writes launches/<child_id>.* artifacts. "
            "For Claude Code: call this BEFORE Agent tool invocation so the child can read "
            "its capability_token from capabilities/<child_id>.json during execution."
        ),
    )
    launch_parser.add_argument("--repo-root", required=True)
    launch_parser.add_argument("--orchestration-id", required=True)
    launch_parser.add_argument("--parent-agent-run-id", required=True,
                               help="UUID of the orchestration (parent) agent.")
    launch_parser.add_argument("--child-agent-run-id", required=True,
                               help="UUID pre-generated for the child agent. "
                                    "For Claude Code this also becomes agent_session_id.")
    launch_parser.add_argument("--request-json", required=True, type=_json_arg,
                               help=_RECORD_LAUNCH_REQUEST_HELP)
    launch_parser.add_argument("--response-json", required=True, type=_json_arg,
                               help=_RECORD_LAUNCH_RESPONSE_HELP)
    launch_parser.add_argument("--relation-type", default="launch")

    orch_read_parser = subparsers.add_parser("orchestration-read")
    orch_read_parser.add_argument("--repo-root", required=True)
    orch_read_parser.add_argument("--orchestration-id", required=True)
    orch_read_parser.add_argument("--agent-run-id", required=True)
    orch_read_parser.add_argument("--read-path", required=True)
    orch_read_parser.add_argument("--capability-token", required=True)

    guarded_patch_parser = subparsers.add_parser("guarded-apply-patch")
    guarded_patch_parser.add_argument("--repo-root", required=True)
    guarded_patch_parser.add_argument("--orchestration-id", required=True)
    guarded_patch_parser.add_argument("--actor-role", required=True)
    guarded_patch_parser.add_argument("--agent-run-id", required=True)
    guarded_patch_parser.add_argument("--paths-json", required=True, type=_json_string_list_arg)
    guarded_patch_parser.add_argument("--patch-text", default=None)
    guarded_patch_parser.add_argument(
        "--patch-file",
        default=None,
        help="Path to a file containing the unified diff. Mutually exclusive with --patch-text. "
             "Use this to avoid OS ARG_MAX limits for large patches.",
    )
    guarded_patch_parser.add_argument("--capability-token", required=True)

    gate_parser = subparsers.add_parser(
        "run-gate",
        description=(
            "Execute a validator gate under orchestration policy. "
            "Use this as the canonical validator invocation path when capability-token/gate enforcement is required."
        ),
    )
    gate_parser.add_argument("--repo-root", required=True)
    gate_parser.add_argument("--orchestration-id", required=True)
    gate_parser.add_argument(
        "--gate",
        required=True,
        choices=sorted(DEFAULT_ALLOWED_GATE_SERVICES),
        help=(
            "Gate name. "
            "validate_pipeline_semantics | check_artifact_syntax | validate_workspace_root | orchestration_read"
        ),
    )
    gate_parser.add_argument("--agent-run-id", required=True)
    gate_parser.add_argument("--args-json", required=True, type=_json_arg, help=_RUN_GATE_ARGS_HELP)
    gate_parser.add_argument("--capability-token", required=True)

    run_parser = subparsers.add_parser(
        "record-agent-run",
        description=(
            "Append one agent run record to agent_runs.jsonl. "
            "For step/substep roles also writes agent.result.json and agent.summary.txt, "
            "and validates that output_refs lie within the capability write_roots."
        ),
    )
    run_parser.add_argument("--repo-root", required=True)
    run_parser.add_argument("--orchestration-id", required=True)
    run_parser.add_argument(
        "--agent-run-json", required=True, type=_json_arg,
        help=(
            "JSON object for the agent run record. "
            "Always required: agent_run_id (UUID), agent_role ('orchestration'|'step'|'substep'), "
            "agent_backend ('claude'|'codex'), status ('running'|'pass'|'fail'|...), "
            "started_at (ISO8601). "
            "Required for step/substep: agent_session_id (for Claude Code = agent_run_id), "
            "context_id (unique UUID per run), context_isolated (true), node_key "
            "(<spec_kind>/<spec_id>@<spec_version>). "
            "Required when status is a terminal state (pass/fail/blocked/timeout/cancel): "
            "finished_at (ISO8601). "
            "Required on pass: output_refs (list of written artifact paths)."
        ),
    )

    finalize_parser = subparsers.add_parser(
        "finalize-child",
        description=(
            "One-call child finalization: record-child-return -> deactivate-child -> "
            "record-reply -> record-agent-run, in that order, reusing each guard. Collapses "
            "the 4 finalize Bash round-trips into one to keep the orchestration transcript "
            "small. --agent-run-json is the record-agent-run payload (its agent_run_id must "
            "equal --agent-run-id); --reply-text is the child's verbatim final message "
            "(budget-checked); --return-token is the Adv-30 parent-bound token."
        ),
    )
    finalize_parser.add_argument("--repo-root", required=True)
    finalize_parser.add_argument("--orchestration-id", required=True)
    finalize_parser.add_argument("--agent-run-id", required=True)
    finalize_parser.add_argument("--return-token", required=True)
    finalize_parser.add_argument("--reply-text")
    finalize_parser.add_argument("--reply-from-stdin", action="store_true")
    finalize_parser.add_argument("--reply-excerpt", default=None)
    finalize_parser.add_argument("--agent-run-json", required=True, type=_json_arg)

    step_parser = subparsers.add_parser(
        "write-step-result",
        description=(
            "Write step_result.json for one step run and validate required fields, "
            "retry semantics, and required_outputs coverage."
        ),
    )
    step_parser.add_argument("--repo-root", required=True)
    step_parser.add_argument("--orchestration-id", required=True)
    step_parser.add_argument("--node-key", required=True)
    step_parser.add_argument("--step", required=True)
    step_parser.add_argument("--agent-run-id", required=True)
    step_parser.add_argument("--result-json", required=True, type=_json_arg, help=_STEP_RESULT_HELP)
    step_parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "Write a step_result for an already-terminal step agent that lacks one, "
            "bypassing the child_finished phase gate and without advancing the phase "
            "state. Only fills a genuine gap (refuses to overwrite), requires the recorded "
            "run to be a terminal step agent for the same node/step, and requires the "
            "payload status to equal the recorded run status (the anti-fabrication guard; "
            "a recorded pass is backfillable too). Used to remediate a step agent stranded "
            "by a checkpoint resume (see docs/CLI_REFERENCE.md)."
        ),
    )

    deactivate_child_parser = subparsers.add_parser("deactivate-child")
    deactivate_child_parser.add_argument("--repo-root", required=True)
    deactivate_child_parser.add_argument("--orchestration-id", required=True)
    deactivate_child_parser.add_argument("--child-run-id", required=True)

    record_reply_parser = subparsers.add_parser("record-reply")
    record_reply_parser.add_argument("--repo-root", required=True)
    record_reply_parser.add_argument("--orchestration-id", required=True)
    record_reply_parser.add_argument("--agent-run-id", required=True)
    record_reply_parser.add_argument("--reply-text")
    record_reply_parser.add_argument("--reply-from-stdin", action="store_true")

    record_child_return_parser = subparsers.add_parser(
        "record-child-return",
        description=(
            "Adv-20: record that the orchestration agent has observed the "
            "Agent tool returning for this child. Required precondition for "
            "deactivate-child (and hence for record-timeout). Writes "
            "workspace/orchestrations/<orch>/child_returns/<arid>.txt as the "
            "ack signal; deactivate-child consumes the file when it succeeds."
        ),
    )
    record_child_return_parser.add_argument("--repo-root", required=True)
    record_child_return_parser.add_argument("--orchestration-id", required=True)
    record_child_return_parser.add_argument("--agent-run-id", required=True)
    record_child_return_parser.add_argument(
        "--return-token", required=True,
        help=(
            "Adv-30: per-arid parent-bound token from "
            "workspace/orchestrations/<orch>/launches/<arid>.parent_return_token "
            "(generated at record-launch). Pass via "
            "`$(cat <that file>)`. record-child-return verifies the token "
            "before issuing the ack and embeds it in the ack file so "
            "deactivate-child can re-verify."
        ),
    )
    record_child_return_parser.add_argument(
        "--reply-excerpt", default=None,
        help="Optional short metadata (e.g. first line of the Agent reply); "
             "stored alongside the ack timestamp for audit. Truncated to 200 chars.",
    )

    record_timeout_parser = subparsers.add_parser(
        "record-timeout",
        description=(
            "Canonical recovery for substep/step API stream idle timeout. "
            "Reads launches/<agent_run_id>.request.json and .response.json to "
            "build a record-agent-run payload with status='timeout' and "
            "delegates to record-agent-run (which validates partial writes and "
            "cleans up workspace/tmp/<agent_run_id>/)."
        ),
    )
    record_timeout_parser.add_argument("--repo-root", required=True)
    record_timeout_parser.add_argument("--orchestration-id", required=True)
    record_timeout_parser.add_argument("--agent-run-id", required=True)
    record_timeout_parser.add_argument(
        "--reason",
        required=True,
        help="Human-readable timeout reason (e.g., 'API stream idle timeout after 600s').",
    )
    record_timeout_parser.add_argument(
        "--force-reason", default=None,
        help=(
            "Adv-26 escape hatch: bypass the active-children/legacy marker guards "
            "for genuinely-wedged children where deactivate-child is unreachable "
            "(e.g. Agent tool process killed before parent observed return). "
            "Required text becomes part of timeout_reason and the run payload "
            "carries forced=True + forced_reason for audit. Use sparingly — the "
            "normal record-child-return → deactivate-child → record-timeout flow "
            "is preferred whenever possible."
        ),
    )

    status_parser = subparsers.add_parser("set-status")
    status_parser.add_argument("--repo-root", required=True)
    status_parser.add_argument("--orchestration-id", required=True)
    status_parser.add_argument("--status", required=True)
    status_parser.add_argument("--reason-code")
    status_parser.add_argument("--reason-detail")
    status_parser.add_argument("--blocking-policy-scope")

    mark_dep_parser = subparsers.add_parser(
        "mark-dependency-readiness",
        description=(
            "Re-verify orchestration_meta.dependency_readiness from real "
            "workspace artifacts and OVERWRITE all detail flags with the "
            "computed result. The runtime resolves every direct dep in "
            "<spec_ref>/deps.yaml (via spec/registry/spec_catalog.yaml + "
            "version_constraint matching) and inspects the latest (mtime) "
            "ir_meta.json / binary_meta.json / aggregate_verdict.json. A "
            "flag is True only when every dep passes its per-stage check. "
            "Top-level compile/execution readiness are derived from detail "
            "flags. The CLI takes no per-stage args: every call performs a "
            "full re-verification (selective updates would let stale `true` "
            "flags survive dependency regressions)."
        ),
    )
    mark_dep_parser.add_argument("--repo-root", required=True)
    mark_dep_parser.add_argument("--orchestration-id", required=True)

    launch_check_parser = subparsers.add_parser(
        "workflow-launch-check",
        description=(
            "Pre-phase gate: checks execution platform availability, session policy, "
            "dependency readiness, and required child agent kind. "
            "Returns JSON with status ('pass'|'fail_closed') and next_action. "
            "Run once before the first phase; fail_closed must stop the orchestration."
        ),
    )
    launch_check_parser.add_argument("--repo-root", required=True)
    launch_check_parser.add_argument("--orchestration-id", required=True)
    launch_check_parser.add_argument(
        "--node-key", required=True,
        help=_NODE_KEY_HELP,
    )
    launch_check_parser.add_argument("--step", required=True,
                                     help="Workflow step name: plan, generate, build, execute, judge, etc.")
    launch_check_parser.add_argument("--backend", default="codex", choices=sorted(SUPPORTED_BACKENDS))
    launch_check_parser.add_argument(
        "--require-child-agent", required=True, choices=("step", "substep"),
        help="Expected child agent kind. Plan/Generate/Tune require 'substep'; "
             "Build/Execute/Judge/Promote require 'step'.",
    )
    launch_check_parser.add_argument(
        "--launch-request-json",
        default=None,
        type=_json_arg,
        help="Optional launch request object for downstream artifact checks (pre_phase_launch).",
    )

    reserve_root_parser = subparsers.add_parser(
        "reserve-phase-root",
        description=(
            "Reserve an ir_id or pipeline_id before the child agent creates the directory. "
            "Writes a reservation marker only; does NOT create workspace/ir/ or "
            "workspace/pipelines/ directories. "
            "Use --step compile to reserve an ir_id; --step generate to reserve a pipeline_id. "
            "Both reservations are typically needed before launching Plan phase substeps, "
            "because record-launch requires a valid pipeline_ref even for Plan."
        ),
    )
    reserve_root_parser.add_argument("--repo-root", required=True)
    reserve_root_parser.add_argument("--orchestration-id", required=True)
    reserve_root_parser.add_argument(
        "--node-key", required=True,
        help=_NODE_KEY_HELP,
    )
    reserve_root_parser.add_argument("--step", required=True,
                                     help="'compile' to reserve an ir_id; 'generate' to reserve a pipeline_id.")
    reserve_root_parser.add_argument(
        "--reserved-id", required=True,
        help=(
            "The ir_id or pipeline_id to reserve. "
            "Format: <slug>_<YYYYMMDD>_<seq3> where slug is hyphen-separated lowercase alphanumeric "
            "(regex: ^[a-z0-9]+(?:-[a-z0-9]+)*_[0-9]{8}_[0-9]{3}$). "
            "Example: 'flux-rsn-p0_20260425_001'. "
            "Underscores inside the slug are INVALID (use hyphens instead)."
        ),
    )
    reserve_root_parser.add_argument("--reserved-by-agent-run-id", required=True,
                                     help="UUID of the agent that will use this reserved ID.")

    read_cp_parser = subparsers.add_parser("read-checkpoint")
    read_cp_parser.add_argument("--repo-root", required=True)
    read_cp_parser.add_argument("--orchestration-id", required=True)

    verify_cp_parser = subparsers.add_parser("verify-checkpoint-integrity")
    verify_cp_parser.add_argument("--repo-root", required=True)
    verify_cp_parser.add_argument("--orchestration-id", required=True)

    check_step_parser = subparsers.add_parser("check-step-completed")
    check_step_parser.add_argument("--repo-root", required=True)
    check_step_parser.add_argument("--orchestration-id", required=True)
    check_step_parser.add_argument("--node-key", required=True)
    check_step_parser.add_argument("--step", required=True)
    check_step_parser.add_argument(
        "--skip-integrity-check",
        action="store_true",
        help="Skip artifact hash verification (testing only).",
    )

    repair_runs_parser = subparsers.add_parser(
        "repair-agent-runs",
        help=(
            "Backfill missing parent_agent_run_id/agent_model on pre-fix "
            "(pre-caa10ab) step/substep agent_runs.jsonl lines so a resumed "
            "orchestration can pass the pre_judge gate."
        ),
    )
    repair_runs_parser.add_argument("--repo-root", required=True)
    repair_runs_parser.add_argument("--orchestration-id", required=True)
    repair_runs_parser.add_argument(
        "--agent-model",
        default=None,
        help=(
            "Model id to assign to legacy lines missing agent_model. Required "
            "only when it cannot be auto-derived from a unique non-empty "
            "agent_model on sibling entries of the same orchestration."
        ),
    )

    repair_step_executor_parser = subparsers.add_parser(
        "repair-step-result-executor",
        help=(
            "Repair a substep-aware step_result.json whose executor_agent_run_id "
            "is a verify-substep arid instead of the orchestration arid (relocates "
            "the file to the orchestration dir, rewrites the field, preserves "
            "substep linkage). Recovers an orchestration locked at "
            "step_result_written by validate_pre_judge_step_result_executor_integrity "
            "without a fresh orchestration. Idempotent."
        ),
    )
    repair_step_executor_parser.add_argument("--repo-root", required=True)
    repair_step_executor_parser.add_argument("--orchestration-id", required=True)
    repair_step_executor_parser.add_argument("--node-key", required=True)
    repair_step_executor_parser.add_argument("--step", required=True)

    reopen_phase_parser = subparsers.add_parser(
        "reopen-phase",
        help=(
            "Reopen a checkpointed-pass phase and everything downstream so a "
            "cross-phase retry (Validate.judge structural_violation/ir -> Compile, "
            "or Generate.verify ir_inconsistency -> Compile) runs in place. Snapshots "
            "the prior attempt's runs as superseded (exempt from the completion "
            "vouch), archives their step_results aside, drops the affected checkpoint "
            "entries, and resets the affected phase_state to not_started. Idempotent. "
            "Requires --trigger-agent-run-id to be a terminal non-pass step/substep "
            "strictly downstream of --from-phase."
        ),
    )
    reopen_phase_parser.add_argument("--repo-root", required=True)
    reopen_phase_parser.add_argument("--orchestration-id", required=True)
    reopen_phase_parser.add_argument("--node-key", required=True, help=_NODE_KEY_HELP)
    reopen_phase_parser.add_argument(
        "--from-phase",
        required=True,
        choices=list(STEP_KEYS_FOR_NODE_STATE),
        help="The earliest phase to reopen; it and all downstream phases are invalidated.",
    )
    reopen_phase_parser.add_argument(
        "--reason",
        required=True,
        help="Reason code for the reopen (e.g. validate_judge_structural_violation_ir).",
    )
    reopen_phase_parser.add_argument(
        "--trigger-agent-run-id",
        required=True,
        help="agent_run_id of the failed downstream substep/step that attributed back to --from-phase.",
    )
    reopen_phase_parser.add_argument(
        "--finding-id",
        default=None,
        help="Optional semantic_review finding id that drove the attribution.",
    )

    dismiss_viol_parser = subparsers.add_parser(
        "dismiss-violation",
        help=(
            "Operator approval gate: mark an unauthorized_write_violation as "
            "intentional / benign so record-agent-run can proceed past the "
            "terminal validation guard on retry."
        ),
    )
    dismiss_viol_parser.add_argument("--repo-root", required=True)
    dismiss_viol_parser.add_argument("--orchestration-id", required=True)
    dismiss_viol_parser.add_argument(
        "--agent-run-id",
        required=True,
        help="agent_run_id of the failing run whose violation is to be dismissed",
    )
    dismiss_viol_parser.add_argument(
        "--dismiss-reason",
        required=True,
        help="Free-form explanation stored in violation JSON (e.g. 'tools/__pycache__ is gitignored Python bytecode')",
    )
    dismiss_viol_parser.add_argument(
        "--operator-token",
        required=True,
        help=(
            "Content of ~/.met-dsl/operator_tokens/<oid>.txt. "
            "Read with: cat ~/.met-dsl/operator_tokens/<oid>.txt"
        ),
    )
    dismiss_viol_parser.add_argument(
        "--paths",
        nargs="+",
        required=True,
        metavar="PATH",
        help="Repo-root-relative paths to dismiss (must be subset of violation's unauthorized_paths)",
    )

    # The bookkeeping subcommands default to a terse result projection (see
    # _project_terse_result): the orchestration agent re-reads its whole
    # transcript every turn, so echoing the full payload (record-agent-run
    # reflects the entire input, up to ~50KB) inflates its resident context and
    # the cache-read cost that scales with it. --verbose restores the full JSON
    # for debugging/audit. The flag lives on each terse subparser so it can be
    # appended after the subcommand (e.g. `record-launch ... --verbose`).
    for _terse_parser in (
        launch_parser,
        gate_parser,
        run_parser,
        step_parser,
        finalize_parser,
        deactivate_child_parser,
        record_reply_parser,
        record_child_return_parser,
    ):
        _terse_parser.add_argument(
            "--verbose",
            action="store_true",
            help="Emit the full result JSON instead of the default terse projection.",
        )

    args = parser.parse_args(argv)
    repo_root = Path(getattr(args, "repo_root")).resolve()

    if args.command == "init":
        if getattr(args, "resume_from_checkpoint", False):
            result = enable_checkpoint_resume(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                spec_ref=args.spec_ref,
                source_dependency_ref=args.source_dependency_ref,
            )
            # Self-heal any step_result.json whose executor arid is a verify-substep
            # arid instead of the orchestration arid (the
            # validate_pre_judge_step_result_executor_integrity lock). Best-effort:
            # runs at init time before any child launch, idempotent, never fatal.
            # MUST run BEFORE repair_legacy_agent_runs: the legacy backfill derives a
            # substep's missing parent_agent_run_id from the step_result's
            # executor_agent_run_id (_load_substep_parent_map), so a still-corrupt
            # executor would either leave the parent unrepairable (graph disagreement)
            # or persist the wrong parent — and the step-result repair does not re-run
            # the parent backfill afterward, so pre_judge would still fail.
            step_executor_repair = repair_all_step_result_executors(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
            )
            # Then bring any pre-caa10ab legacy agent_runs.jsonl lines into pre_judge
            # compliance before the resumed run reaches the gate. Runs at init
            # time (before any child launch / outside the active_child window),
            # so the agent_runs.jsonl write is safe. An explicit --agent-model is
            # forwarded so an operator resolving a `needs_manual` row (agent_model
            # not auto-derivable) can fix it directly on the `--resume` command;
            # without it, repair falls back to sibling_uniform derivation.
            repair_summary = repair_legacy_agent_runs(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                agent_model=args.agent_model,
            )
            if isinstance(result, dict):
                result = {
                    **result,
                    "legacy_record_repair": repair_summary,
                    "step_result_executor_repair": step_executor_repair,
                }
        else:
            result = init_orchestration(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                spec_ref=args.spec_ref,
                source_dependency_ref=args.source_dependency_ref,
                status=args.status,
                agent_backend=args.agent_backend,
                agent_model=args.agent_model,
            )
    elif args.command == "preflight":
        agent_command = args.agent_command
        if not isinstance(agent_command, str) or not agent_command.strip():
            if args.backend == "codex":
                # Keep backward compatibility only for codex backend.
                agent_command = args.codex_command
            elif args.backend == "claude":
                agent_command = args.claude_command
        result = write_preflight(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            payload=probe_execution_platform(
                backend=args.backend,
                agent_command=agent_command,
                repo_root=repo_root,
            ),
            host_session_id=getattr(args, "host_session_id", None),
        )
    elif args.command == "preflight-status":
        result = get_preflight_ttl_status(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
        )
    elif args.command == "record-launch":
        try:
            _validate_record_launch_response_fields(args.response_json)
            result = record_launch(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                parent_agent_run_id=args.parent_agent_run_id,
                child_agent_run_id=args.child_agent_run_id,
                request_payload=args.request_json,
                response_payload=args.response_json,
                relation_type=args.relation_type,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "orchestration-read":
        try:
            gate_out = run_gate(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                gate_name="orchestration_read",
                agent_run_id=args.agent_run_id,
                args_json={"read_path": args.read_path},
                capability_token=args.capability_token,
            )
            result = gate_out.get("result", {})
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "guarded-apply-patch":
        def _emit_apply_patch_error(
            exc_type: str,
            message: str,
            *,
            reason_code: str | None = None,
        ) -> None:
            envelope: dict[str, Any] = {
                "error": "guarded_apply_patch_failed",
                "exception_type": exc_type,
                "message": message,
                "violations": [{"reason": message, "exception_type": exc_type}],
            }
            if reason_code is not None:
                envelope["reason_code"] = reason_code
            print(json.dumps(envelope, ensure_ascii=False), file=sys.stderr)

        try:
            if args.patch_text is not None and args.patch_file is not None:
                _emit_apply_patch_error(
                    "ArgumentError",
                    "--patch-text and --patch-file are mutually exclusive",
                    reason_code="mutually_exclusive_patch_source",
                )
                return 1
            if args.patch_text is None and args.patch_file is None:
                _emit_apply_patch_error(
                    "ArgumentError",
                    "one of --patch-text or --patch-file is required",
                    reason_code="missing_patch_source",
                )
                return 1
            # Reject path-traversal in agent_run_id BEFORE branching on
            # --patch-text vs --patch-file.  agent_run_id is used as a path
            # component in manifest/capability/audit lookups via simple path
            # joins inside guarded_apply_patch(); a traversal segment
            # (e.g. "../..") would target the wrong tree.  We require an
            # identifier shape (alphanumeric, underscore, hyphen) — strict
            # enough to block traversal/null-byte attacks without rejecting
            # legacy fixture names that production code accepts elsewhere.
            _AGENT_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
            if not _AGENT_RUN_ID_RE.match(args.agent_run_id or ""):
                _emit_apply_patch_error(
                    "ArgumentError",
                    (
                        f"--agent-run-id '{args.agent_run_id}' contains "
                        "path-traversal characters or is empty; required "
                        "shape: [A-Za-z0-9][A-Za-z0-9_-]*"
                    ),
                    reason_code="invalid_agent_run_id",
                )
                return 1
            if args.patch_file is not None:
                import stat as _stat_mod
                _PATCH_FILE_MAX_BYTES = int(
                    os.environ.get("METDSL_PATCH_FILE_MAX_BYTES", 10 * 1024 * 1024)
                )
                _pf_fd = -1
                # When using --patch-file the path is constructed as
                # workspace/tmp/<agent_run_id>/, so additionally enforce a
                # strict UUID shape to match the production record-launch
                # convention (UUID is what record-launch generates).
                _UUID_RE = re.compile(
                    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                    re.IGNORECASE,
                )
                if not _UUID_RE.match(args.agent_run_id):
                    _emit_apply_patch_error(
                        "ArgumentError",
                        f"--agent-run-id '{args.agent_run_id}' is not a valid UUID",
                        reason_code="invalid_agent_run_id",
                    )
                    return 1
                try:
                    _allowed_tmp = (
                        Path(repo_root) / "workspace" / "tmp" / args.agent_run_id
                    ).resolve()
                    # Pre-open confinement check using strict resolve (follows symlinks).
                    # This is the fallback path when /proc/self/fd is unavailable.
                    # O_NOFOLLOW (below) ensures the final path component cannot be a
                    # symlink, so the only remaining TOCTOU window is on directory
                    # components — which are within the agent-owned tmp root.
                    _pf_pre_resolved = Path(args.patch_file).resolve(strict=True)
                    if os.path.commonpath([_pf_pre_resolved, _allowed_tmp]) != str(_allowed_tmp):
                        _emit_apply_patch_error(
                            "PatchFileOutsideTmpRoot",
                            (
                                f"--patch-file '{args.patch_file}' is outside the agent's "
                                f"allowed tmp root '{_allowed_tmp}'. "
                                f"Use the literal allowed_tmp_root path (e.g. "
                                f"'{_allowed_tmp}/<name>.patch'); do not 'export TMPDIR=...'."
                            ),
                            reason_code="patch_file_outside_tmp_root",
                        )
                        return 1
                    # O_NOFOLLOW refuses to open if the final path component is a symlink,
                    # preventing leaf-level symlink swap between the pre-open check and open.
                    _o_nofollow = getattr(os, "O_NOFOLLOW", 0)
                    _pf_fd = os.open(args.patch_file, os.O_RDONLY | _o_nofollow)
                    # On Linux, re-verify confinement via /proc/self/fd/<fd> — this check
                    # operates on the already-open fd and is fully race-free. If /proc is
                    # unavailable (non-Linux, restricted container) the OSError is caught
                    # and we rely on the pre-open resolve check + O_NOFOLLOW above.
                    try:
                        _fd_real = Path(os.readlink(f"/proc/self/fd/{_pf_fd}")).resolve()
                        if os.path.commonpath([_fd_real, _allowed_tmp]) != str(_allowed_tmp):
                            _emit_apply_patch_error(
                                "PatchFileOutsideTmpRoot",
                                (
                                    f"--patch-file '{args.patch_file}' is outside the agent's "
                                    f"allowed tmp root '{_allowed_tmp}'. "
                                    f"Use $TMPDIR to construct the path."
                                ),
                                reason_code="patch_file_outside_tmp_root",
                            )
                            return 1
                    except OSError:
                        pass  # /proc unavailable; pre-open check + O_NOFOLLOW suffice
                    # fstat operates on the open fd — not subject to path races.
                    _pf_stat = os.fstat(_pf_fd)
                    if not _stat_mod.S_ISREG(_pf_stat.st_mode):
                        _emit_apply_patch_error(
                            "PatchFileNotRegular",
                            (
                                f"--patch-file '{args.patch_file}' is not a regular file "
                                f"(mode {_pf_stat.st_mode:#o})"
                            ),
                            reason_code="patch_file_not_regular",
                        )
                        return 1
                    if _pf_stat.st_size > _PATCH_FILE_MAX_BYTES:
                        _emit_apply_patch_error(
                            "PatchFileTooLarge",
                            (
                                f"--patch-file '{args.patch_file}' size {_pf_stat.st_size} bytes "
                                f"exceeds limit {_PATCH_FILE_MAX_BYTES} bytes"
                            ),
                            reason_code="patch_file_too_large",
                        )
                        return 1
                    # fdopen takes ownership of _pf_fd; mark sentinel before hand-off.
                    _pf_fobj = os.fdopen(_pf_fd, "r", encoding="utf-8")
                    _pf_fd = -1
                    with _pf_fobj:
                        _patch_text = _pf_fobj.read()
                except (OSError, UnicodeDecodeError) as exc:
                    _emit_apply_patch_error(
                        type(exc).__name__,
                        f"cannot read --patch-file '{args.patch_file}': {exc}",
                        reason_code="patch_file_read_error",
                    )
                    return 1
                finally:
                    if _pf_fd >= 0:
                        os.close(_pf_fd)
            else:
                _patch_text = args.patch_text
            result = guarded_apply_patch(
                repo_root,
                orchestration_id=args.orchestration_id,
                actor_role=args.actor_role,
                agent_run_id=args.agent_run_id,
                changed_paths=args.paths_json,
                patch_text=_patch_text,
                capability_token=args.capability_token,
            )
        except (RuntimeError, ValueError) as exc:
            # Emit a stable JSON envelope on stderr so agents following the
            # docs/ORCHESTRATION.md "output contract" contract can parse failure
            # detail without reading the gate file directly. We attempt to
            # extract violations[] from a JSON-shaped exception message; if the
            # message is plain text, we fall back to a single-violation entry.
            err_payload: dict[str, Any] = {
                "error": "guarded_apply_patch_failed",
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "violations": [],
            }
            _msg = str(exc).strip()
            if _msg.startswith("{") and _msg.endswith("}"):
                try:
                    _parsed = json.loads(_msg)
                    if isinstance(_parsed, dict):
                        v = _parsed.get("violations")
                        if isinstance(v, list):
                            err_payload["violations"] = v
                        for k in ("reason", "reason_code", "detail"):
                            if k in _parsed:
                                err_payload[k] = _parsed[k]
                except json.JSONDecodeError:
                    pass
            if not err_payload["violations"]:
                err_payload["violations"] = [{
                    "reason": _msg,
                    "exception_type": type(exc).__name__,
                }]
            print(json.dumps(err_payload, ensure_ascii=False), file=sys.stderr)
            return 1
        except Exception as exc:
            # Catch-all so unexpected exceptions (e.g. KeyError from a payload
            # quirk) still produce a parseable JSON envelope rather than a
            # bare traceback that breaks the documented recovery contract.
            _emit_apply_patch_error(
                type(exc).__name__,
                f"unexpected error: {exc}",
                reason_code="unexpected_error",
            )
            return 1
    elif args.command == "run-gate":
        try:
            result = run_gate(
                repo_root,
                orchestration_id=args.orchestration_id,
                gate_name=args.gate,
                agent_run_id=args.agent_run_id,
                args_json=args.args_json,
                capability_token=args.capability_token,
            )
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "record-agent-run":
        try:
            _validate_record_agent_run_fields(args.agent_run_json)
            result = record_agent_run(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                payload=args.agent_run_json,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "finalize-child":
        if args.reply_from_stdin:
            reply_text = sys.stdin.read()
        else:
            reply_text = args.reply_text
        if not isinstance(reply_text, str) or not reply_text.strip():
            print("finalize-child requires --reply-text or --reply-from-stdin", file=sys.stderr)
            return 1
        try:
            _validate_record_agent_run_fields(args.agent_run_json)
            result = finalize_child(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                agent_run_id=args.agent_run_id,
                return_token=args.return_token,
                reply_text=reply_text,
                agent_run_payload=args.agent_run_json,
                reply_excerpt=args.reply_excerpt,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "write-step-result":
        try:
            _validate_write_step_result_fields(args.result_json, args.step)
            result = write_step_result(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                node_key=args.node_key,
                step=args.step,
                agent_run_id=args.agent_run_id,
                payload=args.result_json,
                backfill=args.backfill,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "deactivate-child":
        try:
            result = deactivate_child_agent(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                child_run_id=args.child_run_id,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "record-reply":
        if args.reply_from_stdin:
            reply_text = sys.stdin.read()
            if not isinstance(reply_text, str):
                reply_text = ""
        else:
            reply_text = args.reply_text
        if not isinstance(reply_text, str):
            print("record-reply requires --reply-text or --reply-from-stdin", file=sys.stderr)
            return 1
        if not reply_text.strip():
            print("record-reply requires non-empty reply_text", file=sys.stderr)
            return 1
        try:
            result = record_reply_text(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                agent_run_id=args.agent_run_id,
                reply_text=reply_text,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "record-timeout":
        try:
            result = record_timeout(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                agent_run_id=args.agent_run_id,
                reason=args.reason,
                force_reason=args.force_reason,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "record-child-return":
        try:
            result = record_child_return(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                agent_run_id=args.agent_run_id,
                return_token=args.return_token,
                reply_excerpt=args.reply_excerpt,
            )
        except (ValueError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    elif args.command == "read-checkpoint":
        loaded = read_checkpoint(repo_root=repo_root, orchestration_id=args.orchestration_id)
        result = (
            loaded
            if loaded is not None
            else {"orchestration_id": args.orchestration_id, "completed_steps": []}
        )
    elif args.command == "verify-checkpoint-integrity":
        result = verify_checkpoint_integrity(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
        )
    elif args.command == "check-step-completed":
        info = check_step_completed(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            verify_integrity=not args.skip_integrity_check,
        )
        if info:
            result = {"completed": True, **info}
        else:
            result = {
                "completed": False,
                "node_key": args.node_key,
                "step": args.step.strip().lower(),
            }
    elif args.command == "repair-agent-runs":
        result = repair_legacy_agent_runs(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            agent_model=args.agent_model,
        )
    elif args.command == "repair-step-result-executor":
        try:
            result = repair_step_result_executor(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                node_key=args.node_key,
                step=args.step,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"repair-step-result-executor: {exc}", file=sys.stderr)
            return 1
    elif args.command == "reopen-phase":
        try:
            result = reopen_phase(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
                node_key=args.node_key,
                from_phase=args.from_phase,
                reason=args.reason,
                trigger_agent_run_id=args.trigger_agent_run_id,
                finding_id=args.finding_id,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"reopen-phase: {exc}", file=sys.stderr)
            return 1
    elif args.command == "dismiss-violation":
        try:
            result = dismiss_violation(
                repo_root,
                args.orchestration_id,
                agent_run_id=args.agent_run_id,
                dismiss_reason=args.dismiss_reason,
                paths=args.paths,
                operator_token=args.operator_token,
            )
        except (ValueError, FileNotFoundError) as exc:
            print(f"dismiss-violation: {exc}", file=sys.stderr)
            return 1
    elif args.command == "set-status":
        result = update_orchestration_status(
            repo_root=repo_root,
            orchestration_id=args.orchestration_id,
            status=args.status,
            reason_code=args.reason_code,
            reason_detail=args.reason_detail,
            blocking_policy_scope=args.blocking_policy_scope,
        )
    elif args.command == "mark-dependency-readiness":
        # Codex round 26 F2: convert expected verification failures
        # (malformed/missing deps.yaml, missing orchestration_meta.json) into
        # a clean stderr message + exit code 1 rather than letting a
        # traceback escape the CLI surface — orchestration wrappers treat
        # tracebacks as infrastructure crashes, not dependency rejections.
        try:
            result = mark_dependency_readiness(
                repo_root=repo_root,
                orchestration_id=args.orchestration_id,
            )
        except (ValueError, FileNotFoundError) as exc:
            print(f"mark-dependency-readiness: {exc}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            # Codex round 28 F2: PyYAML not installed → `_require_yaml()`
            # raises `RuntimeError` from `_read_deps_yaml` /
            # `_load_spec_catalog_from_bytes`. Surface as a clean stderr
            # message under the same CLI contract as other expected
            # verification failures rather than letting wrappers see a
            # Python traceback (which would be classified as an
            # infrastructure crash, not a dependency rejection).
            if "PyYAML" not in str(exc):
                raise
            print(f"mark-dependency-readiness: {exc}", file=sys.stderr)
            return 1
    elif args.command == "workflow-launch-check":
        # Convert expected validation failures (e.g. an unsupported --step like
        # 'plan') into a clean stderr message + exit 1 rather than letting a
        # Python traceback escape — orchestration wrappers classify tracebacks
        # as infrastructure crashes, not request rejections. Mirrors the
        # mark-dependency-readiness dispatch below.
        try:
            result = pre_phase_launch(
                repo_root,
                orchestration_id=args.orchestration_id,
                node_key=args.node_key,
                step=args.step,
                backend=args.backend,
                require_child_agent=args.require_child_agent,
                launch_request=getattr(args, "launch_request_json", None),
            )
        except ValueError as exc:
            print(f"workflow-launch-check: {exc}", file=sys.stderr)
            return 1
    elif args.command == "reserve-phase-root":
        result = reserve_phase_root(
            repo_root,
            orchestration_id=args.orchestration_id,
            node_key=args.node_key,
            step=args.step,
            reserved_id=args.reserved_id,
            reserved_by_agent_run_id=args.reserved_by_agent_run_id,
        )
    else:
        raise RuntimeError(f"unhandled command: {args.command}")

    if not getattr(args, "verbose", False):
        result = _project_terse_result(args.command, result)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
