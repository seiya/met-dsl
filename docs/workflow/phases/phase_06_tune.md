# Phase contract: Tune

### 6. Tune
- execution input: 固定した `case.resolved.yaml`、探索対象 `impl` 候補
- verification input: 候補ごとの `diagnostics.json` / `perf.json` / `verdict.json`
- 出力: 採用 `impl.resolved.yaml`、チューニング試行ごとの評価結果

#### 6-1. generate substep
- 同一 `case.resolved.yaml` に対して複数 `impl.resolved.yaml` 候補を生成し、比較対象の試行を構成する。

#### 6-2. verify substep
- `verify substep` は候補ごとの `diagnostics.json` / `perf.json` / `verdict.json` を比較し、物理合格を満たす範囲で性能目的関数を最大化する候補を選定する。
- 詳細は `TUNING_WORKFLOW.md` を参照する。

