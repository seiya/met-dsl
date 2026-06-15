# met-dsl

`met-dsl` is a document-driven framework that generates, validates, and operates weather and climate compute kernels from natural-language-first specifications.  
The source of truth is `controlled_spec.md` and `tests.md`, and AI-assisted stages are constrained by deterministic plan artifacts and execution-time validation.

## What This Project Targets

- Generate reusable subroutine libraries (`component` / `operation`) for weather and climate computation.
- Manage specifications with three kinds: `problem` (integration), `component` (reusable operator), and `profile` (selection policy).
- Produce optimized implementations for CPU and GPU targets.
- Use Fortran as the default CPU assumption and CUDA Fortran as the default GPU assumption.
- Keep physics definition and execution optimization separated.
- Record physics-affecting choices in `case.resolved.yaml`.
- Record execution/performance choices in `impl.resolved.yaml`.

## What This Repository Currently Provides

- Workflow and contract specifications for the full pipeline (`Spec -> Plan -> Generate -> Build -> Execute -> Judge -> Tune -> Promote`).
- MCP build/runtime server implementation at `mcp_servers/build_runtime_server.py`.
- Sample `advdiff1d` controlled spec at `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/controlled_spec.md`.
- Sample `advdiff1d` tests at `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/tests.md`.

## Core Workflow (Summary)

1. Define physics and tests in documents (`controlled_spec.md`, `tests.md`).
2. Resolve deterministic physics plan (`case.resolved.yaml`).
3. Resolve implementation plan (`impl.resolved.yaml`).
4. Generate separated `model` and `runner`.
5. Build and run through MCP tools.
6. Judge physical validity and quality checks.
7. Tune implementation choices without changing physics intent.
8. Promote accepted artifacts to `releases/...`.

## MCP Tools

Use `mcp_servers/build_runtime_server.py` as the standard MCP server.  
Available tools:

- `detect_build_system`
- `compile_project`
- `run_program`
- `run_quality_checks`

Minimal MCP client config:

```json
{
  "mcpServers": {
    "build-runtime": {
      "command": "python",
      "args": [
        "./mcp_servers/build_runtime_server.py"
      ]
    }
  }
}
```

## Quick Start (Minimal)

1. Read `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/controlled_spec.md`.
2. Read `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/tests.md`.
3. Configure your MCP client with `mcp_servers/build_runtime_server.py`.
4. Run build and execution via MCP tools (`compile_project`, `run_program`).
5. Evaluate `diagnostics.json`, `perf.json`, and `verdict.json` with the workflow documents.

## Repository Layout

```text
docs/         specifications and workflow contracts
spec/         source specs and registry
releases/     promoted official artifacts
workspace/    working artifacts for plans/pipelines
mcp_servers/  MCP server implementation and examples
```

## Minimal Documentation Entry Points

- `docs/SPEC.md`
- `docs/WORKFLOW.md` (entry point) / `docs/workflow/WORKFLOW_CORE.md` / `docs/workflow/phases/`
- `docs/RUNBOOK.md`
- `docs/GLOSSARY.md`

## Status

This repository is currently specification-first.  
It defines contracts, workflow rules, and MCP execution interfaces, with sample specs for incremental implementation and validation.

## License

BSD 2-Clause. See `LICENSE`.
