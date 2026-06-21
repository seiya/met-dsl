# CLAUDE.md

@AGENTS.md

The shared / cross-backend conventions are imported from `AGENTS.md` above. This file adds only Claude Code-specific notes that every Claude sub-agent needs.

> `tools/workflow_conductor.py` launches each `step agent` / `substep agent` as a leaf (`claude -p`), which gets this file (and the imported `AGENTS.md`) auto-injected. A child agent's contract is [docs/AGENT_CONTRACT.md](docs/AGENT_CONTRACT.md).

Claude-specific operator / maintenance references (not needed by a running sub-agent):
- Claude backend preflight requirements (build-runtime MCP registration + permission): [docs/RUNBOOK.md](docs/RUNBOOK.md) §0-2.
- Hook implementation + the `.claude/settings.json` matcher rule: [docs/HOOKS.md](docs/HOOKS.md).
