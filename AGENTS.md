# AGENTS.md

When editing or creating documents in this repository, follow the writing rules below.

## Purpose
- Write documents as "finished specifications", not as "discussion logs".
- Keep documents in a state where a reader can interpret the decisions and requirements by reading them standalone.

## Style rules
- Use concise, declarative prose.
- State the subject and responsibility explicitly; avoid ambiguous omissions.
- Do not use colloquialisms, metaphors, or expressions of opinion.
- Prioritize describing specifications, requirements, constraints, and decision criteria.

## Terminology rules
- Use the English notation defined in `docs/GLOSSARY.md` for terms, artifact names, role names, phase names, and classification names.
- When adding a new term, first define it in `docs/GLOSSARY.md` in English, then use it across documents.
- Keep terms consistent with the canonical notation in `docs/GLOSSARY.md`; do not introduce ad-hoc synonyms.

## Markdown math notation rules
- Use `$...$` for inline math.
- Use `$$...$$` for block math.
- Do not use `\(...\)` or `\[...\]`.

## Forbidden expressions
- Expressions that reveal the discussion process: "in conclusion", "the reason is", "after discussion", "first", "next", "trial and error", and similar.
- Expressions that reveal AI dialogue: "brainstormed with AI", "the AI thought", "asked the LLM", and similar.
- Colloquial or slang expressions.

## Recommended headings
- `Purpose`
- `Scope`
- `Requirements`
- `Design Policy`
- `Operations Rules`
- `Decision Criteria`

## Writing guidelines
- Write "what is required", not "why it ended up this way".
- For branching decisions, state the conditions and the selection rule explicitly.
- Do not leave undefined items unresolved; state explicitly that an item is undefined and how it is handled (forbidden / error).
- Define abbreviations at first use, and keep terms consistent with the existing documents (`docs/GLOSSARY.md`).

## Change checklist
- No discussion-log-style expressions remain.
- Each section is a self-contained, complete statement that reads standalone.
- Requirements, constraints, input/output, and decision conditions are concrete.
- Terms, artifact names, role names, phase names, and classification names match the English notation in `docs/GLOSSARY.md`.

## CLI reference rules
- Choose the path for obtaining CLI argument information based on subcommand frequency and payload complexity. The detailed table in the "CLI reference conventions" section of `CLAUDE.md` is the canonical source.
- For the frequent subcommands of `tools/orchestration_runtime.py` (`record-launch` / `record-agent-run` / `record-child-return` / `deactivate-child` / `record-reply` / `set-status` / `write-step-result` / `workflow-launch-check` / `reserve-phase-root` / `mark-dependency-readiness` / `guarded-apply-patch` / `run-gate`), `docs/CLI_REFERENCE.md` (Tier-A) is the canonical source.
- For the rare subcommands of `tools/orchestration_runtime.py` (`init` / `preflight` / `preflight-status` / `record-timeout` / `read-checkpoint` / `verify-checkpoint-integrity` / `check-step-completed` / `orchestration-read` / `repair-agent-runs` / `repair-step-result-executor` / `dismiss-violation`), and for `tools/run_workflow.py` / `tools/validate_pipeline_semantics.py` / `tools/audit_orchestration.py`, `<tool> [<sub>] --help` is the canonical source. `docs/CLI_REFERENCE_RARE.md` retains only an overview of the rare subcommands.
- During workflow execution, reading the `.py` implementations under `tools/` directly via the `Read` tool / `grep` / `sed` / `cat` etc. is forbidden and subject to `forbid_tools_direct_read` and `read_manifest_read_guard`. Reading the argparse output via `--help` is not blocked.
- During repository improvement, maintenance, testing, or refactoring, `tools/*.py` is ordinary source code and may be inspected directly. The workflow-execution restriction does not apply to that work.

## MCP execution rules
- Always run `compile` / `run` / `quality check` through the MCP server.
- The standard server is `mcp_servers/build_runtime_server.py`; use `detect_build_system` / `compile_project` / `run_program` / `run_quality_checks` / `run_linter`.
- When `compile` handles `fortran` / `c` / `cpp` / `mixed` families, only allow standard build tools that can handle dependencies. The default is `make`.
- Forbid one-off builds that call `gcc` / `clang` / `gfortran` directly.
- `Generate` runs `static lint` via the MCP `run_linter`. `run_linter` is a separate step from builds via `compile` / `compile_project` / `toolchain.build_system`, and is outside the scope of the rule that requires `compile` to go through a standard build tool.
- For processing other than `compile` / `run` where MCP applies (e.g. build system detection, test execution, check execution), implement MCP tools likewise and avoid direct shell execution.

## MCP configuration reference
- For MCP client configuration, refer to `mcp_servers/mcp_servers.example.json`.
- For operational details, refer to `mcp_servers/README.md`.

## Project Local Skills rules
- Treat the `SKILL.md` files under `skills/` as the canonical source for the execution procedure of each workflow phase.
- For the mapping between phases and `SKILL`, refer to `docs/AGENT_SKILLS.md`.
- For phases that have a `generate -> verify -> regenerate` loop, apply the corresponding `generate` `SKILL` and `verify` `SKILL` separately.
- On `Codex` / `Gemini` / `Claude Code` alike, before starting work read the `SKILL.md` for the target phase and follow the defined input/output contract and decision criteria.

## Workflow document reference rules
- The entry point to the workflow specification is `docs/WORKFLOW.md`. `docs/workflow/WORKFLOW_CORE.md` is the canonical source for the common invariants, phase sequence, and the per-`phase` I/O contract list; the files under `docs/workflow/phases/` are the canonical source for each `phase`'s detailed contract.
- `docs/ORCHESTRATION.md` is the canonical source for the hierarchical execution contract between the `orchestration agent` and the `step agent` / `substep agent`.
- `docs/AGENT_SKILLS.md` is the canonical source for the phase-to-`SKILL` mapping, the decision on where rules are documented, and the phase-switching rules.
- Do not restate workflow-specific prohibitions, the ban on referencing past artifacts, or the independent-`agent` execution-evidence requirements in `AGENTS.md`; refer to the corresponding canonical source.
- The canonical entrypoint for starting the workflow is the user running `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]`.
- When the workflow runs, the canonical source for `METDSL_WORKFLOW_MODE=1` and `METDSL_ORCHESTRATION_ID=<orchestration_id>` is the values set by the `tools/run_workflow.py` that the user started.
