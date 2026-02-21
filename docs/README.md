# Docs Index

このドキュメント群は「読む順＝進め方」になるように構成する。

## 最短の読む順
1. `SPEC.md`（不変原則・最終ゴール・スコープ）
2. `CONTROLLED_SPEC.md`（正本の書式と必須要件）
3. `PHYSICAL_TESTS.md`（physical_tests の書式と必須要件）
4. `PHYSICAL_VALIDATION.md`（物理妥当性判定の要件）
5. `GLOSSARY.md`（Artifacts / 用語）
6. `WORKFLOW.md`（Spec→Plan→Generate→Execute→Judge→Tune→Promote。`LLM` ステージは内部 verify loop を含む）
7. `RUNBOOK.md`（試行を回すための最小運用手順）
8. `IMPL_PLAN_SPEC.md`（impl.resolved.yaml 仕様）
9. `PERFORMANCE_DIAGNOSTICS.md`（perf.json 仕様）
10. `TUNING_WORKFLOW.md`（性能探索の運用指針）

## 役割別の構成
### Core（方向性・契約）
- `SPEC.md`
- `CONTROLLED_SPEC.md`
- `PHYSICAL_TESTS.md`
- `PHYSICAL_VALIDATION.md`
- `GLOSSARY.md`

### Loop（試行を回す）
- `WORKFLOW.md`
- `RUNBOOK.md`

### Execution/Performance（実装と性能）
- `IMPL_PLAN_SPEC.md`
- `PERFORMANCE_DIAGNOSTICS.md`
- `TUNING_WORKFLOW.md`

## 運用ルール
- 迷ったら `SPEC.md` の「不変原則」に立ち戻る。
- 仕様の追加・変更は `SPEC.md` と `CONTROLLED_SPEC.md` と `PHYSICAL_TESTS.md` と対象 `physical_tests` を更新する。
- 言語に依らず、生成コードは `model`（物理計算）と `runner`（実行・判定連携）を分離する。
- `LLM` を使うステージは、ステージ内部で `generate -> verify -> regenerate` を実施し、最終合格成果物のみを保存する。
- `LLM` 利用ステージは各ステージの `<stage>_meta.json` を必須出力とし、標準運用（`debug_mode=false`）では失敗試行成果物を保存しない。
- 試行手順が固まり次第、`RUNBOOK.md` を前提に自動化を進める。
- `compile` / `run` / `quality check` は MCP サーバー（`mcp_servers/build_runtime_server.py`）経由で実行する。
