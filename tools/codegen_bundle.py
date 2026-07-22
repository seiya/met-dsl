#!/usr/bin/env python3
"""Canonical `CodegenBundle` contract (Z0) — shared by the future Z2 runtime and validators.

The bundle is the typed output of target-specific code generation for one optimization
unit: the generated source-file set with roles, the externally visible entrypoints, the
target lowering plan, the harness capability requirements, and the primary-state
bindings. Canonical prose: `docs/workflow/CODEGEN_BUNDLE_CONTRACT.md`. Declarative copy of
the field grammar: `spec/schema/generate/codegen_bundle.schema.json`.

**No producer exists yet.** No workflow phase writes or reads a bundle; the producer
arrives with Z2 (`docs/design/zero_base_architecture.md`). This module is the contract
pinned ahead of it, which is why it has no CLI and no `--stage` wiring: a gate over an
artifact nothing emits would be dead code. Z2 wires `validate_bundle` as the
post-generate gate.

Idiom (`tools/meta_contracts.py`): stdlib only, pure functions, and violation clauses
that are prefix-free, so the caller supplies the artifact path / field prefix in its own
reporting idiom.

Two unrelated senses of "capability" exist in this repository. The agent capability
token (`workspace/orchestrations/<id>/capabilities/<agent_run_id>.json`) is an
authorization credential and is NOT this. Everything here is the harness capability ABI:
a code-generation contract term carrying no authority.
"""

from __future__ import annotations

import collections.abc
import copy
import hashlib
import heapq
import json
import posixpath
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------------------
# Contract constants — the ENFORCEMENT source. The schema JSON is the declarative copy of
# the same facts (plus what a draft-07 pattern can carry of them), and
# `test_codegen_bundle.py` fails on any divergence between the two. Validation reads these
# constants and never the file, so a missing or unreadable schema cannot fail-open a gate.
# --------------------------------------------------------------------------------------

CODEGEN_BUNDLE_SCHEMA_VERSION = "1.0.0"

BUNDLE_SCHEMA_PATH = "spec/schema/generate/codegen_bundle.schema.json"
HARNESS_CAPABILITIES_SCHEMA_PATH = "spec/schema/generate/harness_capabilities.schema.json"

REQUIRED_BUNDLE_KEYS: tuple[str, ...] = (
    "bundle_schema_version",
    "optimization_unit",
    "files",
    "entrypoints",
    "target_lowering_plan",
    "capability_requirements",
)
OPTIONAL_BUNDLE_KEYS: tuple[str, ...] = ("state_bindings",)

FILE_ROLES: tuple[str, ...] = ("model", "checks", "helper", "internal_module")
# Only these roles may define an externally visible symbol. A helper / internal_module
# file is private BY ROLE — privacy is declared, not inferred from a Fortran `private`.
ROLE_FOR_ENTRYPOINT_KIND: dict[str, str] = {"operation": "model", "checks_interface": "checks"}
ENTRYPOINT_KINDS: tuple[str, ...] = tuple(ROLE_FOR_ENTRYPOINT_KIND)
# A file shared by the whole unit (`member_node_key: null`) may only be private.
UNIT_SHAREABLE_ROLES: frozenset[str] = frozenset({"helper", "internal_module"})
# Compile order derives from the role alone — no `use`-statement analysis of generated code.
ROLE_BUILD_PRECEDENCE: tuple[str, ...] = ("internal_module", "helper", "model", "checks")

LANGUAGES: tuple[str, ...] = ("fortran",)
LANGUAGE_EXTENSION_ALLOWLIST: dict[str, tuple[str, ...]] = {"fortran": (".f90",)}

# The no-arbitrary-command rule is structural: the schema is closed (no field a command
# could travel in), these path rules reject build/script files, and the derived build
# graph has no command slot. `files[].content` is NEVER scanned for shell-looking
# strings — a Fortran source legitimately holds string literals, so a content scan buys
# no guarantee and produces false positives.
RESERVED_LOGICAL_FILENAMES: frozenset[str] = frozenset(
    {"Makefile", "makefile", "GNUmakefile", "CMakeLists.txt", "configure"})
FORBIDDEN_EXTENSIONS: frozenset[str] = frozenset({".sh", ".bash", ".mk", ".cmake", ".py"})

# Every grammar pattern anchors the whole string with `^` … `(?![\s\S])`, not `^` … `$`.
# `$` is not portable across the pattern's consumers: under Python `re` (the canonical
# validator via `fullmatch`, and the `jsonschema` library via `re.search`) `$` also matches
# just before a trailing newline, so `^…$` would let a schema-only consumer accept `"1.0.0\n"`
# the canonical validator rejects; while `\A`/`\Z` fix Python but are invalid in ECMA-262
# (Ajv and other draft-07 validators), where `\A` degrades to a literal `A`. The negative
# lookahead `(?![\s\S])` means "no character (not even a newline) follows" — true end of
# string — and is valid and identical in ECMA-262 and Python.
_END = r"(?![\s\S])"
_SEGMENT_BODY = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
LOGICAL_PATH_SEGMENT_PATTERN = rf"^{_SEGMENT_BODY}{_END}"
# The whole-path grammar the schema carries as its `pattern`. It expresses the segment
# grammar and the separator rule; the confinement, normalization, extension, reserved-name,
# and uniqueness rules are `logical_path_violations` (no draft-07 pattern can carry them).
LOGICAL_PATH_PATTERN = rf"^{_SEGMENT_BODY}(?:/{_SEGMENT_BODY})*{_END}"
_SEGMENT_RE = re.compile(LOGICAL_PATH_SEGMENT_PATTERN)
# A Fortran identifier: 1-63 characters (the f2008/f2018 limit). An entrypoint `symbol` /
# `module`, or a `state_bindings` `state_variable` / `storage_symbol`, longer than this cannot
# pass the mandatory `Generate.syntax` compiler gate, so the bundle contract rejects it before
# assembly rather than deferring the failure to the build (v1 is fortran-only; a per-language
# limit arrives with a second language).
FORTRAN_IDENTIFIER_MAX = 63
IDENTIFIER_PATTERN = rf"^[A-Za-z][A-Za-z0-9_]{{0,62}}{_END}"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
# node_key = `<spec_kind>/<spec_id>@<spec_version>`, aligned with the repository's canonical
# parser `tools/orchestration_runtime.py:_parse_node_key_strict`: `spec_id` is dot-separated
# lowercase segments (each `[a-z0-9][a-z0-9_]*`), and `spec_version` is `[0-9][0-9A-Za-z._-]*`
# (a prerelease/dotted version, not only three numeric components). `spec_kind` is the closed
# 4-value domain (docs/GLOSSARY.md), which is narrower than the parser's generic kind grammar
# only because no other kind exists. A narrower pattern here would reject a bundle carrying a
# node the rest of the workflow accepts.
_SPEC_ID_SEGMENT = r"[a-z0-9][a-z0-9_]*"
NODE_KEY_PATTERN = (
    rf"^(problem|component|profile|infrastructure)/"
    rf"{_SPEC_ID_SEGMENT}(?:\.{_SPEC_ID_SEGMENT})*@[0-9][0-9A-Za-z._-]*{_END}")
_NODE_KEY_RE = re.compile(NODE_KEY_PATTERN)
SEMVER_PATTERN = rf"^[0-9]+\.[0-9]+\.[0-9]+{_END}"
_SEMVER_RE = re.compile(SEMVER_PATTERN)
# The declarative schema pins the SUPPORTED MAJOR in its `bundle_schema_version` pattern, so a
# generic JSON Schema consumer (structured generation) rejects an incompatible major at the
# schema boundary instead of admitting it to fail later at `validate_bundle`. The module still
# checks well-formedness (`SEMVER_PATTERN`) then the major separately, to report the two as
# distinct clauses; the single schema pattern collapses both.
BUNDLE_SCHEMA_VERSION_PATTERN = (
    rf"^{re.escape(CODEGEN_BUNDLE_SCHEMA_VERSION.split('.', 1)[0])}\.[0-9]+\.[0-9]+{_END}")

STATE_RESIDENCIES: tuple[str, ...] = ("host", "device", "distributed")
LOWERING_PLAN_REQUIRED_KEYS: tuple[str, ...] = ("precision", "state_residency")
LOWERING_PLAN_OPTIONAL_KEYS: tuple[str, ...] = (
    "data_layout", "parallelization", "decomposition", "communication",
    "accelerator_mapping", "fusion",
)

STATE_CAPTURES: tuple[str, ...] = ("checks_getter", "harness_registration")

# The declarative `impl_defaults.toolchain` fields the derived build graph may echo
# (docs/IMPL_PLAN_SPEC.md). The IR's toolchain object is not closed, so the graph projects
# it onto this allowlist rather than echoing it verbatim: the graph's contract is that it
# carries no command and no flag string, and only a fixed declarative key set keeps that
# structural. A new declarative field is added here alongside its build-graph consumer.
TOOLCHAIN_ECHO_KEYS: tuple[str, ...] = (
    "language", "standard", "build_system", "compiler", "linker", "backend",
)
# `compiler` / `linker` are EXECUTABLE SELECTORS — a backend RUNS them as a program (the
# Makefile pins `FC := <compiler>`). Absence of shell metacharacters is not enough: a bare
# `sh`, an absolute `/tmp/payload`, or a traversal `a/../b` are all runnable. They are echoed
# only when they name a RECOGNIZED compiler/linker driver FOR THE BUNDLE'S LANGUAGE — a bare
# program name (no path separator, so the backend resolves it on a trusted PATH), optionally
# version-suffixed and prefixed by a TARGET TRIPLE, case-insensitive (Fujitsu `FCCpx`). A driver
# for the WRONG language would be pinned as `FC` and deterministically fail on the sources
# (`gcc` cannot compile `.f90`), so the allowlist is keyed by language. An unrecognized selector
# is DROPPED and the backend uses its default (gfortran). v1 is fortran-only; a new language
# adds its driver family set here.
EXECUTABLE_TOOLCHAIN_KEYS: frozenset[str] = frozenset({"compiler", "linker"})
COMPILER_SELECTOR_FAMILIES_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "fortran": (
        "gfortran", "flang", "flang-new", "f95", "g95", "ifort", "ifx", "nvfortran",
        "pgfortran", "pgf90", "pgf95", "xlf", "xlf90", "xlf95", "armflang", "crayftn", "ftn",
        "nagfor", "mpif90", "mpifort", "mpif77", "frt", "frtpx",
    ),
}
# A cross-compiler prefix is a GNU target triple, not an arbitrary token: it must START with a
# known CPU architecture, so `payload-gfortran` / `sh-gfortran` (whose prefix is not an
# architecture) is not mistaken for a compiler even though it ends in a family name.
COMPILER_TARGET_TRIPLE_ARCHES: tuple[str, ...] = (
    "x86_64", "x86", "amd64", "i386", "i486", "i586", "i686",
    "aarch64", "aarch64_be", "arm", "armeb", "armv6", "armv7", "armv7l", "thumbv7",
    "powerpc", "powerpc64", "powerpc64le", "ppc", "ppc64", "ppc64le",
    "riscv32", "riscv64", "s390x", "sparc", "sparc64",
    "mips", "mipsel", "mips64", "mips64el", "wasm32", "wasm64", "loongarch64",
)


