# MCP Server: Build/Runtime Operations

## Purpose
This directory provides the implementation for running `compile` / `run` / `quality check` / `static lint` / `syntax check` for `Generate` via the MCP server.

## Provided server
- `build_runtime_server.py`
  - a minimal MCP server that works without dependency packages (stdio JSON-RPC)
  - provided tools:
    - `detect_build_system`
    - `compile_project`
    - `run_program`
    - `run_quality_checks`
    - `run_linter`
    - `run_syntax_check`

## Important operational rules
- `compile_project` allows only standard build tools that can handle dependencies.
- For `fortran` / `c` / `cpp` / `mixed` families, allow only `make/cmake/meson/ninja`.
- For `fortran` / `c` families, when the build tool is unspecified, the default is `make`.
- The operation of directly calling `gcc` / `clang` / `gfortran` for a one-off build is forbidden.
- `run_linter` is the tool for `Generate`'s `static lint`. Rather than via `compile_project` or a `Makefile`'s `lint` target, it launches `fortitude` / `cppcheck` / `ruff` with only the `preset`. `preset=mixed` runs `fortitude` and `cppcheck` in order. It is outside the scope of the norm that requires `compile` to go through a standard build tool.
- When `run_program` is given `target.class=cpu` (or `target_class=cpu`) and `threads_per_rank`, it auto-sets `OMP_NUM_THREADS` and `OMP_THREAD_LIMIT`.
- `run_quality_checks` allows only the `preset` specification, and forbids the execution of an arbitrary `command`.
- The `preset=pytest` of `run_quality_checks` prepends `project_dir` to `PYTHONPATH` to ensure the reproducibility of import resolution.
- `run_linter` allows only the `preset` specification, and forbids the execution of an arbitrary `command`.
- `run_syntax_check` is the tool for `Generate`'s deterministic `Generate.syntax` gate: it runs a REGISTERED compiler adapter's syntax-only mode (`gfortran -fsyntax-only -std=<toolchain.standard>`, module files into a throwaway `.mods` scratch dir inside `project_dir`) over the staged Fortran sources in module/use dependency order. Because it produces **no build artifacts**, it is lint-class, not a build — like `run_linter` it is outside the scope of the norm that requires `compile` to go through a standard build tool (the "no one-off `gfortran`" rule above targets builds). Adapters are a registry (`_SYNTAX_COMPILER_ADAPTERS`; currently `gfortran`) — a future target compiler (e.g. Fujitsu `frt`, whose adapter may compile with `-c` into the scratch dir) is added by extending the registry, and the conductor selects stages via the `METDSL_SYNTAX_COMPILERS` env var (default `gfortran`; a stage whose compiler binary is absent is reported `skipped`). It forbids the execution of an arbitrary `command`.
- `compile_project` / `run_program` / `run_quality_checks` / `run_linter` / `run_syntax_check` always record the executed command in `JSONL` format.
- The default for `command_log_path` when unspecified is `<project_dir>/command_log.jsonl`.
- The execution result returns `command_id`, `executed_command`, and `command_log_path`, and when the log is under the repository, returns `command_log_ref`.

## MCP configuration examples

The canonical configuration file differs per backend. The repository bundles the following 2 files.

### Claude Code: `.mcp.json` (repository root)

Claude Code reads `.mcp.json` directly under the project root and defines the server. Bundled content:

```json
{
  "mcpServers": {
    "build-runtime": {
      "command": "python3",
      "args": ["./mcp_servers/build_runtime_server.py"]
    }
  }
}
```

`.mcp.json` is **the server definition** only, and enabling it (enablement) per project requires separate approval. The approval sources are (a) the workspace trust dialog at interactive `claude` launch (recorded per-user in `~/.claude.json`), and (b) the `enabledMcpjsonServers` / `enableAllProjectMcpServers` of the repository-committed `.claude/settings.json`.

The `preflight` of `tools/run_workflow.py --llm claude` (`tools/orchestration_runtime.py` `_probe_claude_mcp_registry`) verifies the enablement of `build-runtime` using **only the committed `.claude/settings.json` of (b)** as the canonical source, and stops with `status=fail` when not enabled (`~/.claude.json` is not referenced because it harms reproducibility per-machine). `claude mcp list` is displayed only as an advisory diagnostic.

**In addition to enablement, the tool-call permission is also required.** Even if the server is enabled, without the MCP tool-call permission for the child `Agent` session, `run_linter` etc. fail with `Claude requested permissions … but you haven't granted it yet.`, and Generate/Build/Validate stop entirely. Place the server-level grant `mcp__build-runtime` in the `permissions.allow` of the committed `.claude/settings.json` (because Claude Code's permission rule does not interpret the tool-name wildcard `mcp__build-runtime__*`, use the server level). The preflight (`claude_mcp_build_runtime_permission_granted` check) verifies it ANDed with the enablement, and stops with `status=fail` when not granted.

The repository-bundled `.claude/settings.json` includes the following, so everyone who clones it is enabled/permitted without personal settings:

```json
{
  "enabledMcpjsonServers": ["build-runtime"],
  "permissions": { "allow": ["mcp__build-runtime"] }
}
```

To temporarily disable it in a personal environment, place `"disabledMcpjsonServers": ["build-runtime"]` in `.claude/settings.local.json` (the preflight detects this opt-out and makes it `status=fail`).

### Cursor: `.cursor/mcp.json`

Cursor reads `.cursor/mcp.json`. The repository-bundled `.cursor/mcp.json` already specifies `build-runtime` with an absolute path (to match Cursor's resolution convention).

### General MCP clients

A reference example for other clients:

```json
{
  "mcpServers": {
    "build-runtime": {
      "command": "python",
      "args": [
        "/path/to/met-dsl/mcp_servers/build_runtime_server.py"
      ]
    }
  }
}
```

## Tool call examples
An example of building `fortran` with `make`:

```json
{
  "name": "compile_project",
  "arguments": {
    "project_dir": "/path/to/project",
    "command_log_path": "logs/build_commands.jsonl",
    "language": "fortran",
    "build_system": "make",
    "target": "all",
    "jobs": 8
  }
}
```

An example of running a binary:

```json
{
  "name": "run_program",
  "arguments": {
    "project_dir": "/path/to/project",
    "command_log_path": "logs/run_commands.jsonl",
    "command": ["./bin/simulate", "--case", "case.resolved.yaml"],
    "target.class": "cpu",
    "threads_per_rank": 8,
    "timeout_sec": 1800
  }
}
```

An example of `Generate`'s `static lint` (assuming `fortran`):

```json
{
  "name": "run_linter",
  "arguments": {
    "project_dir": "/path/to/workspace/pipelines/<node_key_safe>/<pipeline_id>/generate/<generation_id>/src",
    "command_log_path": "command_log.jsonl",
    "preset": "fortitude",
    "timeout_sec": 1800
  }
}
```
