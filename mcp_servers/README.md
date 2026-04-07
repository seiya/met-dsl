# MCP Server: Build/Runtime Operations

## 目的
このディレクトリは、`compile` / `run` / `quality check` / `Generate` 用 `static lint` を MCP サーバー経由で実行するための実装を提供する。

## 提供サーバー
- `build_runtime_server.py`
  - 依存パッケージなしで動作する最小 MCP サーバー（stdio JSON-RPC）
  - 提供ツール:
    - `detect_build_system`
    - `compile_project`
    - `run_program`
    - `run_quality_checks`
    - `run_linter`

## 重要運用ルール
- `compile_project` は依存関係を扱える標準ビルドツールのみを許可する。
- `fortran` / `c` / `cpp` / `mixed` 系は、`make/cmake/meson/ninja` のみ許可する。
- `fortran` / `c` 系でビルドツールが未指定の場合、既定値は `make` とする。
- `gcc` / `clang` / `gfortran` を直接呼び出して単発ビルドする運用は禁止する。
- `run_linter` は `Generate` の `static lint` 用のツールである。`compile_project` や `Makefile` の `lint` target 経由ではなく、`preset` のみで `fortitude` / `cppcheck` / `ruff` を起動する。`preset=mixed` は `fortitude` と `cppcheck` を順に実行する。`compile` を標準ビルドツール経由とする規範の対象外とする。
- `run_program` で `target.class=cpu`（または `target_class=cpu`）かつ `threads_per_rank` を指定した場合、`OMP_NUM_THREADS` と `OMP_THREAD_LIMIT` を自動設定する。
- `run_quality_checks` は `preset` 指定のみを許可し、任意 `command` の実行を禁止する。
- `run_quality_checks` の `preset=pytest` は、`project_dir` を `PYTHONPATH` へ先頭追加して import 解決の再現性を確保する。
- `run_linter` は `preset` 指定のみを許可し、任意 `command` の実行を禁止する。
- `compile_project` / `run_program` / `run_quality_checks` / `run_linter` は、実行したコマンドを `JSONL` 形式で必ず記録する。
- `command_log_path` 未指定時の既定値は `<project_dir>/mcp_command_log.jsonl` とする。
- execution result は `command_id` と `executed_command` と `command_log_path` を返却し、リポジトリ配下にログがある場合は `command_log_ref` を返却する。

## MCP 設定例
以下は一般的な MCP クライアント設定の例である。実際の設定形式はクライアント実装に合わせて調整する。

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

## ツール呼び出し例
`fortran` を `make` でビルドする例:

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

バイナリ実行例:

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

`Generate` の `static lint` 例（`fortran` 想定）:

```json
{
  "name": "run_linter",
  "arguments": {
    "project_dir": "/path/to/workspace/pipelines/<node_key_safe>/<pipeline_id>/generate/<generation_id>/src",
    "command_log_path": "mcp_command_log.jsonl",
    "preset": "fortitude",
    "timeout_sec": 1800
  }
}
```