@lru_cache(maxsize=8)
def _compiler_selector_re(families: tuple[str, ...]) -> "re.Pattern[str] | None":
    """The selector regex for a driver family set (a known family, optionally
    target-triple-prefixed and version-suffixed). `None` when no family is available for the
    language (nothing is a valid compiler → drop)."""
    if not families:
        return None
    return re.compile(
        r"^(?:(?:" + "|".join(re.escape(a) for a in COMPILER_TARGET_TRIPLE_ARCHES)
        + r")(?:-[a-z0-9_]+)*-)?"                                    # optional target-triple prefix
        + r"(?:" + "|".join(re.escape(f) for f in families) + r")"   # a known family
        + r"(?:-[0-9][0-9.]*)?" + _END, re.IGNORECASE)               # optional version suffix
# The remaining fields are declarative selectors / flag values (`language`, `standard`,
# `build_system`, `backend`). They are single tokens a backend passes as a quoted argument;
# they are echoed only when free of whitespace and shell metacharacters (`c++17` is fine).
_TOOLCHAIN_VALUE_RE = re.compile(rf"^[A-Za-z0-9_][A-Za-z0-9_.+-]*{_END}")


def _projected_toolchain(toolchain: Mapping[str, Any],
                         bundle_languages: Sequence[str]) -> dict[str, str]:
    """The declarative toolchain fields safe to carry in the build graph. Non-string values,
    keys outside `TOOLCHAIN_ECHO_KEYS`, a `compiler`/`linker` that is not a recognized driver
    FOR THE BUNDLE'S LANGUAGE(S), and any other value that is not a safe declarative token are
    dropped, so the graph can never hold a runnable command or a shell string."""
    # The driver must compile every language the bundle contains, so intersect the family sets;
    # for v1 (fortran-only) that is just the fortran drivers. An unknown language contributes no
    # families, so the intersection is empty and any compiler/linker is dropped (fail-safe).
    family_sets = [set(COMPILER_SELECTOR_FAMILIES_BY_LANGUAGE.get(lang, ()))
                   for lang in bundle_languages]
    families = set.intersection(*family_sets) if family_sets else set()
    compiler_re = _compiler_selector_re(tuple(sorted(families)))
    out: dict[str, str] = {}
    for key in TOOLCHAIN_ECHO_KEYS:
        value = toolchain.get(key)
        if not isinstance(value, str):
            continue
        if key in EXECUTABLE_TOOLCHAIN_KEYS:
            if compiler_re is not None and compiler_re.fullmatch(value):
                out[key] = value
        elif _TOOLCHAIN_VALUE_RE.fullmatch(value):
            out[key] = value
    return out

# --------------------------------------------------------------------------------------
# Harness capability ABI (A6). A token is `<name>@<abi_version>`; the version is exact,
# never a range. Negotiation matches the whole token, so version ordering never implies
# compatibility — compatibility is declared by adding a token to a manifest.
# --------------------------------------------------------------------------------------

HARNESS_CAPABILITY_ABI_VERSION = 1

CAPABILITY_TOKEN_PATTERN = rf"^[a-z][a-z0-9_]*@[0-9]+{_END}"
_CAPABILITY_TOKEN_RE = re.compile(CAPABILITY_TOKEN_PATTERN)

CAPABILITY_VOCABULARY: frozenset[str] = frozenset({
    "sync_single_case",        # A6 minimum: the current harness_fortran_cpu ABI
    "async_device_resident",   # A6 reserved
    "distributed_state",       # A6 reserved
    "batched_cases",           # A6 reserved
    "full_state_capture",      # A4 / Z6 reserved
    "trusted_reductions",      # A4 / Z6 reserved
    "state_registration",      # Z6 reserved
})
# Every bundle states how it expects to be driven: exactly one of these.
EXECUTION_MODEL_CAPABILITIES: frozenset[str] = frozenset(
    {"sync_single_case", "async_device_resident", "batched_cases"})
# A lowering plan cannot claim a residency the target was not asked to provide.
RESIDENCY_REQUIRED_CAPABILITY: dict[str, str] = {
    "device": "async_device_resident",
    "distributed": "distributed_state",
}
# `harness_registration` capture requires the harness to expose state registration (Z6).
CAPABILITY_FOR_CAPTURE: dict[str, str] = {"harness_registration": "state_registration"}

# Tool-side manifest data, deliberately NOT a section of the harness `controlled_spec.md`:
# adding it there would edit a certified artifact and force recertification for no change
# in generated behavior. When the harness spec is next re-specified on content grounds
# (Z6), the manifest moves into the spec and this table becomes its projection.
#
# `sync_single_case@1` is defined as exactly the canonical interface block of
# `harness_fortran_cpu@0.6.0` §5.1 (13 operations, 5 published types, `dp = float64`
# (rendered `real64`), `case_id_len = 64`). Its mechanical enforcer remains
# `tools/runner_renderer.py:assert_harness_pin`; this contract names the ABI, it does not
# re-check it.
HARNESS_CAPABILITY_MANIFESTS: dict[str, frozenset[str]] = {
    "infrastructure/harness_fortran_cpu@0.6.0": frozenset({"sync_single_case@1"}),
}


# --------------------------------------------------------------------------------------
# Schema loading. The loaders exist so the unit suite can assert the constants above agree
# with the declarative copy, and so a Z2 gateway can hand the schema to a model. Validation
# itself never calls them.
# --------------------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# Parsed schemas cached on CONTENT hash, not mtime: an edit that preserves the mtime (a
# metadata-preserving deploy, a coarse-resolution filesystem) must still be observed. Keying on
# content also shares a parse across identical files. The loaders are cold path (the unit suite
# and, later, the Z2 gateway — never validation, which uses the module constants), so reading
# the small schema file each call to hash it is negligible; only the JSON parse is cached.
_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _load_schema_cached(schema_path_str: str) -> dict[str, Any]:
    """Load a schema JSON, cached on its content so any edit at the same path is observed.
    `RuntimeError` on a missing / malformed schema — the fail-closed contract the shape_expr
    loader uses. The loader reports the path actually requested (it is shared by the bundle and
    the harness-capabilities schema, so a hardcoded canonical path would misdirect)."""
    try:
        raw = Path(schema_path_str).read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"codegen schema not found at {schema_path_str}") from exc
    except OSError as exc:
        raise RuntimeError(f"codegen schema {schema_path_str} is unreadable: {exc}") from exc
    key = hashlib.sha256(raw).hexdigest()
    cached = _SCHEMA_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"codegen schema {schema_path_str} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise RuntimeError(f"codegen schema {schema_path_str} must be a JSON object")
    _SCHEMA_CACHE[key] = doc
    return doc


_load_schema_cached.cache_clear = _SCHEMA_CACHE.clear  # type: ignore[attr-defined]


def load_bundle_schema(repo_root: Path | None = None) -> dict[str, Any]:
    """The declarative `CodegenBundle` grammar (`spec/schema/generate/codegen_bundle.schema.json`).

    A deep copy: the cache holds one shared document, and a caller that mutated it would
    corrupt every later reader in the process."""
    root = repo_root or _repo_root()
    return copy.deepcopy(_load_schema_cached(str(root / BUNDLE_SCHEMA_PATH)))


def load_harness_capabilities_schema(repo_root: Path | None = None) -> dict[str, Any]:
    """The declarative harness capability manifest grammar (a deep copy; see above)."""
    root = repo_root or _repo_root()
    return copy.deepcopy(_load_schema_cached(str(root / HARNESS_CAPABILITIES_SCHEMA_PATH)))


def harness_capability_manifest_document() -> dict[str, Any]:
    """`HARNESS_CAPABILITY_MANIFESTS` in the document form of
    `harness_capabilities.schema.json` — the shape the harness spec adopts at Z6."""
    return {
        "harness_capability_abi_version": HARNESS_CAPABILITY_ABI_VERSION,
        "manifests": [
            {"node_key": node_key, "provides": sorted(provides)}
            for node_key, provides in sorted(HARNESS_CAPABILITY_MANIFESTS.items())
        ],
    }


def harness_capability_manifest_document_for(node_key: str | None) -> dict[str, Any]:
    """The manifest document narrowed to ONE harness's entry — the projection a pure leaf sees.

    A pure producer negotiates against exactly one harness: its node's single `infrastructure`
    direct dependency (`Conductor._pure_harness_node_key`, which `_pure_bundle_violations` also
    resolves the acceptance layer from). Handing it the FULL table instead would show it
    capabilities that another harness provides and its own does not, licensing a
    `capability_requirements` token the acceptance layer then rejects as
    `bundle_capability_unsatisfied` — a repair-budget burn on a bundle the leaf had no way to know
    was unsatisfiable. Narrowing here means the context and the negotiation cannot disagree,
    rather than asking the leaf to filter the table by node_key correctly.

    `None`, or a node_key no manifest declares, yields an EMPTY `manifests` list. That mirrors
    `harness_provided_capabilities`'s fail-closed `None`: a leaf whose harness cannot be resolved
    must be shown nothing, never another harness's capabilities."""
    provides = HARNESS_CAPABILITY_MANIFESTS.get(node_key) if node_key is not None else None
    return {
        "harness_capability_abi_version": HARNESS_CAPABILITY_ABI_VERSION,
        "manifests": ([{"node_key": node_key, "provides": sorted(provides)}]
                      if provides is not None else []),
    }


