#!/usr/bin/env python3
"""Codex feature probes for hook runtime."""

from __future__ import annotations

import subprocess
from typing import Callable

DEFAULT_FEATURE_PROBE_TIMEOUT_SECONDS = 10.0


def parse_feature_list(raw: str) -> dict[str, bool]:
    features: dict[str, bool] = {}
    for line in raw.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        enabled = parts[-1].lower()
        if enabled not in {"true", "false"}:
            continue
        name = parts[0].strip()
        if name:
            features[name] = enabled == "true"
    return features


def codex_hooks_feature_enabled(
    *,
    command: str = "codex",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = DEFAULT_FEATURE_PROBE_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    try:
        proc = runner(
            [command, "features", "list"],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"codex features list timed out after {timeout_seconds:.1f}s"
    detail = proc.stdout.strip() or proc.stderr.strip()
    if proc.returncode != 0:
        return False, f"codex features list failed: {detail}"
    features = parse_feature_list(proc.stdout)
    if features.get("hooks") is True:
        return True, "hooks=true"
    return False, f"hooks={features.get('hooks')}"
