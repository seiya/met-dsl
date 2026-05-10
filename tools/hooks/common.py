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
import time
from pathlib import Path
from typing import Any, Protocol

# fcntl is POSIX-only.  On Windows we fall through to fail-closed when the
# auto-read seen-set needs an exclusive lock — there is no portable
# equivalent, and Claude Code on Windows has no direct call sites for the
# orchestration auto-read path today.  Guarded so the import does not raise.
try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — exercised only on non-POSIX
    _fcntl = None  # type: ignore[assignment]

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
    "do not derive rules from tools/, validator scripts, or tests. "
    "See docs/RUNBOOK.md#hook-recovery for the full recovery cheatsheet."
)

WRITE_HINT = (
    "Hint: Write paths route by extension. .json/.txt outputs go through "
    "'guarded-apply-patch' (tools/orchestration_runtime.py) within "
    "output_manifests/<agent_run_id>.json.allowed_output_paths. Other "
    "extensions (.yaml/.yml/.md/source code) are written via Edit/Write "
    "directly and must be listed under allowed_file_tool_paths. "
    "Ensure $TMPDIR is exported from output_manifests.allowed_tmp_root before writing temp files. "
    "See docs/RUNBOOK.md#hook-recovery for the full recovery cheatsheet."
)

# Repo-relative paths that orchestration agent auto-reads at startup (Claude Code behavior).
# These reads are expected and harmless; silently allow them rather than block.
# Authorization is by exact repo-relative path match (NOT suffix match) to prevent
# absolute-path bypasses like /etc/README.md.
_AUTO_READ_TOLERATED_REPO_RELPATHS: frozenset[str] = frozenset({
    "MEMORY.md",
    "README.md",
    "TODO.md",
    "CLAUDE.md",
    ".claude/settings.json",
})

# Project-memory file lives outside the repo root under the user's Claude Code state directory.
# We allow it ONLY when the resolved path is inside ~/.claude/projects/ AND ends with
# the canonical "/memory/MEMORY.md" relative tail.
_AUTO_READ_PROJECT_MEMORY_PARENT_TAIL: str = ".claude/projects"
_AUTO_READ_PROJECT_MEMORY_FILE_TAIL: str = "memory/MEMORY.md"

MANIFEST_HINT = (
    "Hint: Ensure record-launch generated the manifest for this agent_run_id and that the manifest "
    "JSON structure is valid."
)