# --------------------------------------------------------------------------------------
# Field grammar
# --------------------------------------------------------------------------------------

def logical_path_violations(path: Any, *, language: Any) -> list[str]:
    """Canonical `logical_path` rule: relative, POSIX-separated, normalized, confined to
    the assembled source root, language-appropriate extension, no build/script file.

    Uniqueness (a cross-file property) is `bundle_invariant_violations`' job. Clauses are
    prefix-free; the caller prefixes `files[i]` / the artifact path.
    """
    if not isinstance(path, str) or not path:
        return ["logical_path must be a non-empty string"]

    violations: list[str] = []
    if "\\" in path:
        violations.append("logical_path must use POSIX '/' separators")
    if path.startswith("/"):
        violations.append("logical_path must be relative (leading '/' is forbidden)")

    segments = path.split("/")
    if any(seg == "" for seg in segments):
        violations.append("logical_path must not contain an empty segment")
    if any(seg in (".", "..") for seg in segments):
        violations.append("logical_path must not contain a '.' or '..' segment")
    if posixpath.normpath(path) != path:
        violations.append("logical_path must be normalized")
    for seg in segments:
        if seg and seg not in (".", "..") and not _SEGMENT_RE.fullmatch(seg):
            violations.append(
                f"logical_path segment {seg!r} must match {LOGICAL_PATH_SEGMENT_PATTERN}")

    basename = segments[-1]
    _, extension = posixpath.splitext(basename)
    allowed = LANGUAGE_EXTENSION_ALLOWLIST.get(language) if isinstance(language, str) else None
    # One clause per defect (the meta_contracts rule): a reserved build filename is not
    # ALSO reported as a wrong extension, and a forbidden extension is not ALSO reported
    # as an unallowed one.
    if basename in RESERVED_LOGICAL_FILENAMES:
        violations.append(
            f"logical_path basename {basename!r} is a reserved build filename "
            "(the bundle carries no build files)")
    elif extension in FORBIDDEN_EXTENSIONS:
        violations.append(
            f"logical_path extension {extension!r} is a build/script extension "
            "(the bundle carries no build or shell commands)")
    elif allowed is None:
        violations.append(f"logical_path has no extension allowlist for language {language!r}")
    elif extension not in allowed:
        violations.append(
            f"logical_path extension {extension!r} must be one of "
            f"{', '.join(allowed)} for language {language!r}")
    return violations


# A JSON `null` is a PRESENT value, not an absent key. The two are distinguished with an
# explicit sentinel so a required key set to `null` falls through to its own type clause
# instead of being silently accepted by both layers (an absent-key check that fires only
# on `key not in obj`, and a sub-validator that early-returns on `None`).
_MISSING = object()


def _closed_object_violations(obj: Mapping[str, Any], *, required: Sequence[str],
                              optional: Sequence[str], prefix: str) -> list[str]:
    violations: list[str] = []
    for key in required:
        if key not in obj:
            violations.append(f"{prefix}{key} is required")
    known = set(required) | set(optional)
    # key=repr: a JSON object always has string keys, but a Python-constructed doc can mix
    # types, and a gate must not crash on the input it is meant to reject.
    for key in sorted((k for k in obj if k not in known), key=repr):
        violations.append(f"{prefix}unknown key {key!r} (the object is closed)")
    return violations


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def _is_node_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_NODE_KEY_RE.fullmatch(value))


def _is_capability_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_CAPABILITY_TOKEN_RE.fullmatch(value))


def capability_name(token: str) -> str:
    """The name part of a `<name>@<version>` token ('' for a malformed token)."""
    return token.split("@", 1)[0] if isinstance(token, str) and "@" in token else ""


def bundle_schema_version_violations(doc: Mapping[str, Any]) -> list[str]:
    """Version gate. A major mismatch is terminal: nothing else about the document is
    reported, because a different major is a different contract and every other clause
    would be evaluated against the wrong one.

    Compatibility is backward only. A same-major document is validated against the field set
    THIS module implements; a later minor that adds a field is not read here, because the
    closed-object checks reject the unknown key (the closure is a security property, not
    relaxed for forward compatibility). See CODEGEN_BUNDLE_CONTRACT.md "Version compatibility
    is backward only"."""
    version = doc.get("bundle_schema_version", _MISSING)
    if version is _MISSING:
        return ["bundle_schema_version is required"]
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        return ["bundle_schema_version must be a semantic version string (MAJOR.MINOR.PATCH)"]
    major = version.split(".", 1)[0]
    contract_major = CODEGEN_BUNDLE_SCHEMA_VERSION.split(".", 1)[0]
    if major != contract_major:
        return [f"bundle_schema_version major {major} is not supported "
                f"(this contract is {CODEGEN_BUNDLE_SCHEMA_VERSION})"]
    return []


def bundle_schema_violations(doc: Any) -> list[str]:
    """Presence / type / enum / closed-key / per-field grammar violations.

    A non-dict input returns a clause, never an exception: a leaf-authored document is
    untrusted input, and a crash in the gate is a worse failure than a rejection."""
    if not isinstance(doc, dict):
        return ["bundle must be a JSON object"]

    version_violations = bundle_schema_version_violations(doc)
    if version_violations:
        return version_violations

    violations = _closed_object_violations(
        doc, required=REQUIRED_BUNDLE_KEYS, optional=OPTIONAL_BUNDLE_KEYS, prefix="")
    violations += _optimization_unit_schema_violations(doc.get("optimization_unit", _MISSING))
    violations += _files_schema_violations(doc.get("files", _MISSING))
    violations += _entrypoints_schema_violations(doc.get("entrypoints", _MISSING))
    violations += _lowering_plan_schema_violations(doc.get("target_lowering_plan", _MISSING))
    violations += _capability_requirements_schema_violations(
        doc.get("capability_requirements", _MISSING))
    violations += _state_bindings_schema_violations(doc.get("state_bindings", _MISSING))
    return violations


def _optimization_unit_schema_violations(unit: Any) -> list[str]:
    if unit is _MISSING:
        return []
    if not isinstance(unit, dict):
        return ["optimization_unit must be an object"]
    violations = _closed_object_violations(
        unit, required=("members",), optional=(), prefix="optimization_unit.")
    members = unit.get("members", _MISSING)
    if members is _MISSING:
        return violations
    if not isinstance(members, list) or not members:
        violations.append("optimization_unit.members must be a non-empty array")
        return violations
    for index, member in enumerate(members):
        if not _is_node_key(member):
            violations.append(
                f"optimization_unit.members[{index}] must be a node_key "
                "(<spec_kind>/<spec_id>@<spec_version>)")
    return violations


def _files_schema_violations(files: Any) -> list[str]:
    if files is _MISSING:
        return []
    if not isinstance(files, list) or not files:
        return ["files must be a non-empty array"]
    violations: list[str] = []
    for index, entry in enumerate(files):
        prefix = f"files[{index}]."
        if not isinstance(entry, dict):
            violations.append(f"files[{index}] must be an object")
            continue
        violations += _closed_object_violations(
            entry,
            required=("logical_path", "role", "language", "member_node_key", "content", "modules"),
            optional=("compile_after",), prefix=prefix)
        role = entry.get("role")
        if "role" in entry and role not in FILE_ROLES:
            violations.append(
                f"{prefix}role must be one of {', '.join(FILE_ROLES)} "
                "(there is no runner/glue role and no build/script role)")
        language = entry.get("language")
        if "language" in entry and language not in LANGUAGES:
            violations.append(f"{prefix}language must be one of {', '.join(LANGUAGES)}")
        if "logical_path" in entry:
            # The clauses are prefix-free and already start with the field name, so the
            # index prefix composes: "files[0].logical_path must be normalized".
            violations += [
                f"{prefix}{clause}"
                for clause in logical_path_violations(entry.get("logical_path"), language=language)
            ]
        member = entry.get("member_node_key")
        if "member_node_key" in entry and member is not None and not _is_node_key(member):
            violations.append(f"{prefix}member_node_key must be a node_key or null")
        content = entry.get("content")
        if "content" in entry and (not isinstance(content, str) or not content):
            violations.append(f"{prefix}content must be a non-empty string")
        # The Fortran modules this file defines, so the host can resolve an entrypoint's /
        # binding's `module` to the file that owns it (and thereby to a member) without
        # parsing the source. Non-empty: a bundle file publishes through a module.
        if "modules" in entry:
            modules = entry.get("modules")
            if not isinstance(modules, list) or not modules:
                violations.append(f"{prefix}modules must be a non-empty array")
            else:
                for mod_index, module in enumerate(modules):
                    if not _is_identifier(module):
                        violations.append(f"{prefix}modules[{mod_index}] must be an identifier")
        if "compile_after" in entry:
            after = entry.get("compile_after")
            if not isinstance(after, list):
                violations.append(f"{prefix}compile_after must be an array")
            else:
                for after_index, dep in enumerate(after):
                    if not isinstance(dep, str) or not dep:
                        violations.append(
                            f"{prefix}compile_after[{after_index}] must be a non-empty string")
    return violations


def _entrypoints_schema_violations(entrypoints: Any) -> list[str]:
    if entrypoints is _MISSING:
        return []
    if not isinstance(entrypoints, list) or not entrypoints:
        return ["entrypoints must be a non-empty array"]
    violations: list[str] = []
    for index, entry in enumerate(entrypoints):
        prefix = f"entrypoints[{index}]."
        if not isinstance(entry, dict):
            violations.append(f"entrypoints[{index}] must be an object")
            continue
        violations += _closed_object_violations(
            entry, required=("symbol", "kind", "node_key", "defined_in", "module"), optional=(),
            prefix=prefix)
        if "symbol" in entry and not _is_identifier(entry.get("symbol")):
            violations.append(f"{prefix}symbol must be an identifier")
        # The Fortran module that publishes `symbol`, so the host can render
        # `use <module>, only: <symbol>` mechanically — a file may define several modules or
        # name them freely, and the host never parses the source to discover which.
        if "module" in entry and not _is_identifier(entry.get("module")):
            violations.append(f"{prefix}module must be an identifier")
        if "kind" in entry and entry.get("kind") not in ENTRYPOINT_KINDS:
            violations.append(f"{prefix}kind must be one of {', '.join(ENTRYPOINT_KINDS)}")
        if "node_key" in entry and not _is_node_key(entry.get("node_key")):
            violations.append(f"{prefix}node_key must be a node_key")
        defined_in = entry.get("defined_in")
        if "defined_in" in entry and (not isinstance(defined_in, str) or not defined_in):
            violations.append(f"{prefix}defined_in must be a non-empty string")
    return violations


