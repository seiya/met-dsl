# Docs Index

このドキュメント群は「読む順＝進め方」になるように構成する。

## 最短の読む順
1. `DECISIONS.md`（不変の原則）
2. `SPEC.md`（最終ゴールとスコープ）
3. `CONTROLLED_SPEC.md`（正本の書式と必須要件）
4. `PHYSICAL_VALIDATION.md`（物理妥当性判定の要件）
5. `GLOSSARY.md`（Artifacts / 用語）
6. `WORKFLOW.md`（Spec→Plan→Generate→Execute→Judge→Tune）
7. `RUNBOOK.md`（試行を回すための最小運用手順）
8. `IMPL_PLAN_SPEC.md`（impl.resolved.yaml 仕様）
9. `PERFORMANCE_DIAGNOSTICS.md`（perf.json 仕様）
10. `TUNING_WORKFLOW.md`（性能探索の運用指針）

## 役割別の構成
### Core（方向性・契約）
- `DECISIONS.md`
- `SPEC.md`
- `CONTROLLED_SPEC.md`
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
- 迷ったら `DECISIONS.md` に立ち戻る。
- 仕様の追加・変更は `SPEC.md` と `CONTROLLED_SPEC.md` と `physical_tests` を更新する。
- 言語に依らず、生成コードは `model`（物理計算）と `runner`（実行・判定連携）を分離する。
- 試行手順が固まり次第、`RUNBOOK.md` を前提に自動化を進める。
- `compile` / `run` / `quality check` は MCP サーバー（`mcp_servers/build_runtime_server.py`）経由で実行する。
