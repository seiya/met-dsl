#!/usr/bin/env python3
"""Bootstrap workflow orchestration startup for a target spec."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import textwrap
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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

# CLI tools the workflow runtime and its hook-recovery procedures depend on.
# Documented in docs/RUNBOOK.md#0-1. Missing any one fails the run before init,
# so agents never hit a partial-failure state where (e.g.) jq is unavailable
# but TMPDIR extraction is already prescribed.
REQUIRED_CLI_TOOLS = ("python3", "jq", "git")


def _check_required_cli_tools() -> list[str]:
    return [tool for tool in REQUIRED_CLI_TOOLS if shutil.which(tool) is None]


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
    # Claude Code defaults to launching the interactive TUI; `-p` (--print) runs
    # the prompt non-interactively and exits, which is required when invoked from
    # this bootstrap script.
    if llm == "claude":
        return [*command, "-p", prompt_text], None
    return command, prompt_text


def _build_orchestration_prompt(
    *,
    orchestration_id: str,
    orchestration_agent_run_id: str,
    spec_ref: str,
    source_dependency_ref: str,
    until_phase: str,
    workflow_mode: str,
) -> str:
    phase_list = ", ".join(PHASE_ORDER[: PHASE_ORDER.index(until_phase) + 1])
    base = textwrap.dedent(
        f"""
        Workflow を起動する。

        ## startup context
        - orchestration_id: `{orchestration_id}`
        - orchestration_agent_run_id: `{orchestration_agent_run_id}`
        - workflow_mode: `{workflow_mode}`
        - target_spec_ref: `{spec_ref}`
        - dependency_ref: `{source_dependency_ref}`
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
        - 一時ファイルが必要な場合は `/tmp` を直接指定せず、`$TMPDIR` 環境変数を展開した path を使用すること（例: `"${{TMPDIR}}/work.json"` または `$(mktemp)`）。`$TMPDIR` は `workspace/tmp/<orchestration_agent_run_id>/` に設定されており、hook ポリシーの許可範囲内に含まれる。`/tmp/` ハードコードは `output_manifest_write_guard` でブロックされる。
        - Claude Code 起動直後の `~/.claude/projects/.../memory/MEMORY.md` 自動 Read は `read_manifest_read_guard` でブロックされるが、これは想定動作であり workflow の継続に影響しない。再試行や `MEMORY.md` 配下への参照を試みてはならない。
        """
    ).strip() + "\n"

    if workflow_mode == "dev":
        base += textwrap.dedent(
            f"""
            - verify substep で `issue_severity` が `minor` 以外の場合は fail として停止する。
            - fail 時は一次証跡（`agent_runs.jsonl`、`step_result.json`、`agent.summary.txt`、`launches/*.reply.txt`）を優先して原因を調査し、根拠とともに報告する。
            - 途中経過の調査に必要な情報は、可能な限り `workspace/orchestrations/<orchestration_id>/` 配下へ保存する。
            - `failure_analysis.json` を書き込む場合は `"orchestration_agent_run_id": "{orchestration_agent_run_id}"` フィールドを必ず含める。このフィールドは runtime が current-run 同定に使用するため、省略すると timestamp fallback に降格され、ID 再利用時に誤判定が生じる。
            """
        )
    return base


def _canonicalize_spec_ref(repo_root: Path, spec_ref: str) -> str:
    resolved = _resolve_existing_ref_path(repo_root, spec_ref, field_name="spec_ref")
    try:
        rel = resolved.relative_to(repo_root)
        return rel.as_posix()
    except ValueError:
        return str(resolved)


def _validate_source_dependency_ref(source_dependency_ref: str) -> str:
    normalized = source_dependency_ref.strip().replace("\\", "/").strip("/")
    if not normalized:
        raise ValueError("source_dependency_ref must be non-empty")
    if not (normalized.startswith("spec/") and normalized.endswith("/deps.yaml")):
        raise ValueError("source_dependency_ref must match spec/.../deps.yaml")
    return normalized


def _discover_source_dependency_ref(repo_root: Path, spec_ref: str) -> str:
    spec_path = _resolve_existing_ref_path(repo_root, spec_ref, field_name="spec_ref")
    dep_path = (spec_path / "deps.yaml") if spec_path.is_dir() else (spec_path.parent / "deps.yaml")
    if not dep_path.exists():
        raise ValueError(f"source_dependency_ref must exist: {dep_path}")
    try:
        dep_ref = dep_path.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"source_dependency_ref must be under repo root: {dep_path}") from exc
    return _validate_source_dependency_ref(dep_ref)


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
        "orchestration_agent_run_id": meta.get("orchestration_agent_run_id"),
        "orchestration_started_at": meta.get("started_at"),
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


_FAILURE_STATUS_VALUES: frozenset[str] = frozenset(
    {"fail", "fail_closed", "blocked", "timeout", "cancel"}
)


def _is_valid_failure_analysis(
    obj: Any,
    orchestration_id: str,
    *,
    orchestration_agent_run_id: str | None,
) -> bool:
    """Return True only when obj is a substantive failure analysis for this orchestration run.

    Validity requires:
    1. obj is a non-empty dict
    2. orchestration_id matches exactly
    3. status is a recognised failure value
    4. at least one failure-evidence field is non-None / non-empty
    5. Run-identity: orchestration_agent_run_id must be known (non-None) and must match
       the value embedded in obj.  Any other condition — current ID unknown, canonical
       missing the field, or field mismatch — is treated as unverifiable/invalid.
       Timestamp comparison is NOT used: it cannot distinguish same-run from concurrent
       or reused-ID runs and is therefore not a reliable identity proof.
    """
    if not isinstance(obj, dict) or not obj:
        return False
    if obj.get("orchestration_id") != orchestration_id:
        return False
    status = obj.get("status")
    if not isinstance(status, str) or status.strip().lower() not in _FAILURE_STATUS_VALUES:
        return False
    evidence_fields = (
        "reason_code",
        "reason_detail",
        "failed_agent_run",
        "failed_step_results",
        "recommended_retry_decisions",
        "launch_reply_tail",
        "agent_summary_tail",
    )
    has_evidence = any(
        obj.get(f) not in (None, "", []) for f in evidence_fields
    )
    if not has_evidence:
        return False

    # Run-identity: exact orchestration_agent_run_id match is the only accepted proof.
    current_run_id = (
        orchestration_agent_run_id.strip()
        if isinstance(orchestration_agent_run_id, str) and orchestration_agent_run_id.strip()
        else None
    )
    if current_run_id is None:
        # Current run ID unavailable (meta missing/corrupt) — cannot verify ownership.
        return False
    obj_run_id = obj.get("orchestration_agent_run_id")
    return isinstance(obj_run_id, str) and obj_run_id.strip() == current_run_id


def _write_failure_analysis(
    repo_root: Path,
    orchestration_id: str,
    payload: dict[str, Any],
    *,
    tmp_dir: Path | None = None,
) -> tuple[str, str | None, str | None]:
    """Write failure analysis and return (analysis_ref, runtime_ref_or_None, stale_canonical_ref_or_None).

    analysis_ref always points to current-run-valid failure data so callers always
    receive an accurate primary reference regardless of what existed on disk.
    runtime_ref and stale_canonical_ref are supplementary references.

    Ownership contract (startup_contract.md):
    - When failure_analysis.json does not exist: write payload there as safety-net.
      → analysis_ref = failure_analysis.json, runtime_ref = None, stale_canonical_ref = None
    - When failure_analysis.json exists and is valid for this run: preserve canonical,
      write sidecar with existing_file_status="valid".
      → analysis_ref = failure_analysis.json, runtime_ref = failure_analysis.runtime.json,
        stale_canonical_ref = None
    - When failure_analysis.json exists but is invalid/stale: preserve canonical (agent
      owns it), write current payload to sidecar with existing_file_status="invalid".
      analysis_ref is redirected to the sidecar so callers always get current-run data.
      → analysis_ref = failure_analysis.runtime.json, runtime_ref = None,
        stale_canonical_ref = failure_analysis.json
    """
    rel = Path("workspace") / "orchestrations" / orchestration_id / "failure_analysis.json"
    path = repo_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    effective_tmp = tmp_dir or path.parent
    canonical_written = _atomic_write_json_exclusive(path, payload, tmp_dir=effective_tmp)
    if canonical_written:
        return str(rel), None, None
    # File already existed (or appeared concurrently) — agent owns canonical; write sidecar only.
    existing = _read_json_if_exists(path)
    orchestration_agent_run_id = payload.get("orchestration_agent_run_id") if isinstance(payload.get("orchestration_agent_run_id"), str) else None
    existing_is_valid = _is_valid_failure_analysis(
        existing,
        orchestration_id,
        orchestration_agent_run_id=orchestration_agent_run_id,
    )
    existing_file_status = "valid" if existing_is_valid else "invalid"
    # Use a UUID-suffixed sidecar name so concurrent runs with the same orchestration_id
    # do not overwrite each other's runtime analysis.
    runtime_slug = uuid.uuid4().hex[:12]
    runtime_rel = (
        Path("workspace") / "orchestrations" / orchestration_id
        / f"failure_analysis.runtime.{runtime_slug}.json"
    )
    _atomic_write_json(
        repo_root / runtime_rel,
        {**payload, "existing_file_status": existing_file_status},
        tmp_dir=effective_tmp,
    )
    if existing_is_valid:
        # Canonical is current-run data → keep it as primary reference.
        return str(rel), str(runtime_rel), None
    # Canonical is stale — redirect analysis_ref to sidecar so callers get current-run data.
    return str(runtime_rel), None, str(rel)


def _atomic_write_json_exclusive(path: Path, payload: dict[str, Any], *, tmp_dir: Path) -> bool:
    """Write payload to path only if path does not already exist; return True on success.

    Uses write-to-temp + O_CREAT|O_EXCL link/rename to eliminate the TOCTOU window
    between an existence check and the write.  If path already exists (FileExistsError),
    returns False without touching the existing file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        same_device = os.stat(tmp_dir).st_dev == os.stat(path.parent).st_dev
    except OSError:
        same_device = False
    write_dir = tmp_dir if same_device else path.parent
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(dir=write_dir, suffix=".json.tmp")
    tmp = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # O_CREAT|O_EXCL semantics: link fails atomically if destination exists.
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            # Fallback for filesystems that don't support hard links (e.g. some overlayfs).
            # Write to a second temp file, then install it via O_CREAT|O_EXCL rename-equivalent:
            # open the destination exclusively, copy bytes, then close.  If the write fails
            # mid-stream, remove the partial destination to avoid poisoning later runs.
            try:
                excl_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError:
                return False
            dest_created = True
            try:
                with os.fdopen(excl_fd, "w", encoding="utf-8") as ef:
                    ef.write(text)
                dest_created = False  # write succeeded; don't remove on exit
                return True
            finally:
                if dest_created:
                    # Write failed — remove the partial canonical so it doesn't corrupt future runs.
                    path.unlink(missing_ok=True)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any], *, tmp_dir: Path) -> None:
    """Write payload as JSON to path atomically via a unique temp file.

    Temp file is placed in tmp_dir when it is on the same device as path.parent
    (guarantees atomic rename).  Falls back to path.parent otherwise to avoid
    EXDEV on cross-device rename (e.g. split/bind mounts).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Proactively avoid EXDEV: rename is atomic only within the same filesystem.
    try:
        same_device = os.stat(tmp_dir).st_dev == os.stat(path.parent).st_dev
    except OSError:
        same_device = False
    write_dir = tmp_dir if same_device else path.parent
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(dir=write_dir, suffix=".json.tmp")
    tmp = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


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
    missing_tools = _check_required_cli_tools()
    if missing_tools:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "missing_required_cli_tools",
                    "detail": f"missing tools: {','.join(missing_tools)}",
                    "missing": missing_tools,
                    "required": list(REQUIRED_CLI_TOOLS),
                    "docs_ref": "docs/RUNBOOK.md#0-1",
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        workflow_mode = _normalize_workflow_mode(args.mode)
        until_phase = _normalize_phase(args.until_phase)
        repo_root = Path(args.repo_root).resolve()
        orchestration_id = args.orchestration_id or _new_orchestration_id()
        llm_command = args.llm_command or DEFAULT_LLM_COMMANDS[args.llm]
        spec_ref = _canonicalize_spec_ref(repo_root, args.spec_ref)
        source_dependency_ref = _discover_source_dependency_ref(repo_root, spec_ref)
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

    tmp_parent = repo_root / "workspace" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    # TMPDIR must match output_manifest.allowed_tmp_root for the active agent (orchestration uses
    # workspace/tmp/<orchestration_agent_run_id>). Set only after init returns that id; cleanup only
    # that directory so concurrent workflows' workspace/tmp/<other_agent_run_id>/ are untouched.
    orchestration_tmp_for_cleanup: Path | None = None

    env = dict(os.environ)
    env["METDSL_WORKFLOW_MODE"] = "1"
    env["METDSL_ORCHESTRATION_ID"] = orchestration_id
    env["METDSL_WORKFLOW_EXEC_MODE"] = workflow_mode
    env["METDSL_MISSING_ORCHESTRATION_ID_POLICY"] = "strict"
    env["PYTHONPATH"] = str(repo_root) + (f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")

    try:
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
            "--agent-backend",
            args.llm,
        ]
        init_args.extend(["--source-dependency-ref", source_dependency_ref])
        try:
            init_result = _runtime_command(repo_root, env, init_args).payload
            orchestration_agent_run_id = str(init_result.get("orchestration_agent_run_id", "")).strip()
            if not orchestration_agent_run_id:
                raise RuntimeError(
                    "runtime command failed (init): missing orchestration_agent_run_id in init result"
                )
            orch_tmp = tmp_parent / orchestration_agent_run_id
            orch_tmp.mkdir(parents=True, exist_ok=True)
            env["TMPDIR"] = str(orch_tmp)
            orchestration_tmp_for_cleanup = orch_tmp

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
            source_dependency_ref=source_dependency_ref,
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
            if proc.returncode != 0 or workflow_status.lower() in {
                "fail",
                "fail_closed",
                "blocked",
                "timeout",
                "cancel",
            }:
                if workflow_mode == "dev":
                    analysis = _collect_failure_analysis(repo_root, orchestration_id)
                    fail_output: dict[str, Any] = {
                        "status": "fail",
                        "reason": "workflow_failed",
                        "detail": analysis.get("reason_detail") or "workflow execution failed",
                        "orchestration_id": orchestration_id,
                        "workflow_mode": workflow_mode,
                        "workflow_status": workflow_status,
                    }
                    try:
                        analysis_ref, runtime_analysis_ref, stale_canonical_ref = _write_failure_analysis(
                            repo_root,
                            orchestration_id,
                            analysis,
                            tmp_dir=orchestration_tmp_for_cleanup,
                        )
                        fail_output["analysis_ref"] = analysis_ref
                        if runtime_analysis_ref is not None:
                            fail_output["runtime_analysis_ref"] = runtime_analysis_ref
                        if stale_canonical_ref is not None:
                            fail_output["stale_canonical_ref"] = stale_canonical_ref
                    except Exception as primary_exc:  # noqa: BLE001
                        # Primary write failed — attempt an emergency exclusive-create write so
                        # at least some artifact survives without clobbering agent-owned canonical.
                        orch_dir = (
                            repo_root / "workspace" / "orchestrations" / orchestration_id
                        )
                        emergency_payload = {**analysis, "emergency_write": True}
                        canonical_path = orch_dir / "failure_analysis.json"
                        try:
                            orch_dir.mkdir(parents=True, exist_ok=True)
                            # Try exclusive-create on canonical first (succeeds only when absent).
                            wrote_canonical = _atomic_write_json_exclusive(
                                canonical_path,
                                emergency_payload,
                                tmp_dir=orch_dir,
                            )
                            if wrote_canonical:
                                fallback_ref = str(canonical_path.relative_to(repo_root))
                            else:
                                # Canonical already exists (agent owns it) — write to unique sidecar.
                                # Retry with a fresh UUID on collision (bounded to avoid infinite loop).
                                _MAX_SIDECAR_ATTEMPTS = 5
                                fallback_ref = None
                                for _ in range(_MAX_SIDECAR_ATTEMPTS):
                                    slug = uuid.uuid4().hex[:12]
                                    sidecar = orch_dir / f"failure_analysis.fallback.{slug}.json"
                                    if _atomic_write_json_exclusive(
                                        sidecar, emergency_payload, tmp_dir=orch_dir
                                    ):
                                        fallback_ref = str(sidecar.relative_to(repo_root))
                                        break
                                if fallback_ref is None:
                                    raise OSError(
                                        "emergency sidecar write failed after "
                                        f"{_MAX_SIDECAR_ATTEMPTS} attempts"
                                    )
                            fail_output["analysis_ref"] = fallback_ref
                            fail_output["analysis_ref_error"] = str(primary_exc)
                            fail_output["analysis_ref_write_mode"] = "emergency_fallback"
                        except Exception as fallback_exc:  # noqa: BLE001
                            # Both writes failed — no artifact on disk.
                            fail_output["reason"] = "failure_analysis_persist_failed"
                            fail_output["analysis_ref_error"] = str(primary_exc)
                            fail_output["analysis_ref_fallback_error"] = str(fallback_exc)
                    print(json.dumps(fail_output, ensure_ascii=False))
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
    finally:
        if orchestration_tmp_for_cleanup is not None and orchestration_tmp_for_cleanup.exists():
            shutil.rmtree(orchestration_tmp_for_cleanup, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
