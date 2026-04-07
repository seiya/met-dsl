# Phase contract: Build

### 3. Build
- execution input: `generate/<generation_id>/src/`、`impl.resolved.yaml`
- verification input: `dependency.resolved.yaml`、`generate_meta.json`、`impl.resolved.yaml`
- 出力: `build/<build_id>/bin/`、`build_meta.json`、`compile_project` の `command_id` と `command_log_ref`
- `Build` は標準 `substep` を持たない単一 `step` とする。
- `Build` は `MCP` サーバー経由で実行する。
- `Build` は `compile_project` を使用し、`fortran` / `c` / `cpp` / `mixed` 系では依存関係を扱える標準ビルドツール（既定 `make`）を使用する。
- `toolchain.build_system=make` の `Build` 入力は、`src/Makefile` が言語依存のコンパイル順序依存を前提条件として明示した依存関係完全版でなければならない。
- `toolchain.build_system=make` の `Build` は、`make -j` で成否が変化しない依存記述を必須とする。
- `compile_project` の実コマンド記録は `JSONL` 形式で保存し、既定の保存先は `project_dir/mcp_command_log.jsonl` とする。
- `Build` の試行メタデータは `command_id` と `command_log_ref`（または `command_log_path`）を追跡可能に記録する。
- `Build` は依存を持つ `node` で、依存 `operation` 解決先が `dependency.resolved.yaml` と一致することを必須検証とする。不一致時は `Build fail` とする。
- `Build` は `node` 単位で個別実行し、他 `node` の artifact を混在させてはならない。
- `Build` は、依存元 `src/` に依存 `node` 固有の `module`、`subroutine`、または `runner` 実装が混入している場合を `dependency implementation encapsulation` 違反として `fail` にしなければならない。
- `Build` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_build --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` を実行し、必要に応じて `--generation-id <generation_id>` を付与しなければならない。`exit code 0` を必須とし、`fail` 時は `Build` を `fail` としなければならない。