def format_block_reason_with_hint(decision: "HookDecision") -> str:
    """Append audit_detail.fix_hint (next_command + docs_ref) to a BLOCK reason.

    Adapters log audit_detail for forensics, but agents only see the `reason`
    string in the rejection message. Surface the structured fix_hint inline so
    the agent can act on it without consulting the audit log.
    """
    base = decision.reason or "blocked by policy"
    audit = decision.audit_detail or {}
    fix_hint = audit.get("fix_hint") if isinstance(audit, dict) else None
    if not isinstance(fix_hint, dict):
        return base
    next_command = fix_hint.get("next_command")
    docs_ref = fix_hint.get("docs_ref")
    note = fix_hint.get("note")
    appended: list[str] = []
    if isinstance(next_command, str) and next_command.strip():
        appended.append(f"Fix: {next_command.strip()}")
    if isinstance(docs_ref, str) and docs_ref.strip():
        appended.append(f"Docs: {docs_ref.strip()}")
    if isinstance(note, str) and note.strip():
        appended.append(f"Note: {note.strip()}")
    if not appended:
        return base
    return base + "\n\n" + "\n".join(appended)


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
        if "python" in lowered:
            # Fail-closed: ALL inline Python execution (`-c` snippets and
            # `- <<EOF` heredocs) is blocked in workflow mode.  Regex-based
            # write detection is fundamentally unreliable — alias bypasses
            # like `from pathlib import Path as P; P('x').write_text(...)`
            # or `Path('x').open('w').write(...)` would slip through any
            # finite pattern set, and the same goes for `/dev/shm` string
            # literals embedded in inline source.  Agents that need to run
            # Python should use a real script file (`python3 script.py`),
            # which goes through normal write/read manifest validation.
            #
            # Tokenization: shlex puts `-c` and `<<` into separate tokens.
            _py_inline_blocked = False
            _py_inline_reason = ""
            tokens_for_python: list[str] = cmd_tokens
            # Detect `python[3]` invocations specifically (`python` substring
            # in `lowered` is broad — narrow to a token whose basename starts
            # with python).
            has_python_invocation = any(
                tok.split("/")[-1].lower().startswith("python")
                for tok in tokens_for_python
            )
            if has_python_invocation:
                # `-c` form
                if "-c" in tokens_for_python:
                    _py_inline_blocked = True
                    _py_inline_reason = "python -c inline execution is forbidden in workflow mode"
                # heredoc form: `python3 - <<EOF` (still detected via regex
                # because heredoc syntax is not a single token).
                elif re.search(r"""python3?\s+-\s*<<""", command):
                    _py_inline_blocked = True
                    _py_inline_reason = (
                        "python - <<EOF heredoc inline execution is forbidden in workflow mode"
                    )
            if _py_inline_blocked:
                # Intent classification — uuid / json_read / write (default).
                # The block is unconditional, but the recovery hint differs by
                # intent: agents commonly reach for `python -c` to (a) generate
                # a UUID, (b) inspect a JSON file, or (c) write a file. Pointing
                # them at the canonical alternative for the actual intent
                # eliminates the retry loop.
                intent = "write"
                hint_next = (
                    "python3 tools/orchestration_runtime.py guarded-apply-patch "
                    "--repo-root . --orchestration-id <oid> --actor-role <role> "
                    "--agent-run-id <id> --paths-json '[\"<path>\"]' "
                    "--patch-file ${TMPDIR}/x.patch --capability-token <token>"
                )
                if re.search(r"uuid\.uuid[1345]\s*\(", command):
                    # Cover uuid1/uuid3/uuid4/uuid5 — agents typically reach
                    # for uuid4, but uuid1 (host+time) and uuid5 (namespace
                    # SHA-1) also appear. Pattern requires `uuid.<fn>(` so
                    # bare `uuid` strings (e.g. paths/log lines) don't match.
                    intent = "uuid"
                    hint_next = "python3 tools/new_agent_run_id.py"
                elif re.search(r"json\s*\.\s*loads?\s*\(", command):
                    intent = "json_read"
                    hint_next = (
                        "Use the Read tool for the JSON file directly; if Python is "
                        "required, write a script to ${TMPDIR}/x.py and run "
                        "`python3 ${TMPDIR}/x.py`."
                    )
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        f"blocked: {_py_inline_reason}. "
                        "Inline Python is fail-closed because regex-based "
                        "filtering cannot reliably catch alias/string-literal "
                        "bypasses. Use a real script file (python3 script.py) "
                        "for execution, or tools/audit_orchestration.py for "
                        "log inspection. "
                        "Use guarded-apply-patch for .json/.txt outputs, "
                        "or Edit/Write tool for .yaml/.yml/.md/source code. "
                        "See docs/RUNBOOK.md#hook-recovery."
                    ),
                    continue_processing=False,
                    audit_detail={
                        "policy": "forbid_python_inline_write",
                        "command": command,
                        "intent_detected": intent,
                        "fix_hint": {
                            "next_command": hint_next,
                            "docs_ref": "docs/RUNBOOK.md#hook-recovery",
                        },
                    },
                )
        # Block any Bash command that touches /dev/shm in workflow mode.
        # We scan EVERY token of the entire command — not just positional args
        # of the first command — to defeat bypasses via shell control tokens
        # (`cd . && cp ... /dev/shm/x`), wrapper commands (`env cp ...`,
        # `bash -c '...'`), option-arg forms (`install -t /dev/shm`), and
        # long-form options (`cp --target-directory=/dev/shm ...`). The policy
        # is intentionally strict: workflow mode never legitimately needs
        # /dev/shm, since a per-agent $TMPDIR (workspace/tmp/<agent_run_id>/)
        # is provided.
        offending = _find_dev_shm_token(command, cmd_tokens)
        if offending is not None:
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    f"blocked: command touches {offending!r} which is forbidden. "
                    "/dev/shm reads/writes are not permitted; use $TMPDIR "
                    "(workspace/tmp/<agent_run_id>/) for temporary files. "
                    "See docs/RUNBOOK.md#hook-recovery."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "output_manifest_write_guard",
                    "command": command,
                    "destination": offending,
                    "fix_hint": {
                        "next_command": "export TMPDIR from output_manifests/<id>.json allowed_tmp_root",
                        "docs_ref": "docs/RUNBOOK.md#hook-recovery",
                    },
                },
            )
    return HookDecision(action=HookDecisionAction.ALLOW)


