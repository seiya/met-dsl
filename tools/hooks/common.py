#!/usr/bin/env python3
"""Backend-agnostic hook contracts and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Protocol

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lookup_payload_field(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is not None:
        return value
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return inner.get(key)
    return None


READ_HINT = (
    "Hint: workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json "
    "and read_manifests/<agent_run_id>.json may be read directly. For other paths use "
    "'run-gate --gate orchestration_read' within read_manifests/<agent_run_id>.json "
    "allowed_read_roots. "
    "Interpret requirements only from docs/, spec/, and skill_must_read_refs artifacts; "
    "do not derive rules from tools/, validator scripts, or tests."
)

WRITE_HINT = (
    "Hint: Write paths route by extension. .json/.txt outputs go through "
    "'guarded-apply-patch' (tools/orchestration_runtime.py) within "
    "output_manifests/<agent_run_id>.json.allowed_output_paths. Other "
    "extensions (.yaml/.yml/.md/source code) are written via Edit/Write "
    "directly and must be listed under allowed_file_tool_paths."
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


def _extract_read_targets(cmd_name: str, cmd_tokens: list[str]) -> list[str]:
    args = cmd_tokens[1:]
    cmd = cmd_name.lower()
    if not args:
        return []

    if cmd in {"cat", "head", "tail", "less", "more", "bat", "pygmentize"}:
        return [tok for tok in args if not tok.startswith("-")]

    if cmd == "sed":
        positional: list[str] = []
        read_targets: list[str] = []
        has_explicit_script_source = False
        explicit_script_after_positional = False
        idx = 0
        while idx < len(args):
            token = args[idx]
            if token == "--":
                positional.extend(args[idx + 1 :])
                break
            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
                if key == "--file" and value:
                    if positional:
                        explicit_script_after_positional = True
                    read_targets.append(value)
                    has_explicit_script_source = True
                    idx += 1
                    continue
                if key == "--expression":
                    if positional:
                        explicit_script_after_positional = True
                    has_explicit_script_source = True
                    idx += 1
                    continue
            if token in {"-e", "-f"}:
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                if token == "-f" and idx + 1 < len(args):
                    read_targets.append(args[idx + 1])
                idx += 2
                continue
            if token.startswith("-e") and token != "-e":
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                idx += 1
                continue
            if token.startswith("-f") and token != "-f":
                if positional:
                    explicit_script_after_positional = True
                has_explicit_script_source = True
                read_targets.append(token[2:])
                idx += 1
                continue
            if token.startswith("-"):
                idx += 1
                continue
            positional.append(token)
            idx += 1
        if has_explicit_script_source:
            if explicit_script_after_positional and positional:
                return read_targets + positional[1:]
            return read_targets + positional
        if len(positional) <= 1:
            return read_targets
        return read_targets + positional[1:]

    if cmd in {"rg", "grep"}:
        positional: list[str] = []
        idx = 0
        has_explicit_pattern = False
        read_targets: list[str] = []
        while idx < len(args):
            token = args[idx]
            if token == "--":
                positional.extend(args[idx + 1 :])
                break
            if token.startswith("--") and "=" in token:
                key, value = token.split("=", 1)
                if key in {"--file", "--regexp"}:
                    has_explicit_pattern = True
                    if key == "--file" and value:
                        read_targets.append(value)
                    idx += 1
                    continue
            if token in {"-e", "-f", "--regexp", "--file"}:
                has_explicit_pattern = True
                if token in {"-f", "--file"} and idx + 1 < len(args):
                    read_targets.append(args[idx + 1])
                idx += 2
                continue
            if token.startswith("-e") and token != "-e":
                has_explicit_pattern = True
                idx += 1
                continue
            if token.startswith("-f") and token != "-f":
                has_explicit_pattern = True
                read_targets.append(token[2:])
                idx += 1
                continue
            if token.startswith("-"):
                idx += 1
                continue
            positional.append(token)
            idx += 1
        if not positional:
            return read_targets
        if has_explicit_pattern:
            return read_targets + positional
        return read_targets + positional[1:]

    if cmd == "awk":
        positional: list[str] = []
        idx = 0
        read_targets: list[str] = []
        has_program_file = False
        while idx < len(args):
            token = args[idx]
            if token == "--":
                positional.extend(args[idx + 1 :])
                break
            if token.startswith("--file="):
                value = token.split("=", 1)[1]
                if value:
                    read_targets.append(value)
                    has_program_file = True
                idx += 1
                continue
            if token in {"-f", "--file"}:
                if idx + 1 < len(args):
                    read_targets.append(args[idx + 1])
                has_program_file = True
                idx += 2
                continue
            if token.startswith("-f") and token != "-f":
                read_targets.append(token[2:])
                has_program_file = True
                idx += 1
                continue
            if token.startswith("-"):
                idx += 1
                continue
            positional.append(token)
            idx += 1
        if not positional:
            return read_targets
        if has_program_file:
            return read_targets + positional
        return read_targets + positional[1:]

    return []


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
    workflow_mode_val = os.environ.get("METDSL_WORKFLOW_MODE", "0").strip()
    if workflow_mode_val == "1":
        bash_read_cmds = frozenset(
            {"cat", "head", "tail", "less", "more", "bat", "pygmentize", "sed", "rg", "grep", "awk"}
        )
        try:
            cmd_tokens = shlex.split(command)
        except ValueError:
            cmd_tokens = command.split()
        lowered_tokens = [tok.lower() for tok in cmd_tokens]
        first_cmd = lowered_tokens[0].split("/")[-1] if lowered_tokens else ""
        if first_cmd in bash_read_cmds:
            repo_root_raw = hook_input.payload.get("repo_root")
            repo_root = (
                Path(repo_root_raw).resolve()
                if isinstance(repo_root_raw, str) and repo_root_raw.strip()
                else Path.cwd()
            )
            repo_tools_root = (repo_root / "tools").resolve()
            read_targets = _extract_read_targets(first_cmd, cmd_tokens)
            if any(
                _is_path_under_root(_resolve_target_path(repo_root, target), repo_tools_root)
                for target in read_targets
            ):
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        "blocked: direct read from tools/ via Bash is forbidden in workflow mode. "
                        "Derive rules only from docs/, spec/, and skill_must_read_refs artifacts."
                    ),
                    continue_processing=False,
                    audit_detail={"policy": "forbid_tools_direct_read", "command": command},
                )
        if "python" in lowered and "-c" in lowered:
            if re.search(r"""open\s*\([^)]*,\s*['"][wax]""", command):
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        "blocked: python -c with file write (open(..., 'w'/'a'/'x')) detected. "
                        "Use guarded-apply-patch for file writes in workflow mode."
                    ),
                    continue_processing=False,
                    audit_detail={"policy": "forbid_python_inline_write", "command": command},
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


