---
name: workflow-tune-verify
description: Tune ステージの verify を実行し、候補 `impl.resolved.yaml` の物理合格、品質条件、性能目的関数を評価して採用可否を判定するときに使用する。`best impl` の固定と回帰移行判定に適用する。
---

# Workflow Tune Verify

## 目的
Tune ステージ候補の検証責務を固定し、採用候補を客観指標で選別する。

## 適用範囲
- 候補 `impl.resolved.yaml` の trial 結果評価
- `best impl` の確定と再チューニング判定

## 要件
- 物理 `fail` 候補は性能評価対象から除外する。
- 品質比較結果が不合格の候補は採用しない。
- 性能評価は `perf.json` の統計値を用い、目的関数で順位付けする。
- 同一点の再測定結果を扱い、ノイズに頑健な判定を行う。
- 採用候補は `trial_meta.json` と `lineage.json` で追跡可能であることを必須条件にする。
- workflow mode は `METDSL_WORKFLOW_EXEC_MODE` を canonical source とし、未設定時は `dev` を適用する。
- `dev` mode では `issue_severity=major|critical` を検出した時点で `Tune fail` とし、軽微例外扱いを禁止する。

## 運用ルール
1. `verdict` と `aggregate_verdict` が `pass` の候補のみ採用候補に残す。
2. 採用候補の中から目的関数最大の `impl.resolved.yaml` を `best impl` に固定する。
3. 新アーキテクチャまたは新コンパイラ条件では再チューニング判定を実行する。
4. 判定根拠を `tuning` 系メタデータに保存し、回帰監視へ引き渡す。
5. `dev` mode で `fail` した場合は、`failure_analysis.json` 作成に必要な根拠（失敗理由、対象 trial、判定エビデンス）を記録する。

## 判定基準
- 採用候補が物理と品質の必須条件を満たす。
- 性能順位付けが `perf.json` の保存値で再現できる。
- `best impl` の確定根拠が追跡可能である。