def _lowering_plan_schema_violations(plan: Any) -> list[str]:
    if plan is _MISSING:
        return []
    if not isinstance(plan, dict):
        return ["target_lowering_plan must be an object"]
    violations = _closed_object_violations(
        plan, required=LOWERING_PLAN_REQUIRED_KEYS, optional=LOWERING_PLAN_OPTIONAL_KEYS,
        prefix="target_lowering_plan.")
    if "precision" in plan and not isinstance(plan.get("precision"), dict):
        violations.append("target_lowering_plan.precision must be an object")
    if "state_residency" in plan and plan.get("state_residency") not in STATE_RESIDENCIES:
        violations.append(
            f"target_lowering_plan.state_residency must be one of {', '.join(STATE_RESIDENCIES)}")
    # The envelope is closed on keys and OPEN on values: an optional section's interior is
    # a target-backend extension point (A5) and is unconstrained in v1.
    for key in LOWERING_PLAN_OPTIONAL_KEYS:
        if key == "fusion" or key not in plan:
            continue
        if not isinstance(plan.get(key), dict):
            violations.append(f"target_lowering_plan.{key} must be an object")
    fusion = plan.get("fusion")
    if "fusion" in plan:
        if not isinstance(fusion, list):
            violations.append("target_lowering_plan.fusion must be an array")
        else:
            for index, group in enumerate(fusion):
                prefix = f"target_lowering_plan.fusion[{index}]."
                if not isinstance(group, dict):
                    violations.append(f"target_lowering_plan.fusion[{index}] must be an object")
                    continue
                violations += _closed_object_violations(
                    group, required=("members",), optional=(), prefix=prefix)
                members = group.get("members", _MISSING)
                if members is _MISSING:
                    continue
                if not isinstance(members, list) or not members:
                    violations.append(f"{prefix}members must be a non-empty array")
                    continue
                for member_index, member in enumerate(members):
                    if not _is_node_key(member):
                        violations.append(f"{prefix}members[{member_index}] must be a node_key")
    return violations


def _capability_requirements_schema_violations(required: Any) -> list[str]:
    if required is _MISSING:
        return []
    if not isinstance(required, list) or not required:
        return ["capability_requirements must be a non-empty array"]
    violations: list[str] = []
    for index, token in enumerate(required):
        if not _is_capability_token(token):
            violations.append(
                f"capability_requirements[{index}] must be a capability token "
                f"matching {CAPABILITY_TOKEN_PATTERN}")
    return violations


def _state_bindings_schema_violations(bindings: Any) -> list[str]:
    if bindings is _MISSING:
        return []
    if not isinstance(bindings, list):
        return ["state_bindings must be an array"]
    violations: list[str] = []
    for index, entry in enumerate(bindings):
        prefix = f"state_bindings[{index}]."
        if not isinstance(entry, dict):
            violations.append(f"state_bindings[{index}] must be an object")
            continue
        violations += _closed_object_violations(
            entry,
            required=("node_key", "state_variable", "storage_symbol", "module", "capture",
                      "capability"),
            optional=(), prefix=prefix)
        if "node_key" in entry and not _is_node_key(entry.get("node_key")):
            violations.append(f"{prefix}node_key must be a node_key")
        # `module` publishes `storage_symbol`, so the host renders `use <module>, only:
        # <storage_symbol>` mechanically — a member may have several checks files with freely
        # chosen module names, and the host never parses the source to find the exporter.
        for key in ("state_variable", "storage_symbol", "module"):
            if key in entry and not _is_identifier(entry.get(key)):
                violations.append(f"{prefix}{key} must be an identifier")
        if "capture" in entry and entry.get("capture") not in STATE_CAPTURES:
            violations.append(f"{prefix}capture must be one of {', '.join(STATE_CAPTURES)}")
        capability = entry.get("capability")
        if "capability" in entry and capability is not None and not _is_capability_token(capability):
            violations.append(f"{prefix}capability must be a capability token or null")
    return violations


# --------------------------------------------------------------------------------------
# Cross-field invariants
# --------------------------------------------------------------------------------------

def optimization_unit_members(doc: Mapping[str, Any]) -> tuple[str, ...]:
    """The unit's ordered member node_keys. The ordered list IS the unit identity — the
    order is a contract input to code generation, so no derived `unit_id` is stored."""
    unit = doc.get("optimization_unit")
    members = unit.get("members") if isinstance(unit, dict) else None
    return tuple(members) if isinstance(members, list) else ()


