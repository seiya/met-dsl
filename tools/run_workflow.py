#!/usr/bin/env python3
"""Bootstrap workflow orchestration startup for a target spec."""

from __future__ import annotations

import argparse
import json
import os
import re
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

# Direct-CLI import bootstrap. When this script is executed as
# `python3 tools/run_workflow.py ...` (the canonical entrypoint per
# CLAUDE.md), `sys.path[0]` is `tools/`, not the repo root, so absolute
# package imports like `from tools.validate_pipeline_semantics import ...`
# fail with `ModuleNotFoundError` before any structured error handling
# can run. Mirror the pattern used by `tools/validate_pipeline_semantics.py`
# and `tools/orchestration_runtime.py`: detect the missing import and
# prepend the repo root to `sys.path`. The probe import is intentionally
# small (a stdlib-style module name we know lives next to this script)
# so the side effect is just sys.path adjustment.
try:
    from tools import validate_pipeline_semantics as _probe  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover - direct CLI execution
    _THIS_FILE = Path(__file__).resolve()
    _REPO_ROOT = _THIS_FILE.parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    # Re-probe so the in-function imports later in main() succeed.
    from tools import validate_pipeline_semantics as _probe  # noqa: F401

# Post-mortem diagnostics for an incomplete (dangling) child launch. Imported
# after the path bootstrap above so `tools` is importable under direct CLI run.
from tools.orchestration_diagnostics import build_launch_incident

SUPPORTED_LLMS = ("codex", "cursor", "claude")
SUPPORTED_WORKFLOW_MODES = ("dev", "prod")
# Applied when --llm / --mode are omitted on a non-resume run. Kept as the
# historical defaults so plain `run_workflow.py <spec> <phase>` is unchanged.
DEFAULT_LLM = "codex"
DEFAULT_WORKFLOW_MODE = "dev"
DEFAULT_LLM_COMMANDS = {
    "codex": "codex",
    "cursor": "cursor",
    "claude": "claude",
}
# Default orchestration-agent model recorded on the orchestration agent_runs row
# for the claude backend (the host session runs Opus). Operators on a different
# model override it with --agent-model. codex/cursor model ids are not knowable to
# this entrypoint, so they are left to repair-agent-runs sibling backfill.
DEFAULT_CLAUDE_AGENT_MODEL = "claude-opus-4-8"

PHASE_ALIASES = {
    "compile": "Compile",
    "generate": "Generate",
    "build": "Build",
    "validate": "Validate",
}
PHASE_ORDER = ["Compile", "Generate", "Build", "Validate"]

# CLI tools the workflow runtime depends on (used internally by orchestration_runtime
# subcommands such as run-gate / guarded-apply-patch, and by git-based status probes
# in tools/run_workflow.py itself). Missing any one fails the run before init, so
# agents never hit a partial-failure state where (e.g.) jq is unavailable to runtime
# but already in the agent's environment.
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


