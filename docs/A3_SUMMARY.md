# A3要約: LLMベース数理仕様→気象モデル生成基盤

## 1. ゴール
Controlledな数理仕様からLLMで数値モデルコードを生成し、物理的妥当性で合否を保証する。bitwise一致は不要。将来GPU/将来HWと性能最適化を含む。

## 2. 契約（Artifacts）
- case.resolved.yaml（物理決定: 物理アルゴリズムAを固定）
- impl.resolved.yaml（実装決定: 実行アルゴリズムBを指定、探索対象）
- diagnostics.json（物理診断）
- perf.json（性能診断: walltime/throughputなど）
- verdict.json / summary.json（判定と集計）

## 3. アルゴリズム2分類
- A 物理アルゴリズム: 精度・安定に影響（差分/時間積分等）→ case.resolvedで決定的
- B 実行アルゴリズム: 計算過程/性能のみ（タイル/融合/レイアウト/非同期等）→ impl.resolvedで探索

## 4. テスト階層（L0-L3）
L0: 演算子/ガード、L1: 解析解/収束、L2: 保存/制約、L3: ロバスト性/同値性（将来: 性能回帰）。期待失敗は意図どおり失敗したらPASS。

## 5. 選択技術スタック
- Phase 0-1参照実装: Python+NumPy
- GPU入口（Phase 3）: Fortran+OpenACC（nvfortran）、必要時CUDA Fortranでホットスポット最適化
- runner/tuner: Python
- LLM: LLM限定なし（交換可能）。品質はテストで担保。

## 6. AI自動チューニング（ワークフロー）
- tunerはgeneratorから分離
- case.resolvedを固定し、impl.resolvedのselectedを変えて試行
- quick物理ゲート→perf測定→モデル更新（ルール→BO→必要時LLMで実装パターン追加）
- best impl を固定して性能回帰に入れる

## 7. 成功条件
- 物理合格が決定的に再現
- impl（B）探索で物理合格を維持しつつ性能が改善
- LLMを差し替えても回帰が崩れない