def bundle_invariant_violations(doc: Mapping[str, Any]) -> list[str]:
    """Cross-field contract. Assumes a schema-sound document (`validate_bundle` runs the
    schema layer first), so no field's presence or type is re-defended here."""
    violations: list[str] = []
    members = optimization_unit_members(doc)
    member_set = set(members)
    files: list[Mapping[str, Any]] = list(doc.get("files") or [])
    entrypoints: list[Mapping[str, Any]] = list(doc.get("entrypoints") or [])
    plan: Mapping[str, Any] = doc.get("target_lowering_plan") or {}
    required_tokens: list[str] = list(doc.get("capability_requirements") or [])
    bindings: list[Mapping[str, Any]] = list(doc.get("state_bindings") or [])

    seen_members: set[str] = set()
    for member in members:
        if member in seen_members:
            violations.append(f"optimization_unit.members must not repeat {member!r}")
        seen_members.add(member)

    # files: case-folded uniqueness + member/role coupling
    seen_paths: dict[str, str] = {}
    for index, entry in enumerate(files):
        path = entry.get("logical_path")
        folded = path.casefold() if isinstance(path, str) else ""
        if folded in seen_paths:
            violations.append(
                f"files[{index}].logical_path {path!r} collides with {seen_paths[folded]!r} "
                "after case folding (logical_path is unique on a case-insensitive filesystem)")
        else:
            seen_paths[folded] = path if isinstance(path, str) else ""
        role = entry.get("role")
        member = entry.get("member_node_key")
        if member is None:
            if role not in UNIT_SHAREABLE_ROLES:
                violations.append(
                    f"files[{index}].member_node_key may be null only for role "
                    f"{' / '.join(sorted(UNIT_SHAREABLE_ROLES))} (role {role!r} belongs to one member)")
        elif member not in member_set:
            violations.append(
                f"files[{index}].member_node_key {member!r} is not a member of optimization_unit")

    # The build graph keys objects on the derived object name, so two files deriving the
    # same object (`a/b.f90` and `a__b.f90` both flatten to `a__b.o`) would silently
    # compile to one object and drop the other from the link. Compared case-folded, for the
    # same reason logical_path is: on a case-insensitive filesystem `A__B.o` and `a__b.o`
    # are one file.
    seen_objects: dict[str, str] = {}
    for index, entry in enumerate(files):
        path = entry.get("logical_path")
        if not isinstance(path, str) or not path:
            continue
        obj = _object_name(path).casefold()
        # A path already reported as a case-folded duplicate is ONE defect: do not also
        # report the object name it necessarily shares.
        if obj in seen_objects and seen_objects[obj].casefold() != path.casefold():
            violations.append(
                f"files[{index}].logical_path {path!r} derives the same object name as "
                f"{seen_objects[obj]!r}")
        else:
            seen_objects.setdefault(obj, path)

    files_by_path = {
        entry.get("logical_path"): entry for entry in files
        if isinstance(entry.get("logical_path"), str)
    }

    # module -> defining file. A Fortran module name is globally unique in a build (one `.mod`
    # per module), and case-insensitive, so a name defined by two files is a violation. This
    # map ties an entrypoint's / binding's `module` to the file (and thus member) that owns it.
    module_owner: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(files):
        modules = entry.get("modules")
        if not isinstance(modules, list):
            continue
        for module in modules:
            if not isinstance(module, str):
                continue
            folded = module.casefold()
            if folded in module_owner:
                violations.append(
                    f"files[{index}].modules {module!r} is already defined by another file "
                    "(a Fortran module name is unique across the build)")
            else:
                module_owner[folded] = entry

    # compile_after: explicit inter-file compile-order edges. Each must resolve to another
    # bundle file, must not name itself, and the whole edge set must be acyclic — otherwise
    # derive_build_graph could not produce a topological order. Role precedence orders files
    # of DIFFERENT roles; compile_after is what orders two files of the SAME role when one
    # `use`s a module the other defines (which role precedence alone cannot express).
    compile_after_edges: dict[str, list[str]] = {}
    for index, entry in enumerate(files):
        path = entry.get("logical_path")
        after = entry.get("compile_after")
        if not isinstance(after, list) or not isinstance(path, str):
            continue
        resolved: list[str] = []
        for dep in after:
            if dep == path:
                violations.append(
                    f"files[{index}].compile_after must not name the file itself ({path!r})")
            elif dep not in files_by_path:
                violations.append(
                    f"files[{index}].compile_after {dep!r} does not name a files[] entry")
            elif _role_rank(files_by_path[dep].get("role")) > _role_rank(entry.get("role")):
                # An edge may refine order WITHIN a role, or agree with role precedence — it
                # must never REVERSE it. `model.compile_after = [checks]` would force checks
                # before model, contradicting ROLE_BUILD_PRECEDENCE and breaking the M3c
                # order (checks `use`s the model).
                violations.append(
                    f"files[{index}].compile_after {dep!r} has role "
                    f"{files_by_path[dep].get('role')!r}, which build precedence orders after "
                    f"this {entry.get('role')!r} file (compile_after must not reverse "
                    "ROLE_BUILD_PRECEDENCE)")
            else:
                resolved.append(dep)
        compile_after_edges[path] = resolved
    cycle = _compile_after_cycle(compile_after_edges)
    if cycle:
        violations.append(f"files compile_after has a dependency cycle involving {cycle}")

    # entrypoints: reference resolution, role agreement, the privacy invariant, and the
    # attribution invariant (the file that defines a member's entrypoint belongs to THAT
    # member — otherwise "every member is independently addressable" holds only on paper)
    seen_symbols: set[Any] = set()
    for index, entry in enumerate(entrypoints):
        prefix = f"entrypoints[{index}]."
        node_key = entry.get("node_key")
        if node_key not in member_set:
            violations.append(
                f"{prefix}node_key {node_key!r} is not a member of optimization_unit")
        # Uniqueness is scoped to `(module, symbol)`, case-folded (Fortran is case-insensitive
        # in both). A symbol is module-qualified, so each member's checks module legitimately
        # exports the same fixed ABI name (`case_run`, `get_r1`): `a_checks::case_run` and
        # `b_checks::case_run` are distinct procedures. Only the SAME name in the SAME module is
        # an unlinkable duplicate.
        symbol = entry.get("symbol")
        module = entry.get("module")
        symbol_key = (module.casefold() if isinstance(module, str) else module,
                      symbol.casefold() if isinstance(symbol, str) else symbol)
        if symbol_key in seen_symbols:
            violations.append(
                f"{prefix}symbol {symbol!r} is published more than once by module {module!r}")
        seen_symbols.add(symbol_key)
        defined_in = entry.get("defined_in")
        target = files_by_path.get(defined_in)
        if target is None:
            violations.append(f"{prefix}defined_in {defined_in!r} does not name a files[] entry")
            continue
        role = target.get("role")
        if role not in ROLE_FOR_ENTRYPOINT_KIND.values():
            violations.append(
                f"{prefix}defined_in {defined_in!r} has role {role!r}, which is private "
                "and cannot define an entrypoint")
            continue
        expected_role = ROLE_FOR_ENTRYPOINT_KIND.get(entry.get("kind"))
        if expected_role is not None and role != expected_role:
            violations.append(
                f"{prefix}kind {entry.get('kind')!r} must be defined in a file of role "
                f"{expected_role!r}, not {role!r}")
        elif target.get("member_node_key") != node_key:
            violations.append(
                f"{prefix}defined_in {defined_in!r} belongs to member "
                f"{target.get('member_node_key')!r}, not to {node_key!r}")
        # `module` must be one the `defined_in` file actually defines — otherwise `defined_in`
        # could point at the member's own file (passing the ownership check) while `module` /
        # `symbol` route the rendered `use <module>, only: <symbol>` to another member's export.
        module = entry.get("module")
        target_modules = target.get("modules")
        if (isinstance(module, str) and isinstance(target_modules, list)
                and module.casefold() not in {m.casefold() for m in target_modules
                                              if isinstance(m, str)}):
            violations.append(
                f"{prefix}module {module!r} is not defined by {defined_in!r} "
                f"(its modules are {target_modules})")

    # coverage: every member stays independently addressable in the generated code
    for member in members:
        has_model = any(
            entry.get("role") == "model" and entry.get("member_node_key") == member
            for entry in files)
        if not has_model:
            violations.append(f"optimization_unit member {member!r} has no files[] entry of role model")
        # Operation cardinality is by node KIND. A `problem` node publishes exactly one
        # operation — its single integration update path — so two would leave the host with no
        # rule to pick THE update path. A `component` / `infrastructure` node publishes an API of
        # one or more operations (the harness ABI is many). A `profile` publishes EXACTLY ZERO:
        # it is consumed through its selection result, not a call (`phase_02_generate.md`), so an
        # operation entrypoint is an invented callable interface the Generate contract forbids.
        # (Multiple `checks_interface` entrypoints are always fine — a fixed ABI, not the
        # published operation.)
        operation_count = sum(
            1 for entry in entrypoints
            if entry.get("kind") == "operation" and entry.get("node_key") == member)
        spec_kind = member.split("/", 1)[0] if isinstance(member, str) and "/" in member else ""
        if spec_kind == "profile":
            if operation_count > 0:
                violations.append(
                    f"optimization_unit member {member!r} is a profile and publishes no "
                    f"operation, but has {operation_count} operation entrypoint(s)")
        elif operation_count == 0:
            violations.append(f"optimization_unit member {member!r} has no operation entrypoint")
        elif spec_kind == "problem" and operation_count > 1:
            violations.append(
                f"optimization_unit member {member!r} has {operation_count} operation "
                "entrypoints; a problem node publishes exactly one (its integration update path)")

    # capability tokens: closed vocabulary, duplicate-free, exactly one execution model
    seen_tokens: set[str] = set()
    execution_tokens: list[str] = []
    for index, token in enumerate(required_tokens):
        name = capability_name(token)
        if name not in CAPABILITY_VOCABULARY:
            violations.append(
                f"capability_requirements[{index}] name {name!r} is not a known harness "
                "capability (an unrecognized capability is never assumed available)")
        if token in seen_tokens:
            violations.append(f"capability_requirements must not repeat {token!r}")
        seen_tokens.add(token)
        if name in EXECUTION_MODEL_CAPABILITIES:
            execution_tokens.append(token)
    if len(execution_tokens) != 1:
        violations.append(
            "capability_requirements must declare exactly one execution-model capability "
            f"({' / '.join(sorted(EXECUTION_MODEL_CAPABILITIES))}), found "
            f"{len(execution_tokens)}")

    # lowering plan: fusion membership + the residency/capability coupling
    for index, group in enumerate(plan.get("fusion") or []):
        for member in group.get("members") or []:
            if member not in member_set:
                violations.append(
                    f"target_lowering_plan.fusion[{index}].members {member!r} is not a member "
                    "of optimization_unit")
    residency = plan.get("state_residency")
    needed = RESIDENCY_REQUIRED_CAPABILITY.get(residency) if isinstance(residency, str) else None
    if needed is not None and not any(capability_name(t) == needed for t in required_tokens):
        violations.append(
            f"target_lowering_plan.state_residency {residency!r} requires a {needed}@N "
            "capability in capability_requirements")

    # state bindings: the capture/capability coupling, in both directions
    registration_tokens_bound: set[str] = set()
    seen_binding_keys: set[tuple[Any, Any]] = set()
    seen_registration_storage: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(bindings):
        prefix = f"state_bindings[{index}]."
        node_key = entry.get("node_key")
        node_is_member = node_key in member_set
        if not node_is_member:
            violations.append(f"{prefix}node_key {node_key!r} is not a member of optimization_unit")
        # `(node_key, state_variable)` is a member's primary-state identity. A second binding
        # for the same pair leaves the mapping ambiguous — two consumers could register or
        # read different storage for one declared state. Checked before the capture-specific
        # coupling below.
        binding_key = (node_key, entry.get("state_variable"))
        if binding_key in seen_binding_keys:
            violations.append(
                f"{prefix}duplicate binding for state_variable {entry.get('state_variable')!r} "
                f"of member {node_key!r}")
        seen_binding_keys.add(binding_key)
        # `module` publishes `storage_symbol`, so it must be defined by a `checks`-role file
        # OWNED BY THIS MEMBER — for EITHER capture. Otherwise a binding for member A could name
        # member B's checks module and capture/register B's storage as A's state, and the
        # rendered `use <module>, only: <storage_symbol>` would either miscompile or bind the
        # wrong member's state. (Reported only for a member node — a non-member node_key is
        # already flagged above.)
        module = entry.get("module")
        owner = module_owner.get(module.casefold()) if isinstance(module, str) else None
        if node_is_member and (owner is None or owner.get("role") != "checks"
                               or owner.get("member_node_key") != node_key):
            violations.append(
                f"{prefix}module {module!r} must be defined by a checks-role file owned by "
                f"member {node_key!r} (it publishes {entry.get('storage_symbol')!r})")
        capture = entry.get("capture")
        capability = entry.get("capability")
        if capture == "checks_getter":
            if capability is not None:
                violations.append(
                    f"{prefix}capability must be null for capture 'checks_getter' "
                    "(no harness capability is involved)")
        elif capture == "harness_registration":
            # For harness_registration the `storage_symbol` is the ACTUAL registered storage
            # (`q_storage`), not a name-dispatched rank getter, so two state variables sharing
            # one `(module, storage_symbol)` would register the same storage for two semantic
            # states — silently corrupt evidence. Reject the duplicate. (A `checks_getter`
            # shares a rank getter like `get_r1` across same-rank variables and is disambiguated
            # by `state_variable` at the call, so it is intentionally NOT constrained here.)
            module_val = module if isinstance(module, str) else ""
            storage = entry.get("storage_symbol")
            storage_val = storage if isinstance(storage, str) else ""
            storage_key = (module_val.casefold(), storage_val.casefold())
            if storage_key in seen_registration_storage:
                violations.append(
                    f"{prefix}storage target {module_val}::{storage_val} is already registered "
                    f"by state_variable {seen_registration_storage[storage_key]!r} — one "
                    "registered storage serves one state")
            else:
                seen_registration_storage[storage_key] = entry.get("state_variable")
            expected = CAPABILITY_FOR_CAPTURE["harness_registration"]
            if capability_name(capability) != expected:
                violations.append(
                    f"{prefix}capture 'harness_registration' requires a {expected}@N capability")
            elif capability not in required_tokens:
                violations.append(
                    f"{prefix}capability {capability!r} is not declared in capability_requirements")
            else:
                registration_tokens_bound.add(capability)
    # The reverse coupling is PER TOKEN: each declared state_registration@N must have a
    # binding that uses that exact token. A single @1 binding does not license an unused
    # @2 requirement — that would negotiate a wider ABI than the bundle uses.
    for token in required_tokens:
        if (capability_name(token) == "state_registration"
                and token not in registration_tokens_bound):
            violations.append(
                f"capability_requirements declares {token} but no state_bindings[] entry "
                "captures through 'harness_registration' with it")
    return violations


def validate_bundle(doc: Any) -> list[str]:
    """Canonical entry point. Returns the violation clauses; an empty list is a valid bundle.

    The schema layer runs first and the invariants run only on a structurally sound
    document, so an invariant check never has to defend against a missing or mistyped
    field (and one defect is never reported twice, once per layer)."""
    violations = bundle_schema_violations(doc)
    if violations:
        return violations
    return bundle_invariant_violations(doc)


