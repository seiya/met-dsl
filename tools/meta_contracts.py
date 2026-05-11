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