_DEV_SHM_PATH_ACCESS_CMDS: frozenset[str] = frozenset({
    # Commands that take filesystem path arguments and would directly access
    # `/dev/shm` if one is passed.  Search/text commands (grep, rg, awk, sed,
    # echo) are intentionally excluded — `grep '/dev/shm' file.log` is a
    # legitimate diagnostic that does not touch /dev/shm.
    "cp", "mv", "rsync", "install", "dd", "tee", "cat", "ln",
    "ls", "stat", "rm", "mkdir", "rmdir", "touch", "truncate",
    # Archive/search/traversal commands that read or write paths.
    "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "xz", "7z",
    "find", "fd", "du", "df",
    # Interpreters that can be coaxed into accessing arbitrary paths via
    # script arguments — bare `/dev/shm` here means "the interpreter is
    # invoked with /dev/shm as a script/cwd/argv element".  Inline -c
    # snippets (python3 -c "open('/dev/shm/...')") are caught by the
    # fail-closed inline-execution policy below, not here.
    "python", "python3", "perl", "ruby", "node", "lua", "php",
})

_DEV_SHM_WRAPPER_CMDS: frozenset[str] = frozenset({
    "env", "sudo", "nice", "ionice", "stdbuf", "time", "exec",
})

_DEV_SHM_SHELL_CONTROL: frozenset[str] = frozenset({"&&", "||", ";", "|"})


def _find_dev_shm_token(command: str, cmd_tokens: list[str]) -> str | None:
    """Scan a Bash command for any token that touches /dev/shm.

    Strategy:
    - Tokens with an explicit path suffix (`/dev/shm/foo`) are unambiguously
      filesystem references and ALWAYS flagged.
    - Bare tokens (`/dev/shm`) and option-arg destinations are only flagged
      when the surrounding command is a path-access command (cp/mv/rsync/etc.)
      — otherwise `grep '/dev/shm' file` and `echo /dev/shm` would over-block.
    - Quoted shell snippets (`bash -c "..."`) are re-tokenized recursively.
    """
    def _check_token_with_suffix(tok: str) -> str | None:
        """Match `/dev/shm/<...>` (explicit path), `option=/dev/shm[/...]`,
        or shell-redirection-prefixed forms like `>/dev/shm/x`,
        `</dev/shm/x`, `>>/dev/shm/x`, `1>/dev/shm/x`, `&>/dev/shm/x`.

        `shlex.split()` keeps the redirection operator glued to the path
        when there is no whitespace (`echo hi >/dev/shm/x` →
        `['echo', 'hi', '>/dev/shm/x']`); without this branch the redirect
        bypasses the path-suffix check.
        """
        if tok.startswith("/dev/shm/"):
            return tok
        eq_idx = tok.find("=")
        if eq_idx >= 0:
            after = tok[eq_idx + 1 :]
            if after == "/dev/shm" or after.startswith("/dev/shm/"):
                return tok
        # Shell redirection-prefixed forms.  The redirection operator is one
        # of: `>`, `>>`, `<`, `<<`, `<<<`, `&>`, `&>>`, optionally preceded
        # by a single fd digit (`1>`, `2>>`, `3<`, ...).
        # Strip the operator+optional-digit and re-check.
        for prefix_len in range(1, 5):
            if len(tok) <= prefix_len:
                continue
            head = tok[:prefix_len]
            tail = tok[prefix_len:]
            # Pattern: optional fd digit, then redirection operator
            if not head:
                continue
            i = 0
            if i < len(head) and head[i].isdigit():
                i += 1
            op = head[i:]
            if op in (">", ">>", "<", "<<", "<<<", "&>", "&>>"):
                if tail == "/dev/shm" or tail.startswith("/dev/shm/"):
                    return tok
        return None

    def _is_bare_dev_shm(tok: str) -> bool:
        return tok == "/dev/shm"

    def _split_segments(tokens: list[str]) -> list[list[str]]:
        segments: list[list[str]] = []
        current: list[str] = []
        for t in tokens:
            if t in _DEV_SHM_SHELL_CONTROL:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(t)
        if current:
            segments.append(current)
        return segments

    def _segment_cmd_args(segment: list[str]) -> tuple[str, list[str]]:
        """Strip leading wrappers (env, sudo, ...) and env-VAR=value pairs.

        Returns (basename(cmd_lower), remaining_args).
        """
        i = 0
        # Skip wrapper commands and their VAR=value arguments
        while i < len(segment) and segment[i].lower() in _DEV_SHM_WRAPPER_CMDS:
            i += 1
            while (
                i < len(segment)
                and "=" in segment[i]
                and not segment[i].startswith("-")
                and "/" not in segment[i].split("=", 1)[0]
            ):
                i += 1
        if i >= len(segment):
            return ("", [])
        cmd = segment[i].split("/")[-1].lower()
        return (cmd, segment[i + 1 :])

    # Pass 1: explicit path-suffix or option=value forms — always flag.
    for tok in cmd_tokens:
        hit = _check_token_with_suffix(tok)
        if hit is not None:
            return hit

    # Pass 2: bare `/dev/shm` in path-access command segments.
    for seg in _split_segments(cmd_tokens):
        cmd, args = _segment_cmd_args(seg)
        if cmd in _DEV_SHM_PATH_ACCESS_CMDS:
            for tok in args:
                if _is_bare_dev_shm(tok):
                    return tok

    # Pass 3: re-tokenize quoted shell snippets (e.g. `bash -c "..."`).
    for tok in cmd_tokens:
        if " " not in tok and "\t" not in tok and "\n" not in tok:
            continue
        try:
            inner = shlex.split(tok)
        except ValueError:
            continue
        for itok in inner:
            hit = _check_token_with_suffix(itok)
            if hit is not None:
                return hit
        for inner_seg in _split_segments(inner):
            cmd, args = _segment_cmd_args(inner_seg)
            if cmd in _DEV_SHM_PATH_ACCESS_CMDS:
                for itok in args:
                    if _is_bare_dev_shm(itok):
                        return itok

    return None


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


