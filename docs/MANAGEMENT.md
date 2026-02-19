# 全体のマネージメント（運用・ガバナンス：推奨）

この文書は、プロジェクト運用を単独で理解できるようにまとめる。
L0-L3やArtifactsの定義は `docs/GLOSSARY.md` を参照。

## リポジトリ管理（推奨）
- 仕様: `spec/`
- ケース: `cases/`
- コード: `src/`
- 契約: `docs/`
- 実行成果物: `out/`（Git管理外）

## 変更管理（必須）
- 契約の破壊的変更はバージョニング（schema version）
- run_idに git sha を含める（推奨）

## 品質ゲート
- 実行順序: L0 → L1 → L2 → L3
- 早期停止: L0失敗時は上位スキップ（`--no_abort`で解除）
- 期待失敗（Guard/XFAIL）:
 - Phase 0では `checks.cfl_guard` を選択
 - Phase 1で `expect.failure_class` の一般機構へ移行推奨

## 必須成果物
各ケース:
- case.resolved.yaml / diagnostics.json / verdict.json / stdout.log / stderr.log
run全体:
- summary.json