# --------------------------------------------------------------------------------------
# Capability negotiation (pure, fail-closed)
# --------------------------------------------------------------------------------------

def _token_clause(value: Any) -> str:
    """A reportable string for an unsatisfied requirement. The return type of the
    negotiation is `list[str]`, so a caller may join it; a non-string requirement is
    reported by its repr rather than relocating the crash into the caller."""
    return value if isinstance(value, str) else repr(value)


def _as_token_list(value: Any) -> list[Any] | None:
    """A capability-token collection as a list, or `None` when the value is not one.

    A `str` is rejected on purpose: iterating it would negotiate character by character. A
    `Mapping` is rejected too: iterating a dict yields its KEYS, so `{"sync_single_case@1":
    false}` would silently be read as providing that token — a fail-open. Any other iterable
    (list, tuple, set, frozenset, generator) is accepted — a caller passing the `frozenset`
    that `harness_provided_capabilities` returns is the natural symmetric call, and treating
    it as "not a token list" would fail OPEN."""
    if value is None or isinstance(value, (str, bytes, collections.abc.Mapping)):
        return None
    if not isinstance(value, collections.abc.Iterable):
        return None
    return list(value)


def harness_capability_manifest_violations(doc: Any) -> list[str]:
    """Canonical validator of a harness capability manifest document
    (`spec/schema/generate/harness_capabilities.schema.json`).

    The manifests themselves live in `HARNESS_CAPABILITY_MANIFESTS` as tool-side data;
    this function is what checks that data — and, at Z6, the spec-side `capabilities`
    section that replaces it — against the declared shape."""
    if not isinstance(doc, dict):
        return ["harness capability manifest must be a JSON object"]
    violations = _closed_object_violations(
        doc, required=("harness_capability_abi_version", "manifests"), optional=(), prefix="")
    abi = doc.get("harness_capability_abi_version", _MISSING)
    if abi is not _MISSING and (isinstance(abi, bool) or not isinstance(abi, int) or abi < 1):
        violations.append("harness_capability_abi_version must be a positive integer")
    elif abi is not _MISSING and abi != HARNESS_CAPABILITY_ABI_VERSION:
        # The generation is pinned, not a floor. Reading a later-generation manifest under
        # this generation's semantics would assume forward compatibility — the same
        # assumption the exact `name@version` token match exists to refuse, one level up.
        # It mirrors the bundle side, where a `bundle_schema_version` major mismatch is
        # terminal.
        violations.append(
            f"harness_capability_abi_version {abi} is not supported "
            f"(this contract is generation {HARNESS_CAPABILITY_ABI_VERSION})")
    manifests = doc.get("manifests", _MISSING)
    if manifests is _MISSING:
        return violations
    if not isinstance(manifests, list) or not manifests:
        violations.append("manifests must be a non-empty array")
        return violations
    seen_nodes: set[str] = set()
    for index, entry in enumerate(manifests):
        prefix = f"manifests[{index}]."
        if not isinstance(entry, dict):
            violations.append(f"manifests[{index}] must be an object")
            continue
        violations += _closed_object_violations(
            entry, required=("node_key", "provides"), optional=(), prefix=prefix)
        node_key = entry.get("node_key", _MISSING)
        if node_key is _MISSING:
            pass  # already reported as a missing required key; one clause per defect
        elif not _is_node_key(node_key) or not str(node_key).startswith("infrastructure/"):
            violations.append(f"{prefix}node_key must be an infrastructure node_key")
        elif node_key in seen_nodes:
            violations.append(f"{prefix}node_key {node_key!r} is declared more than once")
        else:
            seen_nodes.add(node_key)
        provides = entry.get("provides", _MISSING)
        if provides is _MISSING:
            continue
        if not isinstance(provides, list) or not provides:
            violations.append(f"{prefix}provides must be a non-empty array")
            continue
        seen_tokens: set[str] = set()
        for token_index, token in enumerate(provides):
            if not _is_capability_token(token) or capability_name(token) not in CAPABILITY_VOCABULARY:
                violations.append(
                    f"{prefix}provides[{token_index}] must be a capability token whose name is "
                    "in the harness capability vocabulary")
                continue
            if token in seen_tokens:
                violations.append(f"{prefix}provides must not repeat {token!r}")
            seen_tokens.add(token)
    return violations


def harness_provided_capabilities(node_key: str) -> frozenset[str] | None:
    """The capability set a harness node provides, or `None` when no manifest declares it.

    `None` means "nothing is provided": an undeclared harness satisfies no requirement.
    It is distinguished from the empty set only so a caller can report the two differently."""
    return HARNESS_CAPABILITY_MANIFESTS.get(node_key)


def unsatisfied_capability_requirements(
        required: Iterable[str], provided: Iterable[str] | None) -> list[str]:
    """The required tokens the target does not provide, in requirement order.

    Matching is on the WHOLE `name@version` token: `sync_single_case@2` is not satisfied by
    a harness providing `sync_single_case@1`. Version ordering never implies compatibility;
    compatibility is declared by adding a token to a manifest.

    A token outside `CAPABILITY_VOCABULARY` (or malformed) is reported as unsatisfied even
    when a manifest happens to list it — an unrecognized capability is never negotiated.

    Untrusted input yields a result, never an exception, and never a fail-open. A `required`
    that is not a non-empty token collection — `None`, a bare string, a number, a `Mapping`
    (whose iteration yields keys, not tokens), or an EMPTY collection — is reported as
    unsatisfiable, never as "nothing required": the two directions are not symmetric, and a
    gate whose degenerate input means "satisfied" is not a gate. (`provided` is the opposite
    case by contract: `None` or an empty collection there IS "nothing provided", which is
    already fail-closed, and an entry that is not a token string — including a `Mapping`'s
    keys — is discarded because it cannot satisfy anything.)

    The report is a list of strings in requirement order. An unordered `required` (a set —
    `harness_provided_capabilities` returns a `frozenset`, so a caller can hold both sides
    that way) is reported in a deterministic order instead of an arbitrary one."""
    required_tokens = _as_token_list(required)
    if required_tokens is None:
        return [_token_clause(required)]
    if not required_tokens:
        # An empty requirement set is not a valid negotiation input in this contract (a bundle
        # declares at least one execution-model capability). Reading empty as "all satisfied"
        # would be a fail-open, so it is reported as an unmet requirement.
        return ["capability_requirements must declare at least one capability"]
    if isinstance(required, collections.abc.Set):
        # A set carries no order, so report it in a stable one. Every other iterable
        # (list, tuple, generator, deque) HAS an order and keeps it.
        required_tokens = sorted(required_tokens, key=repr)
    available = frozenset(item for item in (_as_token_list(provided) or ()) if isinstance(item, str))
    unsatisfied: list[str] = []
    for token in required_tokens:
        if not _is_capability_token(token) or capability_name(token) not in CAPABILITY_VOCABULARY:
            unsatisfied.append(_token_clause(token))
            continue
        if token not in available:
            unsatisfied.append(token)
    return unsatisfied


# --------------------------------------------------------------------------------------
# Deterministic build-graph derivation
# --------------------------------------------------------------------------------------

def _object_name(logical_path: str) -> str:
    """The object basename for a bundle/glue source. A flat `<name>.f90` yields
    `<name>.o` (parity with the current Makefile); a nested path is flattened with `__`
    so two files with the same basename in different directories cannot collide."""
    stem, _ = posixpath.splitext(logical_path)
    return stem.replace("/", "__") + ".o"


def _spec_id_of_node_key(node_key: str) -> str:
    """The bare `spec_id` of a `node_key` (`<spec_kind>/<spec_id>@<spec_version>`) — the
    basename the dependency closure and its staged `<spec_id>_model.f90` are keyed on."""
    if not isinstance(node_key, str) or "/" not in node_key:
        return ""
    return node_key.split("/", 1)[1].split("@", 1)[0]


def _role_rank(role: Any) -> int:
    """Build-precedence position of a role (lower compiles first). An unknown role sorts
    last; the schema layer has already restricted `role` to `FILE_ROLES` by the time the
    invariants that use this run."""
    return ROLE_BUILD_PRECEDENCE.index(role) if role in ROLE_BUILD_PRECEDENCE else len(
        ROLE_BUILD_PRECEDENCE)


def _bundle_file_sort_key(entry: Mapping[str, Any], members: Sequence[str]) -> tuple[int, int, str]:
    role_rank = _role_rank(entry.get("role"))
    member = entry.get("member_node_key")
    # A unit-shared file (member_node_key: null) precedes the member-specific files of the
    # same role: it is what they may `use`.
    member_rank = members.index(member) if member in members else -1
    return (role_rank, member_rank, str(entry.get("logical_path")))


