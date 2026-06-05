---
name: workflow-build
description: Build ステージを実行し、`source` artifact を `MCP` サーバー経由の `compile_project` でビルドして `binary_id` artifact を作成するときに使用する。`fortran` / `c` / `cpp` / `mixed` 系の標準ビルドツール制約を守る作業に適用する。
---

# Workflow Build

## 目的
Build ステージの実行責務を固定し、再現可能なビルド artifact を生成する。

## 適用範囲
- `workspace/pipelines/<pipeline_id>/binary/<binary_id>/` を生成する作業
- `source_meta.json` が `verification_status=pass` の artifact をビルドする作業

## 要件
- 本 phase が起動できる validator gate は `skills/workflow-orchestration/references/launch_prompts.md` の「substep ↔ allowed validator gate 対応表」を canonical source とする。
- `compile` は `MCP` サーバーの `compile_project` を使用する。
- `fortran` / `c` / `cpp` / `mixed` 系は `make` / `cmake` / `meson` / `ninja` の標準ビルドツールのみを許可する。
- `gcc` / `clang` / `gfortran` の単発ビルドを禁止する。
- `spec.ir.yaml.impl_defaults.toolchain.build_system=make` の場合、入力 `src/Makefile` は言語依存のコンパイル順序依存をターゲット前提条件として明示し、`make -j` で成否が変化しないことを必須とする。
- **out-of-source override (`build_system=make`):** in-source Make は `compile_project` の `extra_args` で `OBJDIR=<abs>/workspace/tmp/<agent_run_id>/build` と `BINDIR=<abs>/<pipeline>/binary/<binary_id>/bin` を渡す（`make -j<jobs> <target>` の後ろに append される make variable override）。これにより object/`.mod` は per-run tmp（auto-authorize + 成功時 auto-clean）へ、実行 binary は `binary/<binary_id>/bin/` へ出力され、`src/` には cross-phase audit log 以外を書かない。実行 binary `binary/<binary_id>/bin/<exe>` は launch の `allowed_output_paths` に **file 形式**で列挙する（auto-derive で `allowed_file_tool_paths` に入り terminal validation で authorize される。`allowed_file_tool_paths` は通常明示せず auto-derive に委ねる）。
- 依存を持つ `node` は、依存 `operation` の解決先が `spec.ir.yaml.dependency` と一致することを `Build` 時に検証しなければならない。不一致時は `Build fail` とする。
- `binary_meta.json` に `build_system` と `compiler` と `build_log_ref` と `status` と `source_source_id` を記録する。`source_source_id` は本ビルドが入力として使用した `<pipeline>/source/<source_source_id>/` の id を必須記録とする (`Validate.execute` が cross-phase MCP audit log の lineage 検証に使用する)。
- `binary_meta.json#binary_artifact_ref` は実行 binary の canonical 配置 `<pipeline>/binary/<binary_id>/bin/<exe>` を指す（out-of-source `BINDIR` 出力。`src/` 配下を指してはならない。`Validate.execute` の `run_program` 入力検証が `binary/<binary_id>/bin/` 配下解決を要求する）。
- 失敗時は `binary_meta.json` に `failure_category` / `failure_source_refs[]` / `failure_excerpt` を必須記録する（`docs/workflow/phases/phase_03_build.md` の「retry trigger（LLM 非介在）」節を canonical source とする）。`failure_category` は `compile_error` / `link_error` / `make_error` / `dependency_violation` / `validate_post_build_violation` のいずれか。
- `compile_project` の MCP `command_log` 出力は以下 2 つの canonical placement のみ許可する:
  - In-source build (Make for Fortran/C/cpp/mixed): `<pipeline>/source/<source_id>/src/mcp_command_log.jsonl` (cross-phase, project_dir=`<src>/src/`)。launch request に `source_id` を必ず含めて record_launch に通すこと (failed/stale source は record_launch が verification_status check で reject する)。
  - Out-of-source build (CMake/Meson/Ninja): `<pipeline>/binary/<binary_id>/mcp_command_log.jsonl` (in-phase、project_dir=`<binary_id>/`)。
  非 canonical placement に log が落ちる構成 (例: `<binary_id>/bin/mcp_command_log.jsonl`) は terminal validation で `unauthorized_write_violation` になる。
- 出力 `bin/` は `Validate.execute` が参照可能な相対配置にする。
- workflow artifact の保存先ルートは `workspace/` のみを許可し、workflow ルート判定は `workspace/` のみを対象とする。

## 運用ルール
1. `binary_id` を発行し、出力先を `workspace/pipelines/<pipeline_id>/binary/<binary_id>/` に固定する。
2. `source_meta.json` の `verification_status=pass` を開始条件にする。
3. ビルド失敗時は `binary_meta.json` に `failure_category` 等の retry trigger 情報を記録し、`Generate` へ戻す（deterministic な mapping は `docs/workflow/phases/phase_03_build.md` を canonical source）。
4. 同一 `source_id` の再ビルドは別 `binary_id` で append-only 運用にする。
5. 出力先が `workspace/` でない場合は `Build fail` とする。
6. workflow 実行開始前に `workspace/` が存在しない場合、リポジトリルート直下へ `workspace/` を作成する。
7. 開始前と完了前に `python3 tools/validate_workspace_root.py` を実行し、`fail` 時は `Build fail` とする。
8. 完了前に `python3 tools/validate_pipeline_semantics.py --stage post_build --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` を実行し、必要に応じて `--source-id <source_id>` を付与する。`exit code 0` を必須とし、`fail` 時は `Build fail` とする。

## 判定基準
- ビルド手段が `MCP compile_project` のみである。
- `binary_meta.json` の必須項目が欠落しない（失敗時は `failure_category` 等も欠落不可）。
- `workspace` 配置規約が `docs/workflow/WORKFLOW_CORE.md` と `docs/workflow/phases/phase_03_build.md` と一致する。