# Extensionless filenames permitted under a directory allowlist entry.
# Build-control names (makefile, gnumakefile) are intentionally excluded — they must be
# declared as explicit file pins to prevent undeclared command-execution injection.
_ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES: frozenset[str] = frozenset({
    "readme", "license", "changelog", "authors", "install", "notice", "copying",
})

# True compiler byproducts — created directly by the compiler, never via guarded-apply-patch.
# Terminal validation may accept these under a directory allowlist without gate provenance.
# (All other extension-allowlisted files are written through guarded-apply-patch and must
# therefore appear in gate_changed_paths to pass terminal validation.)
_COMPILER_BYPRODUCT_EXTENSIONS: frozenset[str] = frozenset({".mod", ".o", ".a"})

# Allowlist of extensions permitted under a directory allowlist entry via file tools
# (Edit/Write/guarded-apply-patch). Restricted to source code only.
#
# Excluded (must use explicit file pins):
#   - Build control files (.mk, .cmake, .toml, .cfg, .ini, .nml) — can alter downstream
#     build behaviour or inject arbitrary commands via CMakeLists.txt / Makefile fragments.
#   - Structured data/documents (.json, .yaml, .xml, .csv, .md, .txt, etc.) — undeclared
#     data injection is unauditable and can poison downstream steps.
#   - Compiler byproducts (.mod, .o, .a) — created directly by the compiler as subprocess
#     output, never via Edit/Write. File-tool writes of these extensions are blocked here;
#     terminal validation also rejects them unless they have gate provenance — agents must
#     clean up build artefacts before record-agent-run.
#
# Extensionless files are gated by _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES.
# Everything else is rejected (fail-closed).
_ALLOWED_BYPRODUCT_EXTENSIONS: frozenset[str] = frozenset({
    # Fortran source — primary intended output of the generate step
    ".f90", ".f", ".f95", ".f03", ".f08", ".fpp",
    # C/C++ source — primary intended output of the generate step
    ".c", ".h", ".cpp", ".hpp", ".cc", ".hh", ".cxx", ".inc",
})


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


