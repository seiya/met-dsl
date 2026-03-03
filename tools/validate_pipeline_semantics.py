#!/usr/bin/env python3
"""Validate workflow pipeline semantic anti-cheat rules under workspace/."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLACEHOLDER_TEXT_PATTERNS = (
    '"sample":"state_recorded"',
    '"dummy"',
    '"placeholder"',
)

SNAPSHOT_SCHEMA_FILE = "snapshot_schema.json"


@dataclass
class NodeExecution:
    node_key: str
    node_dir: Path
    exec_dir: Path
    pipeline_dir: Path


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
    if pipeline_roots is None:
        pipelines_root = workspace_root / "pipelines"
        if not pipelines_root.exists():
            return result
        targets: list[Path] = []
        for node_safe_dir in sorted(pipelines_root.iterdir()):
            if not node_safe_dir.is_dir():
                continue
            for pipeline_dir in sorted(node_safe_dir.iterdir()):
                if pipeline_dir.is_dir():
                    targets.append(pipeline_dir)
    else:
        targets = sorted(pipeline_roots)

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

    for entry in _iter_command_ref_entries(source_command_ref):
        command_id = entry.get("command_id")
        log_ref = entry.get("command_log_ref") or entry.get("command_log_path")
        if not isinstance(command_id, str) or not command_id:
            violations.append(f"{trial_meta_path}:command_id invalid in source_command_ref")
            continue
        if not isinstance(log_ref, str):
            violations.append(f"{trial_meta_path}:command_log_ref/path invalid in source_command_ref")
            continue
        log_path = repo_root / log_ref if log_ref.startswith("workspace/") else Path(log_ref)
        if not log_path.exists():
            violations.append(f"{trial_meta_path}:command log missing ({log_ref})")
            continue
        found = False
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("command_id") == command_id:
                found = True
                break
        if not found:
            violations.append(
                f"{trial_meta_path}:command_id {command_id} not found in {log_ref}"
            )


def _validate_raw_evidence(execution: NodeExecution, violations: list[str]) -> None:
    required = [
        execution.node_dir / "diagnostics.json",
        execution.node_dir / "perf.json",
        execution.node_dir / "raw" / "metrics_basis.json",
        execution.node_dir / "raw" / "execution_trace.json",
        execution.node_dir / "raw" / "state_snapshots",
        execution.node_dir / "quality_check.json",
    ]
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

            if execution.node_key.startswith("problem/"):
                if not schema_path.exists():
                    violations.append(
                        f"{schema_path}: missing for problem node"
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
                    time_variable = ""
                    if isinstance(schema_data, dict):
                        raw_state_vars = schema_data.get("state_variables")
                        raw_time_var = schema_data.get("time_variable")
                        if isinstance(raw_state_vars, list):
                            for item in raw_state_vars:
                                if isinstance(item, str) and item.strip():
                                    state_variables.append(item.strip())
                        if isinstance(raw_time_var, str) and raw_time_var.strip():
                            time_variable = raw_time_var.strip()
                    else:
                        violations.append(
                            f"{schema_path}: must be json object"
                        )

                    if not state_variables:
                        violations.append(
                            f"{schema_path}: state_variables must be non-empty string list"
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
                        missing_state = set(state_variables)
                        found_time = False
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
                            missing_state -= keys
                            if time_variable in keys:
                                found_time = True
                        if missing_state:
                            violations.append(
                                f"{snapshots_dir}: missing declared state variables in snapshots ({sorted(missing_state)})"
                            )
                        if not found_time:
                            violations.append(
                                f"{snapshots_dir}: declared time_variable not found in snapshots ({time_variable})"
                            )

    diagnostics_path = execution.node_dir / "diagnostics.json"
    metrics_basis_path = execution.node_dir / "raw" / "metrics_basis.json"
    if diagnostics_path.exists() and metrics_basis_path.exists():
        diagnostics = _read_json(diagnostics_path)
        metrics_basis = _read_json(metrics_basis_path)
        if _canonical_json(diagnostics) == _canonical_json(metrics_basis):
            violations.append(
                f"{metrics_basis_path}: must not be identical copy of diagnostics.json"
            )

    quality_path = execution.node_dir / "quality_check.json"
    if quality_path.exists():
        quality = _read_json(quality_path)
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


def _validate_generate_outputs(execution: NodeExecution, violations: list[str]) -> None:
    generate_root = execution.pipeline_dir / "generate"
    if not generate_root.exists():
        violations.append(f"{generate_root}: missing")
        return

    model_files = sorted(generate_root.glob("*/src/*_model.f90"))
    if not model_files:
        violations.append(f"{generate_root}: model source not found")
        return

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
        roots.append(candidate)
    return roots


def validate(
    repo_root: Path, workspace_root: str, pipeline_roots: list[Path] | None = None
) -> list[str]:
    violations: list[str] = []
    workspace_path = repo_root / workspace_root
    if not workspace_path.exists():
        return [f"{workspace_path}: workspace root does not exist"]

    executions = _node_executions(workspace_path, pipeline_roots=pipeline_roots)
    if not executions:
        return [f"{workspace_path}/pipelines: no execution artifacts found"]

    source_hash_map: dict[str, list[SourceFingerprint]] = {}

    for execution in executions:
        _validate_trial_meta(repo_root, execution, violations)
        _validate_raw_evidence(execution, violations)
        _validate_generate_outputs(execution, violations)

        fp = _source_fingerprint(execution)
        if fp is not None:
            source_hash_map.setdefault(fp.digest, []).append(fp)

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--workspace-root", default="workspace")
    parser.add_argument(
        "--pipeline-root",
        action="append",
        default=None,
        help=(
            "Optional pipeline directory to validate. "
            "Can be repeated. Path must be under workspace/."
        ),
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    try:
        pipeline_roots = _resolve_pipeline_roots(
            repo_root=repo_root,
            workspace_root=args.workspace_root,
            raw_values=args.pipeline_root,
        )
    except ValueError as exc:
        print(f"pipeline semantic validation: FAIL\n- {exc}")
        return 1

    violations = validate(
        repo_root=repo_root,
        workspace_root=args.workspace_root,
        pipeline_roots=pipeline_roots,
    )

    if violations:
        print("pipeline semantic validation: FAIL")
        for line in violations:
            print(f"- {line}")
        return 1

    print("pipeline semantic validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
