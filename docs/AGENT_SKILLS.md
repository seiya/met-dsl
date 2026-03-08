# Agent Skills Mapping

この文書は、プロジェクト内で利用する `skills` の参照規約を定義する。

## 目的
- `Codex` / `Gemini` / `Claude Code` で同一の工程定義を使う。

## 適用範囲
- `workflow` 全体を統括する `orchestration agent`
- ワークフロー工程 `Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote`
- 各工程で参照する `skills/<skill_name>/SKILL.md`

## 要件
- エージェントは、作業対象工程を特定してから対応 `SKILL.md` を読み込む。
- `workflow` 実行は `orchestration agent` を最初に起動し、`step` / `substep` の起動・停止・再試行を統括させなければならない。
- 標準 `substep` を持たない各 `step` は `step agent` を独立起動して実行しなければならない。
- `substep` を持つ各工程は、`orchestration agent` が各 `substep` の `substep agent` を独立起動して実行しなければならない。
- `workflow` 開始前に、`step agent` と `substep agent` の独立起動可否を検証する `preflight` を必須実行しなければならない。
- `workflow` 実行のために `step` / `substep` を一括代行する `script` を新規生成または実行してはならない。
- `step agent` と `substep agent` は `agent_run_id` ごとに固有 `context_id` を持ち、`context_isolated=true` を必須記録とする。
- `step` / `substep` の `agent_runs.jsonl` は `agent_session_id` と `launch_request_ref` と `launch_response_ref` を必須記録し、参照先実体を `workspace/orchestrations/<orchestration_id>/launches/` 配下へ保存しなければならない。
- `generate -> verify -> regenerate` を持つ工程は、`generate` 用と `verify` 用の 2 つの `SKILL` を必ず分離適用する。
- workflow 共通の不変規範（過去成果物参照禁止、`dummy` 禁止、検証契約導出、`workspace/` ルート制約、`quality check` 判定軸）は `WORKFLOW.md` を正本とする。
- エージェント階層の実行契約（`orchestration -> step` と `orchestration -> substep`）は `ORCHESTRATION.md` を正本とする。
- 全体方針と `spec` 管理要件（`spec_kind` / 台帳 / 正式版配置 / 命名規則）は `SPEC.md` を正本とする。
- `Build` / `Execute` / `quality check` は `MCP` サーバー経由で実行し、`AGENTS.md` の `MCP 実行ルール` と対応 `SKILL.md` の契約を同時適用する。
- 各工程は、対応 `SKILL.md` に定義された必須出力（例: `<stage>_meta.json`、`verdict.json`）を欠落させてはならない。
- `Promote` 以外の全工程は `workspace/` 配下以外への書き込みを禁止し、各工程開始前後で `write_scope` 検査を実施しなければならない。
- `Generate verify` は `derived_contract.json` の `semantic_dependency.required_sources` と `io_contract.outputs` を正本として `intent(out)` のデータ依存を判定し、特定計算様式の一律必須化を禁止しなければならない。
- `Judge` は固定スクリプト検査に加えて `LLM` 意味検査を実施し、`semantic_review.json` を必須成果物として扱わなければならない。

## 責務判定フロー
1. 追加・変更する規則が workflow 成果物の正当性を直接左右するかを判定する。
2. 正当性を直接左右する場合は `WORKFLOW.md` へ記述する。
3. workflow 共通規範ではなく、`spec` 台帳・命名・配置・昇格などの全体方針を定義する場合は `SPEC.md` へ記述する。
4. 規則がツール呼び出し手順、入力収集順、再生成手順、失敗時オペレーションなど実行方法の詳細である場合は対応 `SKILL.md` へ記述する。
5. エージェント固有の実行便宜（例: プロンプト順序、ログ整理手順）は `SKILL.md` に限定し、`WORKFLOW.md` へ混在させない。
6. 判定に迷う場合は、規則違反時の影響が監査可能性・再現性・判定整合の破壊に及ぶかを判定軸とする。破壊する場合は `WORKFLOW.md`、破壊しない場合は `SKILL.md` を選択する。