def _detect_tmpdir_step0_skipped(bash_command: str | None) -> bool:
    """Heuristic: did the agent skip Step 0 (TMPDIR export from allowed_tmp_root)?

    Triggers when the offending Bash command contains either:
      - "${TMPDIR:-..." or "$TMPDIR:-..." parameter-default expansion (proves the
        agent knew TMPDIR might be unset and wrote a fallback inline)
      - hardcoded "/tmp/" or "/dev/shm/" path inside a redirect/heredoc target
    Both indicate Step 0 (`export TMPDIR=$(jq -er '.allowed_tmp_root' ...)`) was not
    executed before the offending write.
    """
    if not bash_command:
        return False
    if "${TMPDIR:-" in bash_command or "$TMPDIR:-" in bash_command:
        return True
    if "/tmp/" in bash_command or "/dev/shm/" in bash_command:
        return True
    return False


def validate_write_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
    tool_name: str | None = None,
    bash_command: str | None = None,
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
    # Directory allowlist entries end with '/'; file entries are exact-match only.
    normalized_allowed_files: set[str] = set()
    normalized_allowed_dirs: list[str] = []
    for p in allowed_paths:
        norm = _normalize_rel_posix(p)
        if p.endswith("/"):
            normalized_allowed_dirs.append(norm)
        else:
            normalized_allowed_files.add(norm)
    path_is_allowed = rel_target_norm in normalized_allowed_files
    if not path_is_allowed and normalized_allowed_dirs:
        under_dir = any(
            rel_target_norm == d or rel_target_norm.startswith(d + "/")
            for d in normalized_allowed_dirs
        )
        if under_dir:
            ext = os.path.splitext(rel_target_norm)[1].lower()
            if ext in _ALLOWED_BYPRODUCT_EXTENSIONS:
                path_is_allowed = True
            elif ext == "" and os.path.basename(rel_target_norm).lower() in _ALLOWED_EXTENSIONLESS_BYPRODUCT_NAMES:
                path_is_allowed = True
    if not path_is_allowed:
        _tmpdir_hint = f"export TMPDIR from output_manifests/{agent_run_id}.json allowed_tmp_root first"
        step0_skipped = _detect_tmpdir_step0_skipped(bash_command)
        fix_hint_block: dict[str, Any] = {
            # NOTE: must NOT suggest `python3 -c "import json; ..."` —
            # that form is itself blocked by forbid_python_inline_write
            # and would put the agent in a recovery loop. `jq -er` with
            # `// empty` causes fail-fast (exit 1) on missing key/file.
            "next_command": (
                f"export TMPDIR=$(jq -er '.allowed_tmp_root // empty' "
                f"\"workspace/orchestrations/{orchestration_id}"
                f"/output_manifests/{agent_run_id}.json\")"
            ),
            "docs_ref": "docs/RUNBOOK.md#hook-recovery",
            "note": _tmpdir_hint,
        }
        if step0_skipped:
            # Strong signal: agent wrote `${TMPDIR:-fallback}` or hardcoded /tmp/
            # → Step 0 (TMPDIR export from allowed_tmp_root) was clearly not run.
            # Prepend a Step 0 reminder so the fix_hint reads as a recovery cheat-sheet
            # specific to this failure mode rather than a generic write hint.
            fix_hint_block["step0_skipped"] = True
            fix_hint_block["note"] = (
                "Step 0 (TMPDIR setup from allowed_tmp_root) was not executed before this "
                "write. Run the next_command BELOW first, then re-issue the original write "
                "under \"$TMPDIR/...\" (no ${TMPDIR:-fallback} syntax, no hardcoded /tmp/). "
                "See skills/workflow-orchestration/references/startup_contract.md Step 0."
            )
            fix_hint_block["canonical_doc"] = (
                "skills/workflow-orchestration/references/startup_contract.md#step-0--tmpdir-セットアップ必須先頭ステップ"
            )
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
                "allowed_tmp_root": manifest.get("allowed_tmp_root", ""),
                "fix_hint": fix_hint_block,
            },
        )
    if tool_name in {"Edit", "Write", "apply_patch", "Bash"} and rel_target_norm not in allowed_file_tool_paths:
        # Bash redirects (`cat > path`, `tee path`, `>>`) leave no gate
        # provenance, so they must satisfy the same constraint as Edit/Write:
        # either be in allowed_file_tool_paths, or go through guarded-apply-patch.
        # This matches the post-hoc record-agent-run integrity check at
        # tools/orchestration_runtime.py:_validate_actual_write_paths, which
        # rejects writes lacking gate provenance unless they appear in
        # manifest_file_tool_paths.
        # L2: include a Bash-specific recovery note on top of the concrete
        # guarded-apply-patch template so operators redirecting via heredoc
        # see both options ("write to $TMPDIR first" vs "go through
        # guarded-apply-patch") rather than only the patch path.
        bash_note = (
            "Bash redirects must target $TMPDIR (allowed_tmp_root). For "
            "canonical paths, stage the content under $TMPDIR/x.patch and "
            "apply via guarded-apply-patch — see fix_hint.next_command."
            if tool_name == "Bash" else None
        )
        fix_hint: dict[str, Any] = {
            "next_command": (
                f"python3 tools/orchestration_runtime.py guarded-apply-patch "
                f"--repo-root . --orchestration-id {orchestration_id} "
                f"--actor-role <role> --agent-run-id {agent_run_id} "
                f"--paths-json '[\"{file_path}\"]' --patch-file ${{TMPDIR}}/x.patch "
                f"--capability-token <token>"
            ),
            "docs_ref": "docs/RUNBOOK.md#hook-recovery",
        }
        if bash_note:
            fix_hint["note"] = bash_note
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"direct write via {tool_name} is forbidden for this target path. "
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
                "allowed_file_tool_paths": list(allowed_file_tool_paths),
                "fix_hint": fix_hint,
            },
        )
    return HookDecision(action=HookDecisionAction.ALLOW)


