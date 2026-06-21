# Hook implementation policy

Repository-maintenance reference for the hook system (where hook validation and invocations are defined). Hooks act on agents transparently; this is not required reading for a workflow agent at runtime (for recovering from a hook block, see the "Repair cheat sheet on a hook block" section of `docs/RUNBOOK.md`).

- `tools/hooks/common.py` is the canonical source for backend-independent validation. Backend-specific invocation specifications are absorbed by the adapters under `tools/hooks/adapters/`.
- `.codex/hooks.json` is the canonical source for Codex hook invocation definitions; the `hooks` section of `.claude/settings.json` is the canonical source for Claude Code (matcher/wiring details below).
- The Claude Code backend does not need a feature-flag probe, and the `hooks` requirement check is limited to the Codex backend. The common policy follows `evaluate_common_policy()` in `tools/hooks/common.py`.
- The `hooks` section of `.claude/settings.json` wires the 4 events `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop`. Its `matcher` is an **exact-match string** (not a regular expression): write `"Bash"`, unlike `^Bash$` in `.codex/hooks.json`.
