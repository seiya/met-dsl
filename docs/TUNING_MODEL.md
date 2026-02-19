# 自動チューニング（Execution-only）設計

この文書は、性能チューニングを“試行錯誤ループ”として運用するための設計を単独で示す。
用語とA/B分類は `docs/GLOSSARY.md` を参照。
implの仕様は `docs/IMPL_PLAN_SPEC.md` を選択する。

## 結論
- tunerは generator から分離する。
- 物理Plan（case.resolved）は固定し、実装Plan（impl.resolved）を探索する。
- 生成コードはテンプレ骨格を固定し、implノブで分岐する。LLMは実装パターン追加時のみ使う。

詳細な運用ループは `docs/AI_TUNING_WORKFLOW.md` を参照。

## 最小導入（Phase 1-2）
- Phase 1: impl.resolved.yaml を導入し、simulateが selected を解釈できるようにする（探索はまだしない）
- Phase 2: ルールベースで少数の候補を試す（tile/fuse/layout等）
- Phase 3: GPUでBOを導入し、ノイズ対策（再測定、統計）を行う
