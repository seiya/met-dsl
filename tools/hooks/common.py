#!/usr/bin/env python3
"""Backend-agnostic hook contracts and validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import ast
from datetime import datetime, timezone
from enum import Enum
import fnmatch
import glob
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
    "Hint: write every artifact (any extension, including managed .json/.txt) "
    "directly with the Edit/Write tool, to a path listed in "
    "output_manifests/<agent_run_id>.json.allowed_file_tool_paths "
    "(guarded-apply-patch is deprecated). The MCP-owned mcp_command_log.jsonl is "
    "written only by the build-runtime MCP server and is never file-tool-writable. "
    "For temp files, write directly under the literal allowed_tmp_root path "
    "(workspace/tmp/<agent_run_id>/...); do NOT use `export TMPDIR=...`, "
    "`jq -er ...`, or any bootstrap Bash (Claude Code session sandbox approval "
    "would stall the workflow). See docs/AGENT_CONTRACT.md "
    "for the tmp-area contract."
)

# Repo-relative paths that orchestration agent auto-reads at startup (Claude Code behavior).
# These reads are expected and harmless; silently allow them rather than block.
# Authorization is by exact repo-relative path match (NOT suffix match) to prevent
# absolute-path bypasses like /etc/README.md.
# Scope: orchestration agent only. substep agent has narrower allowed roots and
# must not Read these files.
_AUTO_READ_TOLERATED_REPO_RELPATHS: frozenset[str] = frozenset({
    "MEMORY.md",
    "README.md",
    "TODO.md",
    "CLAUDE.md",
})

# Repo-relative paths and prefixes that the Claude Code harness auto-reads at
# startup regardless of agent role (e.g. MCP discovery, settings parsing).
# These reads happen before any agent prompt runs, so the agent cannot avoid
# them. Allow for ALL agent roles (orchestration + step/substep) when the path
# matches lexically. Authorization rules mirror the orchestration set:
# - exact repo-relative match for the RELPATHS set, OR
# - exact-prefix lexical match for the PREFIXES set, where prefix MUST end with
#   "/" so it cannot extend across path components (no suffix bypass).
_HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS: frozenset[str] = frozenset({
    ".claude/settings.json",
    # Claude Code's harness auto-reads project config files at startup regardless
    # of the configured backend; `.cursor/mcp.json` is still probed when present in
    # the checkout, so it stays tolerated even though the cursor backend is gone.
    ".cursor/mcp.json",
    "mcp_servers/README.md",
    "mcp_servers/mcp_servers.example.json",
})
_HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES: frozenset[str] = frozenset({
    "mcp_servers/tools/",
})

# Project-memory file lives outside the repo root under the user's Claude Code state directory.
# We allow it ONLY when the resolved path is inside ~/.claude/projects/ AND ends with
# the canonical "/memory/MEMORY.md" relative tail.
_AUTO_READ_PROJECT_MEMORY_PARENT_TAIL: str = ".claude/projects"
_AUTO_READ_PROJECT_MEMORY_FILE_TAIL: str = "memory/MEMORY.md"

# Claude Code persisted tool-results are written when a tool-result payload exceeds the
# inline size limit.  The payload is saved to:
#   ~/.claude/projects/<repo-slug>/<session-id>/tool-results/<id>.txt
# Agents encounter the `<persisted-output>` wrapper in their context and attempt to Read
# the file to access the full content.  Since these files are written by the Claude Code
# harness (not by the agent), they are never in any agent's read_manifest, causing
# read_manifest_read_guard to fire as a false-positive audit noise entry.
# We quiet-handle these reads for ALL agent roles (not just orchestration), bound to the
# current project's slug to prevent cross-project exfiltration.
_AUTO_READ_PROJECT_TOOL_RESULTS_PARENT_TAIL: str = ".claude/projects"
_AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT: str = "tool-results"

MANIFEST_HINT = (
    "Hint: Ensure record-launch generated the manifest for this agent_run_id and that the manifest "
    "JSON structure is valid."
)


def format_block_reason_with_hint(decision: "HookDecision") -> str:
    """Append audit_detail.fix_hint to a BLOCK reason.

    Adapters log audit_detail for forensics, but agents only see the `reason`
    string in the rejection message. Surface the structured fix_hint inline so
    the agent can act on it without consulting the audit log.

    Supported fix_hint fields: next_command (a runnable command), write_under
    (a literal path prefix), docs_ref (doc anchor), note (free text).
    """
    base = decision.reason or "blocked by policy"
    audit = decision.audit_detail or {}
    fix_hint = audit.get("fix_hint") if isinstance(audit, dict) else None
    if not isinstance(fix_hint, dict):
        return base
    next_command = fix_hint.get("next_command")
    write_under = fix_hint.get("write_under")
    docs_ref = fix_hint.get("docs_ref")
    note = fix_hint.get("note")
    appended: list[str] = []
    if isinstance(next_command, str) and next_command.strip():
        appended.append(f"Fix: {next_command.strip()}")
    if isinstance(write_under, str) and write_under.strip():
        appended.append(f"Write under: {write_under.strip()}")
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
    ALLOW_AUTO_APPROVE = "allow_auto_approve"
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
        "compile": frozenset({"compile", "full"}),
        "generate": frozenset({"post_generate", "post_build", "full"}),
        "build": frozenset({"post_build", "full"}),
        "validate": frozenset({"post_execute", "pre_judge", "full"}),
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


# --- Pipe-tail inline-Python AST allowlist ---------------------------------
# Modules a read-only stdin-parsing snippet may legitimately import.  Anything
# capable of file I/O, subprocess, networking, or dynamic import is excluded.
_PIPE_TAIL_ALLOWED_IMPORT_ROOTS: frozenset[str] = frozenset({
    # NB: `string` is intentionally NOT allowed — string.Formatter().get_field()
    # resolves a string-literal attribute path to a LIVE object (e.g.
    # "0.__class__.__bases__[0].__subclasses__"), an RCE primitive the AST
    # inspector cannot see because the dunder chain lives inside a string.
    "json", "sys", "re", "csv", "math", "collections",
    "itertools", "functools", "decimal", "fractions", "statistics",
    "textwrap", "datetime", "unicodedata", "hashlib", "base64", "html",
})
# Builtins that enable dynamic code execution, attribute reflection, or file
# access.  A call to any of these in the body forces a block.
_PIPE_TAIL_DANGEROUS_CALLS: frozenset[str] = frozenset({
    "eval", "exec", "compile", "__import__", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "open", "input",
    "breakpoint", "memoryview", "exit", "quit", "help",
})
# Bare names that, if referenced, signal a sandbox-escape attempt even when the
# corresponding import was rejected (e.g. relying on an ambient global).
_PIPE_TAIL_DANGEROUS_NAMES: frozenset[str] = frozenset({
    "os", "subprocess", "socket", "shutil", "pathlib", "importlib",
    "ctypes", "builtins", "multiprocessing", "threading", "signal",
    "pty", "fcntl", "mmap", "resource", "platform", "sysconfig",
    "code", "codeop", "runpy", "pickle", "marshal", "gc", "inspect",
    # string.Formatter / operator.attrgetter etc. resolve attribute paths from
    # string literals, defeating AST attribute inspection — block the names too.
    "Formatter", "attrgetter", "methodcaller", "itemgetter",
})
# Non-dunder attributes that are dangerous on an otherwise-allowed module
# (notably `sys.modules`, which reaches every imported module including os;
# and Formatter.get_field, which returns a live object for a string field path).
_PIPE_TAIL_DANGEROUS_ATTRS: frozenset[str] = frozenset({
    "modules", "system", "popen", "spawn", "spawnv", "fork", "execv",
    "execve", "execl", "load_module", "import_module", "find_module",
    "get_field", "get_value", "vformat", "format_field", "convert_field",
})

# Leaf-attribute ALLOWLIST for pipe-tail `-c` bodies.  A blocklist is unsound:
# allowed modules RE-EXPORT other modules (and builtins) as plain non-dunder
# attributes — e.g. `json.codecs.builtins.open`, `statistics.random._os.environ`,
# `re.enum` — reaching arbitrary sinks via ordinary ast.Attribute chains with no
# dunder, no dangerous Name, and no `__` string literal.  So attribute access is
# DENY-BY-DEFAULT: only these well-known data/parse method+attribute names are
# permitted.  Module re-export names (codecs/enum/random/operator/builtins/_os…)
# are absent → the traversal to any dangerous module is severed.
_PIPE_TAIL_ALLOWED_ATTRS: frozenset[str] = frozenset({
    # streams (read-only stdin parsing; .write only reachable on stdout/stderr
    # since `open` is blocked as both Name and attribute)
    "stdin", "stdout", "stderr", "read", "readline", "readlines", "buffer",
    "flush", "write", "writelines", "argv", "maxsize", "byteorder",
    # str / bytes methods
    "strip", "lstrip", "rstrip", "split", "rsplit", "splitlines", "join",
    "replace", "lower", "upper", "casefold", "title", "capitalize",
    "swapcase", "startswith", "endswith", "find", "rfind", "index", "rindex",
    "count", "encode", "decode", "format", "format_map", "zfill", "ljust",
    "rjust", "center", "expandtabs", "partition", "rpartition", "translate",
    "maketrans", "isdigit", "isalpha", "isalnum", "isspace", "isupper",
    "islower", "isnumeric", "isdecimal", "isidentifier", "removeprefix",
    "removesuffix", "hex", "bit_length", "to_bytes", "from_bytes",
    # dict / list / set methods
    "get", "keys", "values", "items", "setdefault", "update", "pop",
    "popitem", "append", "extend", "insert", "remove", "add", "discard",
    "sort", "reverse", "copy", "clear", "fromkeys", "union", "intersection",
    "difference", "issubset", "issuperset", "most_common", "elements",
    "subtract", "total",
    # json
    "loads", "load", "dumps", "dump", "JSONDecodeError",
    # re
    "findall", "match", "search", "fullmatch", "finditer", "sub", "subn",
    "compile", "escape", "group", "groups", "groupdict", "start", "end",
    "span", "expand", "purge", "flags", "pattern",
    "I", "M", "S", "X", "A", "L", "U", "IGNORECASE", "MULTILINE", "DOTALL",
    "VERBOSE", "ASCII", "LOCALE", "UNICODE",
    # csv
    "reader", "writer", "DictReader", "DictWriter", "field_size_limit",
    "excel", "unix_dialect", "register_dialect", "fieldnames",
    "QUOTE_MINIMAL", "QUOTE_ALL", "QUOTE_NONNUMERIC", "QUOTE_NONE",
    # base64 / hashlib
    "b64decode", "b64encode", "b16decode", "b16encode", "b32decode",
    "b32encode", "urlsafe_b64decode", "urlsafe_b64encode",
    "standard_b64decode", "standard_b64encode", "decodebytes", "encodebytes",
    "md5", "sha1", "sha256", "sha512", "sha224", "sha384", "new",
    "hexdigest", "digest", "blake2b", "blake2s",
    # math / statistics / decimal / fractions
    "pi", "e", "tau", "inf", "nan", "sqrt", "floor", "ceil", "trunc", "log",
    "log2", "log10", "exp", "fabs", "factorial", "gcd", "lcm", "isclose",
    "isnan", "isinf", "isfinite", "sin", "cos", "tan", "atan", "atan2",
    "hypot", "degrees", "radians", "mean", "median", "mode", "stdev",
    "variance", "fmean", "fsum", "prod", "comb", "perm",
    "Decimal", "Fraction", "quantize", "numerator", "denominator",
    "as_integer_ratio", "real", "imag", "conjugate",
    # datetime
    "datetime", "date", "time", "timedelta", "timezone", "now", "today",
    "utcnow", "fromisoformat", "fromtimestamp", "utcfromtimestamp",
    "strftime", "strptime", "isoformat", "year", "month", "day", "hour",
    "minute", "second", "microsecond", "weekday", "isoweekday", "timestamp",
    "astimezone", "combine", "utctimetuple", "days", "seconds",
    "total_seconds", "utc",
    # itertools / functools
    "chain", "islice", "cycle", "product", "permutations", "combinations",
    "combinations_with_replacement", "groupby", "accumulate", "starmap",
    "takewhile", "dropwhile", "tee", "zip_longest", "filterfalse", "compress",
    "from_iterable", "reduce", "partial", "lru_cache", "cmp_to_key", "wraps",
    # collections
    "OrderedDict", "defaultdict", "Counter", "deque", "namedtuple",
    "ChainMap", "appendleft", "popleft", "rotate", "maxlen",
    # unicodedata / textwrap
    "normalize", "name", "category", "numeric", "digit", "bidirectional",
    "wrap", "fill", "dedent", "indent", "shorten",
})


def _command_reads_operator_secret(
    command: str,
    cmd_tokens: list[str],
    repo_root: Path,
    met_dsl_root: Path,
) -> bool:
    """True if a Bash command appears to read anything under ~/.met-dsl/.

    Operator tokens live under ~/.met-dsl/.  This guard is NOT gated on the
    command name (the prior version only fired for cat/head/etc., letting
    `od`, `xxd`, `cut`, `read X < ...`, and `x=$(cat ...)` slip through).  Two
    complementary checks:
      (1) a raw-command marker regex catching ~ / $HOME / ${HOME} / <abs-home>
          spellings even when adjacent shell punctuation mangles tokenization;
      (2) per-token path resolution catching `..` traversal and symlinks
          (.resolve() normalizes both) regardless of the leading command.
    """
    home = str(Path.home())
    marker_re = re.compile(
        r"(?:~|\$HOME|\$\{HOME\}|" + re.escape(home) + r")/\.met-dsl(?:/|\b)"
    )
    if marker_re.search(command):
        return True
    # Also test a quote/backslash-collapsed copy of the whole command: shlex
    # normally removes embedded quotes (`~/.met-d''sl`) and escapes (`~/\.met-dsl`),
    # but on a shlex parse failure evaluate_common_policy falls back to
    # command.split(), which does NOT — so collapse them here too (mirrors
    # _command_invokes_dismiss_violation).
    collapsed_cmd = re.sub(r"""['"\\]""", "", command)
    if collapsed_cmd != command and marker_re.search(collapsed_cmd):
        return True
    candidate_tokens = list(cmd_tokens)
    if collapsed_cmd != command:
        candidate_tokens += collapsed_cmd.split()
    for tok in candidate_tokens:
        # Strip shell punctuation that can wrap a path token (redirects,
        # substitution parens, quotes) but keep `$` (expandvars), glob
        # metacharacters `[` `]` `*` `?`, and braces `{` `}` (brace expansion,
        # all handled explicitly below).
        t = tok.strip().strip("<>();|&\"'`")
        if not t:
            continue
        # Brace expansion (`~/.met-{dsl,x}/...`, `{k..m}`, nested) happens in the
        # shell before the path exists; expanduser/glob never see it.  Expand to
        # the cartesian product and test every variant precisely.
        for variant in _brace_expand(t):
            # If braces REMAIN (bounded-out >8 groups, or malformed), fall back
            # to the fail-closed `{...}`→`*` glob catch-all for this variant.
            # (Precise variants skip this, so legit `~/.{config,local}` reads are
            # not over-blocked.)
            if "{" in variant:
                _bg = os.path.expanduser(os.path.expandvars(_braces_to_glob(variant)))
                # Cheap lexical check FIRST (no filesystem touch), then a bounded
                # real-glob — never an unbounded glob.glob on attacker patterns.
                if _glob_pattern_targets_root(_bg, met_dsl_root):
                    return True
                if _glob_targets_secret_bounded(_bg, met_dsl_root):
                    return True
                continue
            expanded = os.path.expanduser(os.path.expandvars(variant))
            # Glob metacharacters (`*?[`) are expanded by the shell at runtime;
            # a literal .resolve() would keep them and miss the match.  e.g.
            # `cat ~/.met-d*/operator_tokens/x.txt` reads the real token.
            if any(ch in expanded for ch in "*?["):
                # Cheap lexical fnmatch FIRST: does the glob pattern target the
                # .met-dsl directory components?  This catches `~/*/*/*` shapes
                # (a `*` at the .met-dsl depth fnmatches it) WITHOUT walking the
                # filesystem — the prior ordering ran glob.glob first and hung
                # the synchronous hook on `~/*/*/*/x`.
                if _glob_pattern_targets_root(expanded, met_dsl_root):
                    return True
                # Then a BOUNDED real-glob for symlink redirection the lexical
                # check can't see (≤1 wildcard component → cheap).
                if _glob_targets_secret_bounded(expanded, met_dsl_root):
                    return True
                continue
            try:
                    p = (
                        Path(expanded).resolve()
                        if os.path.isabs(expanded)
                        else (repo_root / expanded).resolve()
                    )
            except (OSError, ValueError, RuntimeError):
                continue
            if _is_path_under_root(p, met_dsl_root):
                return True
    return False


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are NOT inside a nested `{...}` group."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "{":
            depth += 1
            cur.append(ch)
        elif ch == "}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


_BRACE_SEQUENCE_MAX_SPAN = 1024


def _expand_sequence(spec: str) -> list[str] | None:
    """Bash sequence expansion `{a..z}` / `{0..9}` / `{lo..hi..step}` -> list.

    Returns None for anything not a recognized 2- or 3-part sequence, OR for a
    span larger than _BRACE_SEQUENCE_MAX_SPAN — the caller preserves the braces
    in that case so the fail-closed `{...}`→`*` glob fallback fires.  Bounding
    here is essential: a single `{0..100000000}` has only one `{` (so the
    8-group cap does not apply) and would otherwise materialize 100M strings
    (multi-GB, ~13s) inside this synchronous hook before the product-loop cap.
    """
    m = re.fullmatch(r"([A-Za-z0-9]+)\.\.([A-Za-z0-9]+)(?:\.\.(-?\d+))?", spec)
    if not m:
        return None
    a, b, step_s = m.group(1), m.group(2), m.group(3)
    step = abs(int(step_s)) if step_s else 1
    if step == 0:
        step = 1
    if a.isdigit() and b.isdigit():
        lo, hi = int(a), int(b)
    elif len(a) == 1 and len(b) == 1 and a.isalpha() and b.isalpha():
        lo, hi = ord(a), ord(b)
    else:
        return None
    if (abs(hi - lo) // step) + 1 > _BRACE_SEQUENCE_MAX_SPAN:
        return None
    rng = range(lo, hi + 1, step) if lo <= hi else range(lo, hi - 1, -step)
    if a.isdigit():
        return [str(n) for n in rng]
    return [chr(n) for n in rng]


def _brace_expand(s: str) -> list[str]:
    """Bash brace expansion: comma groups `{x,y}`, sequences `{k..m}`, and
    nested groups `{a,{b,c}}` — cartesian product, balanced-brace aware.

    Bounded to avoid exponential blowup in this synchronous PreToolUse hook:
    more than 8 brace groups, or more than 256 results, → stop expanding (the
    `_braces_to_glob` fail-closed fallback in the caller still blocks anything
    that lexically targets the secret root).
    """
    if s.count("{") > 8:
        return [s]
    # Find the first balanced top-level {...} group.
    depth = 0
    start = -1
    for idx, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                inner = s[start + 1 : idx]
                pre, post = s[:start], s[idx + 1 :]
                parts = _split_top_commas(inner)
                out: list[str] = []
                if len(parts) == 1:
                    seq = _expand_sequence(parts[0])
                    if seq is None:
                        # Not a comma list and not a recognized/bounded sequence
                        # (e.g. an unparsed step form, an oversized range, or a
                        # literal `{x}` bash would leave alone).  PRESERVE the
                        # braces — do NOT substitute literally — so the caller's
                        # `{`-present `_braces_to_glob` fallback still fires.
                        # (Substituting literally would drop the `{`, skip the
                        # fallback, and let `~/.met-ds{k..m..1}/x` through.)
                        for tail in _brace_expand(post):
                            out.append(pre + "{" + inner + "}" + tail)
                            if len(out) > 256:
                                return out
                        return out
                    options = seq
                else:
                    options = parts
                for opt in options:
                    for sub in _brace_expand(opt):
                        for tail in _brace_expand(post):
                            out.append(pre + sub + tail)
                            if len(out) > 256:
                                return out
                return out
    return [s]


def _braces_to_glob(s: str) -> str:
    """Replace every `{...}` run with `*` (innermost-first, repeatedly).

    Fail-closed catch-all for ANY brace form — comma groups, sequence
    expansion `{k..m}`, and nested braces — without emulating bash exactly.
    e.g. `~/.met-ds{k..m}/x` -> `~/.met-ds*/x`, `~/.{met-{dsl,x},y}/z` -> `~/.*/z`.
    The result is then matched as a glob pattern against the secret root.
    """
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\{[^{}]*\}", "*", s)
    return s


def _glob_pattern_targets_root(pattern: str, root: Path) -> bool:
    """True if an absolute glob `pattern` could match a path under `root`.

    Component-wise fnmatch: each literal component of `root` must be matched by
    the corresponding glob component of `pattern` (e.g. `.met-d*` / `.m?t-dsl` /
    `.[m]et-dsl` all fnmatch the literal `.met-dsl`).  Used to fail-closed on
    globbed operator-secret reads even when the file does not yet exist.
    """
    pat = Path(pattern)
    if not pat.is_absolute():
        return False
    pat_parts = pat.parts
    root_parts = root.parts
    if len(pat_parts) < len(root_parts):
        return False
    return all(
        fnmatch.fnmatch(rp, pp) for pp, rp in zip(pat_parts, root_parts)
    )


def _glob_targets_secret_bounded(pattern: str, root: Path) -> bool:
    """Real `glob.glob` (catches symlink redirection the lexical check misses),
    BOUNDED to avoid a synchronous-hook DoS.

    A pattern with multiple wildcard path components (`~/*/*/*/x`) makes
    glob.glob recursively scandir the entire $HOME subtree (multi-second hang).
    Such patterns already lexically target the secret root (a `*` at the
    .met-dsl depth fnmatches it) and are caught by `_glob_pattern_targets_root`
    BEFORE this is called — so here we only run glob when at most ONE component
    carries a wildcard, keeping the filesystem walk cheap.
    """
    if sum(1 for comp in pattern.split(os.sep) if any(c in comp for c in "*?[")) > 1:
        return False
    try:
        for match in glob.glob(pattern):
            if _is_path_under_root(Path(match).resolve(), root):
                return True
    except (OSError, ValueError):
        pass
    return False


_DISMISS_VIOLATION_TOKEN = "dismiss-violation"


def _command_invokes_dismiss_violation(command: str, cmd_tokens: list[str]) -> bool:
    """True if a Bash command invokes the operator-only `dismiss-violation`.

    A raw `\\bdismiss-violation\\b` regex is evaded by shell reassembly the
    runtime ultimately sees as one argv token.  This is best-effort hardening
    against the common forms — quote-splitting (`dismiss-vio""lation`),
    backslash-splitting (`dismiss-vi\\olation`), `$VAR`/`${VAR}` indirection,
    and `${VAR//from/to}` pattern substitution (`V=dismiss_violation;
    ${V//_/-}`).  Fully general shell reassembly (command substitution,
    arrays, `eval`, IFS tricks) is undecidable here; the AUTHORITATIVE gate is
    the operator token required by `dismiss_violation` itself.
    """
    if any(tok == _DISMISS_VIOLATION_TOKEN for tok in cmd_tokens):
        return True
    assigns: dict[str, str] = {}
    for m in re.finditer(
        r"(?:^|[;&|]|\s)\s*([A-Za-z_][A-Za-z0-9_]*)=([^\s;&|]+)", command
    ):
        assigns[m.group(1)] = m.group(2)

    # Bash pattern substitution `${NAME//from/to}` (all) / `${NAME/from/to}`
    # (first).  Apply BEFORE the simple `$NAME` pass so the simple regex does
    # not partially consume `${V` and leave `//_/-}` behind.
    def _pat_sub(m: "re.Match[str]") -> str:
        name, flag, frm, to = m.group(1), m.group(2), m.group(3), m.group(4)
        val = assigns.get(name)
        if val is None:
            return m.group(0)
        return val.replace(frm, to) if flag == "//" else val.replace(frm, to, 1)

    resolved = re.sub(
        r"\$\{([A-Za-z_]\w*)(//|/)([^/}]*)/([^}]*)\}", _pat_sub, command
    )

    # Bash case modification `${NAME,,}` (lower-all) / `${NAME^^}` (upper-all) /
    # `${NAME,}` / `${NAME^}` (first char).  Apply BEFORE the simple `$NAME` pass.
    def _case_sub(m: "re.Match[str]") -> str:
        name, op = m.group(1), m.group(2)
        val = assigns.get(name)
        if val is None:
            return m.group(0)
        if op == ",,":
            return val.lower()
        if op == "^^":
            return val.upper()
        if op == ",":
            return val[:1].lower() + val[1:]
        return val[:1].upper() + val[1:]

    resolved = re.sub(
        r"\$\{([A-Za-z_]\w*)(,,|\^\^|,|\^)\}", _case_sub, resolved
    )
    for name, val in assigns.items():
        resolved = re.sub(r"\$\{?" + re.escape(name) + r"\}?", val, resolved)
    # Case-fold the collapsed string so case-mangled spellings still match the
    # (lowercase) dismiss-violation token.
    collapsed = re.sub(r"""['"\\]""", "", resolved).lower()
    return _DISMISS_VIOLATION_TOKEN in collapsed


def _pipe_tail_body_is_safe(body: str) -> bool:
    """Return True only when an inline `-c` body is a read-only stdin parser.

    Allowlist AST validation (replaces the prior substring blocklist, which was
    trivially defeated by `__import__("os").system(...)`, `exec(input())`,
    `__builtins__.__dict__["open"]`, etc.).  Fail-closed on any parse error.

    Reflection-via-string-literal is also blocked: any string CONSTANT containing
    `__` is rejected, because attribute paths embedded in strings (the
    `string.Formatter().get_field("0.__class__.__bases__...")` /
    `operator.attrgetter("__globals__")` family) reach a live object without ever
    producing an ast.Attribute node the walker can see.  Combined with dropping
    `string` from the import allowlist and blocking the Formatter/attrgetter
    names+methods, this closes the format-string RCE.
    """
    try:
        tree = ast.parse(body, mode="exec")
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _PIPE_TAIL_ALLOWED_IMPORT_ROOTS:
                    return False
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in _PIPE_TAIL_ALLOWED_IMPORT_ROOTS:
                return False
        elif isinstance(node, ast.Attribute):
            # Any dunder attribute (e.g. __class__, __globals__, __dict__,
            # __subclasses__) is an introspection-escape vector.
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return False
            if node.attr in _PIPE_TAIL_DANGEROUS_ATTRS:
                return False
            # DENY-BY-DEFAULT: allowed modules re-export builtins/os/operator as
            # plain attributes (json.codecs.builtins.open, statistics.random._os),
            # so only an explicit leaf-attribute allowlist is sound.
            if node.attr not in _PIPE_TAIL_ALLOWED_ATTRS:
                return False
        elif isinstance(node, ast.Name):
            # Reject dangerous builtins/modules whenever referenced as a bare
            # Name — NOT only as a Call callee.  `e=eval; e(stdin)`,
            # `w=open; w(p,"w")`, `g=getattr; g(obj, name)` alias the builtin
            # through a local, so a Call-callee-only check (the prior bug) let
            # them through.  Every Name node is inspected here.
            if (
                node.id in _PIPE_TAIL_DANGEROUS_NAMES
                or node.id in _PIPE_TAIL_DANGEROUS_CALLS
            ):
                return False
            if node.id.startswith("__") and node.id.endswith("__"):
                return False
        elif isinstance(node, ast.Constant):
            # Reject attribute paths smuggled inside string literals.
            if isinstance(node.value, str) and "__" in node.value:
                return False
    return True


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
    cli_help_audit: dict[str, Any] | None = None
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
        repo_root_raw = hook_input.payload.get("repo_root")
        repo_root = (
            Path(repo_root_raw).resolve()
            if isinstance(repo_root_raw, str) and repo_root_raw.strip()
            else Path.cwd()
        )
        # ~/.met-dsl/ holds operator-only secrets (dismiss-violation tokens).
        # NOT gated on the command name: any command that reads under ~/.met-dsl/
        # (cat, od, xxd, cut, `read X < ...`, `x=$(cat ...)`, ..-traversal, etc.)
        # is blocked.  The Read tool already excludes it (not in
        # allowed_read_roots); this closes the Bash path.
        met_dsl_root = (Path.home() / ".met-dsl").resolve()
        if _command_reads_operator_secret(command, cmd_tokens, repo_root, met_dsl_root):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "blocked: direct read from ~/.met-dsl/ via Bash is forbidden in "
                    "workflow mode. Operator tokens live there and must not enter "
                    "agent context; dismiss-violation is an operator-only action."
                ),
                continue_processing=False,
                audit_detail={"policy": "forbid_operator_secret_direct_read", "command": command},
            )
        if first_cmd in bash_read_cmds:
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
        cli_help_audit = _detect_cli_help_invocation(cmd_tokens, command)
        if "python" in lowered:
            # Inline Python policy (fail-closed with one narrow exception):
            #
            # DEFAULT: ALL standalone `python3 -c` snippets and `python3 - <<EOF`
            # heredocs are blocked.  Regex-based write detection is fundamentally
            # unreliable — alias bypasses like `from pathlib import Path as P;
            # P('x').write_text(...)` or `Path('x').open('w').write(...)` would
            # slip through any finite pattern set.  Agents that need to run Python
            # should use a real script file (`python3 script.py`), which goes
            # through normal write/read manifest validation.
            #
            # EXCEPTION: `... | python3 -c '...'` pipe-tail read-only invocations
            # are allowed when the `-c` body contains no file-write patterns.
            # Pipe-tail means stdin is consumed from the previous stage (not from
            # a file argument), which substantially limits the attack surface.
            # The `-c` body is scanned for write-API patterns; if none are found
            # the invocation is permitted.  Risk: alias-based write bypasses
            # remain theoretically possible; this is a conscious trade-off
            # accepted by the project operator (see plan pure-wibbling-oasis).
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
                # heredoc form: `python3 - <<EOF` (still detected via regex
                # because heredoc syntax is not a single token) — always blocked.
                if re.search(r"""python3?\s+-\s*<<""", command):
                    _py_inline_blocked = True
                    _py_inline_reason = (
                        "python - <<EOF heredoc inline execution is forbidden in workflow mode"
                    )
                # `-c` form — check for the pipe-tail read-only exception first.
                elif "-c" in tokens_for_python:
                    # --- Pipe-tail read-only exception ---
                    # A `... | python3 -c '...'` invocation where python reads
                    # only from stdin is much lower risk than a standalone `-c`
                    # call.  Allow it only if ALL of the following hold:
                    #   (1) there is exactly ONE python3 -c in the entire command
                    #       (mixed commands like `... | python3 -c '...'; python3
                    #       -c 'open(...)'` are rejected in full — the first
                    #       invocation's pipe-tail status must not whitelist a
                    #       second standalone invocation in the same string), AND
                    #   (2) that single python3 -c is immediately preceded by a
                    #       `|` separator in the command string, AND
                    #   (3) the `-c` body contains no recognized file-write API
                    #       calls (open-for-write, write_text, shutil.copy, etc.)
                    # python[0-9.]* — match ANY interpreter version (python,
                    # python2, python3, python3.11), not just python3.  Otherwise
                    # a benign `... | python3 -c '<safe>'` coexisting with a
                    # second `; python2 -c '<evil>'` would count as 1 and wrongly
                    # qualify for the pipe-tail exception while python2 runs
                    # unguarded.
                    _total_python_c_count = len(
                        re.findall(r"(?:\S*/)?python[0-9.]*\s+-c\b", command)
                    )
                    _is_pipe_tail = (
                        _total_python_c_count == 1
                        and bool(
                            re.search(
                                # (?<!\|)\|(?!\|) — match a SINGLE pipe only.  The
                                # negative lookbehind/lookahead exclude `||` (logical
                                # OR), so `cat x || python3 -c '...'` is NOT treated
                                # as a read-only pipe-tail (it would run python3 even
                                # when the left side fails — not a stdin consumer).
                                # [^|;&\n]* — stop at any shell separator so that
                                # `echo x | cat; python3 -c '...'` (semicoloned
                                # standalone) does NOT match as pipe-tail.
                                # (?:\S*/)? — matches optional path prefix so that
                                # `/usr/bin/python3 -c` is detected the same as
                                # bare `python3 -c`.
                                r"(?<!\|)\|(?!\|)[^|;&\n]*(?:\S*/)?python[0-9.]*\s+-c\b",
                                command,
                            )
                        )
                    )
                    # Extract the inline code body for write-pattern scanning.
                    # Use the -c immediately following python3/python in the token
                    # list, not the first -c overall (which could belong to grep -c
                    # or another command that precedes the pipe).
                    # _c_body_reliable tracks whether we successfully extracted the
                    # actual `-c` body.  If shlex fails to parse (unmatched quote) or
                    # the body token is absent, the write-pattern scan cannot be
                    # trusted — we MUST fail-closed (block) rather than silently
                    # scanning an empty string that matches no write pattern.
                    _c_body = ""
                    _c_body_reliable = False
                    try:
                        import shlex as _shlex_mod
                        _toks_s = _shlex_mod.split(command)
                        _ci = None
                        for _tok_idx in range(len(_toks_s) - 1):
                            # Use basename so that path-qualified interpreters
                            # like /usr/bin/python3 are matched correctly.
                            _base = _toks_s[_tok_idx].split("/")[-1]
                            if (
                                _base in ("python3", "python")
                                and _toks_s[_tok_idx + 1] == "-c"
                            ):
                                _ci = _tok_idx + 1
                                break
                        if _ci is not None and _ci + 1 < len(_toks_s):
                            _c_body = _toks_s[_ci + 1]
                            _c_body_reliable = True
                    except Exception:
                        _c_body = ""
                        _c_body_reliable = False
                    # Validate the body with an allowlist AST check (not a
                    # substring blocklist).  The legitimate pipe-tail use case
                    # (parsing stdin JSON/text) only imports read-only modules
                    # and reads from sys.stdin; anything capable of file I/O,
                    # subprocess, networking, dynamic exec, or attribute-escape
                    # is rejected.  See _pipe_tail_body_is_safe.
                    _c_body_safe = _c_body_reliable and _pipe_tail_body_is_safe(_c_body)
                    if _is_pipe_tail and _c_body_safe:
                        # Pipe-tail + reliably-extracted + AST-allowlisted body
                        # → allow.  Fall through (do not set _py_inline_blocked).
                        pass
                    else:
                        _py_inline_blocked = True
                        if not _is_pipe_tail:
                            _py_inline_reason = (
                                "python -c inline execution is forbidden in workflow mode"
                            )
                        elif not _c_body_reliable:
                            _py_inline_reason = (
                                "python -c pipe-tail body could not be parsed reliably "
                                "(unmatched quote / malformed) — blocked fail-closed"
                            )
                        else:
                            _py_inline_reason = (
                                "python -c pipe-tail body is not a read-only stdin parser "
                                "(disallowed import / call / attribute) — blocked fail-closed"
                            )
            if _py_inline_blocked:
                # Intent classification — uuid / json_read / write (default).
                # The block is unconditional for matched cases, but the recovery
                # hint differs by intent: agents commonly reach for `python -c`
                # to (a) generate a UUID, (b) inspect a JSON file, or (c) write
                # a file.  Pointing them at the canonical alternative eliminates
                # the retry loop.
                intent = "write"
                hint_next = (
                    "Write the artifact with the Edit/Write tool to a path listed in "
                    "output_manifests/<agent_run_id>.json.allowed_file_tool_paths "
                    "(any extension, including managed .json/.txt). Do not write files "
                    "from inline Python."
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
                        "required, write a script to workspace/tmp/<agent_run_id>/x.py "
                        "and run `python3 workspace/tmp/<agent_run_id>/x.py` "
                        "(literal path, no $TMPDIR env reference needed)."
                    )
                return HookDecision(
                    action=HookDecisionAction.BLOCK,
                    reason=(
                        f"blocked: {_py_inline_reason}. "
                        "Inline Python is fail-closed because regex-based "
                        "filtering cannot reliably catch alias/string-literal "
                        "bypasses (exception: pipe-tail with no write patterns). "
                        "Use a real script file (python3 script.py) "
                        "for execution, or tools/audit_orchestration.py for "
                        "log inspection. "
                        "Write artifacts (any extension) with the Edit/Write tool to a "
                        "path in allowed_file_tool_paths. "
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
        # Block dismiss-violation in all workflow sessions regardless of how the
        # runtime is invoked (script path, -m module, or wrapper) and regardless
        # of shell reassembly (quote/backslash splitting, variable indirection).
        # An agent cannot bypass this by using METDSL_WORKFLOW_MODE=0 prefix
        # because the hook reads its OWN os.environ (set by run_workflow.py at
        # session start), not the subprocess env override.
        if _command_invokes_dismiss_violation(command, cmd_tokens):
            return HookDecision(
                action=HookDecisionAction.BLOCK,
                reason=(
                    "blocked: dismiss-violation is an operator-only command and "
                    "cannot be invoked from within a running workflow session. "
                    "Run it from the operator terminal outside the workflow."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "forbid_dismiss_violation_in_workflow",
                    "command": command,
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
                    "/dev/shm reads/writes are not permitted; write under the literal "
                    "allowed_tmp_root path (workspace/tmp/<agent_run_id>/) for temporary files. "
                    "See docs/AGENT_CONTRACT.md."
                ),
                continue_processing=False,
                audit_detail={
                    "policy": "output_manifest_write_guard",
                    "command": command,
                    "destination": offending,
                    "fix_hint": {
                        "write_under": "workspace/tmp/<agent_run_id>/...",
                        "docs_ref": "docs/AGENT_CONTRACT.md",
                    },
                },
            )
    if cli_help_audit is not None:
        return HookDecision(action=HookDecisionAction.ALLOW, audit_detail=cli_help_audit)
    return HookDecision(action=HookDecisionAction.ALLOW)


def _detect_cli_help_invocation(
    cmd_tokens: list[str], command: str
) -> dict[str, Any] | None:
    """Detect `python[3] tools/<name>.py [<sub>] --help` invocations.

    Returns audit_detail dict for ALLOW logging, or None if not a help call.
    `--help` against tools/ is permitted (argparse output only, not implementation
    read). Logging frequency informs Tier-A/Tier-B doc split decisions.
    """
    if "--help" not in cmd_tokens and "-h" not in cmd_tokens:
        return None
    py_idx = next(
        (
            i
            for i, tok in enumerate(cmd_tokens)
            if tok.split("/")[-1].lower().startswith("python")
        ),
        -1,
    )
    if py_idx < 0:
        return None
    script_idx = py_idx + 1
    while script_idx < len(cmd_tokens) and cmd_tokens[script_idx].startswith("-"):
        script_idx += 1
    if script_idx >= len(cmd_tokens):
        return None
    script = cmd_tokens[script_idx]
    if not script.startswith("tools/") or not script.endswith(".py"):
        return None
    subcommand: str | None = None
    if script_idx + 1 < len(cmd_tokens):
        candidate = cmd_tokens[script_idx + 1]
        if not candidate.startswith("-"):
            subcommand = candidate
    return {
        "policy": "cli_help_invocation_observed",
        "tool": script,
        "subcommand": subcommand,
        "command": command,
    }


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

# True compiler byproducts — created directly by the compiler as subprocess output.
# Terminal validation accepts these under a directory allowlist as confined build output.
# (NOTE: the legacy "gate provenance / gate_changed_paths" terminal model is gone —
# Phase-2 authorizes step/substep writes by write_roots-containment of the FS-diff.
# See docs/ORCHESTRATION.md.)
_COMPILER_BYPRODUCT_EXTENSIONS: frozenset[str] = frozenset({".mod", ".o", ".a"})

# Allowlist of extensions permitted under a directory allowlist entry via the
# Edit/Write file tools. Restricted to source code only.
#
# Excluded (must use explicit file pins):
#   - Build control files (.mk, .cmake, .toml, .cfg, .ini, .nml) — can alter downstream
#     build behaviour or inject arbitrary commands via CMakeLists.txt / Makefile fragments.
#   - Structured data/documents (.json, .yaml, .xml, .csv, .md, .txt, etc.) — undeclared
#     data injection is unauditable and can poison downstream steps.
#   - Compiler byproducts (.mod, .o, .a) — created directly by the compiler as subprocess
#     output, never via Edit/Write. File-tool writes of these extensions are blocked here;
#     terminal validation also rejects them unless they land under the step's write_roots —
#     agents must clean up build artefacts before record-agent-run.
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
    """Allow a Read of the relevant child's output / read manifest JSON even outside run-gate."""
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
    """Return a BLOCK HookDecision if it matches a CLI-managed path. None on no match."""
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


def _detect_tmpdir_fallback_or_hardcode(bash_command: str | None) -> bool:
    """Heuristic: did the agent use TMPDIR fallback syntax or hardcoded /tmp paths?

    Triggers when the offending Bash command contains either:
      - "${TMPDIR:-..." or "$TMPDIR:-..." parameter-default expansion (the agent
        wrote a fallback inline instead of using the literal allowed_tmp_root path)
      - hardcoded "/tmp/" or "/dev/shm/" path inside a redirect/heredoc target
    Both indicate the agent should switch to the literal `workspace/tmp/<agent_run_id>/`
    path declared in the manifest's `allowed_tmp_root`.
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
    """Verify the write/edit target against the output manifest's allowed_output_paths."""
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
        allowed_tmp_root_value = manifest.get("allowed_tmp_root", "")
        tmp_root_str = (
            allowed_tmp_root_value.strip()
            if isinstance(allowed_tmp_root_value, str) and allowed_tmp_root_value.strip()
            else f"workspace/tmp/{agent_run_id}"
        )
        used_fallback_or_hardcode = _detect_tmpdir_fallback_or_hardcode(bash_command)
        fix_hint_block: dict[str, Any] = {
            # Recommend the literal allowed_tmp_root path. Do NOT recommend `export TMPDIR=...`
            # or `jq -er ...` — those Bash patterns trigger Claude Code session sandbox approval
            # prompts that can stall the workflow indefinitely. The hook only checks whether the
            # write target sits under allowed_tmp_root and ignores $TMPDIR env, so a literal
            # path works without any shell variable setup.
            "write_under": f"{tmp_root_str}/...",
            "docs_ref": "docs/AGENT_CONTRACT.md",
            "note": (
                "Write under the literal allowed_tmp_root path "
                f"({tmp_root_str}/...). Do not use `export TMPDIR=...`, `jq -er ...`, "
                "`${TMPDIR:-fallback}` syntax, or hardcoded /tmp//dev/shm paths."
            ),
        }
        if used_fallback_or_hardcode:
            fix_hint_block["tmpdir_fallback_or_hardcode"] = True
            fix_hint_block["canonical_doc"] = (
                "docs/AGENT_CONTRACT.md"
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
    # Phase-2: shell writes (Bash redirect `cat > path` / `tee` / `sed -i`) are
    # NEVER an authorized artifact-write path — not even when the target is in
    # `allowed_file_tool_paths`. Managed artifacts are written with the structured
    # file-edit tools (Edit / Write, or `apply_patch` on the Codex backend), which
    # are auditable; Bash writes are confined to `allowed_tmp_root` (the tmp check
    # above already ALLOWed those, so any Bash target reaching here is non-tmp).
    # Blocking it regardless of `allowed_file_tool_paths` membership is what keeps a
    # managed output — now Edit/Write-eligible under the direct-write contract —
    # from ALSO silently authorizing shell writes (e.g. `cat > lineage.json`, or a
    # command-substitution exfil like `echo $(cat secret) > out.json`) to a
    # canonical path. The leaf must use the Edit/Write tool instead.
    if tool_name == "Bash":
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                "shell writes (redirect / tee / sed -i) are forbidden for managed "
                "artifacts. Write the artifact with the Edit/Write tool to a path in "
                "output_manifest allowed_file_tool_paths; Bash may only write scratch "
                f"under allowed_tmp_root (workspace/tmp/{agent_run_id}/...)."
            ),
            continue_processing=False,
            audit_detail={
                # Policy id kept as `enforce_guarded_apply_patch` for audit-log /
                # remediation-table continuity: it is the stable identifier for the
                # whole "a direct artifact write was rejected — use the Edit/Write
                # tool" class (docs/RUNBOOK.md#hook-recovery and the audit-claude
                # SKILL key on it). The id is forensic-only and never surfaced to the
                # leaf (only `reason` + `fix_hint` are). Full rename is P2-7 cleanup
                # alongside the guarded-apply-patch retirement.
                "policy": "enforce_guarded_apply_patch",
                "tool_name": tool_name,
                "file_path": file_path,
                "agent_run_id": agent_run_id,
                "allowed_file_tool_paths": list(allowed_file_tool_paths),
                "fix_hint": {
                    "write_under": f"workspace/tmp/{agent_run_id}/...",
                    "docs_ref": "docs/AGENT_CONTRACT.md",
                    "note": (
                        "Write managed artifacts with the Edit/Write tool (not a shell "
                        "redirect / tee / sed -i). Do NOT use `export TMPDIR=...` or "
                        "$TMPDIR env (session approval would stall)."
                    ),
                },
            },
        )
    if tool_name in {"Edit", "Write", "apply_patch"} and rel_target_norm not in allowed_file_tool_paths:
        # The target is a declared output (it passed the allowed_output_paths check
        # above) but is not Edit/Write-eligible — i.e. it is excluded from
        # `allowed_file_tool_paths` (a canonical MCP audit log, or a path the
        # orchestration did not declare as a file-tool output). The recovery is to
        # add the path to `allowed_file_tool_paths` (the orchestration's launch
        # request), not a shell write.
        return HookDecision(
            action=HookDecisionAction.BLOCK,
            reason=(
                f"direct write via {tool_name} is forbidden for this target path: it is "
                "not in output_manifest allowed_file_tool_paths (e.g. an MCP-owned audit "
                "log written only by the build-runtime MCP server). Write only paths "
                "enumerated in allowed_file_tool_paths."
            ),
            continue_processing=False,
            audit_detail={
                "policy": "enforce_guarded_apply_patch",
                "tool_name": tool_name,
                "file_path": file_path,
                "agent_run_id": agent_run_id,
                "allowed_file_tool_paths": list(allowed_file_tool_paths),
                "fix_hint": {
                    "docs_ref": "docs/AGENT_CONTRACT.md",
                    "note": (
                        "The path must be listed in output_manifest allowed_file_tool_paths "
                        "to be written with the Edit/Write tool."
                    ),
                },
            },
        )
    return HookDecision(action=HookDecisionAction.ALLOW)


def _is_persisted_tool_result_read(
    repo_root: Path,
    agent_role: str | None,
    agent_run_id: str,
    file_path: str,
    session_id: str | None = None,
) -> bool:
    """True iff file_path is a persisted Claude Code tool-result this agent may read.

    Matches: ~/.claude/projects/<repo-slug>/<session_dir>/tool-results/<id>.txt
    The session_dir component must equal either `agent_run_id` or `session_id`.
    Two IDs are checked because:
    - Claude Code backend records agent_run_id as agent_session_id (see
      docs/ORCHESTRATION.md), so tool-results may be stored under agent_run_id.
    - The hook payload's session_id is the live Claude Code session identifier
      actually used to name the directory; pass it to cover cases where the two
      differ.
    This prevents reads from a different agent's or previous session's directory.
    Applies to all agent roles — all can receive <persisted-output> wrappers.
    """
    valid_session_dirs: set[str] = {s for s in (agent_run_id, session_id) if s}
    if not valid_session_dirs:
        return False
    try:
        abs_target = _absolute_lexical(repo_root, file_path)
        repo_root_abs = _absolute_lexical(repo_root, str(repo_root))
    except (OSError, ValueError):
        return False
    if not _path_has_no_symlink_redirect(abs_target):
        return False
    try:
        home_abs = Path.home()
        slug = _claude_project_slug(repo_root_abs)
        project_root = home_abs / _AUTO_READ_PROJECT_TOOL_RESULTS_PARENT_TAIL / slug
        if (
            abs_target.name.endswith(".txt")
            and abs_target.parent.name == _AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT
        ):
            rel = abs_target.relative_to(project_root)
            parts = rel.parts
            # parts = (session_dir, "tool-results", filename)
            if (
                len(parts) == 3
                and parts[1] == _AUTO_READ_PROJECT_TOOL_RESULTS_DIR_COMPONENT
                and parts[0] in valid_session_dirs
            ):
                return True
    except (OSError, RuntimeError, ValueError):
        pass
    return False


def _is_auto_read_tolerated(
    repo_root: Path,
    agent_role: str | None,
    file_path: str,
) -> bool:
    """Return True if it is a Claude Code auto-read target.

    Two categories of tolerated auto-reads are recognised:

    1. Harness-mandatory auto-reads (all agent roles):
       Files the Claude Code harness reads at startup regardless of agent
       role (MCP discovery, settings parsing). Apply to orchestration AND
       step/substep agents. Path must lexically match
       `_HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS` or have a component-aligned
       prefix from `_HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES`.

    2. Orchestration-only auto-reads:
       Project state files (MEMORY/README/TODO/CLAUDE) that orchestration
       agent reads during MCP discovery. Apply only when agent_role ==
       "orchestration". Path either lexically matches
       `_AUTO_READ_TOLERATED_REPO_RELPATHS`, or is the project-memory file
       under <home>/.claude/projects/<repo-slug>/memory/MEMORY.md.

    Security invariants for both categories:
    - The requested path itself must NOT traverse any symlink component
      (lstat-based check) to prevent tolerance from being redirected to
      arbitrary host files via symlink swap.
    - Path comparison is done lexically (no .resolve()), so an attacker
      cannot bypass via filesystem symlinks pointing at a tolerated path.
    """
    try:
        abs_target = _absolute_lexical(repo_root, file_path)
        repo_root_abs = _absolute_lexical(repo_root, str(repo_root))
    except (OSError, ValueError):
        return False

    if not _path_has_no_symlink_redirect(abs_target):
        return False

    try:
        rel = abs_target.relative_to(repo_root_abs)
    except ValueError:
        rel = None
    rel_posix = rel.as_posix() if rel is not None else None

    # Category 1: harness-mandatory auto-read (all roles).
    if rel_posix is not None:
        if rel_posix in _HARNESS_AUTO_READ_TOLERATED_REPO_RELPATHS:
            return True
        for prefix in _HARNESS_AUTO_READ_TOLERATED_REPO_PREFIXES:
            # Prefix must end with "/" so match is component-aligned and
            # cannot extend across path segments (no suffix bypass).
            if prefix.endswith("/") and rel_posix.startswith(prefix):
                return True

    # Category 2a: persisted tool-results (all agent roles) are handled upstream
    # in validate_read_access via _is_persisted_tool_result_read, which requires
    # agent_run_id for session-binding and returns ALLOW before this function
    # is called. No fallback needed here.

    # Category 2b: orchestration-only auto-read.
    if agent_role != "orchestration":
        return False

    # (a) repo-contained exact lexical match
    if rel_posix is not None and rel_posix in _AUTO_READ_TOLERATED_REPO_RELPATHS:
        return True

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
    /home/<user>/work/met-dsl → -home-<user>-work-met-dsl.
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
    session_id: str | None = None,
) -> HookDecision:
    """Verify the read target against the read manifest's allowed_read_roots."""
    # Category 2a: persisted tool-results for any agent role.
    # These are harness-internal files (never in read_manifest) that agents must
    # be able to read when large tool outputs have been persisted as
    # <persisted-output>.  Return ALLOW directly — do not route through the
    # block-as-noise path used for startup auto-reads.
    # session_id (the live Claude Code session identifier from the hook payload)
    # is checked alongside agent_run_id because Claude Code stores tool-results
    # under its own session directory, which may differ from agent_run_id.
    if _is_persisted_tool_result_read(
        repo_root, agent_role, agent_run_id, file_path, session_id=session_id
    ):
        return HookDecision(action=HookDecisionAction.ALLOW)
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
