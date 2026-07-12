#!/usr/bin/env python3
"""Canonical stage meta contracts shared by runtime and validators."""

from __future__ import annotations

from typing import Any

STAGE_META_FILENAME_BY_STEP: dict[str, str] = {
    "compile": "ir_meta.json",
    "generate": "source_meta.json",
}

STAGE_META_COMMON_REQUIRED_KEYS: tuple[str, ...] = (
    "attempt_count",
    "verification_status",
    "last_fail_reason",
    "debug_mode",
    "context_isolated",
)

STAGE_META_EXTRA_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "compile": (),
    "generate": (),
}


def required_meta_keys_for_step(step_token: str) -> tuple[str, ...]:
    """Return canonical required keys for a stage meta payload."""
    return STAGE_META_COMMON_REQUIRED_KEYS + STAGE_META_EXTRA_REQUIRED_KEYS.get(step_token, ())


def missing_required_meta_keys(meta_data: dict[str, Any], *, step_token: str) -> list[str]:
    """Compute missing required keys from canonical definition."""
    return [k for k in required_meta_keys_for_step(step_token) if k not in meta_data]


def stage_meta_type_violations(meta_data: dict[str, Any], *, step_token: str) -> list[str]:
    """Canonical VALUE-TYPE contract for a stage meta payload (ir_meta / source_meta).

    Returns bare violation clauses (no path/filename prefix) in canonical key order, so
    each caller can format them in its own idiom: the runtime raises
    ``f"{meta_filename} {clause}: {meta_ref}"``, the validator sweeps append
    ``f"{meta_path}:{clause}"``. Keeping the clauses prefix-free is what lets the three
    historical copies of these checks collapse into one definition.

    Only keys PRESENT in the payload are type-checked — a missing required key is
    ``missing_required_meta_keys``' responsibility, and reporting it twice would
    double-count the same defect.

    `last_fail_reason` is the clause this contract exists for: a verify leaf that records
    a structured incident dict there writes an immutable, unrepairable meta (E2E #4). The
    type is a single plain string (or null), never an object/array.
    """
    violations: list[str] = []
    contract_keys = required_meta_keys_for_step(step_token)

    def present(key: str) -> bool:
        return key in contract_keys and key in meta_data

    # `bool` is a subclass of `int` in Python, so a JSON `true` would satisfy a bare
    # isinstance(_, int). Every doc states this key is an integer; exclude bool so the
    # enforced contract is the documented one.
    attempt_count = meta_data.get("attempt_count")
    if present("attempt_count") and (
        isinstance(attempt_count, bool) or not isinstance(attempt_count, int)
    ):
        violations.append("attempt_count must be integer")
    if present("verification_status"):
        status = meta_data.get("verification_status")
        if not isinstance(status, str) or not status.strip():
            violations.append("verification_status must be non-empty string")
    if present("last_fail_reason"):
        reason = meta_data.get("last_fail_reason")
        if reason is not None and not isinstance(reason, str):
            violations.append("last_fail_reason must be string or null")
    if present("debug_mode") and not isinstance(meta_data.get("debug_mode"), bool):
        violations.append("debug_mode must be boolean")
    if present("context_isolated") and not isinstance(meta_data.get("context_isolated"), bool):
        violations.append("context_isolated must be boolean")
    # constraint_reason is CONDITIONALLY required: only a meta that declares it ran with a
    # non-isolated context must justify that. `is False` (not falsy) so a non-boolean
    # context_isolated is reported by its own clause above and not double-flagged here.
    if meta_data.get("context_isolated") is False:
        reason = meta_data.get("constraint_reason")
        if not isinstance(reason, str) or not reason.strip():
            violations.append(
                "requires non-empty constraint_reason when context_isolated=false"
            )
    return violations