def _is_auto_read_tolerated(
    repo_root: Path,
    agent_role: str | None,
    file_path: str,
) -> bool:
    """orchestration agent の Claude Code auto-read 対象であれば True を返す。

    Authorization rules (must satisfy ALL):
    - agent_role == "orchestration"
    - lexical path is either:
        (a) exactly repo_root/<rel> for some rel in the explicit allowlist, OR
        (b) exactly <home>/.claude/projects/<this-repo-slug>/memory/MEMORY.md
    - the requested path itself is NOT a symlink (lstat-based check) to prevent
      tolerance from being redirected to arbitrary host files via symlink swap.
    Path comparison is done lexically (no .resolve()) so that an attacker
    cannot bypass via filesystem symlinks pointing at the tolerated path.
    """
    if agent_role != "orchestration":
        return False
    try:
        abs_target = _absolute_lexical(repo_root, file_path)
        repo_root_abs = _absolute_lexical(repo_root, str(repo_root))
    except (OSError, ValueError):
        return False

    if not _path_has_no_symlink_redirect(abs_target):
        return False

    # (a) repo-contained exact lexical match
    try:
        rel = abs_target.relative_to(repo_root_abs)
    except ValueError:
        rel = None
    if rel is not None:
        rel_posix = rel.as_posix()
        return rel_posix in _AUTO_READ_TOLERATED_REPO_RELPATHS

    # (b) project-memory file outside the repo: must lexically equal
    # <home>/.claude/projects/<repo-slug>/memory/MEMORY.md, where <repo-slug>
    # is derived from the current repo_root. This binds tolerance to the
    # current project's slot only — preventing cross-project memory
    # exfiltration.
    try:
        home_abs = Path.home()
    except (OSError, RuntimeError):
        return False
    expected_slug = _claude_project_slug(repo_root_abs)
    expected_path = (
        home_abs
        / _AUTO_READ_PROJECT_MEMORY_PARENT_TAIL
        / expected_slug
        / "memory"
        / "MEMORY.md"
    )
    return abs_target == expected_path


def _absolute_lexical(repo_root: Path, path_token: str) -> Path:
    """Return absolute, lexically-normalized path WITHOUT following symlinks."""
    raw = path_token.strip()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    # os.path.normpath collapses '.', '..' lexically without following symlinks.
    return Path(os.path.normpath(str(candidate)))


def _path_has_no_symlink_redirect(target: Path) -> bool:
    """True iff no segment of `target` is itself a symlink.

    Walks each path component (root → leaf) and lstat's it. A non-existent
    component is fine (no symlink possible). Any S_ISLNK component returns
    False — refusing tolerance whenever the path could be redirected.
    """
    import stat as _stat
    parts = list(target.parts)
    accumulator = Path(parts[0]) if parts else Path("/")
    # On absolute POSIX paths, parts[0] is "/", subsequent parts are segments.
    for part in parts[1:]:
        accumulator = accumulator / part
        try:
            st = os.lstat(str(accumulator))
        except FileNotFoundError:
            # A non-existent intermediate (or leaf) cannot be a symlink target.
            continue
        except OSError:
            return False
        if _stat.S_ISLNK(st.st_mode):
            return False
    return True


