# Runbook（試行を回す最小手順）

この文書は「試行を回すための最小運用手順」を定義する。運用知見に応じて更新する。

## 0. 目的
- Controlled Spec（物理定義）と physical_tests（妥当性検証プロファイル）から実行と判定を行い、物理妥当性と性能を評価する。
- 失敗の原因を**Spec / Plan / Generate / Execute / Judge / Tune**のどこにあるか切り分ける。

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

## 2. 最小ループ
1. **Spec 更新**: Controlled Spec を修正し、曖昧さ・欠落を解消する
2. **Test 更新**: 実験条件・判定条件を physical_tests で更新する
3. **Plan 生成**: `case.resolved.yaml` を決定的に生成
4. **実装 Plan 決定**: `impl.resolved.yaml` を固定（探索する場合は候補集合を用意）
5. **生成**: LLM またはテンプレ補完で `model` と `runner` を分離して生成
6. **Build**: MCP サーバーの `compile_project` で依存関係を扱える標準ビルドツールを実行する（`fortran` / `c` 系の既定値は `make`）
7. **実行**: MCP サーバーの `run_program` で runner（例: `simulate`）を実行し、runner 経由で model を呼び出して diagnostics/perf を出力
8. **判定**: 物理判定を実施し、verdict を生成
9. **記録**: spec_version / test_suite_version / case_hash / impl_hash / git_sha を保存
   - `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` を保存する
10. **次アクション**: 失敗分類に応じて戻る場所を決める

## 3. 失敗時の戻り先（指針）
- **Spec 不備**: 曖昧・欠落・単位不整合 → Spec へ戻る
- **Test 不備**: ケース展開・閾値・実行条件の矛盾 → physical_tests へ戻る
- **物理 fail**: A の選択ミス、境界実装の矛盾 → Controlled Spec または case へ戻る
- **実装 fail**: 生成ミス、未対応ノブ → Generate/impl へ戻る
- **性能未達**: B の探索不足 → impl 探索へ戻る
- **再現性崩れ**: 決定性の破壊 → Plan/ 実行環境へ戻る

## 4. 運用の最小チェックリスト
- Spec に未定義項目がない
- case.resolved が決定的に生成できる
- diagnostics/perf/verdict が揃って出る
- 物理判定の根拠が追跡できる