def _topological_order(base_order: Sequence[str],
                       edges: Mapping[str, Sequence[str]]) -> list[str]:
    """Stable topological order of `base_order` honoring `edges` (each `dep` in
    `edges[p]` must precede `p`). Ties break by position in `base_order`, so the result is
    a deterministic function of `base_order` alone.

    Returns a PARTIAL order (shorter than `base_order`) only when a cycle is present; a
    valid bundle has none, because `bundle_invariant_violations` rejects a compile_after
    cycle. The caller appends any leftover deterministically so a graph is still produced.
    """
    priority = {p: i for i, p in enumerate(base_order)}
    successors: dict[str, list[str]] = {p: [] for p in base_order}
    indegree: dict[str, int] = {p: 0 for p in base_order}
    for p in base_order:
        for dep in dict.fromkeys(edges.get(p, ())):  # dedup so indegree matches edge count
            if dep in indegree and dep != p:
                successors[dep].append(p)
                indegree[p] += 1
    ready = [priority[p] for p in base_order if indegree[p] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node = base_order[heapq.heappop(ready)]
        order.append(node)
        for succ in successors[node]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                heapq.heappush(ready, priority[succ])
    return order


def _compile_after_cycle(edges: Mapping[str, Sequence[str]]) -> list[str]:
    """The nodes that cannot be topologically ordered under `edges` (they sit in or behind a
    compile_after cycle), sorted. Empty when the edge set is acyclic."""
    nodes = sorted(set(edges) | {dep for deps in edges.values() for dep in deps})
    ordered = set(_topological_order(nodes, edges))
    return sorted(node for node in nodes if node not in ordered)


def derive_build_graph(doc: Mapping[str, Any], *,
                       dependency_closure: Sequence[str] = (),
                       toolchain: Mapping[str, str],
                       host_glue_sources: Sequence[str] = (),
                       dependency_edges: Mapping[str, Iterable[str]] | None = None) -> dict[str, Any]:
    """Derive the compile/link graph of a valid bundle. Pure data — NO command strings.

    Command synthesis belongs to the target backend (Z2). A graph that could carry a
    command would reintroduce the build authority the file-role and path rules exist to
    deny, so there is no slot for one here.

    `dependency_closure` is the deepest-first dependency **node_key** list
    (`workflow_conductor._dependency_closure_nodes` semantics); each dependency contributes
    the staged `<spec_id>_model.f90`. node_keys, not bare spec_ids, so a dependency absorbed
    into this optimization unit is matched by its exact identity (a distinct dependency that
    merely shares a `spec_id` with a member — `component/foo@2.0.0` vs a `component/foo@1.0.0`
    member — is NOT dropped; it stays, and the `<spec_id>_model.o` basename collision it then
    forms surfaces loudly rather than silently omitting an implementation).
    `host_glue_sources` are the host-rendered contract-boundary files (today:
    `<spec_id>_runner.f90`), which link last. `dependency_edges` (optional) maps each closure
    node_key to the node_keys it depends on; when supplied it rejects a staged dependency that
    depends on an absorbed optimization-unit member (an unbuildable straddle). It is derived
    from the dependency-graph sidecar, not from the flat closure's order.

    Order derives from roles and the bundle's declared `compile_after` edges — never from
    `use`-statement analysis of the generated source: dependency closure → bundle files by
    `ROLE_BUILD_PRECEDENCE` (tie-broken by unit-member order then `logical_path`), refined by
    a stable topological sort over `compile_after` so a file compiles after any bundle file
    it declares a dependency on → host glue. `prerequisite_objects` is the conservative total
    order, the same safe convention the current deterministic Makefile uses.

    Determinism: permuting `files[]` does not change the result — `json.dumps(graph,
    sort_keys=True)` is byte-identical.

    Raises `RuntimeError` when two sources of any origin derive the same object name. The
    within-bundle case is already a `validate_bundle` clause; the cross-origin case is a
    defect in the HOST's assembly inputs (the closure, the glue), which no bundle validator
    can see, so assembly is where it fails closed.
    """
    members = optimization_unit_members(doc)
    files = [entry for entry in (doc.get("files") or []) if isinstance(entry, dict)]

    # The closure is node_keys (`_dependency_closure_nodes`), NOT bare spec_ids (the shape the
    # older `_dependency_closure` returns). A bare id would derive an empty spec_id and emit a
    # corrupt `staged:_model.f90` / `_model.o`; fail closed on a malformed entry instead.
    malformed = [nk for nk in dependency_closure if not _is_node_key(nk)]
    if malformed:
        raise RuntimeError(
            f"dependency_closure entries must be node_keys, got malformed {malformed} — pass "
            "`_dependency_closure_nodes` (node_keys), not `_dependency_closure` (bare spec_ids)")

    # A dependency absorbed into this optimization unit as a member is generated INSIDE the
    # bundle (its own model file), so it must NOT also be staged from the closure: staging it
    # would either collide on `<spec_id>_model.o` or link two implementations of the same
    # member. Matched by EXACT node_key (not bare spec_id): a distinct dependency that only
    # shares a spec_id with a member is kept, and its basename collision is surfaced below.
    # The closure can be passed whole; membership is a bundle property, reconciled here.
    member_set = set(members)
    staged_nodes = [nk for nk in dependency_closure if nk not in member_set]

    # The staged deps all compile before the bundle. A staged dep that DEPENDS ON an absorbed
    # member would `use` a module the bundle provides but compile before it — an unbuildable
    # `bundle → staged → bundle` straddle. This needs actual dependency EDGES: the flat closure
    # is a global topological order, and two INDEPENDENT branches can be ordered `(member,
    # staged)` with no dependency between them (position is not ancestry). When the caller
    # supplies `dependency_edges` (each node_key → the node_keys it depends on, from the
    # `dependency_graph.json` sidecar), a proven straddle fails closed; without them the check is
    # skipped, so a single-node closure and independent branches are never false-rejected.
    if dependency_edges is not None:
        for staged_node in staged_nodes:
            crossing = sorted(m for m in (dependency_edges.get(staged_node) or ())
                              if m in member_set)
            if crossing:
                raise RuntimeError(
                    f"optimization unit straddles a staged dependency: staged {staged_node!r} "
                    f"depends on absorbed member(s) {crossing}, whose implementation the bundle "
                    "provides and compiles after it. Absorb the dependent into the unit, or "
                    "leave the member unfused.")

    staged = [_spec_id_of_node_key(nk) for nk in staged_nodes]

    sources: list[str] = [f"staged:{spec_id}_model.f90" for spec_id in staged]
    objects: list[str] = [f"{spec_id}_model.o" for spec_id in staged]

    # Role precedence is the DEFAULT order; explicit compile_after edges refine it so a file
    # compiles after any bundle file it `use`s (role precedence alone cannot order two files
    # of the same role). The base order is the deterministic tie-break, so permuting files[]
    # does not change the result.
    base_order = [str(e.get("logical_path"))
                  for e in sorted(files, key=lambda e: _bundle_file_sort_key(e, members))]
    edges = {str(e.get("logical_path")): e.get("compile_after") for e in files
             if isinstance(e.get("compile_after"), list)}
    ordered_paths = _topological_order(base_order, edges)
    if len(ordered_paths) < len(base_order):  # a cycle (rejected by validate_bundle) — still
        seen = set(ordered_paths)             # emit every file, in base order, deterministically
        ordered_paths += [p for p in base_order if p not in seen]

    for path in ordered_paths:
        sources.append(f"bundle:{path}")
        objects.append(_object_name(path))

    for glue in host_glue_sources:
        sources.append(f"glue:{glue}")
        objects.append(_object_name(glue))

    # Fail closed on a collision the bundle validator cannot see: a bundle file whose
    # object name equals a staged dependency's or the host-rendered glue's. `validate_bundle`
    # checks uniqueness WITHIN the bundle, but the closure and the glue are host inputs, so
    # only assembly can compare the three origins. A bundle file at the runner's path would
    # otherwise overwrite the host-rendered glue object — exactly the contract-boundary
    # capture that the "no runner/glue role" rule exists to deny.
    folded = [obj.casefold() for obj in objects]
    duplicates = sorted({obj for obj, key in zip(objects, folded) if folded.count(key) > 1})
    if duplicates:
        raise RuntimeError(
            f"build graph object name collision {duplicates}: the bundle files, the staged "
            f"dependency closure {staged} (unit members excluded), and the host glue "
            f"{list(host_glue_sources)} must derive distinct object names")

    # Fail closed on a Fortran MODULE-name collision the bundle validator cannot see either: a
    # bundle file may declare a `modules` name equal to a staged dependency's derived
    # `<spec_id>_model` module even when the OBJECT names differ, and two definitions of one
    # module overwrite the dependency's `.mod` and break the build. `validate_bundle` checks
    # module uniqueness only WITHIN the bundle; the closure's module names are a host input.
    staged_modules = {f"{spec_id}_model".casefold(): f"{spec_id}_model" for spec_id in staged}
    module_clashes = sorted({
        module for entry in files for module in (entry.get("modules") or [])
        if isinstance(module, str) and module.casefold() in staged_modules})
    if module_clashes:
        raise RuntimeError(
            f"build graph module name collision {module_clashes}: a bundle file declares a "
            f"module a staged dependency also defines as `<spec_id>_model` (closure {staged}); "
            "two definitions of one Fortran module overwrite the dependency's .mod")

    compile_units = [
        {"source": source, "object": obj, "prerequisite_objects": objects[:index]}
        for index, (source, obj) in enumerate(zip(sources, objects))
    ]
    return {
        # Projected onto the declarative allowlist AND value-validated (against the bundle's
        # own languages), not echoed verbatim: the IR toolchain object is not closed, so a stray
        # `command`/`flags` key, a shell-syntax `compiler`, or a driver for the wrong language
        # would otherwise ride into the graph and defeat its guarantees.
        "toolchain": _projected_toolchain(
            toolchain, sorted({str(e.get("language")) for e in files})),
        "compile_units": compile_units,
        "link": {"objects": list(objects)},
    }


def m3c_literal_name_violation(doc: Mapping[str, Any], spec_id: str) -> str | None:
    """The M3c host-runner literal-name constraint on a bundle's model/checks files, or None.

    The host-rendered runner glue emits `use <spec_id>_model` / `use <spec_id>_checks`, so the
    bundle MUST carry a `model`-role file named `<spec_id>_model.f90` declaring module
    `<spec_id>_model`, and a `checks`-role file `<spec_id>_checks.f90` declaring `<spec_id>_checks`.
    A different name leaves the runner's `use` unresolved at link — a defect only the host (which
    owns the runner) can see, so it is caught here rather than at build."""
    want = {
        "model": (f"{spec_id}_model.f90", f"{spec_id}_model"),
        "checks": (f"{spec_id}_checks.f90", f"{spec_id}_checks"),
    }
    files = [e for e in (doc.get("files") or []) if isinstance(e, dict)]
    for role, (want_path, want_module) in want.items():
        candidates = [e for e in files if e.get("role") == role]
        if not candidates:
            return (f"the host-rendered runner requires a {role}-role file named "
                    f"{want_path!r} declaring module {want_module!r}; the bundle has none")
        # EXACT, not casefold: `logical_path` becomes a filename, and the gate that ultimately
        # demands it opens `src_dir / f"{spec_id}_checks.f90"` on a case-sensitive filesystem
        # (`_validate_checks_source_files`). Casefolding here accepted `Shallow_Water2d_Checks.f90`
        # — which lints and compiles fine, since Fortran resolves `use` by module name and never
        # by filename — and then `Generate.static` rejected it on the name, reopening the phase.
        # The module comparison below stays casefolded for the mirror-image reason: a Fortran
        # identifier IS case-insensitive.
        match = next((e for e in candidates
                      if str(e.get("logical_path", "")) == want_path), None)
        if match is None:
            return (f"the {role}-role file must be named {want_path!r} exactly (the host-rendered "
                    f"runner `use`s module {want_module!r} by fixed name, and the deterministic "
                    f"gate opens that filename verbatim); got "
                    f"{[e.get('logical_path') for e in candidates]}")
        modules = {str(m).casefold() for m in (match.get("modules") or []) if isinstance(m, str)}
        if want_module.casefold() not in modules:
            return (f"{want_path} must declare module {want_module!r} (the host-rendered "
                    f"runner `use`s it); its modules are {sorted(match.get('modules') or [])}")
    return None


def m3c_checks_abi_violation(doc: Mapping[str, Any], spec_id: str) -> str | None:
    """The fixed-ABI constraint on the bundle's checks module, or None.

    An M3c node's `<spec_id>_checks` module must publish the SAME fixed set of names for every
    node — `runner_renderer.CHECKS_PUBLIC_NAMES`, the authority — each as a SUBROUTINE, because
    the host-rendered runner `call`s them (`runner_renderer.render_runner`'s `checks_syms` imports
    and the per-case call sites). Two later gates enforce
    halves of that and this layer pre-empts both, turning what were phase reopens into one
    bounded in-conversation repair (Z2 defect D: sw2d burned its whole retry budget re-guessing
    an ABI it had never been shown):

    - `Generate.static` (`_validate_checks_source_files`) requires all of the names to be
      PUBLISHED. It does not distinguish a subroutine from a function.
    - `Generate.syntax` rejects a name the runner imports but the module does not define
      (`Symbol '<name>' not found in module`) or defines as a FUNCTION (`'<name>' ... has a type,
      which is not consistent with the CALL`).

    The required set is the FULL fixed ABI, never the subset the node's own runner happens to
    import: the runner's import list IS dynamic in the IR (`get_r<rank>` per declared rank,
    `metric_compute` only with metrics), but `Generate.static` requires all ten regardless, so
    requiring only the imported subset would accept a bundle that gate then rejects — which is
    what it did until a review caught it.

    Conservative NECESSARY condition: every ABI name is published — defined in this module, or
    named in one of its `public ::` statements — and none is defined HERE as a function. It does
    not check dummy-argument agreement; `Generate.syntax` stages the runner with the source and
    owns call resolution.

    "Published" is `Generate.static`'s own notion, deliberately, not a better one. Fortran has
    ways to export a callable name that neither gate models — a whole-module `use` re-export with
    no `public ::`, a generic `interface`, an interface-only module implemented by a submodule —
    and all of them compile and `call` fine while both gates report the ABI unpublished. That is a
    shared false positive, and it stays shared ON PURPOSE: this layer exists to pre-empt
    `Generate.static`, so being more permissive than it would just move the rejection later and
    recreate the disagreement this defect is about, in the other direction. The certified idiom
    (a bare `private` plus an explicit `public ::` list, which authoring rule 1 mandates and all
    16 certified modules use) is well inside what both accept. The parse is delegated to `validate_pipeline_semantics.checks_module_abi_facts` —
    the SAME parser `Generate.static` uses, so the two gates cannot disagree about what a given
    source publishes. (A second implementation is exactly how this layer came to accept output
    `Generate.static` rejected.)"""
    from tools.runner_renderer import CHECKS_PUBLIC_NAMES
    from tools.validate_pipeline_semantics import checks_module_abi_facts
    # `m3c_literal_name_violation` runs first and guarantees this file exists and declares this
    # module. Scope to it: a bundle may legally carry OTHER checks-role files, and reading their
    # text too would let a sibling module vouch for a name `use <spec_id>_checks` cannot resolve.
    want_path = f"{spec_id}_checks.f90"
    match = next((e for e in (doc.get("files") or [])
                  if isinstance(e, dict) and e.get("role") == "checks"
                  and str(e.get("logical_path", "")) == want_path), None)
    if match is None:  # unreachable via the ordered contract; fail-closed if ever called alone
        return f"the bundle carries no checks-role file named {want_path!r}"
    published, subroutines, defined = checks_module_abi_facts(
        str(match.get("content") or ""), spec_id)
    unpublished = [n for n in CHECKS_PUBLIC_NAMES if n not in published]
    # Only POSITIVE evidence of the wrong kind rejects: the name is defined right here and is not
    # a subroutine, so it is a function. A published name with NO local definition is not proof of
    # anything — it can be `use`-associated, declared via an `interface`/generic block, or
    # implemented in a submodule, all of which compile, link, and `call` fine. Rejecting those
    # would fail a LEGAL module with findings telling the producer to write the subroutine it had
    # already written: an unexitable repair loop, which is this defect's own failure mode.
    # `Generate.syntax` stages the runner with the source and is the authority on call resolution.
    wrong_kind = [n for n in CHECKS_PUBLIC_NAMES
                  if n not in unpublished and n in defined and n not in subroutines]
    if unpublished or wrong_kind:
        parts = []
        if unpublished:
            parts.append(
                f"not published by module {spec_id}_checks: {', '.join(unpublished)} (define it "
                f"there and, under a bare `private` default, name it in a `public ::` statement)")
        if wrong_kind:
            parts.append(
                f"defined here as a FUNCTION: {', '.join(wrong_kind)} (every ABI name is a "
                f"subroutine by contract, and the runner reaches the ones it imports with a "
                f"`call`, so a function of that name cannot satisfy it)")
        return (f"module {spec_id}_checks must define and publish the fixed checks ABI as "
                f"subroutines — " + "; ".join(parts)
                + f". The full required set is {', '.join(CHECKS_PUBLIC_NAMES)} for EVERY M3c "
                f"node, whatever subset this node's runner imports.")
    return None


def pure_bundle_contract_violation(
    doc: Mapping[str, Any],
    *,
    node_key: str,
    spec_id: str,
    ir_state_variables: Iterable[str],
    harness_provided: Iterable[str] | None,
    harness_label: str | None = None,
    build_graph: Callable[[Mapping[str, Any]], Any],
) -> tuple[str, str] | None:
    """The full Z2 pure-CodegenBundle host acceptance contract as `(category, findings)`, or None.

    The SINGLE source of the acceptance layers shared by the producer's in-conversation gate
    (`Conductor._pure_bundle_violations`) and the deterministic post-generate tamper gate
    (`validate_pipeline_semantics._validate_post_generate_bundle`), so a bundle the producer
    would reject can never be certified by the independent re-check (and the two cannot drift).

    Fail-closed layers, in order, each STOPPING at the first that fails (so one defect is one
    report AND a later layer never runs on a doc an earlier one already rejected): schema
    (`validate_bundle`) -> single-node unit shape -> harness capability negotiation (the manifest
    MUST exist — an undeclared harness satisfies nothing) -> state_variable ∈ IR
    algorithm.state_variables -> the M3c literal name the host-rendered runner `use`s -> the
    fixed checks-module ABI -> `build_graph(doc)` (the caller's assembly derivation, which raises
    RuntimeError on a cross-origin object/module collision or a straddle).

    `harness_provided` is the caller-resolved harness capability set (`None` = undeclared harness
    = nothing provided, fail-closed); `harness_label` only names it in the findings text.
    `build_graph(doc)` performs the caller's `derive_build_graph` assembly. (The former
    diagnostics-contract check-id literal-presence layer is gone: pure-8's runner-driven per-id
    checks ABI passes each declared id to `checks_compute` as a literal actual, so a dropped id is
    structurally impossible — the module authors only the status. `post_execute` stays the
    sufficient backstop for status honesty.)"""
    violations = validate_bundle(doc)
    if violations:
        return ("bundle_schema_violation",
                "CodegenBundle failed validate_bundle:\n- " + "\n- ".join(violations))
    members = optimization_unit_members(doc)
    if members != (node_key,):
        return ("bundle_shape_unsupported",
                f"optimization_unit.members must be exactly [{node_key!r}] on the live "
                f"pure path (single-node unit); got {list(members)}")
    required = doc.get("capability_requirements") or []
    unsatisfied = unsatisfied_capability_requirements(required, harness_provided)
    if unsatisfied:
        label = f" {harness_label!r}" if harness_label else ""
        return ("bundle_capability_unsatisfied",
                f"capability_requirements not satisfied by harness{label}: "
                + ", ".join(unsatisfied))
    # Canonical IRs carry `algorithm.state_variables` as OBJECTS (`{name, shape_expr, ...}`);
    # older/degenerate specs may use a bare-string list. Accept both — a comprehension that kept
    # only `str` entries would silently yield an EMPTY set on every canonical IR and reject every
    # bundle that declares a state_binding as a mismatch.
    ir_state_vars: set[str] = set()
    for v in ir_state_variables:
        if isinstance(v, str):
            ir_state_vars.add(v)
        elif isinstance(v, collections.abc.Mapping) and isinstance(v.get("name"), str):
            ir_state_vars.add(v["name"])
    for idx, binding in enumerate(doc.get("state_bindings") or []):
        # Fail-CLOSED on an empty declared set too: a bundle that binds state a stateless IR
        # never declared is an invented registration, so an EMPTY ir_state_vars rejects any
        # binding rather than accepting all of them.
        sv = binding.get("state_variable") if isinstance(binding, dict) else None
        if isinstance(sv, str) and sv not in ir_state_vars:
            return ("bundle_state_binding_mismatch",
                    f"state_bindings[{idx}].state_variable {sv!r} is not an IR "
                    f"algorithm.state_variable (declared: {sorted(ir_state_vars)})")
    name_violation = m3c_literal_name_violation(doc, spec_id)
    if name_violation is not None:
        return ("bundle_assembly_collision", name_violation)
    abi_violation = m3c_checks_abi_violation(doc, spec_id)
    if abi_violation is not None:
        return ("bundle_checks_abi_violation", abi_violation)
    try:
        build_graph(doc)
    except RuntimeError as exc:
        return ("bundle_assembly_collision", f"build-graph assembly failed: {exc}")
    return None
