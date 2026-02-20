# 自動チューニング運用ワークフロー

## 基本方針
tuner は generator から分離し、次のループを標準運用とする。

1. 物理 Plan 固定（case.resolved）
2. 実装 Plan 探索（impl.resolved の selected を変更）
3. 生成/ビルド/実行/評価の自動化
4. 物理合格を制約とした性能目的関数の最適化
5. 最良候補の固定と回帰監視への移行

設計要点:
- 物理保証（A 固定）と性能探索（B 探索）を明確に分離できる
- 生成モデルを交換しても tuner のロジックが変わらない
- 失敗時の原因局所化がしやすい（物理 fail vs 性能ノイズ vs 実装未対応）

## 1. ループの構成（実務的な最小形）
### Inputs
- `case.resolved.yaml`（物理固定）
- `impl.search.yaml`（探索範囲: search_space）
- コードテンプレ（実装パターン群）

### Per-trial Outputs
- `impl.resolved.yaml`（selected を確定）
- `<stage>_meta.json`（`LLM` 利用ステージ内検証の結果）
- `diagnostics.json`（物理）
- `perf.json`（性能）
- `verdict.json`（物理合否 + 可能なら性能判定）
- `trial_meta.json`（ビルドログ、環境、乱数、git sha）

### Loop
- 候補生成（LLM 支援/BO/ ルール）
- （必要なら）generator でコード差分生成
- `LLM` を利用する候補生成・コード生成は `SPEC.md` の「LLM の扱い」を適用する
- 標準運用は `debug_mode=false` とし、失敗試行成果物を保存しない。調査時のみ `debug_mode=true` を許可する
- 推奨: **コードの構造はテンプレで固定し、impl ノブで分岐**。 LLM は新しい実装パターン追加時のみ使う。
- ビルド（ターゲット別）
- quick physics gate（L0-L2 のサブセット）
- perf 測定（複数回・統計）
- モデル更新（次候補を提案）

## 2. 候補生成方式
段階戦略を選択する。

### Stage A: ルールベース（最初に必須）
- 安全なノブのみ（tile,fuse,vectorize,layout）
- 探索点数を絞る（例: 20-50）
- 目的: “明確に速くなる”領域を見つける

### Stage B: ベイズ最適化（推奨）
- 離散ノブでも扱える BO（TPE 等）を想定
- ノイズを考慮し、同一点を再測定する

### Stage C: LLM 支援（必要時のみ）
- 新しい実装パターンの追加（例: fused kernel を新設、async halo を追加）
- “ノブの追加”を提案し、search_space を拡張する
- 注意: LLM は探索の主役ではなく、探索空間の設計支援に使う

## 3. 物理合格ゲート
- 物理 fail の候補は性能評価しない（評価コスト削減）
- 許容は物理的妥当性一致（bitwise 不要）
- quick→full の 2 段階を選択
- quick: 小さい nx/ 短い t_end（ノイズと時間のバランス）
- full: 本番に近いケース

## 4. 性能測定の扱い
- `perf.json` は最小でも `walltime_sec`、`throughput_cells_per_sec`、`parallelism` を必須
- GPU はウォームアップを入れ、複数回測定して平均/分散を保存
- baseline と比較する performance regression を L3 に追加可能

## 5. キャッシュと再利用
- `case_hash` と `impl_hash` で結果をキャッシュし、同一試行を再実行しない
- ビルド成果物も hash で再利用する（可能なら）

## 6. いつ“決め打ち”するか
- チューニングで得た best impl を `impl.resolved.yaml` として固定する。
- 固定後は回帰（物理 + 性能）へ移行する。
- 新アーキ/新コンパイラのときだけ再チューニングする
