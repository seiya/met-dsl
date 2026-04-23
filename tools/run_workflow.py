#!/usr/bin/env python3
"""Bootstrap workflow orchestration startup for a target spec."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUPPORTED_LLMS = ("codex", "cursor", "claude")
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
    command = ["python3", "tools/codex_orchestration_runtime.py", *args]
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
    spec_ref: str,
    dependency_ref: str | None,
    until_phase: str,
) -> str:
    phase_list = ", ".join(PHASE_ORDER[: PHASE_ORDER.index(until_phase) + 1])
    dependency_line = (
        f"- dependency_ref: `{dependency_ref}`"
        if isinstance(dependency_ref, str) and dependency_ref.strip()
        else "- dependency_ref: `(not specified)`"
    )
    return textwrap.dedent(
        f"""
        Workflow を起動する。

        ## startup context
        - orchestration_id: `{orchestration_id}`
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
    parser.add_argument("--llm", default="codex", choices=SUPPORTED_LLMS)
    parser.add_argument("--llm-command", help="Override backend command used by preflight and optional launch.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--dependency-ref")
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
        until_phase = _normalize_phase(args.until_phase)
        repo_root = Path(args.repo_root).resolve()
        orchestration_id = args.orchestration_id or _new_orchestration_id()
        llm_command = args.llm_command or DEFAULT_LLM_COMMANDS[args.llm]
        _resolve_existing_ref_path(repo_root, args.spec_ref, field_name="spec_ref")
        if args.dependency_ref:
            _resolve_existing_ref_path(repo_root, args.dependency_ref, field_name="dependency_ref")
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
    env["PYTHONPATH"] = str(repo_root) + (f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")

    init_args = [
        "init",
        "--repo-root",
        str(repo_root),
        "--orchestration-id",
        orchestration_id,
        "--spec-ref",
        args.spec_ref,
        "--status",
        args.status,
    ]
    if args.dependency_ref:
        init_args.extend(["--dependency-ref", args.dependency_ref])
    try:
        _runtime_command(repo_root, env, init_args)
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
        spec_ref=args.spec_ref,
        dependency_ref=args.dependency_ref,
        until_phase=until_phase,
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
        if proc.returncode != 0:
            return proc.returncode

    print(
        json.dumps(
            {
                "status": "ok",
                "orchestration_id": orchestration_id,
                "llm": args.llm,
                "llm_command": llm_command,
                "target_spec_ref": args.spec_ref,
                "until_phase": until_phase,
                "metdsl_workflow_mode": env["METDSL_WORKFLOW_MODE"],
                "prompt_ref": str(prompt_path.relative_to(repo_root)),
                "llm_invoked": launched,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
