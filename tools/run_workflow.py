#!/usr/bin/env python3
"""Bootstrap workflow orchestration startup for a target spec."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
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
# AGENTS.md), `sys.path[0]` is `tools/`, not the repo root, so absolute
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


# Orchestration is conductor-only (the deterministic Python phase loop in
# tools/workflow_conductor.py). The conductor has leaf launchers for claude and
# codex; the former LLM-orchestrator driver and the cursor backend (which only ran
# under that driver) were removed.
SUPPORTED_LLMS = ("codex", "claude")
SUPPORTED_WORKFLOW_MODES = ("dev", "prod")
# Applied when --llm / --mode are omitted on a non-resume run, so plain
# `run_workflow.py <spec> <phase>` uses the claude backend by default.
DEFAULT_LLM = "claude"
DEFAULT_WORKFLOW_MODE = "dev"
DEFAULT_LLM_COMMANDS = {
    "codex": "codex",
    "claude": "claude",
}
# Default orchestration-agent model recorded on the orchestration agent_runs row
# for the claude backend, as an UNPINNED alias (e.g. "opus") read from the
# operator's settings — never a pinned version, which would go stale as versions
# update. Operators on a different model override it with --agent-model. The codex
# model id is not knowable to this entrypoint, so it is left to repair-agent-runs
# sibling backfill. The exact version each leaf actually ran is resolved post-run
# from its transcript by the conductor (resolve_claude_model_from_transcript).
def _default_claude_agent_model() -> str:
    from tools.orchestration_runtime import resolve_claude_model_alias
    return resolve_claude_model_alias()

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


def _build_invocation_record(
    *,
    argv: list[str] | None,
    spec_ref: str,
    until_phase: str,
    llm: str,
    llm_command: str,
    workflow_mode: str,
    agent_model: str | None,
    with_deps: bool,
    closure_id: str | None = None,
    closure_target_spec_ref: str | None = None,
    closure_until_phase: str | None = None,
) -> dict[str, Any]:
    """Assemble the reproduction/provenance record persisted to
    `orchestration_meta.json#invocation`.

    Records BOTH the raw argv (as invoked) and the resolved/canonical params: spec
    paths are canonicalized by `_canonicalize_spec_ref`, so the raw argv alone is not
    enough to reproduce the run. The `closure_*` fields are present only for nodes of
    a `--with-deps` closure; closure-aware resume reads `closure_id` /
    `closure_target_spec_ref` / `closure_until_phase` from here to detect closure
    membership and re-derive the closure (`_index_closure_orchestrations`)."""
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    record: dict[str, Any] = {
        "argv": raw_argv,
        "command": shlex.join(["python3", "tools/run_workflow.py", *raw_argv]),
        "spec_ref": spec_ref,
        "until_phase": until_phase,
        "llm": llm,
        "llm_command": llm_command,
        "mode": workflow_mode,
        "with_deps": bool(with_deps),
    }
    if agent_model:
        record["agent_model"] = agent_model
    if closure_id:
        record["closure_id"] = closure_id
        record["closure_target_spec_ref"] = closure_target_spec_ref or ""
        record["closure_until_phase"] = closure_until_phase or ""
    return record


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




def _build_orchestration_prompt(
    *,
    orchestration_id: str,
    orchestration_agent_run_id: str,
    spec_ref: str,
    source_dependency_ref: str,
    until_phase: str,
    workflow_mode: str,
) -> str:
    """Render the orchestration start record written to
    `launches/orchestration.start.prompt.txt`.

    Orchestration is conductor-driven (Python, no parent orchestration LLM), so
    this is no longer an LLM prompt — it is the canonical carrier of the run's
    startup parameters. `--resume` recovers `spec_ref` / `until_phase` /
    `workflow_mode` from this file via `_extract_prompt_params`, so the
    `target_spec_ref:` / `end phase:` / `workflow_mode:` markers are load-bearing
    and pinned by a round-trip unit test. Keep them when editing the wording.
    """
    phase_list = ", ".join(PHASE_ORDER[: PHASE_ORDER.index(until_phase) + 1])
    return textwrap.dedent(
        f"""
        Conductor workflow start record (driver: conductor).

        ## startup context
        - orchestration_id: `{orchestration_id}`
        - orchestration_agent_run_id: `{orchestration_agent_run_id}`
        - workflow_mode: `{workflow_mode}`
        - target_spec_ref: `{spec_ref}`
        - dependency_ref: `{source_dependency_ref}`
        - target_phases: `{phase_list}` (end phase: `{until_phase}`)
        """
    ).strip() + "\n"


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


def _index_closure_orchestrations(repo_root: Path, closure_id: str) -> dict[str, str]:
    """Map `spec_ref -> orchestration_id` for every orchestration recorded as part of
    the given closure (`orchestration_meta.json#invocation.closure_id == closure_id`).

    Keeps the latest per `spec_ref` by `started_at` (id as a deterministic tie-break),
    mirroring `_find_latest_orchestration`'s ranking, so a dependency that was run more
    than once under one closure resolves to its most recent orchestration. Used by
    closure-aware resume to find each not-ready node's prior orchestration so it can be
    resumed (warm, from its checkpoint) rather than re-run cold."""
    orch_root = repo_root / "workspace" / "orchestrations"
    if not orch_root.is_dir():
        return {}
    # spec_ref -> (started_at_key, orch_id) best seen so far
    best: dict[str, tuple[str, str]] = {}
    for path in orch_root.iterdir():
        if not path.is_dir():
            continue
        meta = _read_json_if_exists(path / "orchestration_meta.json")
        if not isinstance(meta, dict):
            continue
        invocation = meta.get("invocation")
        if not isinstance(invocation, dict) or invocation.get("closure_id") != closure_id:
            continue
        spec_ref = meta.get("spec_ref")
        if not isinstance(spec_ref, str) or not spec_ref.strip():
            continue
        spec_ref = spec_ref.strip()
        started_at = meta.get("started_at")
        started_key = started_at.strip() if isinstance(started_at, str) else ""
        candidate = (started_key, path.name)
        prior = best.get(spec_ref)
        if prior is None or candidate > prior:
            best[spec_ref] = candidate
    return {spec_ref: value[1] for spec_ref, value in best.items()}


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
    - closure_id / closure_target_spec_ref / closure_until_phase
                                       ← orchestration_meta.json#invocation
    Missing/unparseable values are returned as None for the caller to validate. The
    `closure_*` keys are set only when the run was a `--with-deps` node (older
    orchestrations lack the `invocation` block → None → single-node resume).
    """
    orch_root = repo_root / "workspace" / "orchestrations" / orchestration_id
    meta = _read_json_if_exists(orch_root / "orchestration_meta.json") or {}
    preflight = _read_json_if_exists(orch_root / "preflight.json") or {}
    prompt_path = orch_root / "launches" / "orchestration.start.prompt.txt"
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    prompt_params = _extract_prompt_params(prompt_text)
    invocation = meta.get("invocation")
    invocation = invocation if isinstance(invocation, dict) else {}

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
        "closure_id": _clean(invocation.get("closure_id")),
        "closure_target_spec_ref": _clean(invocation.get("closure_target_spec_ref")),
        "closure_until_phase": _clean(invocation.get("closure_until_phase")),
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
            "orchestration when omitted. When the resumed orchestration is a node of a "
            "--with-deps closure (recorded in orchestration_meta.json#invocation), the "
            "whole closure is re-derived and continued to the target — not just one node."
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
            "Model id (or unpinned alias) of the orchestration agent itself, recorded "
            "on its agent_runs row for cost attribution / reproducibility. Defaults to "
            "the operator's configured claude alias (e.g. 'opus') only for the claude "
            "backend running the unmodified default command; with a custom --llm-command "
            "(which may launch a different model) it is omitted unless given here. When "
            "omitted, repair-agent-runs backfills it from sibling rows on resume. Prefer "
            "an unpinned alias over a pinned version so it does not go stale."
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
            "dependencies are skipped. On --resume the closure is re-derived and "
            "continued automatically from the recorded invocation (no need to re-pass "
            "--with-deps)."
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
        help="Prepare orchestration artifacts only; do not run the conductor.",
    )
    parser.add_argument(
        "--stdout-format",
        choices=("human", "jsonl"),
        default="human",
        help=(
            "Stdout output format for the orchestration event stream. 'human' "
            "(default) renders the node/phase/substep events as compact human-"
            "readable lines so an operator can follow progress at a glance. "
            "'jsonl' emits the raw structured JSON payload of every event "
            "(suitable for piping into a parser). Regardless of this flag, the "
            "run_logs/ jsonl file under the orchestration directory always "
            "receives the full raw JSON payload of every event."
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
    # Raw command line as invoked, for the reproduction record persisted to
    # orchestration_meta.json#invocation. Captured before any normalization so it
    # reflects exactly what the operator typed.
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
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
    # prompt). Without --resume the defaults (claude / dev) apply.
    resume_mode = bool(args.resume)
    # Recovered resume metadata (populated in the resume branch). The reuse decision
    # in the try block compares the effective spec/backend against these to tell an
    # actual change from an explicit no-op restate.
    resume_recovered_spec_ref: str | None = None
    resume_recovered_dep_ref: str | None = None
    resume_recovered_llm: str | None = None
    resume_recovered_llm_command: str | None = None
    # Closure-aware resume state (populated in the resume branch when the resumed
    # orchestration is a node of a `--with-deps` closure).
    resume_is_closure = False
    resume_closure_id: str | None = None
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
        spec_ref_arg = args.spec_ref
        until_phase_arg = args.until_phase
        # A lone positional is ambiguous on resume: argparse binds it to spec_ref,
        # but overriding until_phase (e.g. extending the run further) is the common
        # case while overriding spec_ref is not. If only spec_ref was given and it
        # names a known phase, treat it as the until_phase override instead.
        if spec_ref_arg and not until_phase_arg and spec_ref_arg.strip().lower() in PHASE_ALIASES:
            until_phase_arg = spec_ref_arg
            spec_ref_arg = None
        # Closure-aware resume: if the resumed orchestration is a node of a
        # `--with-deps` closure (recorded in orchestration_meta.json#invocation), the
        # whole closure is re-walked and driven to the TARGET spec — not just this one
        # node. Retarget spec_ref/until_phase to the closure target so the shared
        # startup validation below canonicalizes the target and discovers the target's
        # dependency ref. An explicit spec override (a non-phase positional) is the
        # escape hatch back to single-node resume of that spec.
        closure_id_recovered = recovered.get("closure_id")
        closure_target_recovered = recovered.get("closure_target_spec_ref")
        closure_until_recovered = recovered.get("closure_until_phase")
        # The closure end-phase lives authoritatively on the TARGET orchestration: its
        # start-prompt end-phase is rewritten by _run_node on every run/resume, so a
        # prior phase override survives there, whereas a DEPENDENCY node's copied
        # closure_until_phase goes stale. When we entered via a dependency (entry id !=
        # closure/target id) AND the target orchestration exists and belongs to this
        # closure (its own invocation.closure_id matches — guarding a reused
        # --orchestration-id that names an unrelated run), prefer the target's
        # recovered until_phase. When we entered via the target itself, its own
        # recovered value is already freshest (and must not override the partial-block
        # guard below).
        if closure_id_recovered and closure_id_recovered != orchestration_id:
            target_recovered = _load_resume_params(repo_root, closure_id_recovered)
            if (
                target_recovered.get("closure_id") == closure_id_recovered
                and target_recovered.get("until_phase")
            ):
                closure_until_recovered = target_recovered.get("until_phase")
        force_single_node = bool(spec_ref_arg)
        # All three closure fields are co-written by _build_invocation_record, so
        # require all three: if any is missing (corrupt/partial block), fall back to
        # single-node resume rather than driving the closure with a wrong until_phase
        # (the recovered dep until_phase, e.g. Compile, is NOT the target's).
        if (
            closure_id_recovered
            and closure_target_recovered
            and closure_until_recovered
            and not force_single_node
        ):
            resume_is_closure = True
            resume_closure_id = closure_id_recovered
            spec_ref_in = closure_target_recovered
            until_phase_in = until_phase_arg or closure_until_recovered
        else:
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

    try:
        workflow_mode = _normalize_workflow_mode(mode_in)
        if not until_phase_in:
            raise ValueError(
                "until_phase is required unless --resume is set; "
                f"choose one of: {', '.join(PHASE_ORDER)}"
            )
        until_phase = _normalize_phase(until_phase_in)
        llm = llm_in
        # The conductor only has a leaf launcher for claude/codex; reject an
        # unsupported backend up front instead of failing at the first substep
        # after init/preflight already created the orchestration.
        if llm not in ("claude", "codex"):
            raise ValueError(
                f"conductor orchestration supports --llm claude|codex, not {llm!r}"
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
    # Warm-resume minor-fix repairs are ALWAYS active (claude only; no env gate): a
    # generate.lint / generate.static / compile.static finding (and the build->generate reuse
    # repairs) re-run the phase's producer substep (generate.generate / compile.generate) by
    # resuming the prior leaf's session with context intact, instead of a cold restart —
    # avoiding the cold-start re-read cost. `restart` repairs stay cold (anchoring avoidance).
    # The conductor falls back to a cold launch if the producer session transcript is gone.
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

    # Closure-aware resume: the resumed orchestration is a node of a `--with-deps`
    # closure, so re-derive the closure and drive it to the TARGET (spec_ref is the
    # closure target here). Prior node orchestrations are resumed; not-yet-run nodes
    # run fresh; already-ready nodes are skipped.
    if resume_mode and resume_is_closure and resume_closure_id:
        prior_map = _index_closure_orchestrations(repo_root, resume_closure_id)
        return _run_with_dependency_closure(
            repo_root=repo_root,
            base_env=base_env,
            target_orchestration_id=resume_closure_id,
            target_spec_ref=spec_ref,
            target_source_dependency_ref=source_dependency_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            workflow_mode=workflow_mode,
            agent_model=args.agent_model,
            status=args.status,
            invoke_llm=args.invoke_llm,
            stdout_format=args.stdout_format,
            resume=True,
            prior_orch_by_spec=prior_map,
            raw_argv=raw_argv,
        )

    # `--with-deps` runs the target's transitive dependency closure bottom-up
    # (one orchestration per node) before the target. Scoped to fresh runs: a
    # `--resume` of a `--with-deps` run is handled by the closure-aware branch above.
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
            stdout_format=args.stdout_format,
            resume=False,
            prior_orch_by_spec=None,
            raw_argv=raw_argv,
        )

    # Plain single node. A cold run records the reproduction block (no closure); a
    # single-node resume passes None (the runtime preserves the existing block).
    single_node_invocation = (
        None
        if resume_mode
        else _build_invocation_record(
            argv=raw_argv,
            spec_ref=spec_ref,
            until_phase=until_phase,
            llm=llm,
            llm_command=llm_command,
            workflow_mode=workflow_mode,
            agent_model=args.agent_model,
            with_deps=False,
        )
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
        invocation=single_node_invocation,
        stdout_format=args.stdout_format,
    )


def _format_event_human(payload: dict[str, Any]) -> str | None:
    """Render a structured event payload as a compact human-readable line.

    The event vocabulary is small and stable: the node/dependency announcements
    written here by run_workflow.py, the conductor's `phase_start` /
    `phase_complete` / `substep_start` / `substep_complete` / warn emits, and
    the run's final `status: ok` / `status: fail` summary. An unknown payload
    shape returns None so the caller can fall back to the raw JSON — the
    human-mode formatter is best-effort presentation and must not swallow
    information it cannot classify.

    Indentation conveys nesting: node = column 0, phase = 2 spaces, substep /
    warn = 4 spaces. Pass results are tagged `ok`; non-pass results carry the
    raw verdict text (`fail`, `fail_closed`, `blocked`, ...) so the operator
    sees the actual classification rather than a uniform red flag.
    """
    status = payload.get("status")
    event = payload.get("event")

    if status == "info" and event == "node_start":
        spec = payload.get("spec_ref", "?")
        until = payload.get("until_phase", "?")
        orch = payload.get("orchestration_id", "?")
        flag = " [resume]" if payload.get("resume") else ""
        return f"[node] spec={spec} until={until} orch={orch}{flag}"

    if status == "info" and event == "dependency_node_begin":
        node = payload.get("node", "?")
        spec = payload.get("spec_ref", "?")
        until = payload.get("until_phase", "?")
        orch = payload.get("orchestration_id", "?")
        return f"[dep ] node={node} spec={spec} until={until} orch={orch}"

    if status == "info" and event == "phase_start":
        phase = payload.get("phase", "?")
        attempt = payload.get("attempt", 1)
        return f"  [phase   ] {phase} (attempt {attempt})"

    if status == "info" and event == "phase_complete":
        phase = payload.get("phase", "?")
        result = payload.get("result", "?")
        if result == "skipped":
            return f"  [phase   ] {phase} skipped (resumed)"
        marker = "ok" if result == "pass" else result
        elapsed = payload.get("elapsed_seconds")
        suffix = f" ({elapsed}s)" if elapsed is not None else ""
        return f"  [phase   ] {phase} {marker}{suffix}"

    if status == "info" and event == "substep_start":
        phase = payload.get("phase", "?")
        substep = payload.get("substep") or "step"
        return f"    [substep] {phase}.{substep} ..."

    if status == "info" and event == "substep_complete":
        phase = payload.get("phase", "?")
        substep = payload.get("substep") or "step"
        result = payload.get("result", "?")
        marker = "ok" if result == "pass" else result
        elapsed = payload.get("elapsed_seconds")
        suffix = f" ({elapsed}s)" if elapsed is not None else ""
        arid = payload.get("agent_run_id")
        arid_suffix = f" arid={arid}" if arid and result != "pass" else ""
        return f"    [substep] {phase}.{substep} {marker}{suffix}{arid_suffix}"

    if status == "info" and event == "resume_session_unavailable":
        phase = payload.get("phase", "?")
        substep = payload.get("substep") or "?"
        target = payload.get("target", "?")
        return f"    [warn   ] resume session unavailable: {phase}.{substep} target={target}"

    if status == "info" and event == "leaf_transient_retry":
        phase = payload.get("step", "?")
        substep = payload.get("substep") or "step"
        tag = payload.get("tag", "?")
        attempt = payload.get("attempt", "?")
        total = payload.get("max_attempts", "?")
        backoff = payload.get("backoff_seconds", "?")
        return (f"    [warn   ] transient leaf failure ({tag}) in {phase}.{substep} "
                f"[attempt {attempt}/{total}]: retrying in {backoff}s")

    if status == "info" and event == "diagnose_launch_failed":
        phase = payload.get("phase", "?")
        err = payload.get("error", "")
        return f"    [warn   ] diagnose launch failed in {phase}: {err}"

    if status == "ok":
        orch = payload.get("orchestration_id", "?")
        ws = payload.get("workflow_status") or "ok"
        invoked = payload.get("llm_invoked")
        suffix = "" if invoked is None else ("" if invoked else " (no-launch)")
        deps = payload.get("dependency_runs")
        dep_suffix = f" deps={len(deps)}" if isinstance(deps, list) and deps else ""
        return f"[ok  ] orch={orch} workflow_status={ws}{suffix}{dep_suffix}"

    if status == "fail":
        orch = payload.get("orchestration_id")
        reason = payload.get("reason", "?")
        detail = payload.get("detail")
        parts = [f"reason={reason}"]
        if orch:
            parts.append(f"orch={orch}")
        if detail:
            d = str(detail).replace("\n", " ").strip()
            if len(d) > 240:
                d = d[:240] + "..."
            parts.append(f"detail={d}")
        return "[FAIL] " + " ".join(parts)

    return None


def _emit_closure_event(payload: dict[str, Any], stdout_format: str) -> None:
    """Print a dependency-closure-level event, honoring `--stdout-format`.

    The closure driver (`_run_with_dependency_closure`) emits its own events
    (`dependency_node_begin` and the various closure failure summaries) OUTSIDE
    any `_run_node` call, so no `_StdoutTee` is installed to translate them. In
    `human` mode this would leak raw JSON; route the payload through the same
    `_format_event_human` renderer the tee uses, falling back to the raw JSON
    line when the event shape is unknown so no information is dropped.
    """
    line = json.dumps(payload, ensure_ascii=False)
    if stdout_format == "human":
        human = _format_event_human(payload)
        if human is not None:
            line = human
    print(line, flush=True)


class _StdoutTee:
    """Mirror writes to a run-log file while optionally rendering JSON event
    lines to the real terminal in a compact human-readable form.

    Installed for the duration of a node run so the workflow event stream
    (``node_start``, the conductor's ``phase_start`` / ``phase_complete`` /
    ``substep_start`` / ``substep_complete`` emits, and the final ok/fail
    summary) is uniformly persisted to the workspace and is presented to the
    operator in the mode they asked for.

    The ``mode`` parameter governs the terminal stream:
    - ``"jsonl"`` (legacy default): every byte is passed through to the wrapped
      terminal stream unchanged, identical to the pre-format-aware tee.
    - ``"human"``: each completed stdout line is buffered, parsed as JSON, and
      — if it matches a known event shape — rendered as a compact human-
      readable line on the terminal. Lines that don't parse / don't match are
      forwarded verbatim so the operator never loses output.

    Run-log writes are mode-independent: the file ALWAYS receives the original
    raw bytes (which, for the workflow event stream, is the full structured
    JSON payload of every event). This means ``run_logs/run_*.jsonl`` is a
    full-fidelity record regardless of ``--stdout-format``.

    Writes to the log file are best-effort: a log-file IO error must never
    break the run or swallow terminal output, so file errors are silently
    ignored. Attribute access falls through to the wrapped stream so the
    object remains a drop-in ``sys.stdout`` (e.g. subprocesses derive
    ``fileno()`` from the parent fd via this fall-through).
    """

    def __init__(self, stream: Any, log_file: Any, mode: str = "jsonl") -> None:
        self._stream = stream
        self._log = log_file
        self._mode = mode if mode in ("human", "jsonl") else "jsonl"
        # Buffer of bytes received but not yet terminated by a newline; only
        # used in human mode (jsonl mode pipes straight through).
        self._buffer = ""

    def write(self, data: str) -> int:
        # The run-log mirrors the inbound bytes verbatim, before any human-mode
        # rewriting — so the workspace record stays canonical even when the
        # operator picked the compact terminal format.
        try:
            self._log.write(data)
        except Exception:  # noqa: BLE001 - never let log IO break the run
            pass
        if self._mode != "human":
            return self._stream.write(data)
        self._buffer += data
        while True:
            nl = self._buffer.find("\n")
            if nl == -1:
                break
            line = self._buffer[:nl]
            self._buffer = self._buffer[nl + 1:]
            self._stream.write(self._render_line(line) + "\n")
        return len(data)

    def _render_line(self, line: str) -> str:
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                human = _format_event_human(payload)
                if human is not None:
                    return human
        return line

    def flush(self) -> None:
        # In human mode a trailing partial line (no newline yet) is held in the
        # buffer; flush forwards it through the formatter so an operator sees
        # the tail promptly. The run-log already saw it on the inbound write().
        if self._mode == "human" and self._buffer:
            try:
                self._stream.write(self._render_line(self._buffer))
            except Exception:  # noqa: BLE001
                pass
            self._buffer = ""
        self._stream.flush()
        try:
            self._log.flush()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _open_run_log(repo_root: Path, orchestration_id: str) -> Any:
    """Open a fresh timestamped run-log file under the orchestration dir.

    The name is `run_<UTC timestamp>_<uuid8>.jsonl` so repeated runs against the
    same orchestration_id (notably `--resume`) never collide. The `run_logs/`
    prefix is exempt from the runtime write-snapshot
    (`_should_ignore_runtime_snapshot_path`), so this host-side write never
    contaminates a leaf's terminal write-diff. Returns the open file object, or
    None if it could not be created (logging is best-effort)."""
    try:
        run_logs_dir = (
            repo_root / "workspace" / "orchestrations" / orchestration_id / "run_logs"
        )
        run_logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = run_logs_dir / f"run_{stamp}_{uuid.uuid4().hex[:8]}.jsonl"
        return path.open("w", encoding="utf-8")
    except Exception:  # noqa: BLE001 - logging must never break the run
        return None


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
    invocation: dict[str, Any] | None = None,
    closure_until_phase: str | None = None,
    extra_output: dict[str, Any] | None = None,
    stdout_format: str = "jsonl",
) -> int:
    """Run a single node's orchestration (init → preflight → prompt → launch →
    terminalize) and print its JSON result. Returns the process exit code
    (0 = ok). Each call uses its own orchestration_id / TMPDIR so the
    dependency-closure driver can run one orchestration per node without
    cross-node env/tmp leakage. `extra_output`, when given, is merged into the
    final ok/fail JSON (used to carry the `dependency_runs` summary onto the
    target node's result). `invocation`, when given, is persisted immutably to
    `orchestration_meta.json#invocation` on the COLD init path only (the resume
    path preserves the existing block); it carries the reproduction record and the
    closure back-link that drives closure-aware resume."""
    env = dict(base_env)
    env["METDSL_ORCHESTRATION_ID"] = orchestration_id

    tmp_parent = repo_root / "workspace" / "tmp"
    tmp_parent.mkdir(parents=True, exist_ok=True)
    # TMPDIR must match output_manifest.allowed_tmp_root for the active agent (orchestration uses
    # workspace/tmp/<orchestration_agent_run_id>). Set only after init returns that id; cleanup only
    # that directory so concurrent workflows' workspace/tmp/<other_agent_run_id>/ are untouched.
    orchestration_tmp_for_cleanup: Path | None = None

    # Tee this node's stdout JSONL event stream to a timestamped run-log file
    # under the orchestration dir, so the same information (node_start, the
    # conductor's phase_start/phase_complete emits, final ok/fail summary) is
    # recoverable from the workspace afterwards, not only on the terminal.
    # `_open_run_log` is internally exception-safe (returns None on failure), and
    # the `saved_stdout` capture cannot raise, so both stay outside the try. The
    # stdout swap and the node_start print, however, go INSIDE the try: otherwise
    # a raising print (e.g. BrokenPipeError when terminal stdout is a closed pipe,
    # which the tee does not swallow for the real stream) would skip the finally,
    # leaking the log handle and leaving sys.stdout wrapped.
    run_log_file = _open_run_log(repo_root, orchestration_id)
    saved_stdout = sys.stdout

    try:
        if run_log_file is not None:
            sys.stdout = _StdoutTee(saved_stdout, run_log_file, mode=stdout_format)

        # Announce node start on stdout (uniform for the single/target/dependency
        # nodes), matching the JSONL info-event stream the rest of this driver emits.
        print(
            json.dumps(
                {
                    "status": "info",
                    "event": "node_start",
                    "spec_ref": spec_ref,
                    "until_phase": until_phase,
                    "orchestration_id": orchestration_id,
                    "resume": resume_mode,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

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
            # Refresh this node's persisted closure end-phase to the effective closure
            # until_phase, so an operator phase override survives on the dependency
            # nodes themselves (durable even if the target orchestration never starts).
            if closure_until_phase:
                init_args += ["--closure-until-phase", closure_until_phase]
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
            # Otherwise default to the operator's configured (unpinned) claude alias
            # ONLY for the claude backend running the UNMODIFIED default command — an
            # overridden --llm-command (e.g. a wrapper selecting a different model)
            # could launch a different model, so we must not assert the alias there;
            # leave it for sibling backfill on resume instead.
            orchestration_model = agent_model
            if (
                not orchestration_model
                and llm == "claude"
                and llm_command == DEFAULT_LLM_COMMANDS["claude"]
            ):
                orchestration_model = _default_claude_agent_model()
            if orchestration_model:
                init_args += ["--agent-model", orchestration_model]
            # Persist the reproduction/closure record on the cold init only. On the
            # resume branch above the runtime preserves the original block, so we must
            # not re-pass it there (that would be a no-op at best, and risks recording
            # a divergent block if the immutability guard were ever relaxed).
            if invocation:
                init_args += ["--invocation-json", json.dumps(invocation, ensure_ascii=False)]
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
        if invoke_llm:
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
                # If the conductor/runtime already terminalized with a specific terminal
                # status (e.g. record-launch set fail_closed/sandbox_enforcement_violation
                # when a bwrap profile could not be built), preserve it rather than
                # clobbering it with a generic conductor_error.
                meta_now = _read_json_if_exists(
                    repo_root / "workspace" / "orchestrations" / orchestration_id
                    / "orchestration_meta.json") or {}
                cur_status = str(meta_now.get("status") or "").strip().lower()
                if cur_status in {"fail_closed", "blocked", "timeout", "cancel"}:
                    print(json.dumps(
                        {"status": cur_status,
                         "reason": meta_now.get("reason_code") or "conductor_terminal",
                         "detail": str(exc), "orchestration_id": orchestration_id},
                        ensure_ascii=False))
                    return 2
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
            # The conductor terminalizes meta itself; report a non-pass terminal here
            # and exit nonzero (otherwise a failed run falls through to the generic ok
            # output with exit 0). In dev mode, also collect + persist
            # `failure_analysis.json` — the documented dev-failure artifact that
            # `init --resume-from-checkpoint` reads (`_derive_resume_directive`) to
            # build the cross-phase reopen `resume_directive` on resume.
            if workflow_status.strip().lower() != "pass":
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
                            wrote_canonical = _atomic_write_json_exclusive(
                                canonical_path, emergency_payload, tmp_dir=orch_dir
                            )
                            if wrote_canonical:
                                fallback_ref = str(canonical_path.relative_to(repo_root))
                            else:
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
                            fail_output["reason"] = "failure_analysis_persist_failed"
                            fail_output["analysis_ref_error"] = str(primary_exc)
                            fail_output["analysis_ref_fallback_error"] = str(fallback_exc)
                    print(json.dumps(fail_output, ensure_ascii=False))
                    return 2
                fail_output = {
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
        if extra_output:
            ok_output.update(extra_output)
        print(json.dumps(ok_output, ensure_ascii=False))
        return 0
    finally:
        if run_log_file is not None:
            sys.stdout = saved_stdout
            try:
                run_log_file.close()
            except Exception:  # noqa: BLE001
                pass
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
    verification.

    R6-lite rides on the same `_verify_dep_stage` call: its `ir_ref` stage also requires the
    node's RECORDED dependency resolution (its `dependency_graph.json` sidecar) to match the
    one today's `deps.yaml` + `spec_catalog.yaml` derive. So a node certified against an older
    version of one of ITS dependencies (e.g. harness 0.2.1 after the catalog moved to 0.3.0)
    reports not-ready here and this driver re-runs it — which is how "a dependency spec was
    updated, so its dependents are regenerated" becomes a mechanism rather than an operator
    ritual. No content-free version bump of the dependents is required."""
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
    from tools.runner_renderer import spec_id_length_violation

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
        # M3d spec-input gate at closure-build: reject an over-length spec_id here, before
        # any node runs. resolve_node gates each node's own run, but an ALREADY-READY
        # dependency is skipped before it reaches `_run_node` → resolve_node — so gating
        # only there could let an over-length ready dep slip past. Checking every visited
        # spec (target + all deps) here is the closure-level mirror of resolve_node's bound
        # (runner_renderer.MAX_SPEC_ID_LEN). A >55 fortran node cannot certify (so cannot be
        # ready), but this makes the canonical capture point robust regardless.
        _sid_violation = spec_id_length_violation(Path(spec_ref).name)
        if _sid_violation:
            error = {"reason": "spec_id_too_long", "detail": f"{spec_ref}: {_sid_violation}"}
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
    stdout_format: str = "jsonl",
    resume: bool = False,
    prior_orch_by_spec: dict[str, str] | None = None,
    raw_argv: list[str] | None = None,
) -> int:
    """Run the target's dependency closure bottom-up, then the target.

    Each not-ready dependency node runs as its own orchestration (one per node);
    nodes already satisfying the required readiness are skipped. On the first
    dependency failure the run stops (the target is not launched). The target's
    final JSON result carries a `dependency_runs` summary.

    This drives BOTH the fresh `--with-deps` path and closure-aware `--resume`:
    - Fresh (`resume=False`, `prior_orch_by_spec=None`): every not-ready node gets a
      fresh orchestration id and a cold run. Behavior is unchanged from before, with
      the one additive effect that each node now records an `invocation` block whose
      `closure_id` = `target_orchestration_id`, which is what makes a LATER resume
      closure-aware.
    - Resume (`resume=True`): `prior_orch_by_spec` maps a node's spec_ref to its prior
      orchestration id; a not-ready node with a prior orchestration is resumed (warm,
      from its checkpoint) instead of re-run cold, and the target reuses
      `target_orchestration_id` (= closure_id), resumed when its orchestration dir
      already exists. The closure itself is re-derived here deterministically, so
      already-ready deps are skipped and any deps.yaml/catalog change is reflected.

    `raw_argv` is threaded into each node's `invocation` record so the reproduction
    command is captured on every closure node.
    """
    prior_orch_by_spec = prior_orch_by_spec or {}
    ordered, error = _resolve_dependency_closure(repo_root, target_spec_ref)
    if error is not None:
        _emit_closure_event(
            {
                "status": "fail",
                "reason": "dependency_closure_unresolved",
                "detail": error.get("detail"),
                "reason_code": error.get("reason"),
                "target_spec_ref": target_spec_ref,
            },
            stdout_format,
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

        # Closure-aware resume: a not-ready node with a prior orchestration under this
        # closure is resumed (warm) from its checkpoint; otherwise mint a fresh id and
        # cold-run it. Fresh `--with-deps` runs pass an empty map → always fresh/cold.
        prior_dep_orch_id = prior_orch_by_spec.get(spec_ref) if resume else None
        dep_orch_id = prior_dep_orch_id or _new_orchestration_id()
        dep_resume = prior_dep_orch_id is not None
        try:
            dep_source_dependency_ref = _discover_source_dependency_ref(repo_root, spec_ref)
        except ValueError as exc:
            _emit_closure_event(
                {
                    "status": "fail",
                    "reason": "dependency_dep_ref_unresolved",
                    "detail": str(exc),
                    "failed_dependency_node": node_label,
                    "spec_ref": spec_ref,
                    "dependency_runs": dependency_runs,
                    "target_spec_ref": target_spec_ref,
                },
                stdout_format,
            )
            return 2
        # The per-node `node_start` event is emitted uniformly inside _run_node;
        # here we only announce which dependency node (with its pretty label) the
        # closure is about to drive, so the stream stays human-traceable.
        _emit_closure_event(
            {
                "status": "info",
                "event": "dependency_node_begin",
                "node": node_label,
                "spec_ref": spec_ref,
                "until_phase": dep_until_phase,
                "orchestration_id": dep_orch_id,
                "resume": dep_resume,
            },
            stdout_format,
        )
        # Cold run records the reproduction/closure block; a resumed node preserves
        # the block it already carries, so pass None there.
        dep_invocation = None if dep_resume else _build_invocation_record(
            argv=raw_argv,
            spec_ref=spec_ref,
            until_phase=dep_until_phase,
            llm=llm,
            llm_command=llm_command,
            workflow_mode=workflow_mode,
            agent_model=agent_model,
            with_deps=True,
            closure_id=target_orchestration_id,
            closure_target_spec_ref=target_spec_ref,
            closure_until_phase=until_phase,
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
            resume_mode=dep_resume,
            invocation=dep_invocation,
            # On resume, refresh this dep's persisted closure end-phase to the
            # effective closure until_phase so an operator phase override stays durable
            # on the dependency nodes even if the target orchestration is never created.
            closure_until_phase=until_phase if dep_resume else None,
            stdout_format=stdout_format,
        )
        dependency_runs.append(
            {
                "node": node_label,
                "spec_ref": spec_ref,
                "skipped": False,
                "resumed": dep_resume,
                "orchestration_id": dep_orch_id,
                "exit_code": rc,
            }
        )
        if rc != 0:
            _emit_closure_event(
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
                stdout_format,
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
            _emit_closure_event(
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
                stdout_format,
            )
            return 2

    # All dependencies are ready — run the target node, carrying the summary. On
    # closure-aware resume, reuse the closure id as the target orchestration id and
    # resume it when its orchestration is actually THIS closure's target from a prior
    # attempt (it may have failed after the deps, or never started). Warm-resume ONLY
    # when the existing meta is linked to this closure — its own invocation.closure_id
    # equals the closure/target id — AND its spec matches. A reserved id that already
    # named an UNRELATED pre-existing orchestration (a reused --orchestration-id, even
    # one for the SAME spec from a standalone run) must be cold-initialized as the
    # intended target, not resumed off the unrelated run's stale checkpoint/phase state.
    target_meta_path = (
        repo_root / "workspace" / "orchestrations" / target_orchestration_id
        / "orchestration_meta.json"
    )
    target_meta = _read_json_if_exists(target_meta_path) if resume else None
    target_meta_invocation = (
        target_meta.get("invocation") if isinstance(target_meta, dict) else None
    )
    target_resume = (
        resume
        and isinstance(target_meta, dict)
        and target_meta.get("spec_ref") == target_spec_ref
        and isinstance(target_meta_invocation, dict)
        and target_meta_invocation.get("closure_id") == target_orchestration_id
    )
    target_invocation = None if target_resume else _build_invocation_record(
        argv=raw_argv,
        spec_ref=target_spec_ref,
        until_phase=until_phase,
        llm=llm,
        llm_command=llm_command,
        workflow_mode=workflow_mode,
        agent_model=agent_model,
        with_deps=True,
        closure_id=target_orchestration_id,
        closure_target_spec_ref=target_spec_ref,
        closure_until_phase=until_phase,
    )
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
        resume_mode=target_resume,
        invocation=target_invocation,
        closure_until_phase=until_phase if target_resume else None,
        extra_output={"dependency_runs": dependency_runs},
        stdout_format=stdout_format,
    )


if __name__ == "__main__":
    raise SystemExit(main())
