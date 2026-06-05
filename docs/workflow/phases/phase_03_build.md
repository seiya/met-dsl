# Phase 3: Build

## 概要
`Generate` が生成したソースを **決定的** にコンパイルしてバイナリを生産する phase。LLM 推論を要さず、MCP サーバー経由のビルドコマンド呼び出しのみを行う。Build は標準 `substep` を持たない単一 `step` とする。

## I/O 契約
- execution input: `source/<source_id>/src/`、`spec.ir.yaml` の `impl_defaults`
- verification input: `spec.ir.yaml`、`source_meta.json`
- 出力: `workspace/pipelines/<node_key_safe>/<pipeline_id>/binary/<binary_id>/bin/`、`binary_meta.json`、`compile_project` の `command_id` と `command_log_ref`
  - `build_system=make` の in-source Make は out-of-source override で実行する: 実行 binary は `BINDIR=<pipeline>/binary/<binary_id>/bin` へ、object/`.mod` は `OBJDIR=workspace/tmp/<agent_run_id>/build`（per-run tmp、auto-clean）へ出力し、`src/` には cross-phase MCP audit log 以外を書かない。`binary_meta.json#binary_artifact_ref` は `binary/<binary_id>/bin/<exe>` を指す。

## `binary_id` フォーマット
- 形式: `bin_<YYYYMMDD>_<seq3>`、例: `bin_20260511_001`

## 必須要件
- `Build` は `MCP` サーバー経由で実行する。LLM 推論で artifact を生成してはならない。
- `Build` は `compile_project` を使用し、`fortran` / `c` / `cpp` / `mixed` 系では依存関係を扱える標準ビルドツール（既定 `make`）を使用する。
- `spec.ir.yaml.impl_defaults.toolchain.build_system=make` の `Build` 入力は、`src/Makefile` が言語依存のコンパイル順序依存を前提条件として明示した依存関係完全版でなければならない。
- `spec.ir.yaml.impl_defaults.toolchain.build_system=make` の `Build` は、`make -j` で成否が変化しない依存記述を必須とする。
- `spec.ir.yaml.impl_defaults.toolchain.build_system=make` の `Build` は、`compile_project` の `extra_args` に `OBJDIR=<abs>/workspace/tmp/<agent_run_id>/build` と `BINDIR=<abs>/<pipeline>/binary/<binary_id>/bin` を渡し、build artifact を `src/` 外（object は per-run tmp、exe は `binary/<binary_id>/bin/`）へ出さなければならない。実行 binary path は `allowed_output_paths` に file 形式で列挙する。`src/` 配下への `.o`/`.mod`/exe 書き込みは Build capability write_root（`binary/` のみ）外であり `unauthorized_write_violation` → `fail_closed` を招く。
- `compile_project` の実コマンド記録は `JSONL` 形式で保存し、既定の保存先は `project_dir/mcp_command_log.jsonl` とする。
- `Build` の試行メタデータ (`binary_meta.json`) は `command_id` と `command_log_ref`（または `command_log_path`）を追跡可能に記録する。
- `Build` は依存を持つ `node` で、依存 `operation` 解決先が `spec.ir.yaml.dependency` と一致することを必須検証とする。不一致時は `Build fail` とする。
- `Build` は `node` 単位で個別実行し、他 `node` の artifact を混在させてはならない。
- `Build` は、依存元 `src/` に依存 `node` 固有の `module`、`subroutine`、または `runner` 実装が混入している場合を `dependency implementation encapsulation` 違反として `fail` にしなければならない。
- `Build` 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_build --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` を実行し、必要に応じて `--source-id <source_id>` を付与する。`exit code 0` を必須とし、`fail` 時は `Build fail` とする。

## `binary_meta.json` 必須 key
- `attempt_count`、`verification_status`、`last_fail_reason`
- `source_source_id`: 入力ソースを発行した `source_id`
- `binary_artifact_ref`: 実行 binary の canonical path `binary/<binary_id>/bin/<exe>`（out-of-source `BINDIR` 出力。`src/` 配下を指してはならない）。`Validate.execute` の `run_program` 入力解決に必須。
- `command_id`、`command_log_ref`（または `command_log_path`）
- `validation_stage`: `post_build` または `full`
- `failure_category`、`failure_source_refs[]`、`failure_excerpt`: `verification_status=fail` の場合に必須。Generate retry の deterministic な trigger に使用（詳細は「失敗時挙動」節）。

## 失敗時挙動
- `Build` 失敗は **必ず `Generate` に retry をフィードバック** する（Build は決定的処理ゆえコード以外に修正余地がない）。
- `Build` 自身を内部 retry してはならない（同一 source に対する再ビルドは結果が変わらない）。

### retry trigger（LLM 非介在）
`Build` 失敗時の Generate への戻りは **LLM 推論を伴わず deterministic に決定**する。`binary_meta.json` に次のフィールドを必須記録し、`orchestration agent` はこれを読んで Generate に転送する:

| field | 値域 | 抽出元 |
|---|---|---|
| `failure_category` | `compile_error` / `link_error` / `make_error` / `dependency_violation` / `validate_post_build_violation` | `command_log` の終了コードと stderr パターンで機械的に分類 |
| `failure_source_refs[]` | `src/...` への path list | `command_log` 内のエラー出力から抽出したソース path 群 |
| `failure_excerpt` | text (last 50 lines of stderr) | `command_log` 直接抜粋 |

`failure_category` の分類規約:
- `compile_error`: コンパイラの非 0 終了 + stderr に `error:` / `エラー:` を含む。
- `link_error`: linker の `undefined reference` / `unresolved external` 等を検出。
- `make_error`: `make` 自体の依存解決失敗（`No rule to make target` 等）。
- `dependency_violation`: `dependency implementation encapsulation` 違反検出（依存 `node` の実装混入）。
- `validate_post_build_violation`: `validate_pipeline_semantics --stage post_build` の structural fail。

### `repair_strategy` の決定（LLM 非介在）
`orchestration agent` は `failure_category` から deterministic に `repair_strategy` を選ぶ:

| `failure_category` | `repair_strategy` | 根拠 |
|---|---|---|
| `compile_error` | `reuse` | syntax / type 局所修正で収束する想定 |
| `link_error` | `reuse` | 関数シグネチャ整合の局所修正 |
| `make_error` | `restart` | Makefile / 依存記述全体の再構成が必要 |
| `dependency_violation` | `restart` | encapsulation 違反は構造再生成を要する |
| `validate_post_build_violation` | `restart` | 構造 invariant 違反は局所修正で収束しない |

`Generate` 再投入の `launches/<new_agent_run_id>.request.json#repair_reason` には `binary_meta.json` の `failure_category` / `failure_source_refs[]` / `failure_excerpt` を引用する。Generate 側は `failure_source_refs[]` が指すソースに限定して修正することを既定とする（`restart` 時を除く）。

## 設計トレードオフ
- 失敗が必ず `Generate` に戻ることから「Build を Generate の verify に統合すべきか」が論点となるが、Build は **観測可能な一次成果物 (binary)** を生産する primary producer であり、phase を独立させる方が:
  - LLM 駆動の合成 (`Generate`) と 決定的コンパイル (`Build`) という性質の違いを明示できる
  - バイナリが `workspace/` 一級 artifact として残り、外部ツール (debugger / profiler / 再実行) からも再利用可能
  - build 成功 ≠ 実行成功 の観測点が orchestration から見える

  という利点が勝るため、独立 phase とする。
