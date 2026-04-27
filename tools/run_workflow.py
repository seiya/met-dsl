#!/usr/bin/env python3
"""Bootstrap workflow orchestration startup for a target spec."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SUPPORTED_LLMS = ("codex", "cursor", "claude")
SUPPORTED_WORKFLOW_MODES = ("dev", "prod")
DEFAULT_LLM_COMMANDS = {
    "codex": "codex",
    "cursor": "cursor",
    "claude": "claude",
}

PHASE_ALIASES = {
    "plan": "Plan",
    "generate": "Generate",
    "build": "Build",
    "execute": "Execute",
    "judge": "Judge",
    "tune": "Tune",
    "promote": "Promote",
}
PHASE_ORDER = ["Plan", "Generate", "Build", "Execute", "Judge", "Tune", "Promote"]


@dataclass(frozen=True)
class RuntimeResult:
    payload: dict[str, Any]
    raw_stdout: str


def _normalize_workflow_mode(token: str) -> str:
    normalized = token.strip().lower()
    if normalized not in SUPPORTED_WORKFLOW_MODES:
        choices = ", ".join(SUPPORTED_WORKFLOW_MODES)
        raise ValueError(f"unknown workflow mode: {token!r} (expected one of: {choices})")
    return normalized


def _normalize_phase(token: str) -> str:
    normalized = token.strip().lower()
    if normalized not in PHASE_ALIASES:
        choices = ", ".join(PHASE_ALIASES.keys())
        raise ValueError(f"unknown phase: {token!r} (expected one of: {choices})")
    return PHASE_ALIASES[normalized]


def _new_orchestration_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    return f"orch_{ts}_{suffix}"


def _runtime_command(repo_root: Path, env: dict[str, str], args: list[str]) -> RuntimeResult:
    command = ["python3", "tools/orchestration_runtime.py", *args]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        detail = stderr or stdout or f"exit={completed.returncode}"
        raise RuntimeError(f"runtime command failed ({' '.join(args)}): {detail}")
    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError(f"runtime command returned empty output ({' '.join(args)})")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"runtime command returned non-JSON output ({' '.join(args)}): {stdout}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"runtime command must return JSON object ({' '.join(args)})")
    return RuntimeResult(payload=payload, raw_stdout=stdout)


def _launch_command_and_input(*, llm: str, llm_command: str, prompt_text: str) -> tuple[list[str], str | None]:
    command = shlex.split(llm_command)
    if not command:
        raise ValueError("llm_command must be non-empty")
    # Codex default entrypoint requires a terminal in interactive mode.
    # Use non-interactive subcommand to run from this bootstrap script.
    if llm == "codex":
        return [*command, "exec", prompt_text], None
    return command, prompt_text


def _build_orchestration_prompt(
    *,
    orchestration_id: str,
    orchestration_agent_run_id: str,
    spec_ref: str,
    dependency_ref: str | None,
    until_phase: str,
    workflow_mode: str,
) -> str:
    phase_list = ", ".join(PHASE_ORDER[: PHASE_ORDER.index(until_phase) + 1])
    dependency_line = (
        f"- dependency_ref: `{dependency_ref}`"
        if isinstance(dependency_ref, str) and dependency_ref.strip()
        else "- dependency_ref: `(not specified)`"
    )
    base = textwrap.dedent(
        f"""
        Workflow を起動する。

        ## startup context
        - orchestration_id: `{orchestration_id}`
        - orchestration_agent_run_id: `{orchestration_agent_run_id}`
        - workflow_mode: `{workflow_mode}`
        - target_spec_ref: `{spec_ref}`
        {dependency_line}
        - target_phases: `{phase_list}`（終了 phase: `{until_phase}`）

        ## execution constraints
        - まず `skills/workflow-orchestration/SKILL.md` と `skills/workflow-orchestration/references/startup_contract.md` を読む。
        - workflow 実行中は `METDSL_WORKFLOW_MODE=1` を維持する。
        - 起動に必要な情報が不足している場合は即停止し、不足項目を列挙して報告する。
        - 不足情報を推測・補完して進行してはならない。
        - この起動は `tools/run_workflow.py` が生成した context を canonical input とする。これ以外の経路で workflow を開始してはならない。
        - phase artifact の本体処理は child agent に委譲し、親 agent が代行しない。
        - 子 agent の要求定義と判定規則の canonical source は `docs/`、`spec/`、当該試行 artifact に限定する。
        - child agent 起動前に `workflow-launch-check` を実行し、失敗時は停止する。
        - 進行は開始 phase から `{until_phase}` までとし、それ以降の phase には進まない。
        """
    ).strip() + "\n"

    if workflow_mode == "dev":
        base += textwrap.dedent(
            """
            - verify substep で `issue_severity` が `minor` 以外の場合は fail として停止する。
            - fail 時は一次証跡（`agent_runs.jsonl`、`step_result.json`、`agent.summary.txt`、`launches/*.reply.txt`）を優先して原因を調査し、根拠とともに報告する。
            - 途中経過の調査に必要な情報は、可能な限り `workspace/orchestrations/<orchestration_id>/` 配下へ保存する。
            """
        )
    return base


def _normalize_spec_ref_token(token: str) -> str:
    value = token.strip().replace("\\", "/").strip("/")
    if value.endswith("/controlled_spec.md"):
        return value[: -len("/controlled_spec.md")]
    return value


def _expand_spec_ref_tokens(token: str) -> set[str]:
    normalized = _normalize_spec_ref_token(token)
    if not normalized:
        return set()
    candidates = {normalized}
    posix_path = PurePosixPath(normalized)
    # Treat directory-style refs and file-style refs under it as equivalent.
    if posix_path.suffix:
        parent = posix_path.parent.as_posix()
        if parent not in {"", "."}:
            candidates.add(parent.strip("/"))
    return candidates


def _canonicalize_spec_ref(repo_root: Path, spec_ref: str) -> str:
    resolved = _resolve_existing_ref_path(repo_root, spec_ref, field_name="spec_ref")
    try:
        rel = resolved.relative_to(repo_root)
        return rel.as_posix()
    except ValueError:
        return str(resolved)


def _spec_ref_matches(lhs: str, rhs: str) -> bool:
    lhs_candidates = _expand_spec_ref_tokens(lhs)
    rhs_candidates = _expand_spec_ref_tokens(rhs)
    return bool(lhs_candidates and rhs_candidates and (lhs_candidates & rhs_candidates))


def _parse_iso_like_ts(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    token = value.strip()
    return token if token else ""


def _pick_latest_by_meta(records: list[dict[str, str]]) -> str | None:
    if not records:
        return None

    def _sort_key(item: dict[str, str]) -> tuple[str, str, str, str]:
        return (
            item.get("finished_at", ""),
            item.get("resumed_at", ""),
            item.get("started_at", ""),
            item.get("orchestration_id", ""),
        )

    selected = max(records, key=_sort_key)
    return selected.get("dependency_ref")


def _extract_spec_identity_from_controlled_spec(spec_path: Path) -> tuple[str, str, str] | None:
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError:
        return None
    kind_match = re.search(r"-\s*`spec_kind`:\s*`([^`]+)`", text)
    id_match = re.search(r"-\s*`spec_id`:\s*`([^`]+)`", text)
    version_match = re.search(r"-\s*`spec_version`:\s*`([^`]+)`", text)
    if not kind_match or not id_match or not version_match:
        return None
    spec_kind = kind_match.group(1).strip()
    spec_id = id_match.group(1).strip()
    spec_version = version_match.group(1).strip()
    if not spec_kind or not spec_id or not spec_version:
        return None
    return spec_kind, spec_id, spec_version


def _discover_dependency_ref(repo_root: Path, spec_ref: str) -> str | None:
    spec_canonical = _canonicalize_spec_ref(repo_root, spec_ref)

    orch_root = repo_root / "workspace" / "orchestrations"
    candidates: list[dict[str, str]] = []
    if orch_root.is_dir():
        for meta_path in orch_root.glob("*/orchestration_meta.json"):
            payload = _read_json_if_exists(meta_path)
            if not isinstance(payload, dict):
                continue
            meta_spec_ref = payload.get("spec_ref")
            dep_ref = payload.get("dependency_ref")
            if not isinstance(meta_spec_ref, str) or not meta_spec_ref.strip():
                continue
            if not isinstance(dep_ref, str) or not dep_ref.strip():
                continue
            dep_ref_token = dep_ref.strip()
            dep_path = repo_root / dep_ref_token
            if not dep_path.exists():
                continue
            if not _spec_ref_matches(spec_canonical, meta_spec_ref):
                continue
            candidates.append(
                {
                    "orchestration_id": str(payload.get("orchestration_id", meta_path.parent.name)),
                    "dependency_ref": dep_ref_token,
                    "started_at": _parse_iso_like_ts(payload.get("started_at")),
                    "resumed_at": _parse_iso_like_ts(payload.get("resumed_at")),
                    "finished_at": _parse_iso_like_ts(payload.get("finished_at")),
                }
            )
    selected = _pick_latest_by_meta(candidates)
    if selected:
        return selected

    # Single-candidate fallback for bootstrap usability.
    global_dep_candidates = sorted((repo_root / "workspace" / "plans").glob("*/*/dependency.resolved.yaml"))
    if len(global_dep_candidates) == 1:
        return str(global_dep_candidates[0].relative_to(repo_root)).replace("\\", "/")

    spec_path = (repo_root / spec_canonical).resolve()
    controlled_spec_path = spec_path
    if controlled_spec_path.is_dir():
        controlled_spec_path = controlled_spec_path / "controlled_spec.md"
    identity = _extract_spec_identity_from_controlled_spec(controlled_spec_path)
    if identity is None:
        return None
    spec_kind, spec_id, spec_version = identity
    node_safe = f"{spec_kind}__{spec_id}__{spec_version}"
    plan_root = repo_root / "workspace" / "plans" / node_safe
    if not plan_root.is_dir():
        return None
    dep_paths = sorted(plan_root.glob("*/dependency.resolved.yaml"))
    if not dep_paths:
        return None
    latest = dep_paths[-1]
    return str(latest.relative_to(repo_root)).replace("\\", "/")


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            payload = json.loads(token)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _tail_text(path: Path, *, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _collect_noncanonical_write_violations(repo_root: Path, orchestration_id: str) -> list[dict[str, Any]]:
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    violations_root = orch_root / "violations"
    if not violations_root.is_dir():
        return []
    collected: list[dict[str, Any]] = []
    for path in sorted(violations_root.glob("*.noncanonical_phase_write_attempt.json")):
        payload = _read_json_if_exists(path)
        if not isinstance(payload, dict):
            continue
        attempted = payload.get("attempted_paths")
        attempted_paths = (
            [str(item).strip() for item in attempted if isinstance(item, str) and str(item).strip()]
            if isinstance(attempted, list)
            else []
        )
        collected.append(
            {
                "violation_ref": str(path.relative_to(repo_root)),
                "agent_run_id": str(payload.get("agent_run_id") or "").strip(),
                "reason_code": str(payload.get("reason_code") or "").strip(),
                "attempted_paths": attempted_paths,
            }
        )
    return collected


def _collect_unauthorized_write_violations(repo_root: Path, orchestration_id: str) -> list[dict[str, Any]]:
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    violations_root = orch_root / "violations"
    if not violations_root.is_dir():
        return []
    collected: list[dict[str, Any]] = []
    for path in sorted(violations_root.glob("*.unauthorized_write_violation.json")):
        payload = _read_json_if_exists(path)
        if not isinstance(payload, dict):
            continue
        unauthorized_obj = payload.get("unauthorized_paths")
        unauthorized_paths = (
            [str(item).strip() for item in unauthorized_obj if isinstance(item, str) and str(item).strip()]
            if isinstance(unauthorized_obj, list)
            else []
        )
        collected.append(
            {
                "violation_ref": str(path.relative_to(repo_root)),
                "agent_run_id": str(payload.get("agent_run_id") or "").strip(),
                "reason_code": "unauthorized_write_violation",
                "attempted_paths": unauthorized_paths,
            }
        )
    return collected


def _collect_failure_analysis(repo_root: Path, orchestration_id: str) -> dict[str, Any]:
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    meta_path = orch_root / "orchestration_meta.json"
    meta = _read_json_if_exists(meta_path) or {}
    runs = _read_jsonl(orch_root / "agent_runs.jsonl")
    terminal_fail_statuses = {"fail", "blocked", "timeout", "cancel"}
    failed_runs = [
        run
        for run in runs
        if isinstance(run.get("status"), str) and str(run.get("status")).strip().lower() in terminal_fail_statuses
    ]
    failed_run = failed_runs[-1] if failed_runs else None

    failed_step_results: list[dict[str, Any]] = []
    for step_result_path in sorted(orch_root.glob("steps/*/*/*/step_result.json")):
        payload = _read_json_if_exists(step_result_path)
        if not payload:
            continue
        status = str(payload.get("status") or "").strip().lower()
        if status and status != "pass":
            failed_step_results.append(
                {
                    "path": str(step_result_path.relative_to(repo_root)),
                    "status": status,
                    "required_outputs": payload.get("required_outputs"),
                    "failed_substeps": payload.get("failed_substeps"),
                }
            )

    launch_reply_tail = ""
    agent_summary_tail = ""
    if isinstance(failed_run, dict):
        launch_reply_ref = failed_run.get("launch_reply_ref")
        if isinstance(launch_reply_ref, str) and launch_reply_ref.strip():
            launch_reply_tail = _tail_text(repo_root / launch_reply_ref.strip())
        agent_summary_ref = failed_run.get("agent_summary_ref")
        if isinstance(agent_summary_ref, str) and agent_summary_ref.strip():
            agent_summary_tail = _tail_text(repo_root / agent_summary_ref.strip())

    noncanonical_write_violations = _collect_noncanonical_write_violations(repo_root, orchestration_id)
    unauthorized_write_violations = _collect_unauthorized_write_violations(repo_root, orchestration_id)
    write_contract_violations = [*noncanonical_write_violations, *unauthorized_write_violations]
    recommended_retry_decisions: list[dict[str, Any]] = []
    for violation in write_contract_violations:
        target_run = str(violation.get("agent_run_id") or "").strip()
        if not target_run:
            continue
        paths = violation.get("attempted_paths")
        attempted_paths = paths if isinstance(paths, list) else []
        reason_code = str(violation.get("reason_code") or "").strip() or "noncanonical_phase_write_attempt"
        recommended_retry_decisions.append(
            {
                "issue_severity": "major",
                "repair_strategy": "restart",
                "repair_target_agent_run_id": target_run,
                "repair_reason": reason_code + ": " + ",".join(attempted_paths),
            }
        )

    return {
        "status": "fail",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "orchestration_id": orchestration_id,
        "orchestration_status": meta.get("status"),
        "reason_code": meta.get("reason_code"),
        "reason_detail": meta.get("reason_detail"),
        "failed_agent_run": failed_run,
        "failed_step_results": failed_step_results,
        "noncanonical_write_violations": noncanonical_write_violations,
        "unauthorized_write_violations": unauthorized_write_violations,
        "recommended_retry_decisions": recommended_retry_decisions,
        "launch_reply_tail": launch_reply_tail,
        "agent_summary_tail": agent_summary_tail,
    }


def _write_failure_analysis(repo_root: Path, orchestration_id: str, payload: dict[str, Any]) -> str:
    rel = Path("workspace") / "orchestrations" / orchestration_id / "failure_analysis.json"
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(rel)


def _detect_non_minor_verify_issue(repo_root: Path, orchestration_id: str) -> dict[str, Any] | None:
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    verify_steps = {"plan", "generate", "tune"}
    for step_result_path in sorted(orch_root.glob("steps/*/*/*/step_result.json")):
        payload = _read_json_if_exists(step_result_path)
        if not payload:
            continue
        step_token = step_result_path.parts[-3].strip().lower()
        if step_token not in verify_steps:
            continue
        retry_decisions = payload.get("retry_decisions")
        if not isinstance(retry_decisions, list):
            continue
        for idx, decision in enumerate(retry_decisions):
            if not isinstance(decision, dict):
                continue
            severity = str(decision.get("issue_severity") or "").strip().lower()
            if severity and severity != "minor":
                return {
                    "step_result_ref": str(step_result_path.relative_to(repo_root)),
                    "step": step_token,
                    "retry_decision_index": idx,
                    "issue_severity": severity,
                    "repair_reason": decision.get("repair_reason"),
                }
    return None


def _ensure_preflight_pass(preflight: dict[str, Any]) -> tuple[bool, str]:
    status = preflight.get("status")
    can_step = preflight.get("can_launch_step_agents")
    can_substep = preflight.get("can_launch_substep_agents")
    reasons: list[str] = []
    if status != "pass":
        reasons.append(f"status={status!r}")
    if can_step is not True:
        reasons.append(f"can_launch_step_agents={can_step!r}")
    if can_substep is not True:
        reasons.append(f"can_launch_substep_agents={can_substep!r}")
    if reasons:
        return False, ", ".join(reasons)
    return True, "pass"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap workflow startup (init + preflight + prompt).",
    )
    parser.add_argument("spec_ref", help="Target spec path/reference.")
    parser.add_argument("until_phase", help="Final phase to execute (plan/generate/build/execute/judge/tune/promote).")
    parser.add_argument(
        "--mode",
        default="dev",
        choices=SUPPORTED_WORKFLOW_MODES,
        help="Workflow execution mode: dev (default) or prod.",
    )
    parser.add_argument("--llm", default="codex", choices=SUPPORTED_LLMS)
    parser.add_argument("--llm-command", help="Override backend command used by preflight and optional launch.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--orchestration-id", help="If omitted, generated automatically.")
    parser.add_argument("--status", default="running", help="Initial orchestration status for init.")
    parser.set_defaults(invoke_llm=True)
    parser.add_argument(
        "--invoke-llm",
        dest="invoke_llm",
        action="store_true",
        help="Invoke the selected LLM command and pipe startup prompt via stdin (default: enabled).",
    )
    parser.add_argument(
        "--no-invoke-llm",
        dest="invoke_llm",
        action="store_false",
        help="Prepare orchestration artifacts only; do not invoke the LLM command.",
    )
    return parser.parse_args(argv)


def _resolve_existing_ref_path(repo_root: Path, ref: str, *, field_name: str) -> Path:
    path = Path(ref)
    resolved = path if path.is_absolute() else (repo_root / path)
    resolved = resolved.resolve()
    if not resolved.exists():
        raise ValueError(f"{field_name} must exist: {ref}")
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        workflow_mode = _normalize_workflow_mode(args.mode)
        until_phase = _normalize_phase(args.until_phase)
        repo_root = Path(args.repo_root).resolve()
        orchestration_id = args.orchestration_id or _new_orchestration_id()
        llm_command = args.llm_command or DEFAULT_LLM_COMMANDS[args.llm]
        spec_ref = _canonicalize_spec_ref(repo_root, args.spec_ref)
        dependency_ref = _discover_dependency_ref(repo_root, spec_ref)
        if isinstance(dependency_ref, str) and dependency_ref.strip():
            _resolve_existing_ref_path(repo_root, dependency_ref, field_name="dependency_ref")
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "invalid_startup_input",
                    "detail": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2

    env = dict(os.environ)
    env["METDSL_WORKFLOW_MODE"] = "1"
    env["METDSL_ORCHESTRATION_ID"] = orchestration_id
    env["METDSL_WORKFLOW_EXEC_MODE"] = workflow_mode
    env["METDSL_MISSING_ORCHESTRATION_ID_POLICY"] = "strict"
    env["PYTHONPATH"] = str(repo_root) + (f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")

    init_args = [
        "init",
        "--repo-root",
        str(repo_root),
        "--orchestration-id",
        orchestration_id,
        "--spec-ref",
        spec_ref,
        "--status",
        args.status,
    ]
    if isinstance(dependency_ref, str) and dependency_ref.strip():
        init_args.extend(["--dependency-ref", dependency_ref])
    try:
        init_result = _runtime_command(repo_root, env, init_args).payload
        orchestration_agent_run_id = str(init_result.get("orchestration_agent_run_id", "")).strip()
        preflight_result = _runtime_command(
            repo_root,
            env,
            [
                "preflight",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--backend",
                args.llm,
                "--agent-command",
                llm_command,
            ],
        ).payload
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "runtime_command_failed",
                    "detail": str(exc),
                    "orchestration_id": orchestration_id,
                },
                ensure_ascii=False,
            )
        )
        return 2
    passed, detail = _ensure_preflight_pass(preflight_result)
    if not passed:
        _runtime_command(
            repo_root,
            env,
            [
                "set-status",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--status",
                "fail",
                "--reason-code",
                "preflight_failed",
                "--reason-detail",
                detail,
                "--blocking-policy-scope",
                "preflight",
            ],
        )
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "preflight_failed",
                    "detail": detail,
                    "orchestration_id": orchestration_id,
                },
                ensure_ascii=False,
            )
        )
        return 2

    prompt_text = _build_orchestration_prompt(
        orchestration_id=orchestration_id,
        orchestration_agent_run_id=orchestration_agent_run_id,
        spec_ref=spec_ref,
        dependency_ref=dependency_ref,
        until_phase=until_phase,
        workflow_mode=workflow_mode,
    )
    prompt_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "launches"
        / "orchestration.start.prompt.txt"
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_text, encoding="utf-8")

    launched = False
    workflow_status = "running"
    if args.invoke_llm:
        launch_command, launch_input = _launch_command_and_input(
            llm=args.llm,
            llm_command=llm_command,
            prompt_text=prompt_text,
        )
        proc = subprocess.run(
            launch_command,
            cwd=repo_root,
            env=env,
            text=True,
            input=launch_input,
            check=False,
        )
        launched = proc.returncode == 0
        meta_after_launch = _read_json_if_exists(
            repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
        )
        if isinstance(meta_after_launch, dict):
            workflow_status = str(meta_after_launch.get("status") or "running")
        if workflow_mode == "dev":
            severe_verify_issue = _detect_non_minor_verify_issue(repo_root, orchestration_id)
            if severe_verify_issue is not None:
                try:
                    _runtime_command(
                        repo_root,
                        env,
                        [
                            "set-status",
                            "--repo-root",
                            str(repo_root),
                            "--orchestration-id",
                            orchestration_id,
                            "--status",
                            "fail",
                            "--reason-code",
                            "verify_issue_severity_violation",
                            "--reason-detail",
                            (
                                "verify substep severity must be minor in dev mode: "
                                f"{severe_verify_issue['issue_severity']} ({severe_verify_issue['step_result_ref']})"
                            ),
                            "--blocking-policy-scope",
                            "verify",
                        ],
                    )
                    workflow_status = "fail"
                except RuntimeError:
                    workflow_status = "fail"
        if proc.returncode != 0 or workflow_status.lower() in {"fail", "fail_closed", "blocked", "timeout", "cancel"}:
            if workflow_mode == "dev":
                analysis = _collect_failure_analysis(repo_root, orchestration_id)
                analysis_ref = _write_failure_analysis(repo_root, orchestration_id, analysis)
                print(
                    json.dumps(
                        {
                            "status": "fail",
                            "reason": "workflow_failed",
                            "detail": analysis.get("reason_detail") or "workflow execution failed",
                            "orchestration_id": orchestration_id,
                            "workflow_mode": workflow_mode,
                            "workflow_status": workflow_status,
                            "analysis_ref": analysis_ref,
                        },
                        ensure_ascii=False,
                    )
                )
                return 2
            return proc.returncode if proc.returncode != 0 else 2

    print(
        json.dumps(
            {
                "status": "ok",
                "orchestration_id": orchestration_id,
                "llm": args.llm,
                "llm_command": llm_command,
                "target_spec_ref": args.spec_ref,
                "until_phase": until_phase,
                "workflow_mode": workflow_mode,
                "metdsl_workflow_mode": env["METDSL_WORKFLOW_MODE"],
                "metdsl_workflow_exec_mode": env["METDSL_WORKFLOW_EXEC_MODE"],
                "workflow_status": workflow_status,
                "prompt_ref": str(prompt_path.relative_to(repo_root)),
                "llm_invoked": launched,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