def _normalize_rel_posix(path_token: str) -> str:
    """Normalize repo-relative path into stable POSIX token."""
    token = path_token.strip().replace("\\", "/").lstrip("/")
    while "//" in token:
        token = token.replace("//", "/")
    return token.rstrip("/")


def _is_path_under_root(target: Path, root: Path) -> bool:
    target_s = str(target)
    root_s = str(root)
    return target_s == root_s or target_s.startswith(root_s.rstrip("/") + "/")


def _is_self_agent_manifest_read_path(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
) -> bool:
    """当該 child の output / read manifest JSON への Read は run-gate 外でも許可する。"""
    orch = orchestration_id.strip()
    rid = agent_run_id.strip()
    if not orch or not rid:
        return False
    abs_target = _resolve_target_path(repo_root, file_path)
    try:
        rel = abs_target.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return False
    rel_norm = _normalize_rel_posix(rel)
    out_rel = _normalize_rel_posix(f"workspace/orchestrations/{orch}/output_manifests/{rid}.json")
    read_rel = _normalize_rel_posix(f"workspace/orchestrations/{orch}/read_manifests/{rid}.json")
    return rel_norm == out_rel or rel_norm == read_rel


@dataclass(frozen=True)
class _CliManagedPath:
    pattern: re.Pattern[str]
    cli_hint: str


_CLI_MANAGED_PATHS: list[_CliManagedPath] = [
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/launches/[^/]+\.(?:response\.json|reply\.txt|prompt\.txt|request\.json)$"),
        "python3 tools/orchestration_runtime.py record-launch ...",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/agent_runs\.jsonl$"),
        "python3 tools/orchestration_runtime.py record-agent-run ...",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/step_results/[^/]+\.json$"),
        "python3 tools/orchestration_runtime.py write-step-result ...",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/orchestration_meta\.json$"),
        "python3 tools/orchestration_runtime.py init-orchestration / run_workflow.py (auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/(?:output|read)_manifests/[^/]+\.json$"),
        "python3 tools/orchestration_runtime.py record-launch (manifests are auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/preflight\.json$"),
        "python3 tools/run_workflow.py ... (preflight is auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/capabilities/[^/]+\.json$"),
        "python3 tools/orchestration_runtime.py record-launch (capability is auto-generated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/orchestration_checkpoint\.json$"),
        "python3 tools/orchestration_runtime.py write-step-result (checkpoint is auto-updated)",
    ),
    _CliManagedPath(
        re.compile(r"workspace/orchestrations/[^/]+/phase_state\.json$"),
        "python3 tools/orchestration_runtime.py (phase_state is managed by the runtime)",
    ),
]


