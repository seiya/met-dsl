# AI自動チューニングの試行錯誤ワークフロー

## 結論
**tunerは generator から分離**し、次のループを選択する。

1) 物理Plan固定（case.resolved） 
2) 実装Plan探索（impl.resolvedのselectedを変える） 
3) 生成/ビルド/実行/評価を自動化 
4) 物理合格を制約にして性能目的関数を最適化 
5) 最良候補を“固定”して回帰監視に入れる 

この方式が良い理由:
- 物理保証（A固定）と性能探索（B探索）を明確に分離できる
- 生成モデルを交換しても tuner のロジックが変わらない
- 失敗時の原因局所化がしやすい（物理fail vs 性能ノイズ vs 実装未対応）

## 1. ループの構成（実務的な最小形）
### Inputs
- `case.resolved.yaml`（物理固定）
- `impl.search.yaml`（探索範囲: search_space）
- コードテンプレ（実装パターン群）

### Per-trial Outputs
- `impl.resolved.yaml`（selectedを確定）
- `diagnostics.json`（物理）
- `perf.json`（性能）
- `verdict.json`（物理合否 + 可能なら性能判定）
- `trial_meta.json`（ビルドログ、環境、乱数、git sha）

### Loop
- 候補生成（AI/BO/ルール）
- （必要なら）generatorでコード差分生成
 - 推奨: **コードの構造はテンプレで固定し、implノブで分岐**。LLMは新しい実装パターン追加時のみ使う。
- ビルド（ターゲット別）
- quick physics gate（L0-L2のサブセット）
- perf測定（複数回・統計）
- モデル更新（次候補を提案）

## 2. 候補生成方式
段階戦略を選択する。

### Stage A: ルールベース（最初に必須）
- 安全なノブのみ（tile, fuse, vectorize, layout）
- 探索点数を絞る（例: 20-50）
- 目的: “明確に速くなる”領域を見つける

### Stage B: ベイズ最適化（推奨）
- 離散ノブでも扱えるBO（TPE等）を想定
- ノイズを考慮し、同一点を再測定する

### Stage C: LLM支援（必要時のみ）
- 新しい実装パターンの追加（例: fused kernelを新設、async haloを追加）
- “ノブの追加”を提案し、search_spaceを拡張する
- 注意: LLMは探索の主役ではなく、探索空間の設計支援に使う

## 3. 物理合格ゲート
- 物理failの候補は性能評価しない（評価コスト削減）
- 許容は物理的妥当性一致（bitwise不要）
- quick→full の2段階を選択
 - quick: 小さいnx/短いt_end（ノイズと時間のバランス）
 - full: 本番に近いケース

## 4. 性能測定の扱い
- `perf.json` は最小でも walltime と throughput を必須
- GPUはウォームアップを入れ、複数回測定して平均/分散を保存
- baseline と比較する performance regression を L3 に追加可能

## 5. キャッシュと再利用
- `case_hash` と `impl_hash` で結果をキャッシュし、同一試行を再実行しない
- ビルド成果物も hash で再利用する（可能なら）

## 6. いつ“決め打ち”するか
- チューニングで得た best impl を `impl.resolved.yaml` として固定し、
 - 回帰（物理 + 性能）に入れる
- 新アーキ/新コンパイラのときだけ再チューニングする
