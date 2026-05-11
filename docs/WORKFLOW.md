# 全体ワークフロー: Spec -> Compile -> Generate -> Build -> Validate

`workflow` の最終目的は、自然言語の `Controlled Spec` から実行可能なコード（`model` + `runner`）を生成し、`tests` で要求された振る舞いを満たすことを実行結果で確認することにある。本 workflow はこの目的のために 5 phase で構成し、各 phase は **observable な primary producer** として一次成果物を 1 種類だけ生産する。

workflow の契約本文は `docs/workflow/` 配下に分割して配置する。本ファイルはその入口である。terms は `GLOSSARY.md` を参照する。

## phase 序列と一次成果物

| # | phase | 役割 | 一次成果物 |
|---|-------|------|----------|
| 0 | Spec | 自然言語仕様の人手記述 | `controlled_spec.md` / `tests.md` / `deps.yaml` |
| 1 | Compile | 自然言語仕様 → 構造化 IR | `spec.ir.yaml` |
| 2 | Generate | IR → ソースコード | `source/<source_id>/` |
| 3 | Build | ソース → バイナリ（決定的） | `binary/<binary_id>/bin/` |
| 4 | Validate | 実行 + 合否判定 | `verdict.json` / `aggregate_verdict.json` |

phase 境界は **「観測可能な一次成果物の階層」** で切る。失敗時のフィードバック方向（例: Build 失敗 → Generate 再走）は phase 境界の判定基準としない。

## 任意フロー

最適化 (`Tune`) と昇格 (`Promote`) は core workflow の必須経路から外し、独立した任意フローとして扱う。core workflow は構造 IR と実装裁量の混在を持たず、Tune がこれを variant 探索する。詳細は別 plan で扱う。

## 共通編（canonical source）

- [workflow/WORKFLOW_CORE.md](workflow/WORKFLOW_CORE.md): phase sequence、workflow 共通不変規範、`phase` 別 I/O 契約一覧、artifact layout rules、完了判定基準、エージェント参照範囲

## phase 契約詳細（canonical source）

- [workflow/phases/phase_00_spec.md](workflow/phases/phase_00_spec.md): 0 Spec（人手）
- [workflow/phases/phase_01_compile.md](workflow/phases/phase_01_compile.md): 1 Compile
- [workflow/phases/phase_02_generate.md](workflow/phases/phase_02_generate.md): 2 Generate
- [workflow/phases/phase_03_build.md](workflow/phases/phase_03_build.md): 3 Build
- [workflow/phases/phase_04_validate.md](workflow/phases/phase_04_validate.md): 4 Validate

## 横断規約（canonical source）

- [ORCHESTRATION.md](ORCHESTRATION.md): エージェント階層実行契約
- [SPEC.md](SPEC.md): 全体方針、`spec` 管理要件、registry 要件
