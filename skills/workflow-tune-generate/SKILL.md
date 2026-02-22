---
name: workflow-tune-generate
description: Tune ステージの generate を実行し、固定された `case.resolved.yaml` を維持したまま候補 `impl.resolved.yaml` を生成するときに使用する。性能探索用の `impl.search.yaml` から trial 候補を展開する作業に適用する。
---

# Workflow Tune Generate

## 目的
Tune ステージの候補生成責務を固定し、物理固定条件下で性能探索候補を作成する。

## 適用範囲
- `impl.search.yaml` から `impl.resolved.yaml` 候補を生成する作業
- `LLM` 支援で探索空間拡張候補を生成する作業

## 要件
- `case.resolved.yaml` は固定し、物理アルゴリズムを変更しない。
- 候補は `impl.resolved.yaml` の `selected` を変更して生成する。
- `tile` と `fuse` と `vectorize` と `layout` など安全ノブを優先する。
- 新規実装パターン提案時は `search_space` への追加根拠を記録する。
- `LLM` を使う場合は `SPEC.md` の `LLM` 規約を適用し、`<stage>_meta.json` を出力する。

## 運用ルール
1. 候補ごとに `impl_hash` を発行し、重複候補を再実行しない。
2. 候補生成後は `Generate` / `Build` / `Execute` / `Judge` を同一 `case` で実行する。
3. `debug_mode=false` を標準にし、失敗試行成果物を保存しない。
4. 候補生成失敗時は `last_fail_reason` を更新し、verify に引き渡す。

## 判定基準
- すべての候補が `case.resolved.yaml` 固定条件を満たす。
- 候補差分が `impl.resolved.yaml` の探索対象ノブに限定される。
- メタデータが再実行に必要な情報を保持する。
