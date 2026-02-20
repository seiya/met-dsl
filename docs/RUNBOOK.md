# Runbook（試行を回す最小手順）

この文書は「試行を回すための最小運用手順」を定義する。運用知見に応じて更新する。

## 0. 目的
- Controlled Spec（物理定義）と physical_tests（妥当性検証プロファイル）から実行と判定を行い、物理妥当性と性能を評価する。
- 失敗の原因を**Spec / Plan / Generate / Execute / Judge / Tune / Promote**のどこにあるか切り分ける。

## 1. 入力と成果物（最小）
- 入力: `CONTROLLED_SPEC`（物理・アルゴリズム定義）
- 入力: `physical_tests`（ケース展開・実行条件・判定閾値）
- 生成: `case.resolved.yaml`（物理アルゴリズム A の固定）
- 生成: `impl.resolved.yaml`（実行アルゴリズム B の固定または探索候補）
- 生成: `model`（物理計算モジュール）と `runner`（実行・判定連携）
- 出力: `diagnostics.json`,`perf.json`,`verdict.json`,`summary.json`

## 1-1. 成果物配置（運用必須）
- Plan は `workspace/plans/<plan_id>/` に保存する。
- Generate/Build/Execute は `workspace/pipelines/<pipeline_id>/` に保存する。
- 各 `pipeline` には `lineage.json` を必須配置する。
- `execution` 成果物は `workspace/pipelines/<pipeline_id>/execute/<execution_id>/` に保存する。
- 判定時は `execution_id` 単位で読み込む。`execution_id` を跨ぐファイル混在を禁止する。
- 正式版成果物は `releases/<domain>/<component>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` に保存する。`workspace` は試行用途に限定する。

## 2. 最小ループ
1. **Spec 更新**: Controlled Spec を修正し、曖昧さ・欠落を解消する
2. **Test 更新**: 実験条件・判定条件を physical_tests で更新する
3. **Plan 生成**: `case.resolved.yaml` を決定的に生成する。`LLM` を利用する場合は `SPEC.md` の「LLM の扱い」を適用する
4. **実装 Plan 決定**: `impl.resolved.yaml` を固定（探索する場合は候補集合を用意）。`toolchain.language` の既定値は `target.class=cpu` で `fortran`、`target.class=gpu` で `cuda_fortran` とする
5. **生成**: `LLM` またはテンプレ補完で `model` と `runner` を分離して生成する。`LLM` を利用する場合は `SPEC.md` の「LLM の扱い」を適用する
6. **Build**: MCP サーバーの `compile_project` で依存関係を扱える標準ビルドツールを実行する（`fortran` / `c` 系の既定値は `make`）
7. **実行**: MCP サーバーの `run_program` で runner（例: `simulate`）を実行し、runner 経由で model を呼び出して diagnostics/perf を出力
8. **判定**: 物理判定を実施し、verdict を生成
9. **記録**: spec_version / test_suite_version / case_hash / impl_hash / git_sha を保存
   - `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` を保存する
   - `LLM` 利用ステージは各ステージの `<stage>_meta.json`（コード生成は `generate_meta.json`）に `attempt_count` / `verification_status` / `last_fail_reason` / `debug_mode` を保存する
   - `context_isolated=false` の場合は制約理由を記録する
   - `debug_mode=true` で失敗試行を保存した場合は保存件数と保存先を記録する
10. **チューニング**: 物理合格を満たす候補の中から性能目的関数で最良候補を選定し、採用する `impl.resolved` を確定する
11. **正式版昇格**: 採用する試行は `releases/<domain>/<component>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` へ昇格保存し、`spec/registry/spec_catalog.yaml` の `official_releases` に `release_id` / `target_architecture` / `toolchain_language` / `target_backend` / `source_pipeline_id` / `source_generation_id` / `source_build_id` / `source_execution_id` / `artifact_root` / `promoted_at` / `status` を記録する
12. **次アクション**: 失敗分類に応じて戻る場所を決める

## 3. 失敗時の戻り先（指針）
- **Spec 不備**: 曖昧・欠落・単位不整合 → Spec へ戻る
- **Test 不備**: ケース展開・閾値・実行条件の矛盾 → physical_tests へ戻る
- **LLM ステージ検証 fail**: `LLM` 利用ステージの出力が入力契約と不一致 → 当該ステージへ戻る（必要に応じて Spec/Test へ戻る）
- **物理 fail**: A の選択ミス、境界実装の矛盾 → Controlled Spec または case へ戻る
- **実装 fail**: 生成ミス、未対応ノブ → Generate/impl へ戻る
- **性能未達**: B の探索不足 → impl 探索へ戻る
- **再現性崩れ**: 決定性の破壊 → Plan/ 実行環境へ戻る

## 4. 運用の最小チェックリスト
- Spec に未定義項目がない
- case.resolved が決定的に生成できる
- `LLM` 利用ステージのメタデータで `verification_status` が pass である
- `debug_mode=false` の試行で失敗試行成果物が保存されていない
- diagnostics/perf/verdict が揃って出る
- 物理判定の根拠が追跡できる
- 正式版昇格を実施した試行は `spec_catalog.yaml` の `official_releases` と `release` 成果物配置が一致する
