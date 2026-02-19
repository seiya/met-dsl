# 技術スタック案と連携

用語は `docs/GLOSSARY.md` を参照。

## 方針
- 契約（case.resolved / diagnostics / verdict / perf）は固定し、実装ターゲットを差し替える。
- 物理アルゴリズム（A）は決定的に固定し、実行アルゴリズム（B）は探索可能にする。
- LLMは交換可能。品質はテストで担保する。

## 推奨スタック
### Spec
- Markdown + YAMLブロック（当面）

### Plan/Runner/Tuner
- Python
 - runner: deterministic expansion + 判定 + artifacts管理
 - tuner（Phase 2+）: impl候補生成と探索、perf目的関数評価

### Simulator（参照実装）
- Phase 0-1: Python+NumPy
- Phase 2+: Fortran互換実装を追加
- Phase 3 GPU入口: Fortran+OpenACC、必要に応じCUDA Fortran等で最適化

### 性能計測
- Phase 0: walltime/throughput（必須）
- Phase 3: プロファイル要約（任意）, 複数回統計（推奨）

## 連携I/F（必須）
- simulate:
 - 入力: case.resolved.yaml, impl.resolved.yaml（任意）
 - 出力: diagnostics.json, perf.json
- runner:
 - 判定: verdict.json
 - 集計: summary.json


関連ドキュメント:
- `docs/IMPL_PLAN_SPEC.md`（impl.resolved.yaml仕様）
- `docs/AI_TUNING_WORKFLOW.md`（自動チューニング運用）
