# AGENTS.md

Conventions every agent (Codex / Claude Code) working in this repository must follow. This file is kept to content that **all agents** need; role- or task-specific rules live in dedicated documents (below). Claude Code loads this file by importing it from `CLAUDE.md` via `@AGENTS.md`.

## Dedicated rule documents
- **Authoring repository documents** (style, terminology, math notation, forbidden expressions, structure, checklist): `docs/DOC_STYLE.md`.
- **CLI argument information-acquisition policy** (which subcommand uses a doc vs `--help`): the "Information-acquisition policy" section of `docs/CLI_REFERENCE.md`.
- **Hook implementation structure** (where hook validation / invocations are defined): `docs/HOOKS.md`.

## MCP execution rules
- Always run `compile` / `run` / `quality check` through the MCP server.
- The standard server is `mcp_servers/build_runtime_server.py`; use `detect_build_system` / `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check`.
- When `compile` handles `fortran` / `c` / `cpp` / `mixed` families, only allow standard build tools that can handle dependencies. The default is `make`.
- Forbid one-off builds that call `gcc` / `clang` / `gfortran` directly.
- `Generate` runs `static lint` via the MCP `run_linter`, and its deterministic `Generate.syntax` gate runs a compiler front-end in syntax-only mode (producing no build artifacts) via the MCP `run_syntax_check`. Both are separate steps from builds via `compile` / `compile_project` / `toolchain.build_system`, and both are outside the scope of the rule that requires `compile` to go through a standard build tool (and outside the ban on calling `gfortran` directly, which targets builds).
- For processing other than `compile` / `run` where MCP applies (e.g. build system detection, test execution, check execution), implement MCP tools likewise and avoid direct shell execution.
- For MCP client configuration, refer to `mcp_servers/mcp_servers.example.json`; for operational details, `mcp_servers/README.md`.

## Project Local Skills rules
- Treat the `SKILL.md` files under `skills/` as the canonical source for the execution procedure of each workflow phase.
- For the mapping between phases and `SKILL`, refer to `docs/AGENT_SKILLS.md`.
- For phases that have a `generate -> verify -> regenerate` loop, apply the corresponding `generate` `SKILL` and `verify` `SKILL` separately.
- On `Codex` / `Claude Code` alike, before starting work read the `SKILL.md` for the target phase and follow the defined input/output contract and decision criteria.

## Workflow document reference rules
- The entry point to the workflow specification is `docs/WORKFLOW.md`. `docs/workflow/WORKFLOW_CORE.md` is the canonical source for the common invariants, phase sequence, and the per-`phase` I/O contract list; the files under `docs/workflow/phases/` are the canonical source for each `phase`'s detailed contract.
- `tools/workflow_conductor.py` drives the deterministic phase/substep loop and launches each `step agent` / `substep agent` as a leaf. `docs/ORCHESTRATION.md` is the canonical orchestration design + contract spec; `docs/AGENT_CONTRACT.md` is the canonical child step/substep agent contract.
- `docs/AGENT_SKILLS.md` is the canonical source for the phase-to-`SKILL` mapping, the decision on where rules are documented, and the phase-switching rules.
- Do not restate workflow-specific prohibitions, the ban on referencing past artifacts, or the independent-`agent` execution-evidence requirements in `AGENTS.md`; refer to the corresponding canonical source.
- The canonical entrypoint for starting the workflow is the user running `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|claude>]` (add `--with-deps` to run the dependency closure, `--resume` to recover a failed run; see `docs/RUNBOOK.md`).
- When the workflow runs, the canonical source for `METDSL_WORKFLOW_MODE=1` and `METDSL_ORCHESTRATION_ID=<orchestration_id>` is the values set by the `tools/run_workflow.py` that the user started.