def _claude_project_slug(repo_root: Path) -> str:
    """Derive Claude Code's project-directory slug from a repo root.

    Claude Code stores per-project state under ~/.claude/projects/<slug>/, where
    <slug> is the absolute repo path with each '/' replaced by '-'. For example,
    /home/seiya/work/met-dsl → -home-seiya-work-met-dsl.
    """
    abs_str = str(repo_root)
    return abs_str.replace("/", "-")


def _auto_reads_seen_path(repo_root: Path, orchestration_id: str, agent_run_id: str) -> Path:
    return (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "audit"
        / f"{agent_run_id}.auto_reads_seen.json"
    )


def _canonical_auto_read_key(repo_root: Path, file_path: str) -> str:
    """Return a canonical key for the auto-read seen-set.

    Different spellings of the same file (`MEMORY.md`, `./MEMORY.md`, the
    absolute repo path) MUST produce the same key, otherwise the first-read
    invariant can be defeated by re-spelling. We normalize via the same
    `_absolute_lexical` helper used by `_is_auto_read_tolerated` and key by
    the absolute lexical path string.
    """
    try:
        abs_target = _absolute_lexical(repo_root, file_path)
    except (OSError, ValueError):
        # Fall back to a stripped form rather than the raw string so trivial
        # whitespace differences don't multiply keys.
        return file_path.strip()
    return str(abs_target)


_AUTO_READ_STARTUP_WINDOW_SECONDS: int = 120


