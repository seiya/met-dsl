# 全体ワークフロー: Spec -> Plan -> Generate -> Build -> Execute -> Judge -> Tune -> Promote

workflow の契約本文は `docs/workflow/` 配下に分割して配置する。本ファイルはその入口である。terms は `GLOSSARY.md` を参照する。

## 共通編（canonical source）

- [workflow/WORKFLOW_CORE.md](workflow/WORKFLOW_CORE.md): phase sequence、workflow 共通不変規範、`phase` 別 I/O 契約一覧、artifact layout rules、完了判定基準、エージェント参照範囲

## phase 契約詳細（canonical source）

- [workflow/phases/phase_00_spec.md](workflow/phases/phase_00_spec.md): 0 仕様作成（人間）
- [workflow/phases/phase_01_plan.md](workflow/phases/phase_01_plan.md): 1 Plan
- [workflow/phases/phase_02_generate.md](workflow/phases/phase_02_generate.md): 2 Generate
- [workflow/phases/phase_03_build.md](workflow/phases/phase_03_build.md): 3 Build
- [workflow/phases/phase_04_execute.md](workflow/phases/phase_04_execute.md): 4 Execute
- [workflow/phases/phase_05_judge.md](workflow/phases/phase_05_judge.md): 5 Judge
- [workflow/phases/phase_06_tune.md](workflow/phases/phase_06_tune.md): 6 Tune
- [workflow/phases/phase_07_promote.md](workflow/phases/phase_07_promote.md): 7 Promote

## 横断規約（canonical source）

- [ORCHESTRATION.md](ORCHESTRATION.md): エージェント階層実行契約
