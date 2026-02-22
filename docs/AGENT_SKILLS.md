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
- `Build` / `Execute` / `quality check` は `MCP` サーバー経由で実行する。
- `SKILL` に記載された必須出力（例: `<stage>_meta.json`、`verdict.json`）を欠落させない。

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

## 判定基準
- 対象工程で使用した `SKILL` パスを説明できる。
- 生成成果物と判定成果物が、対応 `SKILL` の契約と一致する。
- エージェント間で同一入力に対する工程選択が一致する。
