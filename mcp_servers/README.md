# MCP Server: Build/Runtime Operations

## 目的
このディレクトリは、`compile` / `run` / `quality check` を MCP サーバー経由で実行するための実装を提供する。

## 提供サーバー
- `build_runtime_server.py`
  - 依存パッケージなしで動作する最小 MCP サーバー（stdio JSON-RPC）
  - 提供ツール:
    - `detect_build_system`
    - `compile_project`
    - `run_program`
    - `run_quality_checks`

## 重要運用ルール
- `compile_project` は依存関係を扱える標準ビルドツールのみを許可する。
- `fortran` / `c` / `cpp` / `mixed` 系は、`make/cmake/meson/ninja` のみ許可する。
- `fortran` / `c` 系でビルドツールが未指定の場合、既定値は `make` とする。
- `gcc` / `clang` / `gfortran` を直接呼び出して単発ビルドする運用は禁止する。

## MCP 設定例
以下は一般的な MCP クライアント設定の例である。実際の設定形式はクライアント実装に合わせて調整する。

```json
{
  "mcpServers": {
    "build-runtime": {
      "command": "python",
      "args": [
        "/home/seiya/met-dsl/mcp_servers/build_runtime_server.py"
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
    "command": ["./bin/simulate", "--case", "case.resolved.yaml"],
    "timeout_sec": 1800
  }
}
```
