# フィージビリティ（実現可能性）とリスク整理

この文書は、提案全体が実現可能かを単独で評価できるようにまとめる。
ArtifactsやL0-L3の定義は `docs/GLOSSARY.md` を参照。

## 選択する前提
- Phase 0-1参照実装: Python+NumPy
- Phase 3 GPU入口: Fortran+OpenACC（nvfortran）
- LLMは特定モデルに固定しない。品質はテストで保証する。

## 主なリスクと対策
- 生成の非決定性: Plan決定、テンプレ穴埋め、小粒度パッチ
- テスト爆発: レベル分け（L0-L3）、メタデータ、quick/slow
- GPU同値性: CPU参照固定、物理妥当性指標、同値性回帰
- 物理過程の曖昧さ: 力学から開始、性質テスト中心

## 成功条件
- Phase 0: L0-L3が安定して回り、契約（Artifacts）が固定される
- Phase 1: Spec→planが決定的になり、LLM生成でも回帰が安定
- Phase 3: GPU同値性回帰が成立