def _launch_command_and_input(
    *, llm: str, llm_command: str, prompt_text: str, session_id: str | None = None
) -> tuple[list[str], str | None]:
    command = shlex.split(llm_command)
    if not command:
        raise ValueError("llm_command must be non-empty")
    # Codex default entrypoint requires a terminal in interactive mode.
    # Use non-interactive subcommand to run from this bootstrap script.
    if llm == "codex":
        return [*command, "exec", prompt_text], None
    # Claude Code defaults to launching the interactive TUI; `-p` (--print) runs
    # the prompt non-interactively and exits, which is required when invoked from
    # this bootstrap script. `--session-id` pins the host session UUID so the
    # transcript at ~/.claude/projects/<slug>/<session_id>.jsonl is addressable and
    # recordable in orchestration_meta.json#host_session_id (observability).
    if llm == "claude":
        session_flags = ["--session-id", session_id] if session_id else []
        return [*command, *session_flags, "-p", prompt_text], None
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
    allowed_tmp_root = f"workspace/tmp/{orchestration_agent_run_id}"
    base = textwrap.dedent(
        f"""
        Start the workflow.

        ## tmp area (reference by literal path)

        This orchestration agent's `allowed_tmp_root` is the following literal path:

        ```
        {allowed_tmp_root}
        ```

        When a temporary file is needed, specify `{allowed_tmp_root}/...` **literally**.
        Because `output_manifest_write_guard` only judges whether it is under the manifest's `allowed_tmp_root`
        and does not look at the `$TMPDIR` env, a reference via an env variable is unnecessary.
        Do not call `export TMPDIR=...`, `jq -er ...`, `printenv`, or `bash -c` in Bash
        (it is a cause of the workflow stopping on a session-sandbox approval request).
        The env (`METDSL_ORCHESTRATION_ID` / `ORCHESTRATION_AGENT_RUN_ID` / `TMPDIR`) is
        already inherited into the subprocess by `tools/run_workflow.py`, so a confirmation Bash is also unnecessary.

        ## startup context
        - orchestration_id: `{orchestration_id}`
        - orchestration_agent_run_id: `{orchestration_agent_run_id}`
        - workflow_mode: `{workflow_mode}`
        - target_spec_ref: `{spec_ref}`
        - dependency_ref: `{source_dependency_ref}`
        - target_phases: `{phase_list}` (end phase: `{until_phase}`)

        ## execution constraints
        - First read `skills/workflow-orchestration/SKILL.md` and `skills/workflow-orchestration/references/startup_contract.md`.
        - Maintain `METDSL_WORKFLOW_MODE=1` during workflow execution.
        - If the information needed to start is insufficient, stop immediately, enumerate the missing items, and report.
        - Do not proceed by guessing or completing the missing information.
        - This launch uses the context generated by `tools/run_workflow.py` as the canonical input. Do not start the workflow by any other path.
        - Delegate the body processing of phase artifacts to the child agent; the parent agent does not proxy it.
        - The canonical source for the child agent's requirement definition and judgment rules is limited to `docs/`, `spec/`, and the relevant trial's artifacts.
        - Run `workflow-launch-check` before launching a child agent, and stop on failure.
        - Proceed from the starting phase up to `{until_phase}`, and do not proceed to any later phase.
        - When a temporary file is needed, do not specify `/tmp` or `/dev/shm`; directly use the literal path of the `tmp area` section above (`{allowed_tmp_root}/...`). Hard-coding `/tmp/` is blocked by `output_manifest_write_guard`.
        - The auto-Read of `~/.claude/projects/.../memory/MEMORY.md` immediately after Claude Code startup is blocked by `read_manifest_read_guard`, but this is expected behavior and does not affect the continuation of the workflow. Do not retry or attempt a reference under `MEMORY.md`.
        """
    ).strip() + "\n"

    if workflow_mode == "dev":
        base += textwrap.dedent(
            f"""
            - In the verify substep, if `issue_severity` is other than `minor`, stop with fail.
            - On fail, prioritize the primary evidence (`agent_runs.jsonl`, `step_result.json`, `agent.summary.txt`, `launches/*.reply.txt`) to investigate the cause, and report it with the basis.
            - Save the information needed to investigate the progress, as far as possible, under `workspace/orchestrations/<orchestration_id>/`.
            - When writing `failure_analysis.json`, always include the `"orchestration_agent_run_id": "{orchestration_agent_run_id}"` field. Because the runtime uses this field to identify the current run, omitting it demotes it to a timestamp fallback and causes a misjudgment when the ID is reused.
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


def _orchestration_used_conductor(repo_root: Path, orchestration_id: str) -> bool:
    """True when the orchestration was driven by the deterministic conductor
    (run_conductor writes an orchestrator marker), so --resume restores the driver."""
    marker = _read_json_if_exists(
        repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestrator.json")
    return isinstance(marker, dict) and marker.get("orchestrator") == "conductor"


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

    def _run_key(run: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(run.get("node_key") or ""),
            str(run.get("step") or ""),
            str(run.get("substep") or ""),
        )

    # Index of the last passing run per (node_key, step, substep). A terminal-nonpass
    # run is "resolved" (superseded) when a *later* run of the same key passed — e.g. the
    # judge timeout that the orchestration re-ran to pass, or a blocked/cancelled verify
    # later re-run green. Such runs must not be reported as the workflow failure. The
    # agent_runs `superseded`/`superseded_by` fields are not currently written, so
    # reconcile by key + replay order instead.
    last_pass_index: dict[tuple[str, str, str], int] = {}
    for idx, run in enumerate(runs):
        if isinstance(run.get("status"), str) and str(run.get("status")).strip().lower() == "pass":
            last_pass_index[_run_key(run)] = idx

    failed_runs = [
        run
        for idx, run in enumerate(runs)
        if isinstance(run.get("status"), str)
        and str(run.get("status")).strip().lower() in terminal_fail_statuses
        and last_pass_index.get(_run_key(run), -1) < idx
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

    # Surface any dangling-launch incident snapshot (written at incident time by the
    # synchronous-launch capture in main()) so failure_analysis links to it. Globbed
    # rather than threaded through a parameter so it also resolves on resume / re-collect.
    launch_incident_refs = [
        str(p.relative_to(repo_root))
        for p in sorted(orch_root.glob("launch_incident.runtime.*.json"))
    ]

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
        "launch_incident_refs": launch_incident_refs,
    }


_FAILURE_STATUS_VALUES: frozenset[str] = frozenset(
    {"fail", "fail_closed", "blocked", "timeout", "cancel"}
)
# Statuses that make an orchestration safe to auto-select as "the latest" for
# implicit (`--resume` without `--orchestration-id`) resume. A non-terminal status
# (e.g. `running`) is ambiguous — it may be an active concurrent run whose shared
# workspace/tmp/<arid> resume would clobber, or a crashed run — so implicit resume
# refuses it and asks for an explicit id.
_RESUMABLE_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"pass", "fail", "fail_closed", "blocked", "timeout", "cancel"}
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
        # In the degraded dangling-launch path (both terminalize set-status calls
        # failed), the dangling child has no terminal agent_runs row and meta carries
        # no reason_code/detail, so the incident snapshot ref is the only evidence.
        "launch_incident_refs",
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
    verify_steps = {"compile", "generate", "validate"}
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


def _find_latest_orchestration(repo_root: Path) -> str | None:
    """Return the most recent orchestration_id under workspace/orchestrations.

    Ranking uses orchestration_meta.json#started_at (UTC ISO8601, microsecond
    precision — chronologically sortable as text) rather than the directory name.
    A lexical max over ids is wrong because: (1) ids may be caller-supplied via
    --orchestration-id (e.g. `orch_unit`) and would sort after timestamp ids like
    `orch_202606...`, and (2) ids generated in the same second differ only by a
    random suffix, so name order is not creation order. Orchestrations whose meta
    lacks a usable started_at sort oldest; the id is a deterministic tie-breaker.

    Any subdirectory carrying an orchestration_meta.json is an orchestration —
    the id need not start with `orch_`, since --orchestration-id accepts arbitrary
    caller-supplied ids and those runs must remain resumable as "the latest".
    """
    orch_root = repo_root / "workspace" / "orchestrations"
    if not orch_root.is_dir():
        return None
    candidates: list[tuple[str, str]] = []
    for path in orch_root.iterdir():
        if not path.is_dir():
            continue
        meta = _read_json_if_exists(path / "orchestration_meta.json")
        if not isinstance(meta, dict):
            continue
        started_at = meta.get("started_at")
        started_key = started_at.strip() if isinstance(started_at, str) else ""
        candidates.append((started_key, path.name))
    if not candidates:
        return None
    # max over (started_at, id): newest start wins; equal/empty starts fall back
    # to a stable lexical id tie-break.
    return max(candidates, key=lambda item: (item[0], item[1]))[1]


def _extract_prompt_params(prompt_text: str) -> dict[str, str]:
    """Recover startup params embedded by _build_orchestration_prompt().

    Returns whichever of {until_phase, mode, spec_ref} can be parsed from the
    `orchestration.start.prompt.txt` body. A round-trip unit test pins this
    extractor to the prompt format so a wording change cannot silently break it.
    """
    found: dict[str, str] = {}
    mode_match = re.search(r"workflow_mode:\s*`([^`]+)`", prompt_text)
    if mode_match:
        found["mode"] = mode_match.group(1).strip()
    phase_match = re.search(r"end phase:\s*`([^`]+)`", prompt_text)
    if phase_match is None:
        # Backward compatibility: orchestrations created before the English
        # translation of the start prompt used the Japanese "終了 phase:" label.
        phase_match = re.search(r"終了 phase:\s*`([^`]+)`", prompt_text)
    if phase_match:
        found["until_phase"] = phase_match.group(1).strip()
    spec_match = re.search(r"target_spec_ref:\s*`([^`]+)`", prompt_text)
    if spec_match:
        found["spec_ref"] = spec_match.group(1).strip()
    return found


def _load_resume_params(repo_root: Path, orchestration_id: str) -> dict[str, str | None]:
    """Recover launch params for a resume from an orchestration's existing artifacts.

    No dedicated params file is persisted: every value is recovered from artifacts
    that run_workflow.py already writes on every start.
    - spec_ref / source_dependency_ref ← orchestration_meta.json
    - llm                              ← preflight.json#backend
    - llm_command                      ← preflight.json#probe_command
    - until_phase / mode               ← launches/orchestration.start.prompt.txt
    Missing/unparseable values are returned as None for the caller to validate.
    """
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    meta = _read_json_if_exists(orch_root / "orchestration_meta.json") or {}
    preflight = _read_json_if_exists(orch_root / "preflight.json") or {}
    prompt_path = orch_root / "launches" / "orchestration.start.prompt.txt"
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    prompt_params = _extract_prompt_params(prompt_text)

    def _clean(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    return {
        "spec_ref": _clean(meta.get("spec_ref")) or prompt_params.get("spec_ref"),
        "source_dependency_ref": _clean(meta.get("source_dependency_ref")),
        "llm": _clean(preflight.get("backend")),
        # probe_command is the agent command run_workflow used for both preflight
        # and launch on the original run; reuse it so a custom --llm-command (e.g.
        # a wrapper / non-PATH binary) survives resume.
        "llm_command": _clean(preflight.get("probe_command")),
        "until_phase": prompt_params.get("until_phase"),
        "mode": prompt_params.get("mode"),
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap workflow startup (init + preflight + prompt).",
    )
    parser.add_argument(
        "spec_ref",
        nargs="?",
        help="Target spec path/reference. Optional with --resume (recovered from the resumed orchestration).",
    )
    parser.add_argument(
        "until_phase",
        nargs="?",
        help=(
            "Final phase to execute (compile/generate/build/validate). "
            "Optional with --resume (recovered from the resumed orchestration)."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume the latest orchestration (or --orchestration-id) from its checkpoint. "
            "spec_ref / until_phase / --llm / --mode are recovered from the resumed "
            "orchestration when omitted."
        ),
    )
    parser.add_argument(
        "--mode",
        default=None,
        choices=SUPPORTED_WORKFLOW_MODES,
        help="Workflow execution mode: dev (default) or prod.",
    )
    parser.add_argument("--llm", default=None, choices=SUPPORTED_LLMS)
    parser.add_argument("--llm-command", help="Override backend command used by preflight and optional launch.")
    parser.add_argument(
        "--agent-model",
        default=None,
        help=(
            "Model id of the orchestration agent itself, recorded on its agent_runs "
            "row for cost attribution / reproducibility. Defaults to "
            f"'{DEFAULT_CLAUDE_AGENT_MODEL}' only for the claude backend running the "
            "unmodified default command; with a custom --llm-command (which may launch "
            "a different model) it is omitted unless given here. When omitted, "
            "repair-agent-runs backfills it from sibling rows on resume."
        ),
    )
    parser.add_argument(
        "--with-deps",
        action="store_true",
        help=(
            "Before running the target, resolve its transitive dependency closure "
            "(deps.yaml + spec_catalog.yaml) and run each not-yet-ready dependency "
            "node's workflow bottom-up (dependency order), one orchestration per "
            "node. Dependency nodes run to Compile when the target ends at compile, "
            "else to Validate (matching compile / execution readiness). Already-ready "
            "dependencies are skipped. Ignored with --resume (target only)."
        ),
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--orchestration-id", help="If omitted, generated automatically (or, with --resume, the latest orchestration).")
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
    parser.add_argument(
        "--orchestrator",
        choices=("llm", "conductor"),
        default=None,
        help=(
            "Orchestration driver. 'llm' (default): spawn an LLM orchestration agent "
            "to drive the phase loop. 'conductor': drive the deterministic phase/substep "
            "loop in Python (tools/workflow_conductor.py), invoking the LLM only as a "
            "leaf for each substep body — removes the parent orchestration LLM's "
            "per-turn cache_read overhead. See docs/design/deterministic_conductor.md."
        ),
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
    repo_root = Path(args.repo_root).resolve()

    # Resolve effective startup inputs. With --resume, omitted spec_ref /
    # until_phase / --llm / --mode are recovered from the target orchestration's
    # existing artifacts (orchestration_meta.json + preflight.json + the start
    # prompt). Without --resume the historical defaults (codex / dev) apply.
    resume_mode = bool(args.resume)
    # Recovered resume metadata (populated in the resume branch). The reuse decision
    # in the try block compares the effective spec/backend against these to tell an
    # actual change from an explicit no-op restate.
    resume_recovered_spec_ref: str | None = None
    resume_recovered_dep_ref: str | None = None
    resume_recovered_llm: str | None = None
    resume_recovered_llm_command: str | None = None
    resume_recovered_orchestrator: str | None = None
    if resume_mode:
        explicit_id = bool(args.orchestration_id)
        orchestration_id = args.orchestration_id or _find_latest_orchestration(repo_root)
        if not orchestration_id:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "no_resumable_orchestration",
                        "detail": "no orchestration found under workspace/orchestrations to resume",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        if not explicit_id:
            # Implicit "latest" must be a terminalized run. A non-terminal latest
            # (e.g. an active concurrent `running` orchestration) would share its
            # orchestration_agent_run_id, and resume's tmp cleanup could delete the
            # live run's workspace/tmp/<arid>. Require an explicit id in that case.
            latest_meta = _read_json_if_exists(
                repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
            ) or {}
            latest_status = str(latest_meta.get("status") or "").strip().lower()
            if latest_status not in _RESUMABLE_TERMINAL_STATUSES:
                print(
                    json.dumps(
                        {
                            "status": "fail",
                            "reason": "latest_orchestration_not_resumable",
                            "detail": (
                                f"latest orchestration {orchestration_id} has non-terminal status "
                                f"'{latest_status or 'unknown'}'; pass --orchestration-id to resume a specific run"
                            ),
                            "orchestration_id": orchestration_id,
                        },
                        ensure_ascii=False,
                    )
                )
                return 2
        recovered = _load_resume_params(repo_root, orchestration_id)
        # Restore the orchestration driver: a conductor-driven run leaves a marker,
        # so a plain `--resume` does not silently fall back to the LLM orchestrator.
        if _orchestration_used_conductor(repo_root, orchestration_id):
            resume_recovered_orchestrator = "conductor"
        spec_ref_arg = args.spec_ref
        until_phase_arg = args.until_phase
        # A lone positional is ambiguous on resume: argparse binds it to spec_ref,
        # but overriding until_phase (e.g. extending the run further) is the common
        # case while overriding spec_ref is not. If only spec_ref was given and it
        # names a known phase, treat it as the until_phase override instead.
        if spec_ref_arg and not until_phase_arg and spec_ref_arg.strip().lower() in PHASE_ALIASES:
            until_phase_arg = spec_ref_arg
            spec_ref_arg = None
        spec_ref_in = spec_ref_arg or recovered.get("spec_ref")
        until_phase_in = until_phase_arg or recovered.get("until_phase")
        llm_in = args.llm or recovered.get("llm")
        mode_in = args.mode or recovered.get("mode")
        # Carry the recovered values; the reuse decision happens in the try block
        # below, keyed on whether the *effective* spec/backend actually changed
        # (not merely whether the arg was passed) — passing the same value
        # explicitly must still reuse the recovered dependency/command.
        resume_recovered_spec_ref = recovered.get("spec_ref")
        resume_recovered_dep_ref = recovered.get("source_dependency_ref")
        resume_recovered_llm = recovered.get("llm")
        resume_recovered_llm_command = recovered.get("llm_command")
        missing = [
            name
            for name, value, ok in (
                ("spec_ref", spec_ref_in, bool(spec_ref_in)),
                ("until_phase", until_phase_in, bool(until_phase_in)),
                ("llm", llm_in, llm_in in SUPPORTED_LLMS),
                ("mode", mode_in, bool(mode_in)),
            )
            if not ok
        ]
        if missing:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "resume_params_unrecoverable",
                        "detail": (
                            f"could not recover {', '.join(missing)} for orchestration "
                            f"{orchestration_id}; pass them explicitly"
                        ),
                        "orchestration_id": orchestration_id,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
    else:
        orchestration_id = args.orchestration_id or _new_orchestration_id()
        spec_ref_in = args.spec_ref
        until_phase_in = args.until_phase
        llm_in = args.llm or DEFAULT_LLM
        mode_in = args.mode or DEFAULT_WORKFLOW_MODE

    # Effective orchestration driver: explicit --orchestrator wins; otherwise on
    # --resume restore the original run's driver (from its marker); else default llm.
    orchestrator = args.orchestrator or resume_recovered_orchestrator or "llm"

    try:
        workflow_mode = _normalize_workflow_mode(mode_in)
        if not until_phase_in:
            raise ValueError("until_phase is required unless --resume is set")
        until_phase = _normalize_phase(until_phase_in)
        llm = llm_in
        # The deterministic conductor only has a leaf launcher for claude/codex;
        # reject an unsupported backend up front instead of failing at the first
        # substep after init/preflight already created the orchestration.
        if orchestrator == "conductor" and llm not in ("claude", "codex"):
            raise ValueError(
                f"--orchestrator conductor supports --llm claude|codex, not {llm!r}"
            )
        # Reuse the recovered agent command unless --llm-command was given or the
        # backend actually changed; restating the same --llm must keep the command.
        if args.llm_command:
            llm_command = args.llm_command
        elif resume_recovered_llm_command and llm == resume_recovered_llm:
            llm_command = resume_recovered_llm_command
        else:
            llm_command = DEFAULT_LLM_COMMANDS[llm]
        if not spec_ref_in:
            raise ValueError("spec_ref is required unless --resume is set")
        spec_ref = _canonicalize_spec_ref(repo_root, spec_ref_in)
        # Reuse the recovered dependency ref when the spec is unchanged (compared
        # canonically, so restating the same spec still counts as unchanged).
        # Format-validate only — no existence check — so resume stays stable even if
        # the dependency file moved/was renamed after the original run. A genuine
        # spec change rediscovers the dependency next to the new spec.
        if resume_recovered_dep_ref and spec_ref == resume_recovered_spec_ref:
            source_dependency_ref = _validate_source_dependency_ref(resume_recovered_dep_ref)
        else:
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

    # Startup assertion: validate_pipeline_semantics now fail-closes when the
    # active repo_root's `spec/schema/ir/shape_expr.schema.json` is missing,
    # malformed, contains an invalid regex, or fails the structural classifier.
    # We must surface ALL of those failure modes here BEFORE any orchestration
    # state mutation (init/preflight/launches/...), otherwise the run would
    # create `workspace/tmp/<arid>/` and orchestration_meta.json only to
    # collapse later with `schema_load_failed` mid-phase, leaving partially
    # initialized state to clean up.
    #
    # Reuse the validator's actual schema loader so the check exercises the
    # same code path as the gate it is guarding — `is_file()` alone would
    # miss malformed JSON, invalid regex, and structural-classifier failures.
    required_schema = repo_root / "spec" / "schema" / "ir" / "shape_expr.schema.json"
    try:
        from tools.validate_pipeline_semantics import (
            _get_shape_expr_patterns,
            _load_shape_expr_patterns_cached,
        )
        _load_shape_expr_patterns_cached.cache_clear()
        _get_shape_expr_patterns(repo_root=repo_root)
    except (RuntimeError, ModuleNotFoundError) as exc:
        try:
            missing_path_rel = str(required_schema.relative_to(repo_root))
        except ValueError:
            missing_path_rel = str(required_schema)
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "missing_canonical_schema",
                    "detail": (
                        f"canonical schema invalid or missing: {missing_path_rel}. "
                        f"{exc}"
                    ),
                    "missing_path": missing_path_rel,
                },
                ensure_ascii=False,
            )
        )
        return 2

    # Base env shared by every node. METDSL_ORCHESTRATION_ID / TMPDIR /
    # ORCHESTRATION_AGENT_RUN_ID are per-node and set inside _run_node so a
    # dependency-closure run (one orchestration per node) never leaks the
    # previous node's ids/tmp into the next.
    base_env = dict(os.environ)
    base_env["METDSL_WORKFLOW_MODE"] = "1"
    base_env["METDSL_WORKFLOW_EXEC_MODE"] = workflow_mode
    base_env["METDSL_MISSING_ORCHESTRATION_ID_POLICY"] = "strict"
    # Orchestration driver selector, read back in _run_node. Carried via base_env
    # (rather than threaded through every _run_node / dependency-closure call site)
    # to keep the wiring localized; it is a harmless no-op in child subprocess env.
    base_env["METDSL_ORCHESTRATOR"] = orchestrator
    base_env["PYTHONPATH"] = str(repo_root) + (
        f":{base_env['PYTHONPATH']}" if base_env.get("PYTHONPATH") else ""
    )
    # Prevent Python from writing *.pyc / __pycache__ bytecode under tools/.
    # Without this, any `python3 tools/orchestration_runtime.py` call made by
    # the orchestration agent (or child subprocesses) generates
    # tools/__pycache__/orchestration_runtime.cpython-<ver>.pyc, which is not
    # in any agent's output_manifest and triggers unauthorized_write_violation
    # at record-agent-run terminal validation.  Setting this in the shared env
    # dict ensures it propagates to: (a) _runtime_command() subprocesses,
    # (b) the orchestration agent launch subprocess, and (c) any grandchild
    # `python3 tools/...` invocations the agent makes.
    base_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    # `--with-deps` runs the target's transitive dependency closure bottom-up
    # (one orchestration per node) before the target. Scoped to fresh runs:
    # `--resume` re-enters a single existing orchestration (the target), so the
    # closure is not re-walked there.
    if getattr(args, "with_deps", False) and not resume_mode:
        return _run_with_dependency_closure(
            repo_root=repo_root,
            base_env=base_env,
            target_orchestration_id=orchestration_id,
            target_spec_ref=spec_ref,
            target_source_dependency_ref=source_dependency_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            workflow_mode=workflow_mode,
            agent_model=args.agent_model,
            status=args.status,
            invoke_llm=args.invoke_llm,
        )

    return _run_node(
        repo_root=repo_root,
        base_env=base_env,
        orchestration_id=orchestration_id,
        spec_ref=spec_ref,
        source_dependency_ref=source_dependency_ref,
        until_phase=until_phase,
        llm=llm,
        llm_command=llm_command,
        workflow_mode=workflow_mode,
        agent_model=args.agent_model,
        status=args.status,
        invoke_llm=args.invoke_llm,
        resume_mode=resume_mode,
    )


def _run_node(
    *,
    repo_root: Path,
    base_env: dict[str, str],
    orchestration_id: str,
    spec_ref: str,
    source_dependency_ref: str,
    until_phase: str,
    llm: str,
    llm_command: str,
    workflow_mode: str,
    agent_model: str | None,
    status: str,
    invoke_llm: bool,
    resume_mode: bool,
    extra_output: dict[str, Any] | None = None,
) -> int:
    """Run a single node's orchestration (init → preflight → prompt → launch →
    terminalize) and print its JSON result. Returns the process exit code
    (0 = ok). Each call uses its own orchestration_id / TMPDIR so the
    dependency-closure driver can run one orchestration per node without
    cross-node env/tmp leakage. `extra_output`, when given, is merged into the
    final ok/fail JSON (used to carry the `dependency_runs` summary onto the
    target node's result)."""
    env = dict(base_env)
    env["METDSL_ORCHESTRATION_ID"] = orchestration_id

    tmp_parent = repo_root / "workspace" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    # TMPDIR must match output_manifest.allowed_tmp_root for the active agent (orchestration uses
    # workspace/tmp/<orchestration_agent_run_id>). Set only after init returns that id; cleanup only
    # that directory so concurrent workflows' workspace/tmp/<other_agent_run_id>/ are untouched.
    orchestration_tmp_for_cleanup: Path | None = None

    # For the Claude backend, pin a fresh host session UUID per launch so the real
    # Claude Code transcript (~/.claude/projects/<slug>/<host_session_id>.jsonl) is
    # addressable and can be recorded in orchestration_meta.json#host_session_id.
    # A resume spawns a new session, so a fresh id is generated each invocation.
    # Gate on invoke_llm: with --no-invoke-llm no `claude --session-id` process
    # ever starts, so recording a host_session_id would point meta at a transcript
    # that does not exist. (host_session_id is recorded at init for run-write-baseline
    # integrity; on a subsequent real launch via --resume it is regenerated.)
    host_session_id: str | None = (
        str(uuid.uuid4()) if (llm == "claude" and invoke_llm) else None
    )

    try:
        if resume_mode:
            # Resume an existing orchestration: enable checkpoint resume (sets
            # resume_enabled=true and preserves orchestration_agent_run_id) instead
            # of re-initializing. The returned meta carries orchestration_agent_run_id.
            # Pass the resolved spec/dependency refs so meta stays in sync when they
            # were overridden on the CLI — otherwise a later implicit resume would
            # recover the stale meta value and revert the override.
            init_args = [
                "init",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--resume-from-checkpoint",
                "--spec-ref",
                spec_ref,
                "--source-dependency-ref",
                source_dependency_ref,
            ]
            # Forward an EXPLICIT --agent-model to the resume repair (it overrides
            # repair-agent-runs' sibling derivation, e.g. for a `needs_manual` row).
            # Do NOT apply the claude default here: with no override, sibling_uniform
            # derives the run's actual model, which is more accurate than a default.
            if agent_model:
                init_args += ["--agent-model", agent_model]
        else:
            init_args = [
                "init",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--spec-ref",
                spec_ref,
                "--status",
                status,
                "--agent-backend",
                llm,
                "--source-dependency-ref",
                source_dependency_ref,
            ]
            # Record the orchestration agent's own model so its agent_runs row is
            # not a cost-attribution blind spot. Explicit --agent-model wins.
            # Otherwise default to Opus ONLY for the claude backend running the
            # UNMODIFIED default command — an overridden --llm-command (e.g. a wrapper
            # selecting a different model) could launch a non-Opus model, so we must
            # not assert Opus there; leave it for sibling backfill on resume instead.
            orchestration_model = agent_model
            if (
                not orchestration_model
                and llm == "claude"
                and llm_command == DEFAULT_LLM_COMMANDS["claude"]
            ):
                orchestration_model = DEFAULT_CLAUDE_AGENT_MODEL
            if orchestration_model:
                init_args += ["--agent-model", orchestration_model]
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
            env["ORCHESTRATION_AGENT_RUN_ID"] = orchestration_agent_run_id
            orchestration_tmp_for_cleanup = orch_tmp

            preflight_args = [
                "preflight",
                "--repo-root",
                str(repo_root),
                "--orchestration-id",
                orchestration_id,
                "--backend",
                llm,
                "--agent-command",
                llm_command,
            ]
            # Record host_session_id only when preflight is launchable (write_preflight
            # gates it), so a failed/non-launchable preflight never points meta at a
            # session that did not start. host_session_id is set only for claude +
            # invoke_llm.
            if host_session_id:
                preflight_args += ["--host-session-id", host_session_id]
            preflight_result = _runtime_command(repo_root, env, preflight_args).payload
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
        cli_returncode_warning: int | None = None
        if invoke_llm and env.get("METDSL_ORCHESTRATOR") == "conductor":
            # Deterministic conductor: drive the phase loop in Python (no parent
            # orchestration LLM). The leaf substeps are spawned by the conductor.
            from tools.workflow_conductor import run_conductor

            try:
                workflow_status = run_conductor(
                    repo_root=repo_root,
                    orchestration_id=orchestration_id,
                    orchestration_agent_run_id=orchestration_agent_run_id,
                    spec_ref=spec_ref,
                    source_dependency_ref=source_dependency_ref,
                    until_phase=until_phase,
                    backend=llm,
                    agent_model=agent_model or "",
                    workflow_mode=workflow_mode,
                    env=env,
                    llm_command=llm_command,
                    resume=resume_mode,
                )
            except Exception as exc:  # noqa: BLE001 - terminalize on conductor error
                _runtime_command(
                    repo_root, env,
                    ["set-status", "--repo-root", str(repo_root), "--orchestration-id",
                     orchestration_id, "--status", "fail", "--reason-code",
                     "conductor_error", "--reason-detail", str(exc)[:200]],
                )
                print(json.dumps(
                    {"status": "fail", "reason": "conductor_error", "detail": str(exc),
                     "orchestration_id": orchestration_id}, ensure_ascii=False))
                return 2
            launched = True
            # The conductor terminalizes meta itself. The LLM-path failure-reporting
            # block below is skipped for the conductor, so report a non-pass terminal
            # here and exit nonzero (otherwise a failed run falls through to the
            # generic ok output with exit 0).
            if workflow_status.strip().lower() != "pass":
                fail_output: dict[str, Any] = {
                    "status": "fail",
                    "reason": "workflow_failed",
                    "orchestration_id": orchestration_id,
                    "workflow_mode": workflow_mode,
                    "workflow_status": workflow_status,
                }
                if extra_output:
                    fail_output.update(extra_output)
                print(json.dumps(fail_output, ensure_ascii=False))
                return 2
        elif invoke_llm:
            launch_command, launch_input = _launch_command_and_input(
                llm=llm,
                llm_command=llm_command,
                prompt_text=prompt_text,
                session_id=host_session_id,
            )
            proc = subprocess.run(
                launch_command,
                cwd=repo_root,
                env=env,
                text=True,
                input=launch_input,
                check=False,
            )
            # `launched` means "the LLM subprocess was actually invoked" (distinguishing
            # the ok path from --no-invoke-llm), NOT "it returned 0". Success/return-code
            # is conveyed separately: a nonzero exit either takes the fail branch below or,
            # when meta.status=pass, surfaces as `cli_returncode_warning` in the ok output.
            launched = True
            meta_after_launch = _read_json_if_exists(
                repo_root / "workspace" / "orchestrations" / orchestration_id / "orchestration_meta.json"
            )
            if isinstance(meta_after_launch, dict):
                workflow_status = str(meta_after_launch.get("status") or "running")
            # Capture an incomplete (dangling) child launch. record-launch opened the
            # active_child window but the child never returned (hang/interrupt), and the
            # host process may still have exited cleanly (returncode 0 — orchestration
            # agent ended its turn with an "I've paused" message). The returncode!=0
            # terminalize below would miss that, silently leaving the orchestration
            # "running" with no in-repo record of WHY. Detect it here (returncode-agnostic),
            # snapshot the decisive — and ephemeral — ~/.claude transcript tail in-repo, and
            # terminalize so `--resume` can recover it. The snapshot uses the runtime-owned
            # `launch_incident.runtime.<uuid12>.json` name exempted in
            # orchestration_runtime._should_ignore_runtime_snapshot_path so it is not
            # misattributed as an unauthorized child write in the terminal diff.
            launch_incident_ref: str | None = None
            launch_incident_detected = False
            try:
                launch_incident = build_launch_incident(repo_root, orchestration_id)
            except Exception:  # noqa: BLE001 - diagnostics must never break the run
                launch_incident = None
            if launch_incident is not None:
                launch_incident_detected = True
                orch_dir = repo_root / "workspace" / "orchestrations" / orchestration_id
                snapshot_path = orch_dir / f"launch_incident.runtime.{uuid.uuid4().hex[:12]}.json"
                try:
                    if _atomic_write_json_exclusive(snapshot_path, launch_incident, tmp_dir=orch_dir):
                        launch_incident_ref = str(snapshot_path.relative_to(repo_root))
                except Exception:  # noqa: BLE001 - best-effort snapshot; never block the run
                    launch_incident_ref = None
                if workflow_status.lower() not in _RESUMABLE_TERMINAL_STATUSES:
                    child = launch_incident.get("dangling_child", {})
                    abort = launch_incident.get("abort_marker") or {}
                    detail = (
                        "child launch did not return (active_child window left open): "
                        f"child={child.get('agent_run_id')} "
                        f"step={child.get('step')}/{child.get('substep')} "
                        f"launch_recorded_at={child.get('launch_recorded_at')} "
                        f"elapsed={child.get('elapsed_seconds')}s "
                        f"last_activity={abort.get('last_activity_ts')} "
                        f"dead_air={abort.get('dead_air_seconds')}s "
                        f"abort={abort.get('interrupt_text')}"
                    )
                    # Surface a transient API error (e.g. 529 Overloaded) so the
                    # operator can tell at a glance this dangling launch was a
                    # transport blip — safe to `--resume` without investigation —
                    # rather than a genuine hang.
                    api_error = abort.get("api_error") if isinstance(abort, dict) else None
                    if isinstance(api_error, dict) and api_error.get("status") is not None:
                        retry_hint = " retryable, safe to --resume" if api_error.get("retryable") else ""
                        detail += (
                            f" api_error={api_error.get('status')}"
                            f" {str(api_error.get('message') or '').strip()[:120]}{retry_hint}"
                        )
                    if launch_incident_ref:
                        detail += f" incident_ref={launch_incident_ref}"
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
                                "launch_incomplete_active_child",
                                "--reason-detail",
                                detail,
                                "--blocking-policy-scope",
                                "launch",
                            ],
                        )
                        workflow_status = "fail"
                    except RuntimeError:
                        # set-status failed: leave workflow_status as-is. The failure
                        # path below is still entered via launch_incident_detected (NOT
                        # via workflow_status, which is still non-terminal here), and its
                        # own set-status retry re-attempts terminalization as a fallback.
                        pass
            # The orchestration agent records meta.status="pass" via the gated set-status
            # only after aggregate_verdict=pass and the pre_judge gate. That recorded
            # terminal success is authoritative over a transport-induced nonzero CLI
            # returncode or a superseded/recovered nonpass agent_run, so it short-circuits
            # the failure-reporting path below (audit: orch_20260615T095217Z_74450292
            # reported workflow_failed for a fully-passing run).
            meta_status_is_pass = workflow_status.strip().lower() == "pass"
            # A dev-mode major/critical verify issue is a fail-closed contract violation
            # (docs/workflow/WORKFLOW_CORE.md, startup_contract.md, SKILL.md: dev mode
            # must treat major/critical verify severities as fail). It overrides even a
            # recorded meta.status=pass — the backstop for an orchestration agent that
            # wrongly records pass despite a severe verify issue. The meta=pass
            # short-circuit below is scoped to the CLI returncode only, never to this.
            severe_verify_fail = False
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
                    severe_verify_fail = True
            # meta.status=pass is authoritative ONLY over a transport-induced nonzero CLI
            # returncode / a superseded-and-recovered nonpass agent_run — NOT over a
            # severe_verify_fail, which fails closed regardless.
            if severe_verify_fail or (
                not meta_status_is_pass
                and (
                    proc.returncode != 0
                    or launch_incident_detected
                    or workflow_status.lower() in {
                        "fail",
                        "fail_closed",
                        "blocked",
                        "timeout",
                        "cancel",
                    }
                )
            ):
                # When the launched LLM process exited without the orchestration
                # agent recording a terminal status (e.g. a token/session-limit
                # kill mid-run), the orchestration meta is still non-terminal
                # ("running"). run_workflow launched the child synchronously and
                # it has now returned, so the child is provably dead — terminalize
                # the orchestration ourselves so an implicit `--resume` (which
                # refuses a non-terminal latest, see _RESUMABLE_TERMINAL_STATUSES)
                # can recover it. Best-effort: failure reporting continues even if
                # set-status raises. Runs before failure-analysis collection so the
                # reason is reflected in meta.reason_code/reason_detail.
                if workflow_status.lower() not in _RESUMABLE_TERMINAL_STATUSES:
                    # Preserve the specific dangling-launch signal when this fallback
                    # is reached because the dedicated launch_incomplete_active_child
                    # set-status above raised — otherwise resume diagnostics would
                    # degrade to the generic returncode reason.
                    if launch_incident_detected:
                        fallback_reason_code = "launch_incomplete_active_child"
                        fallback_reason_detail = (
                            "child launch did not return (active_child window left open); "
                            "dedicated terminalization failed, recovered via launch fallback "
                            f"(returncode={proc.returncode}, status '{workflow_status}')"
                        )
                    else:
                        fallback_reason_code = "llm_launch_interrupted"
                        fallback_reason_detail = (
                            "LLM launch process exited (returncode="
                            f"{proc.returncode}) without the orchestration agent "
                            "recording a terminal status; orchestration left non-terminal "
                            f"'{workflow_status}'"
                        )
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
                                fallback_reason_code,
                                "--reason-detail",
                                fallback_reason_detail,
                                "--blocking-policy-scope",
                                "launch",
                            ],
                        )
                        workflow_status = "fail"
                    except RuntimeError:
                        # set-status failed — proceed with failure reporting using
                        # the observed non-terminal status; resume may still need an
                        # explicit --orchestration-id in this degraded case.
                        pass
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
                    if extra_output:
                        fail_output.update(extra_output)
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
            elif proc.returncode != 0:
                # meta.status=pass but the launched CLI exited nonzero (e.g. a transport
                # hiccup the orchestration already recovered from). Treat the recorded
                # pass as authoritative; surface the returncode as an advisory only.
                cli_returncode_warning = proc.returncode

        ok_output: dict[str, Any] = {
            "status": "ok",
            "orchestration_id": orchestration_id,
            "resumed": resume_mode,
            "llm": llm,
            "llm_command": llm_command,
            "target_spec_ref": spec_ref,
            "until_phase": until_phase,
            "workflow_mode": workflow_mode,
            "metdsl_workflow_mode": env["METDSL_WORKFLOW_MODE"],
            "metdsl_workflow_exec_mode": env["METDSL_WORKFLOW_EXEC_MODE"],
            "workflow_status": workflow_status,
            "prompt_ref": str(prompt_path.relative_to(repo_root)),
            "llm_invoked": launched,
        }
        if cli_returncode_warning is not None:
            ok_output["cli_returncode_warning"] = cli_returncode_warning
        if extra_output:
            ok_output.update(extra_output)
        print(json.dumps(ok_output, ensure_ascii=False))
        return 0
    finally:
        if orchestration_tmp_for_cleanup is not None and orchestration_tmp_for_cleanup.exists():
            shutil.rmtree(orchestration_tmp_for_cleanup, ignore_errors=True)


