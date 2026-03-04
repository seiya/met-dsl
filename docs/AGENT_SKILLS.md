# Agent Skills Mapping

この文書は、プロジェクト内で利用する `skills` の参照規約を定義する。

## 目的
- `Codex` / `Gemini` / `Claude Code` で同一の工程定義を使う。

## 適用範囲
- ワークフロー工程 `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- 各工程で参照する `skills/<skill_name>/SKILL.md`

## 要件
- エージェントは、作業対象工程を特定してから対応 `SKILL.md` を読み込む。
- `generate -> verify -> regenerate` を持つ工程は、`generate` 用と `verify` 用の 2 つの `SKILL` を必ず分離適用する。
- workflow 共通の不変規範（過去成果物参照禁止、`dummy` 禁止、検証契約導出、`workspace/` ルート制約、`quality check` 判定軸）は `WORKFLOW.md` を正本とする。
- 全体方針と `spec` 管理要件（`spec_kind` / 台帳 / 正式版配置 / 命名規則）は `SPEC.md` を正本とする。
- `Build` / `Execute` / `quality check` は `MCP` サーバー経由で実行し、`AGENTS.md` の `MCP 実行ルール` と対応 `SKILL.md` の契約を同時適用する。
- 各工程は、対応 `SKILL.md` に定義された必須出力（例: `<stage>_meta.json`、`verdict.json`）を欠落させてはならない。

## 責務判定フロー
1. 追加・変更する規則が workflow 成果物の正当性を直接左右するかを判定する。
2. 正当性を直接左右する場合は `WORKFLOW.md` へ記述する。
3. workflow 共通規範ではなく、`spec` 台帳・命名・配置・昇格などの全体方針を定義する場合は `SPEC.md` へ記述する。
4. 規則がツール呼び出し手順、入力収集順、再生成手順、失敗時オペレーションなど実行方法の詳細である場合は対応 `SKILL.md` へ記述する。
5. エージェント固有の実行便宜（例: プロンプト順序、ログ整理手順）は `SKILL.md` に限定し、`WORKFLOW.md` へ混在させない。
6. 判定に迷う場合は、規則違反時の影響が監査可能性・再現性・判定整合の破壊に及ぶかを判定軸とする。破壊する場合は `WORKFLOW.md`、破壊しない場合は `SKILL.md` を選択する。

## 工程と Skill 対応表
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
1. 1 回の作業で複数工程を扱う場合、工程ごとに対応 `SKILL` を切り替える。
2. `verify` で失敗した場合、同一工程の `generate` に戻し、再生成後に再検証する。
3. ループの状態と失敗理由は、該当工程のメタデータへ記録する。
4. `SKILL` 定義を変更した場合、この対応表を同一変更で更新する。
5. workflow 契約を変更する場合は `WORKFLOW.md` を先に更新し、その変更に追従して `SKILL.md` を更新する。
6. 依存 `DAG` 実行時は、`topo_level` 昇順で `node` を処理し、同一 `topo_level` の独立 `node` のみ並列実行する。
7. 同一 `topo_level` で一部 `node` が `fail` しても、独立 `node` の実行を中断しない。`topo_level` 完了後に次レベル進行可否を判定する。
8. 直下依存 `node` が `pass` または `xfail` でない場合、上位 `node` を `blocked` として終了する。
9. 直下依存に起因して `blocked` で終了する `node` は、`self_verdict=not_evaluated` を明示し、停止理由を `trial_meta.json` に記録する。
10. 工程入力不足で開始条件を満たせない場合、当該工程を `fail` で停止する。推測補完で進めない。
11. `spec_kind` を問わない workflow 実行で、リポジトリ管理外パス（例: `/tmp`）の補助スクリプトを workflow 実行経路に使用してはならない。

## 判定基準
- 対象工程で使用した `SKILL` パスを説明できる。
- 生成成果物と判定成果物が、対応 `SKILL` の契約と一致する。
- エージェント間で同一入力に対する工程選択が一致する。
