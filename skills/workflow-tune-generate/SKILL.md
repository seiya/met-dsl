---
name: workflow-tune-generate
description: Tune ステージの generate を実行し、core workflow で確定した `spec.ir.yaml` の構造を不変に保ったまま `impl_defaults` の knob レイヤ上書き候補を生成するときに使用する。任意フローとして性能探索用の `tuning.spec` から trial 候補を展開する作業に適用する。
---

# Workflow Tune Generate

## 目的
Tune ステージの候補生成責務を固定し、物理固定条件下で性能探索候補を作成する。本フローは core workflow から分離された **任意フロー** であり、`spec.ir.yaml` を不変前提として扱う。

## 適用範囲
- `tuning.spec` から `spec.ir.yaml.impl_defaults` の knob レイヤ上書き variant を生成する作業
- `LLM` 支援で探索空間拡張候補を生成する作業

## 要件
- `spec.ir.yaml` の `case` / `algorithm` / `io_contract` / `dependency` セクションは固定し、物理アルゴリズムを変更しない。
- 候補は `spec.ir.yaml.impl_defaults` の **knob レイヤのみ** (`abstract.*` / `backend_overrides.*`) を変更して生成する。fixed レイヤ (`target.*` / `toolchain.*` / `selected.*`) の越境は禁止する（canonical 境界: `docs/workflow/phases/phase_01_compile.md` の「impl_defaults の fixed / knob 境界」節）。
- `tuning.spec` が fixed sub-key を上書きする entry を含む場合、Tune を起動せず `fail_closed` で停止する。
- `tile` と `fuse` と `vectorize` と `layout` など安全ノブを優先する。
- 新規実装パターン提案時は `tuning.spec` の `search_space` への追加根拠を記録する。
- `LLM` を使う場合は `SPEC.md` の `LLM` 規約を適用し、`<stage>_meta.json` を出力する。

## 運用ルール
1. 候補ごとに `impl_hash` を発行し、重複候補を再実行しない。`impl_hash` は `spec.ir.yaml.impl_defaults` の最終値（knob 上書き後）から計算する。
2. 候補生成後は variant 用 `spec.ir.yaml` を別 path に保存し、core workflow と同等の `Generate` / `Build` / `Validate` を同一 `case` で実行する。
3. `debug_mode=false` を標準にし、失敗試行 artifact を保存しない。
4. 候補生成失敗時は `last_fail_reason` を更新し、verify に引き渡す。

## 判定基準
- すべての候補が `spec.ir.yaml` の `case` / `algorithm` / `io_contract` / `dependency` 固定条件を満たす。
- 候補差分が `impl_defaults` の knob レイヤ (`abstract.*` / `backend_overrides.*`) に限定される。
- メタデータが再実行に必要な情報を保持する。