def _orchestration_started_at(repo_root: Path, orchestration_id: str) -> datetime | None:
    """Return orchestration_meta.json's `started_at` as a tz-aware datetime."""
    meta_path = (
        repo_root
        / "workspace"
        / "orchestrations"
        / orchestration_id
        / "orchestration_meta.json"
    )
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("started_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _record_and_check_first_auto_read(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
) -> bool:
    """Track per-agent first-read of allowlisted auto-read paths.

    Returns True iff this read should be classified as a benign Claude Code
    startup auto-read.  TWO conditions must hold:
    (a) This is the FIRST time `agent_run_id` has read `file_path` (within
        an allowlisted path).  Path identity is determined by
        `_canonical_auto_read_key`, so different spellings collapse to a
        single seen-set entry.
    (b) The read happened within a startup window after orchestration
        `started_at`.  Outside the window, even a first-read is treated as
        prompt-induced (substantive) — the platform's auto-reads should
        complete in the first few seconds, so a much later "first read"
        of MEMORY.md is far more likely to be agent behavior than a
        delayed startup probe.
    """
    # (b) Time-window check — fail-closed: if `started_at` is missing,
    # malformed, or outside the startup window, classify the read as
    # substantive.  Without a verifiable startup signal we cannot prove
    # the read is benign platform behavior, so we must err on the side of
    # surfacing it as a real policy hit.
    started_at = _orchestration_started_at(repo_root, orchestration_id)
    if started_at is None:
        return False
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if elapsed < 0 or elapsed > _AUTO_READ_STARTUP_WINDOW_SECONDS:
        return False

    # (a) First-read check.  We perform a serialized read-modify-write on
    # the seen-set file via fcntl.flock so that concurrent hook invocations
    # (multiple Read tool calls in flight) cannot both classify the same
    # file as "first read" by racing on an empty set.  If we cannot persist
    # the updated set (read-only audit dir, ENOSPC, etc.) we fail-CLOSED:
    # without a durable record of "seen," we cannot honor the first-read
    # invariant on the next call, so we refuse benign classification now
    # rather than risk hiding a real policy hit on subsequent reads.
    if _fcntl is None:
        # Non-POSIX (Windows): no portable file lock available — fail-closed.
        return False
    state_path = _auto_reads_seen_path(repo_root, orchestration_id, agent_run_id)
    canonical_key = _canonical_auto_read_key(repo_root, file_path)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False  # cannot establish persistent state → fail-closed
    try:
        # O_RDWR | O_CREAT — open existing or create empty; flock then
        # truncate-and-write the updated set under exclusive lock.
        fd = os.open(str(state_path), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False  # fail-closed: cannot acquire state file
    try:
        # Acquire the exclusive lock with a bounded retry — a stuck holder
        # (zombie sibling, NFS lock-server hiccup, debugger-paused process)
        # would otherwise hang every subsequent Read hook on this
        # orchestration indefinitely. Retry a small number of times with a
        # short backoff, then fail-closed.
        _LOCK_RETRY_LIMIT = 5
        _LOCK_RETRY_BACKOFF_S = 0.1
        _lock_acquired = False
        for _ in range(_LOCK_RETRY_LIMIT):
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                _lock_acquired = True
                break
            except BlockingIOError:
                time.sleep(_LOCK_RETRY_BACKOFF_S)
            except OSError:
                return False  # locking unavailable → fail-closed
        if not _lock_acquired:
            return False  # persistent contention → fail-closed
        # Read current contents under lock.  Cap at 64 KiB — far above
        # legitimate need (the seen-set holds ≤ a handful of allowlisted
        # paths) and small enough that an oversized file is a clear
        # corruption/attack signal.  Read in a loop until EOF or cap so
        # that no payload below the cap is silently truncated.
        _MAX_SEEN_BYTES = 64 * 1024
        seen: set[str] = set()
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                file_size = os.fstat(fd).st_size
            except OSError:
                file_size = 0
            if file_size > _MAX_SEEN_BYTES:
                # Suspicious / corrupted seen-set — fail-closed; never reset
                # the file silently (would discard legitimate prior entries
                # in the recoverable case, and would aid an attacker in the
                # corruption case).
                return False
            buf = b""
            while len(buf) < _MAX_SEEN_BYTES:
                chunk = os.read(fd, _MAX_SEEN_BYTES - len(buf))
                if not chunk:
                    break
                buf += chunk
            raw = buf.decode("utf-8")
            if raw.strip():
                data = json.loads(raw)
                if isinstance(data, list):
                    seen = {str(x) for x in data if isinstance(x, str)}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            seen = set()
        if canonical_key in seen:
            return False
        seen.add(canonical_key)
        # Truncate and write updated set
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            payload = json.dumps(sorted(seen), ensure_ascii=False).encode("utf-8")
            os.write(fd, payload)
            os.fsync(fd)
        except OSError:
            return False  # write/fsync failure → fail-closed
        return True
    finally:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def validate_read_access(
    repo_root: Path,
    orchestration_id: str,
    agent_run_id: str,
    file_path: str,
    agent_role: str | None = None,
) -> HookDecision:
    """read manifest の allowed_read_roots に対して read 対象を検証する。"""
    if _is_auto_read_tolerated(repo_root, agent_role, file_path):
        # Keep the read-trust boundary intact: persistent state files
        # (MEMORY.md, README.md, ~/.claude/projects/.../memory/MEMORY.md) must
        # NOT enter the orchestration agent's context, even though Claude Code
        # auto-issues these reads at session start.
        #
        # Only the FIRST read of each allowlisted path by this agent is
        # classified as benign platform noise (`auto_read_expected_block`).
        # Subsequent reads of the same path indicate a prompt-induced
        # post-startup access and fall through to the normal substantive
        # policy, where they show up in audit as real read_manifest_read_guard
        # violations rather than benign noise.
        if _record_and_check_first_auto_read(
            repo_root, orchestration_id, agent_run_id, file_path
        ):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    f"blocked (expected auto-read): {file_path!r} is a Claude Code "
                    "auto-read path that must not enter orchestration context. "
                    "This block is harmless platform behavior; ignore in retry logic."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "auto_read_expected_block",
                    "file_path": file_path,
                    "agent_role": agent_role,
                    "agent_run_id": agent_run_id,
                    "orchestration_id": orchestration_id,
                },
            )
        # Fall through to the substantive read-manifest path below — repeated
        # reads of the same allowlisted file are not classified as benign.
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
            "fix_hint": {
                "next_command": (
                    f"python3 tools/orchestration_runtime.py run-gate "
                    f"--gate orchestration_read --agent-run-id {agent_run_id} "
                    f"--capability-token <token> --args-json '{{\"read_path\":\"{file_path}\"}}'"
                ),
                "docs_ref": "docs/RUNBOOK.md#hook-recovery",
            },
        },
    )