## 工程と Skill 対応表
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
1. 1 回の作業で複数工程を扱う場合、工程ごとに対応 `SKILL` を切り替える。
2. `verify` で失敗した場合、同一工程の `generate` に戻し、再生成後に再検証する。
3. ループの状態と失敗理由は、該当工程のメタデータへ記録する。
4. `SKILL` 定義を変更した場合、この対応表を同一変更で更新する。
5. workflow 契約を変更する場合は `WORKFLOW.md` を先に更新し、その変更に追従して `SKILL.md` を更新する。
6. 依存 `DAG` 実行時は、`topo_level` 昇順で `node` を逐次処理する。同一 `topo_level` の独立 `node` を並列実行してはならない。
7. 同一 `topo_level` の途中で `node` が `fail` した場合も、未処理 `node` の起動可否を 1 件ずつ判定する。自動並列継続は行わない。
8. 直下依存 `node` が `pass` または `xfail` でない場合、上位 `node` を `blocked` として終了する。
9. 直下依存に起因して `blocked` で終了する `node` は、`self_verdict=not_evaluated` を明示し、停止理由を `trial_meta.json` に記録する。
10. 工程入力不足で開始条件を満たせない場合、当該工程を `fail` で停止する。推測補完で進めない。
11. `spec_kind` を問わない workflow 実行で、リポジトリ管理外パス（例: `/tmp`）の補助スクリプトを workflow 実行経路に使用してはならない。
12. 各工程開始前に `write_scope_baseline` を取得し、工程完了前に `workspace/` 配下以外の差分を検出する `write_scope` 検査を必須実行する。
13. `python` 実行を workflow 経路で使用する場合、`__pycache__` を `workspace/` 配下に限定する。`PYTHONDONTWRITEBYTECODE=1` または `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/` を必須適用する。
14. `write_scope` 検査で違反を検出した場合、当該工程を `fail` とし、`write_scope_violation.json` を `workspace/` 配下へ記録して下流工程を停止する。
15. `Execute` と `Judge` は `derived_contract.json` の `raw_requirements.required_evidence` に基づいて一次証跡の必須構成を判定しなければならない。
16. `Judge` は `semantic_review.json` の `decision=pass` を満たさない場合、`fail` として終了しなければならない。
17. `orchestration agent` は工程成果物を直接生成せず、`step agent` または `substep agent` の起動、待機、結果集約のみを担当しなければならない。
18. `substep` を持つ工程では、`orchestration agent` が対応 `SKILL` に従って `substep` 群を列挙し、`substep agent` を直接起動して結果を集約しなければならない。
19. `step agent` と `substep agent` は、それぞれ `agent_run_id` と `parent_agent_run_id` を記録し、親子関係を追跡可能にしなければならない。`substep agent.parent_agent_run_id` は `orchestration agent_run_id` を許可する。
20. `substep agent` の失敗時は推測補完で継続せず、当該 `step` を `fail` として終了しなければならない。
21. `step_result.json` は `executor_agent_run_id` と `substep_agent_run_ids` を必須記録し、`executor_agent_run_id` は保存先 `agent_run_id` と一致しなければならない。`substep` を持たない工程の `substep_agent_run_ids` は空配列を許可する。
22. `agent_runs.jsonl` の `step` / `substep` ロールは `agent_backend` と `agent_model` と `context_id` と `context_isolated=true` と `agent_session_id` と `launch_request_ref` と `launch_response_ref` を必須記録しなければならない。
23. `launch_request_ref` と `launch_response_ref` は `workspace/orchestrations/<orchestration_id>/launches/` 配下を参照し、参照先ファイルが存在しなければならない。
24. `agent_runs.jsonl` と `step_result.json` で独立 `agent` 実行を追跡できない試行は `fail` とし、`script` 実行ログで代替してはならない。
25. `python3 tools/validate_pipeline_semantics.py` の実行で `--allow-missing-orchestration` と `--allow-missing-llm-review` を常用してはならない。互換移行を明示した試行以外で指定した場合は `fail` とする。

## 判定基準
- 対象工程で使用した `SKILL` パスを説明できる。
- 生成成果物と判定成果物が、対応 `SKILL` の契約と一致する。
- エージェント間で同一入力に対する工程選択が一致する。
- `orchestration agent` から `step agent` または `substep agent` への起動関係を説明できる。
- `step` と `substep` の `context_id` が重複せず、`context_isolated=true` を満たすことを説明できる。
- `step` と `substep` の `agent_session_id` と `launch_request_ref` と `launch_response_ref` を追跡できる。
- `write_scope` 検査結果が `pass` であり、`workspace/` 配下以外の差分が検出されない。
- `semantic_review.json` が存在し、`decision=pass` と一次証跡参照が記録されている。
