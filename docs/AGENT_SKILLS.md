# Agent Skills Mapping

この文書は、プロジェクト内で利用する `skills` の参照規約を定義する。

## 目的
- `Codex` / `Gemini` / `Claude Code` で同一の phase 定義を使う。

## 適用範囲
- `workflow` 全体を統括する `orchestration agent`
- ワークフロー phase `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- 各 phase で参照する `skills/<skill_name>/SKILL.md`

## 要件
- エージェントは、作業対象 phase を特定してから対応 `SKILL.md` を読み込む。
- `generate -> verify -> regenerate` を持つ phase は、`generate` 用と `verify` 用の 2 つの `SKILL` を必ず分離適用する。
- workflow 共通の不変規範（過去 artifact 参照禁止、`dummy` 禁止、検証契約導出、`workspace/` ルート制約、`quality check` 判定軸）は `docs/workflow/WORKFLOW_CORE.md` を canonical source とする。各 `phase` の詳細契約は `docs/workflow/phases/` 配下のファイルを canonical source とする。仕様への入口は `docs/WORKFLOW.md` とする。
- エージェント階層の実行契約（`orchestration -> step` と `orchestration -> substep`）は `ORCHESTRATION.md` を canonical source とする。
- 全体方針と `spec` 管理要件（`spec_kind` / registry / 正式版配置 / 命名規則）は `SPEC.md` を canonical source とする。
- `Build` / `Execute` / `quality check` は `MCP` サーバー経由で実行し、`AGENTS.md` の `MCP 実行ルール` と対応 `SKILL.md` の契約を同時適用する。
- 各 phase は、対応 `SKILL.md` に定義された必須出力（例: `plan_meta.json`、`build_meta.json`、`verdict.json`）を欠落させてはならない。
- `SKILL.md` へは実行手順と当該 `SKILL` 固有の手続きを記述し、`phase` の I/O 契約・artifact 形式・数値的正規要件を `docs/workflow/WORKFLOW_CORE.md` または `docs/workflow/phases/phase_*.md` と矛盾する形で重複定義してはならない。

## 責務判定フロー
1. 追加・変更する規則が workflow artifact の正当性を直接左右するかを判定する。
2. 正当性を直接左右する場合は `docs/workflow/WORKFLOW_CORE.md` または該当する `docs/workflow/phases/phase_*.md` へ記述する。
3. workflow 共通規範ではなく、`spec` registry・命名・配置・昇格などの全体方針を定義する場合は `SPEC.md` へ記述する。
4. 規則がツール呼び出し手順、入力収集順、再生成手順、失敗時オペレーションなど実行方法の詳細である場合は対応 `SKILL.md` へ記述する。
5. エージェント固有の実行便宜（例: プロンプト順序、ログ整理手順）は `SKILL.md` に限定し、`docs/workflow/WORKFLOW_CORE.md` および `docs/workflow/phases/` へ混在させない。
6. 判定に迷う場合は、規則違反時の影響が監査可能性・再現性・判定整合の破壊に及ぶかを判定軸とする。破壊する場合は `docs/workflow/` 配下の契約文書、破壊しない場合は `SKILL.md` を選択する。

## phase と Skill 対応表
- `Workflow orchestration`: `skills/workflow-orchestration/SKILL.md`
- `Plan generate`: `skills/workflow-plan-generate/SKILL.md`
- `Plan verify`: `skills/workflow-plan-verify/SKILL.md`
- `Generate generate`: `skills/workflow-generate-generate/SKILL.md`
- `Generate verify`: `skills/workflow-generate-verify/SKILL.md`
- `Build`: `skills/workflow-build/SKILL.md`
- `Execute`: `skills/workflow-execute/SKILL.md`
- `Judge`: `skills/workflow-judge/SKILL.md`
- `Tune generate`: `skills/workflow-tune-generate/SKILL.md`
- `Tune verify`: `skills/workflow-tune-verify/SKILL.md`
- `Promote`: `skills/workflow-promote/SKILL.md`

## 運用ルール
1. 1 回の作業で複数 phase を扱う場合、phase ごとに対応 `SKILL` を切り替える。
2. `verify` で失敗した場合、同一 phase の `generate` に戻し、再生成後に再検証する。
3. ループの状態と失敗理由は、該当 phase のメタデータへ記録する。
4. `SKILL` 定義を変更した場合、この対応表を同一変更で更新する。
5. workflow 契約を変更する場合は `docs/workflow/WORKFLOW_CORE.md` または該当 `docs/workflow/phases/phase_*.md` を先に更新し、その変更に追従して `SKILL.md` を更新する。
6. workflow 共通規範の変更は `docs/workflow/WORKFLOW_CORE.md`、各 `phase` 詳細契約の変更は `docs/workflow/phases/`、階層実行契約の変更は `ORCHESTRATION.md`、phase 手順の変更は対応 `SKILL.md` へ記述する。
7. `AGENT_SKILLS.md` へは規則本文を再掲せず、参照先と責務判定を記述する。

## 判定基準
- 対象 phase で使用した `SKILL` パスを説明できる。
- 生成 artifact と判定 artifact が、対応 `SKILL` の契約と一致する。
- エージェント間で同一入力に対する phase 選択が一致する。
- workflow 共通規範、階層実行契約、phase 手順の参照先が一意に定まる。
- 同一規則が `docs/workflow/WORKFLOW_CORE.md` または `docs/workflow/phases/` と `ORCHESTRATION.md` と `SKILL.md` に重複再掲されていない。