def check_cli_managed_path(repo_root: Path, file_path: str) -> "HookDecision | None":
    """CLI 管理パスに一致するなら BLOCK の HookDecision を返す。一致なしは None。"""
    abs_target = _resolve_target_path(repo_root, file_path)
    try:
        rel = abs_target.relative_to(repo_root).as_posix()
    except ValueError:
        rel = file_path
    for entry in _CLI_MANAGED_PATHS:
        if entry.pattern.search(rel):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    f"Direct write to CLI-managed path is forbidden: {rel!r}\n"
                    f"Use: {entry.cli_hint}"
                ),
                continue_processing=False,
                audit_detail={"policy": "cli_managed_path", "path": rel, "cli_hint": entry.cli_hint},
            )
    return None


def validate_write_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
    tool_name: str | None = None,
) -> HookDecision:
    """output manifest の allowed_output_paths に対して write/edit 対象を検証する。"""
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
    abs_target = _resolve_target_path(repo_root, file_path)
    try:
        rel_target = abs_target.relative_to(repo_root).as_posix()
    except ValueError:
        rel_target = str(abs_target).replace("\\", "/")
    rel_target_norm = _normalize_rel_posix(rel_target)

    tmp_root = manifest.get("allowed_tmp_root", "")
    if isinstance(tmp_root, str) and tmp_root.strip():
        tmp_norm = _normalize_rel_posix(tmp_root.strip())
        tmp_prefix = tmp_norm + "/"
        if rel_target_norm == tmp_norm or rel_target_norm.startswith(tmp_prefix):
            return HookDecision(action=HookDecisionAction.ALLOW)

    allowed_file_tool_paths_obj = manifest.get("allowed_file_tool_paths")
    if not isinstance(allowed_file_tool_paths_obj, list):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest missing allowed_file_tool_paths list for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    allowed_file_tool_paths: set[str] = set()
    for item in allowed_file_tool_paths_obj:
        if not isinstance(item, str):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "output manifest allowed_file_tool_paths must contain only strings "
                    f"for agent_run_id={agent_run_id!r}. {MANIFEST_HINT}"
                ),
                continue_processing=False,
            )
        token = _normalize_rel_posix(item)
        if token:
            allowed_file_tool_paths.add(token)

    allowed_paths_obj = manifest.get("allowed_output_paths")
    if not isinstance(allowed_paths_obj, list):
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"output manifest missing allowed_output_paths list for agent_run_id={agent_run_id!r}. "
                f"{MANIFEST_HINT}"
            ),
            continue_processing=False,
        )
    allowed_paths = [str(item).strip() for item in allowed_paths_obj if isinstance(item, str) and item.strip()]
    normalized_allowed_paths = {_normalize_rel_posix(p) for p in allowed_paths}
    if rel_target_norm not in normalized_allowed_paths:
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"unauthorized write: {file_path!r} is not in output_manifest allowed_output_paths "
                f"(agent_run_id={agent_run_id!r}). {WRITE_HINT}"
            ),
            continue_processing=False,
            audit_detail={
                "policy": "output_manifest_write_guard",
                "file_path": file_path,
                "agent_run_id": agent_run_id,
                "allowed_output_paths": allowed_paths,
            },
        )
    if tool_name in {"Edit", "Write", "apply_patch"} and rel_target_norm not in allowed_file_tool_paths:
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                "direct write via Edit/Write/apply_patch tool is forbidden for this target path. "
                "Use guarded-apply-patch instead or include the path in "
                "output_manifest allowed_file_tool_paths: "
                "python3 tools/orchestration_runtime.py guarded-apply-patch ..."
            ),
            continue_processing=False,
            audit_detail={
                "policy": "enforce_guarded_apply_patch",
                "tool_name": tool_name,
                "file_path": file_path,
                "agent_run_id": agent_run_id,
            },
        )
    return HookDecision(action=HookDecisionAction.ALLOW)


def validate_read_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
) -> HookDecision:
    """read manifest の allowed_read_roots に対して read 対象を検証する。"""
    if _is_self_agent_manifest_read_path(repo_root, orchestration_id, agent_run_id, file_path):
        return HookDecision(action=HookDecisionAction.ALLOW)
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
