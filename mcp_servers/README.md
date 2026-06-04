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

backend ごとに canonical 設定ファイルが異なる。リポジトリには以下 2 ファイルを同梱する。

### Claude Code: `.mcp.json` (リポジトリ root)

Claude Code は project root 直下の `.mcp.json` を読み、server を定義する。同梱内容:

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

`.mcp.json` は **server 定義** のみで、各 project で **有効化 (enablement)** するには別途承認が要る。承認ソースには (a) `claude` 対話起動時の workspace trust dialog (`~/.claude.json` に per-user で記録)、(b) リポジトリにコミットされた `.claude/settings.json` の `enabledMcpjsonServers` / `enableAllProjectMcpServers` がある。

`tools/run_workflow.py --llm claude` の `preflight` (`tools/orchestration_runtime.py` `_probe_claude_mcp_registry`) は **(b) のコミット対象 `.claude/settings.json` のみ**を canonical source として `build-runtime` の有効化を検証し、未有効なら `status=fail` で停止する (`~/.claude.json` は per-machine で再現性を損なうため参照しない)。`claude mcp list` は advisory diagnostic としてのみ表示する。

**enablement に加えて tool 呼び出し permission も必須。** server を有効化しても、子 `Agent` session に MCP tool の呼び出し許可が無いと `run_linter` 等は `Claude requested permissions … but you haven't granted it yet.` で失敗し、Generate/Build/Validate が全停止する。コミット対象 `.claude/settings.json` の `permissions.allow` に server 単位 grant `mcp__build-runtime` を置くこと (Claude Code の permission rule は tool 名 wildcard `mcp__build-runtime__*` を解さないため server 単位を使う)。preflight (`claude_mcp_build_runtime_permission_granted` check) が enablement と AND で検証し、未付与なら `status=fail` で停止する。

リポジトリ同梱の `.claude/settings.json` には次が含まれており、clone した全員が個人設定なしで有効化・許可される:

```json
{
  "enabledMcpjsonServers": ["build-runtime"],
  "permissions": { "allow": ["mcp__build-runtime"] }
}
```

個人環境で一時的に無効化する場合は `.claude/settings.local.json` に `"disabledMcpjsonServers": ["build-runtime"]` を置く (この opt-out は preflight が検出して `status=fail` にする)。

### Cursor: `.cursor/mcp.json`

Cursor は `.cursor/mcp.json` を読む。リポジトリ同梱の `.cursor/mcp.json` で `build-runtime` を絶対パス指定済み (Cursor の解決規約に合わせる)。

### 一般的な MCP クライアント

その他クライアント向け参照例:

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
