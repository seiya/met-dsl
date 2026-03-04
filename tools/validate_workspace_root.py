#!/usr/bin/env python3
"""Validate canonical workflow artifact root rules.

This checker enforces that workflow artifacts are stored under `workspace/`.
If `workspace/` is missing, the checker creates it before validation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
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

ALLOWED_WORKSPACE_TOP_LEVEL_DIRS = {
    "plans",
    "pipelines",
    "index",
    ".pycache",
}
NODE_KEY_SAFE_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*__[a-z0-9][a-z0-9_]*__[0-9][0-9A-Za-z._-]*$"
)


def _normalize_workspace_root_token(workspace_root: str) -> str:
    token = workspace_root.strip().replace("\\", "/")
    token = token.lstrip("./")
    while "//" in token:
        token = token.replace("//", "/")
    return token.rstrip("/")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_relpath(path: str) -> str:
    token = path.strip()
    if token.startswith("./"):
        token = token[2:]
    return token.replace("\\", "/")


def _is_under_workspace(rel_path: str, workspace_root: str) -> bool:
    normalized_path = _normalize_relpath(rel_path)
    normalized_ws = _normalize_relpath(workspace_root).rstrip("/")
    return normalized_path == normalized_ws or normalized_path.startswith(normalized_ws + "/")


def _git_status_paths(repo_root: Path) -> tuple[set[str], set[str]]:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return set(), set()

    tracked_diff: set[str] = set()
    untracked_files: set[str] = set()
    for raw in proc.stdout.splitlines():
        line = raw.rstrip("\n")
        if not line:
            continue
        status = line[:2]
        payload = line[3:].strip() if len(line) > 3 else ""
        if not payload:
            continue
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1].strip()
        payload = _normalize_relpath(payload)
        if status == "??":
            untracked_files.add(payload)
        else:
            tracked_diff.add(payload)
    return tracked_diff, untracked_files


def _validate_write_scope_from_baseline(
    *,
    repo_root: Path,
    workspace_root: str,
    baseline_path: Path,
) -> list[str]:
    violations: list[str] = []
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        return [f"{baseline_path}: invalid write_scope_baseline.json ({exc})"]

    if not isinstance(baseline, dict):
        return [f"{baseline_path}: write_scope_baseline must be json object"]

    baseline_tracked_raw = baseline.get("tracked_diff", [])
    baseline_untracked_raw = baseline.get("untracked_files", [])
    if not isinstance(baseline_tracked_raw, list) or not isinstance(baseline_untracked_raw, list):
        return [f"{baseline_path}: tracked_diff/untracked_files must be list"]

    baseline_tracked = {_normalize_relpath(str(item)) for item in baseline_tracked_raw}
    baseline_untracked = {_normalize_relpath(str(item)) for item in baseline_untracked_raw}
    current_tracked, current_untracked = _git_status_paths(repo_root)

    new_paths = sorted((current_tracked - baseline_tracked) | (current_untracked - baseline_untracked))
    outside_workspace = [path for path in new_paths if not _is_under_workspace(path, workspace_root)]
    if outside_workspace:
        violations.append(
            f"{baseline_path}: write_scope_violation detected outside workspace ({outside_workspace})"
        )
    return violations


def _capture_write_scope_baseline(
    *,
    repo_root: Path,
    workspace_root: str,
    baseline_path: Path,
    stage: str,
    node_key: str,
    pipeline_id: str,
) -> None:
    tracked_diff, untracked_files = _git_status_paths(repo_root)
    payload = {
        "stage": stage,
        "node_key": node_key,
        "pipeline_id": pipeline_id,
        "captured_at": _utc_now_iso(),
        "tracked_diff": sorted(tracked_diff),
        "untracked_files": sorted(untracked_files),
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def _scan_workspace_for_forbidden_scripts(workspace_root: Path) -> list[str]:
    violations: list[str] = []
    for py_path in sorted(workspace_root.rglob("*.py")):
        violations.append(
            f"{py_path}: python script under workspace/ is forbidden"
        )
    return violations


def _scan_workspace_layout(workspace_root: Path) -> list[str]:
    violations: list[str] = []
    for child in sorted(workspace_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in ALLOWED_WORKSPACE_TOP_LEVEL_DIRS:
            continue
        violations.append(
            f"{child}: non-canonical workspace directory name; allowed top-level directories are {sorted(ALLOWED_WORKSPACE_TOP_LEVEL_DIRS)}"
        )

    for stage_root_name in ("plans", "pipelines"):
        stage_root = workspace_root / stage_root_name
        if not stage_root.exists() or not stage_root.is_dir():
            continue

        for node_safe_dir in sorted(stage_root.iterdir()):
            if not node_safe_dir.is_dir():
                continue
            node_safe = node_safe_dir.name
            if not NODE_KEY_SAFE_PATTERN.match(node_safe):
                violations.append(
                    f"{node_safe_dir}: invalid node_key_safe directory name; expected <spec_kind>__<spec_id>__<spec_version>"
                )
                continue

            for id_dir in sorted(node_safe_dir.iterdir()):
                if not id_dir.is_dir():
                    continue
                expected_prefix = node_safe + "_"
                if not id_dir.name.startswith(expected_prefix):
                    violations.append(
                        f"{id_dir}: invalid {stage_root_name} id directory name; expected prefix {expected_prefix}"
                    )
    return violations


def validate(repo_root: Path, workspace_root: str) -> tuple[list[str], bool]:
    return validate_with_scope(
        repo_root=repo_root,
        workspace_root=workspace_root,
        write_scope_baseline=None,
        stage="",
        node_key="",
        pipeline_id="",
    )


def validate_with_scope(
    repo_root: Path,
    workspace_root: str,
    write_scope_baseline: str | None,
    stage: str,
    node_key: str,
    pipeline_id: str,
) -> tuple[list[str], bool]:
    violations: list[str] = []
    created_workspace = False
    normalized_workspace_root = _normalize_workspace_root_token(workspace_root)
    if normalized_workspace_root != "workspace":
        return [f"workspace_root must be exactly 'workspace' (given: {workspace_root})"], created_workspace

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

    if canonical_root.exists() and canonical_root.is_dir():
        violations.extend(_scan_workspace_for_forbidden_scripts(canonical_root))
        violations.extend(_scan_workspace_layout(canonical_root))

    if write_scope_baseline:
        baseline_path = Path(write_scope_baseline)
        if not baseline_path.is_absolute():
            baseline_path = repo_root / baseline_path
        baseline_path = baseline_path.resolve()
        try:
            baseline_path.relative_to(canonical_root.resolve())
        except ValueError:
            violations.append(
                f"{baseline_path}: write_scope_baseline must be under {canonical_root}"
            )
            return violations, created_workspace

        if baseline_path.exists():
            violations.extend(
                _validate_write_scope_from_baseline(
                    repo_root=repo_root,
                    workspace_root=workspace_root,
                    baseline_path=baseline_path,
                )
            )
        else:
            _capture_write_scope_baseline(
                repo_root=repo_root,
                workspace_root=workspace_root,
                baseline_path=baseline_path,
                stage=stage,
                node_key=node_key,
                pipeline_id=pipeline_id,
            )

    return violations, created_workspace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--workspace-root", default="workspace")
    parser.add_argument(
        "--write-scope-baseline",
        default=None,
        help="Path to write_scope_baseline.json. If file exists, validate diff from baseline.",
    )
    parser.add_argument("--stage", default="")
    parser.add_argument("--node-key", default="")
    parser.add_argument("--pipeline-id", default="")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    violations, created_workspace = validate_with_scope(
        repo_root=repo_root,
        workspace_root=args.workspace_root,
        write_scope_baseline=args.write_scope_baseline,
        stage=args.stage,
        node_key=args.node_key,
        pipeline_id=args.pipeline_id,
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
