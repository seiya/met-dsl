#!/usr/bin/env python3
"""Validate canonical workflow artifact root rules.

This checker enforces that workflow artifacts are stored under `workspace/`.
If `workspace/` is missing, the checker creates it before validation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


STRICT_WORKSPACE_REF_KEYS = {
    "dependency_ref",
    "plan_dir",
    "pipeline_dir",
    "build_log_ref",
    "source_command_ref",
    "process_trace_ref",
}


def _scan_json_for_violations(json_path: Path) -> list[str]:
    violations: list[str] = []
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        return [f"{json_path}: invalid json ({exc})"]

    def walk(node: Any, dotted_path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{dotted_path}.{key}" if dotted_path else key

                if key in STRICT_WORKSPACE_REF_KEYS and isinstance(value, str):
                    if value.startswith("/"):
                        violations.append(
                            f"{json_path}:{child_path}: absolute path is not allowed ({value})"
                        )
                    elif not value.startswith("workspace/"):
                        violations.append(
                            f"{json_path}:{child_path}: must start with workspace/ ({value})"
                        )

                if key == "raw_artifact_refs" and isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, str) and not item.startswith("workspace/"):
                            violations.append(
                                f"{json_path}:{child_path}[{i}]: must start with workspace/ ({item})"
                            )

                walk(value, child_path)
            return

        if isinstance(node, list):
            for i, item in enumerate(node):
                child_path = f"{dotted_path}[{i}]"
                walk(item, child_path)
            return

    walk(data, "")
    return violations


def validate(repo_root: Path, workspace_root: str) -> tuple[list[str], bool]:
    violations: list[str] = []
    created_workspace = False

    canonical_root = repo_root / workspace_root
    if canonical_root.exists():
        if canonical_root.is_symlink():
            violations.append(f"{canonical_root}: symlink workspace root is not allowed")
        elif not canonical_root.is_dir():
            violations.append(f"{canonical_root}: workspace root must be a directory")
    else:
        canonical_root.mkdir(parents=True, exist_ok=True)
        created_workspace = True

    if canonical_root.exists() and canonical_root.is_dir():
        for json_file in sorted(canonical_root.rglob("*.json")):
            violations.extend(_scan_json_for_violations(json_file))

    return violations, created_workspace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--workspace-root", default="workspace")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    violations, created_workspace = validate(
        repo_root=repo_root,
        workspace_root=args.workspace_root,
    )
    if violations:
        print("workspace root validation: FAIL")
        for line in violations:
            print(f"- {line}")
        return 1

    if created_workspace:
        print(f"workspace root created: {repo_root / args.workspace_root}")
    print("workspace root validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
