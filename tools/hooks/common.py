#!/usr/bin/env python3
"""Backend-agnostic hook contracts and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
from typing import Any, Protocol

READ_HINT = (
    "Hint: Read only via 'run-gate --gate orchestration_read' and only within "
    "read_manifests/<agent_run_id>.json allowed_read_roots. "
    "Interpret requirements only from docs/, spec/, and skill_must_read_refs artifacts; "
    "do not derive rules from tools/, validator scripts, or tests."
)

WRITE_HINT = (
    "Hint: Write only via 'guarded-apply-patch' (tools/orchestration_runtime.py) "
    "and only within output_manifests/<agent_run_id>.json write_roots."
)

MANIFEST_HINT = (
    "Hint: Ensure record-launch generated the manifest for this agent_run_id and that the manifest "
    "JSON structure is valid."
)


class HookEventName(str, Enum):
    SESSION_START = "session_start"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_COMMAND_EXECUTE = "pre_command_execute"
    PERMISSION_REQUEST = "permission_request"
    POST_COMMAND_EXECUTE = "post_command_execute"
    STOP = "stop"


class HookDecisionAction(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    CONTINUE_WITH_MESSAGE = "continue_with_message"


@dataclass(frozen=True)
class HookInput:
    event_name: HookEventName
    backend: str
    payload: dict[str, Any]
    command: str | None = None
    prompt: str | None = None
    tool_name: str | None = None
    file_path: str | None = None
    session_id: str | None = None
    agent_session_id: str | None = None


@dataclass(frozen=True)
class HookDecision:
    action: HookDecisionAction
    reason: str | None = None
    additional_context: str | None = None
    continue_processing: bool = True
    audit_detail: dict[str, Any] | None = None


class HookBackendAdapter(Protocol):
    def supported_events(self) -> set[HookEventName]:
        """Return events this adapter can decode/encode."""

    def decode_event(self, event_name: str, payload: dict[str, Any]) -> HookInput:
        """Normalize backend-native event payload to HookInput."""

    def encode_decision(self, decision: HookDecision) -> tuple[int, str]:
        """Return `(exit_code, stdout_text)` for backend hook process protocol."""


def normalize_hook_event_name(event_name: str) -> HookEventName:
    token = event_name.strip()
    mapping = {
        "SessionStart": HookEventName.SESSION_START,
        "UserPromptSubmit": HookEventName.USER_PROMPT_SUBMIT,
        "PreToolUse": HookEventName.PRE_COMMAND_EXECUTE,
        "PermissionRequest": HookEventName.PERMISSION_REQUEST,
        "PostToolUse": HookEventName.POST_COMMAND_EXECUTE,
        "Stop": HookEventName.STOP,
        "session_start": HookEventName.SESSION_START,
        "user_prompt_submit": HookEventName.USER_PROMPT_SUBMIT,
        "pre_command_execute": HookEventName.PRE_COMMAND_EXECUTE,
        "permission_request": HookEventName.PERMISSION_REQUEST,
        "post_command_execute": HookEventName.POST_COMMAND_EXECUTE,
        "stop": HookEventName.STOP,
    }
    if token in mapping:
        return mapping[token]
    raise ValueError(f"unsupported hook event name: {event_name!r}")


def validate_pipeline_semantics_stage(*, step_key: str, args_json: dict[str, Any]) -> str:
    """Validate `validate_pipeline_semantics` stage input for a step capability."""
    allowed_by_step: dict[str, frozenset[str]] = {
        "plan": frozenset({"plan", "full"}),
        "generate": frozenset({"post_generate", "post_build", "full"}),
        "tune": frozenset({"post_generate", "post_build", "full"}),
        "build": frozenset({"post_build", "full"}),
        "execute": frozenset({"post_execute", "full"}),
        "judge": frozenset({"pre_judge", "full"}),
        "promote": frozenset(
            {"plan", "post_generate", "post_build", "post_execute", "pre_judge", "full"}
        ),
    }
    stage = args_json.get("stage") or args_json.get("--stage")
    if not isinstance(stage, str) or not stage.strip():
        raise ValueError(
            "pre_command_execute hook: validate_pipeline_semantics requires args_json.stage "
            "(or --stage) as non-empty string"
        )
    stage_l = stage.strip().lower()
    allowed = allowed_by_step.get(step_key)
    if allowed is not None and stage_l not in allowed:
        raise ValueError(
            "pre_command_execute hook: validate_pipeline_semantics "
            f"--stage {stage_l!r} not permitted for capability step={step_key!r} "
            f"(allowed={sorted(allowed)})"
        )

    if stage_l == "pre_judge":
        for key, val in args_json.items():
            key_s = str(key).lower().replace("_", "-")
            if "allow-missing-orchestration" in key_s or "allow-missing-llm-review" in key_s:
                if val is True or val == 1:
                    raise ValueError(
                        "pre_command_execute hook: pre_judge forbids allow-missing-orchestration "
                        "and allow-missing-llm-review"
                    )
                if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes"}:
                    raise ValueError(
                        "pre_command_execute hook: pre_judge forbids allow-missing-orchestration "
                        "and allow-missing-llm-review"
                    )
    return stage_l


def _extract_command(payload: dict[str, Any]) -> str | None:
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    return None


def evaluate_common_policy(hook_input: HookInput) -> HookDecision:
    """Apply backend-agnostic policy checks."""
    if hook_input.event_name not in {
        HookEventName.PRE_COMMAND_EXECUTE,
        HookEventName.PERMISSION_REQUEST,
    }:
        return HookDecision(action=HookDecisionAction.ALLOW)

    command = hook_input.command or _extract_command(hook_input.payload)
    if not command:
        return HookDecision(action=HookDecisionAction.ALLOW)
    lowered = command.lower()
    if "git reset --hard" in lowered:
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason="blocked by common hook policy: git reset --hard is forbidden",
            continue_processing=False,
            audit_detail={"policy": "forbid_git_reset_hard", "command": command},
        )
    workflow_mode_raw = os.environ.get("METDSL_WORKFLOW_EXEC_MODE")
    workflow_mode = (workflow_mode_raw or "dev").strip().lower()
    if workflow_mode == "dev":
        forbidden_tokens = (
            "--allow-missing-orchestration",
            "--allow-missing-llm-review",
            "--allow-soft-fail",
            "--allow-soft-verify",
            "--ignore-verify-fail",
            "--force-pass",
        )
        matched = [token for token in forbidden_tokens if token in lowered]
        if matched:
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "blocked by common hook policy: dev mode forbids verify bypass flags: "
                    + ", ".join(matched)
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "forbid_verify_bypass_flags_in_dev_mode",
                    "workflow_mode": workflow_mode,
                    "command": command,
                    "matched_tokens": matched,
                },
            )
    return HookDecision(action=HookDecisionAction.ALLOW)


def _resolve_target_path(repo_root: Path, path_token: str) -> Path:
    raw = path_token.strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (repo_root / candidate).resolve()


def _resolve_manifest_root(repo_root: Path, root_token: str) -> Path:
    raw = root_token.strip()
    if not raw:
        return repo_root
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (repo_root / candidate).resolve()


def _is_path_under_root(target: Path, root: Path) -> bool:
    target_s = str(target)
    root_s = str(root)
    return target_s == root_s or target_s.startswith(root_s.rstrip("/") + "/")


def validate_write_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
) -> HookDecision:
    """output manifest の write_roots に対して write/edit 対象を検証する。"""
    manifest_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "output_manifests"
        / f"{agent_run_id}.json"
    )
    if not manifest_path.exists():
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest not found for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest is unreadable or invalid JSON for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    if not isinstance(manifest, dict):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest must be a JSON object for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    write_roots_obj = manifest.get("write_roots")
    if not isinstance(write_roots_obj, list):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest missing write_roots list for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    write_roots = [str(item) for item in write_roots_obj]
    abs_target = _resolve_target_path(repo_root, file_path)
    for root in write_roots:
        abs_root = _resolve_manifest_root(repo_root, root)
        if _is_path_under_root(abs_target, abs_root):
            return HookDecision(action=HookDecisionAction.ALLOW)
    return HookDecision(
        action=HookDecisionAction.BLOCK,
        reason=(
            f"unauthorized write: {file_path!r} is not in output_manifest write_roots "
            f"(agent_run_id={agent_run_id!r}). {WRITE_HINT}"
        ),
        continue_processing=False,
        audit_detail={
            "policy": "output_manifest_write_guard",
            "file_path": file_path,
            "agent_run_id": agent_run_id,
            "write_roots": write_roots,
        },
    )


def validate_read_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
) -> HookDecision:
    """read manifest の allowed_read_roots に対して read 対象を検証する。"""
    manifest_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "read_manifests"
        / f"{agent_run_id}.json"
    )
    if not manifest_path.exists():
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest not found for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest is unreadable or invalid JSON for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    if not isinstance(manifest, dict):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest must be a JSON object for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    allowed_roots_obj = manifest.get("allowed_read_roots")
    if not isinstance(allowed_roots_obj, list):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"read manifest missing allowed_read_roots list for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    allowed_roots = [str(item) for item in allowed_roots_obj]
    abs_target = _resolve_target_path(repo_root, file_path)
    for root in allowed_roots:
        abs_root = _resolve_manifest_root(repo_root, root.rstrip("/"))
        if _is_path_under_root(abs_target, abs_root):
            return HookDecision(action=HookDecisionAction.ALLOW)
    return HookDecision(
        action=HookDecisionAction.BLOCK,
        reason=(
            f"unauthorized read: {file_path!r} is not in read_manifest allowed_read_roots "
            f"(agent_run_id={agent_run_id!r}). {READ_HINT}"
        ),
        continue_processing=False,
        audit_detail={
            "policy": "read_manifest_read_guard",
            "file_path": file_path,
            "agent_run_id": agent_run_id,
            "allowed_read_roots": allowed_roots,
        },
    )