def _dependency_node_ready(
    repo_root: Path, node: dict[str, Any], required_stages: list[str]
) -> bool:
    """True iff the dependency node already satisfies `required_stages`.

    Mirrors the runtime readiness contract (`_verify_dependency_readiness`): a
    node is ready when ANY single matching catalog version has a coherent
    artifact chain across all required stages (the same version V must satisfy
    every stage). Kept module-level so the closure driver uses one consistent
    readiness rule for both the pre-run skip check and the post-run
    verification."""
    from tools.orchestration_runtime import _verify_dep_stage

    kind, sid = node["spec_kind"], node["spec_id"]
    return any(
        all(_verify_dep_stage(repo_root, kind, sid, v, st) for st in required_stages)
        for v in node["spec_versions"]
    )


def _resolve_dependency_closure(
    repo_root: Path, target_spec_ref: str
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Resolve the target's transitive dependency closure in topological order.

    Returns `(ordered, error)`:
      - `ordered`: dependency nodes in dependency order (dependencies before
        dependents), EXCLUDING the target. Each is
        `{spec_ref, spec_kind, spec_id, spec_versions}`. `spec_versions` is the
        descending list of catalog versions satisfying the requiring edge's
        constraint (intersected across edges when a node is required more than
        once). The readiness check mirrors the runtime contract
        (`_verify_dependency_readiness`): a node is ready when ANY one of these
        versions has a coherent artifact chain — so we keep all of them, not
        just the highest, to avoid re-running a dependency that an older
        matching version already satisfies.
      - `error`: None on success; else `{reason, detail}` for a cycle,
        unresolvable dependency, version conflict, malformed/missing deps.yaml,
        or catalog corruption (all fail-closed — no node is run).

    Edges come from `<spec_ref>/deps.yaml` resolved against `spec_catalog.yaml`
    via the canonical runtime helpers (`_parse_dep_entries`,
    `_matching_dep_versions`, `resolve_spec_ref_for`). Post-order DFS yields the
    topological order; a node already on the DFS stack is a cycle.
    """
    from tools.orchestration_runtime import (
        SpecCatalogCorruption,
        _load_spec_catalog,
        _matching_dep_versions,
        _parse_dep_entries,
        _read_deps_yaml,
        resolve_spec_ref_for,
    )

    # The catalog is loaded lazily — only once a dependency edge is actually
    # encountered. A leaf target (empty deps.yaml) needs no catalog, so a
    # missing/corrupt registry must not turn an otherwise-launchable leaf
    # workflow into a failure (matching the runtime readiness path, which
    # treats no-deps specs as vacuously ready without the catalog).
    catalog_cache: dict[tuple[str, str], tuple[str, ...]] | None = None

    def _get_catalog() -> dict[tuple[str, str], tuple[str, ...]]:
        nonlocal catalog_cache
        if catalog_cache is None:
            catalog_cache = _load_spec_catalog(str(repo_root.resolve()))
        return catalog_cache

    ordered_refs: list[str] = []
    # Per spec_ref: the (kind, sid) identity, and the set of catalog versions
    # satisfying every edge that required it (intersection across edges).
    kindid_by_ref: dict[str, tuple[str, str]] = {}
    matched_by_ref: dict[str, tuple[str, ...]] = {}
    visiting: set[str] = set()
    done: set[str] = set()
    error: dict[str, str] | None = None

    def visit(spec_ref: str) -> None:
        nonlocal error
        if error is not None or spec_ref in done:
            return
        if spec_ref in visiting:
            error = {
                "reason": "dependency_cycle",
                "detail": f"dependency cycle detected at {spec_ref}",
            }
            return
        visiting.add(spec_ref)
        deps_doc = _read_deps_yaml(repo_root, spec_ref)
        if not isinstance(deps_doc, dict):
            error = {
                "reason": "dependency_deps_unreadable",
                "detail": f"{spec_ref}/deps.yaml is missing or unparseable",
            }
            return
        entries, well_formed = _parse_dep_entries(deps_doc)
        if not well_formed:
            error = {
                "reason": "dependency_deps_malformed",
                "detail": f"{spec_ref}/deps.yaml has a malformed dependency schema",
            }
            return
        for kind, sid, constraint in entries:
            try:
                matched = _matching_dep_versions(_get_catalog(), kind, sid, constraint)
            except SpecCatalogCorruption as exc:
                error = {"reason": "spec_catalog_corrupt", "detail": str(exc)}
                return
            if not matched:
                error = {
                    "reason": "dependency_unresolvable",
                    "detail": (
                        f"{kind}/{sid} constraint {constraint!r} has no matching "
                        "catalog version"
                    ),
                }
                return
            dep_spec_ref = resolve_spec_ref_for(repo_root, kind, sid)
            if not dep_spec_ref:
                error = {
                    "reason": "dependency_spec_ref_unresolved",
                    "detail": f"no unique spec directory in catalog for {kind}/{sid}",
                }
                return
            prior_kindid = kindid_by_ref.get(dep_spec_ref)
            if prior_kindid is not None and prior_kindid != (kind, sid):
                error = {
                    "reason": "dependency_identity_conflict",
                    "detail": (
                        f"{dep_spec_ref} required as both {prior_kindid} and "
                        f"{(kind, sid)}"
                    ),
                }
                return
            kindid_by_ref[dep_spec_ref] = (kind, sid)
            # Intersect the matching-version sets across edges. An empty
            # intersection means two edges pin incompatible version ranges for
            # the same node — a genuine conflict, fail-closed.
            prior_versions = matched_by_ref.get(dep_spec_ref)
            if prior_versions is None:
                matched_by_ref[dep_spec_ref] = tuple(matched)
            else:
                matched_set = set(matched)
                intersection = tuple(v for v in prior_versions if v in matched_set)
                if not intersection:
                    error = {
                        "reason": "dependency_version_conflict",
                        "detail": (
                            f"{dep_spec_ref} ({kind}/{sid}) required with "
                            f"incompatible constraints: {prior_versions} vs {tuple(matched)}"
                        ),
                    }
                    return
                matched_by_ref[dep_spec_ref] = intersection
            visit(dep_spec_ref)
            if error is not None:
                return
        visiting.discard(spec_ref)
        done.add(spec_ref)
        ordered_refs.append(spec_ref)

    visit(target_spec_ref)
    if error is not None:
        return [], error
    ordered: list[dict[str, Any]] = []
    for ref in ordered_refs:
        if ref == target_spec_ref:
            continue
        kind, sid = kindid_by_ref[ref]
        ordered.append(
            {
                "spec_ref": ref,
                "spec_kind": kind,
                "spec_id": sid,
                "spec_versions": list(matched_by_ref[ref]),
            }
        )
    return ordered, None


def _run_with_dependency_closure(
    *,
    repo_root: Path,
    base_env: dict[str, str],
    target_orchestration_id: str,
    target_spec_ref: str,
    target_source_dependency_ref: str,
    until_phase: str,
    llm: str,
    llm_command: str,
    workflow_mode: str,
    agent_model: str | None,
    status: str,
    invoke_llm: bool,
) -> int:
    """Run the target's dependency closure bottom-up, then the target.

    Each dependency node runs as its own fresh orchestration (one per node).
    Nodes already satisfying the required readiness are skipped. On the first
    dependency failure the run stops (the target is not launched). The target's
    final JSON result carries a `dependency_runs` summary.
    """
    ordered, error = _resolve_dependency_closure(repo_root, target_spec_ref)
    if error is not None:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "reason": "dependency_closure_unresolved",
                    "detail": error.get("detail"),
                    "reason_code": error.get("reason"),
                    "target_spec_ref": target_spec_ref,
                },
                ensure_ascii=False,
            )
        )
        return 2

    # Dependency depth follows the target: Compile-only readiness when the
    # target stops at Compile, else full execution readiness (Build+Validate).
    dep_until_phase = "Compile" if until_phase == "Compile" else "Validate"
    required_stages = (
        ["ir_ref"]
        if dep_until_phase == "Compile"
        else ["ir_ref", "pipeline_ref", "aggregate_verdict"]
    )

    dependency_runs: list[dict[str, Any]] = []
    for node in ordered:
        kind, sid, spec_ref = node["spec_kind"], node["spec_id"], node["spec_ref"]
        node_label = f"{kind}/{sid}@{node['spec_versions'][0]}"
        if _dependency_node_ready(repo_root, node, required_stages):
            dependency_runs.append(
                {"node": node_label, "spec_ref": spec_ref, "skipped": True, "status": "ready"}
            )
            continue

        dep_orch_id = _new_orchestration_id()
        try:
            dep_source_dependency_ref = _discover_source_dependency_ref(repo_root, spec_ref)
        except ValueError as exc:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "dependency_dep_ref_unresolved",
                        "detail": str(exc),
                        "failed_dependency_node": node_label,
                        "spec_ref": spec_ref,
                        "dependency_runs": dependency_runs,
                        "target_spec_ref": target_spec_ref,
                    },
                    ensure_ascii=False,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "info",
                    "event": "dependency_node_start",
                    "node": node_label,
                    "spec_ref": spec_ref,
                    "until_phase": dep_until_phase,
                    "orchestration_id": dep_orch_id,
                },
                ensure_ascii=False,
            )
        )
        rc = _run_node(
            repo_root=repo_root,
            base_env=base_env,
            orchestration_id=dep_orch_id,
            spec_ref=spec_ref,
            source_dependency_ref=dep_source_dependency_ref,
            until_phase=dep_until_phase,
            llm=llm,
            llm_command=llm_command,
            workflow_mode=workflow_mode,
            agent_model=agent_model,
            status=status,
            invoke_llm=invoke_llm,
            resume_mode=False,
        )
        dependency_runs.append(
            {
                "node": node_label,
                "spec_ref": spec_ref,
                "skipped": False,
                "orchestration_id": dep_orch_id,
                "exit_code": rc,
            }
        )
        if rc != 0:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "dependency_node_failed",
                        "failed_dependency_node": node_label,
                        "spec_ref": spec_ref,
                        "orchestration_id": dep_orch_id,
                        "exit_code": rc,
                        "dependency_runs": dependency_runs,
                        "target_spec_ref": target_spec_ref,
                    },
                    ensure_ascii=False,
                )
            )
            return rc

        # A zero exit code does not by itself prove the dependency reached the
        # required readiness: `--no-invoke-llm` only prepares artifacts, and a
        # launched agent can exit cleanly with the orchestration still
        # non-terminal ("running") without producing the ir/pipeline/verdict
        # evidence. Re-verify before launching the dependent/target node;
        # otherwise the next node would just fail-close at workflow-launch-check.
        if not _dependency_node_ready(repo_root, node, required_stages):
            dependency_runs[-1]["status"] = "not_ready_after_run"
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "reason": "dependency_not_ready_after_run",
                        "detail": (
                            f"{node_label} ran (exit 0) but did not produce the "
                            f"required readiness ({'/'.join(required_stages)}); "
                            "common causes: --no-invoke-llm, or the agent exited "
                            "without recording a terminal pass (status still running)."
                        ),
                        "failed_dependency_node": node_label,
                        "spec_ref": spec_ref,
                        "orchestration_id": dep_orch_id,
                        "dependency_runs": dependency_runs,
                        "target_spec_ref": target_spec_ref,
                    },
                    ensure_ascii=False,
                )
            )
            return 2

    # All dependencies are ready — run the target node, carrying the summary.
    return _run_node(
        repo_root=repo_root,
        base_env=base_env,
        orchestration_id=target_orchestration_id,
        spec_ref=target_spec_ref,
        source_dependency_ref=target_source_dependency_ref,
        until_phase=until_phase,
        llm=llm,
        llm_command=llm_command,
        workflow_mode=workflow_mode,
        agent_model=agent_model,
        status=status,
        invoke_llm=invoke_llm,
        resume_mode=False,
        extra_output={"dependency_runs": dependency_runs},
    )


if __name__ == "__main__":
    raise SystemExit(main())
