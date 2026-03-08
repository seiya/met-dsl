# AGENTS.md

このリポジトリで文書を編集・作成するときは、以下の表現ルールに従う。

## 目的
- ドキュメントを「議論ログ」ではなく「完成済み仕様」として記述する。
- 読者が単独で読んでも、意思決定や要求事項を解釈できる状態を保つ。

## 文体ルール
- 常体（だ・である調）で統一する。
- 主語と責務を明示し、曖昧な省略を避ける。
- 口語・比喩・感想表現を使わない。
- 仕様・要件・制約・判定基準を優先して記述する。

## 全角・半角の表記ルール
- 和文文字（漢字・ひらがな・カタカナ）と英数字トークンの境界には、半角スペースを 1 つ入れる。
- 英数字トークンは、用語・識別子・略語・ファイル名を含む。
- 英数字トークン内部の記号連結（例: `CPU/GPU`,`pass/fail`,`L0-L3`,`case.resolved.yaml`）には空白を入れない。
- インラインコード（バッククォート内）は文字列を変更しない。

## Markdown 数式表記ルール
- インライン数式は `$...$` を使用する。
- ブロック数式は `$$...$$` を使用する。
- `\(...\)` と `\[...\]` は使用しない。

## 禁止表現
- 議論過程を示す表現: 「結論」「理由は」「議論した結果」「まず」「次に」「試行錯誤」など
- AI 対話の痕跡を示す表現: 「AI と壁打ち」「AI が考えた」「LLM に聞いた」など
- 口語・俗語: 「ご本尊」「潰す」「次の一手」など

## 推奨見出し
- `目的`
- `適用範囲`
- `要件`
- `設計方針`
- `運用ルール`
- `判定基準`

## 記述指針
- 「なぜそうなったか」ではなく「何を要求するか」を書く。
- 判断が分岐する箇所は、条件と選択規則を明示する。
- 未定義項目は保留せず、未定義であることと扱い（禁止・エラー）を明記する。
- 略語は初出で定義し、用語は既存文書（`docs/GLOSSARY.md`）と整合させる。

## 変更時チェック
- 議論ログ調の表現が残っていない。
- 各節が単独で読める完結文になっている。
- 要件・制約・入出力・判定条件が具体化されている。

## MCP 実行ルール
- `compile` / `run` / `quality check` は、必ず MCP サーバー経由で実行する。
- 標準サーバーは `mcp_servers/build_runtime_server.py` とし、`detect_build_system` / `compile_project` / `run_program` / `run_quality_checks` を使用する。
- `compile` で `fortran` / `c` / `cpp` / `mixed` 系を扱う場合、依存関係を扱える標準ビルドツールのみを許可する。既定値は `make` とする。
- `gcc` / `clang` / `gfortran` を直接呼び出す単発ビルドを禁止する。
- `compile` / `run` 以外で MCP 適用が有効な処理（例: build system 判定、test 実行、check 実行）は、同様に MCP ツールを実装し、直接シェル実行を避ける。

## MCP 設定参照
- MCP クライアント設定は `mcp_servers/mcp_servers.example.json` を参照する。
- 運用詳細は `mcp_servers/README.md` を参照する。

## Project Local Skills 運用ルール
- `skills/` 配下の `SKILL.md` を、ワークフロー工程ごとの実行手順の正本として扱う。
- 工程と `SKILL` の対応は `docs/AGENT_SKILLS.md` を参照する。
- `generate -> verify -> regenerate` ループを持つ工程では、対応する `generate` 用 `SKILL` と `verify` 用 `SKILL` を分離して適用する。
- `Codex` / `Gemini` / `Claude Code` のいずれでも、作業開始前に対象工程の `SKILL.md` を読み、定義された入出力契約と判定基準に従う。

## Workflow 実行禁止事項
- `workflow` 実行の代替として、`Plan` / `Generate` / `Build` / `Execute` / `Judge` を一括自動化する `script`（例: `python` / `bash`）の新規生成と実行を禁止する。
- `workflow` は必ず `orchestration agent` 起点の独立 `LLM agent` 実行で完了させる。`substep` を持つ工程は `orchestration agent -> substep agent`、標準 `substep` を持たない工程は `orchestration agent -> step agent` を正本とする。
- `agent_runs.jsonl` と `agent_graph.json` と `step_result.json` で独立 `agent` 実行を追跡できない試行は `fail` とする。

## Codex CLI workflow 実行ルール
- `Codex CLI` で `workflow` を開始する `orchestration agent` は、開始前に `multi_agent` 機能と子 `agent` 起動可否を確認し、利用不可の場合は `preflight fail` とする。
- `Codex CLI` の `orchestration agent` は、`Plan` / `Generate` / `Build` / `Execute` / `Judge` / `Tune` / `Promote` の工程成果物を直接生成してはならない。各 `step` は必ず `spawn_agent` で起動した子 `agent` へ委譲する。
- `Plan` / `Generate` / `Tune` のように `substep` を持つ工程では、`orchestration agent` は `generate` と `verify` を別々の `substep agent` として `spawn_agent` で直接起動しなければならない。
- `Build` / `Execute` / `Judge` / `Promote` のように標準 `substep` を持たない工程では、`orchestration agent` は単一の `step agent` を `spawn_agent` で起動し、その `step agent` が工程責務を完了させなければならない。
- `spawn_agent` の応答で得た子 `agent` 識別子を `agent_session_id` として `agent_runs.jsonl` へ記録しなければならない。
- `context_id` は `agent_run_id` ごとに新規発行し、1 つの子 `agent` 実行に 1 つの `context_id` を対応付けなければならない。親 `agent` と子 `agent` の `context_id` 共有を禁止する。
- `launch_request_ref` は子 `agent` 起動前に保存した起動要求 `JSON` を指し、`launch_response_ref` は `spawn_agent` の応答 `JSON` を指さなければならない。
- `orchestration agent` は、子 `agent` の完了待機中に当該子 `agent` の責務を代行してはならない。`step agent` を起動する工程では `step agent` も同様に子 `agent` の責務を代行してはならない。親 `agent` の責務は起動、待機、結果集約、下流起動判定に限定する。
- `step agent` の再試行が必要な場合も、同一 `agent_session_id` を使い回してはならない。再試行ごとに新規 `agent_run_id` と新規 `agent_session_id` を発行しなければならない。
- `skills/*/agents/openai.yaml` の表示名または説明文だけで独立 `agent` 起動契約を満たしたとみなしてはならない。`Codex CLI` へ渡す起動要求本文に、`spawn_agent` の使用義務、入力契約、期待出力、保存先、失敗時停止条件を明示しなければならない。

## 過去成果物の参照禁止ルール
- 過去実行で生成された成果物は、ディレクトリ名に関係なく閲覧・参照・コピー・比較・流用を禁止する。
- `workspace/` 配下に過去成果物が存在する場合も、中身の閲覧と入力参照を禁止する。
- workflow は毎回独立実行し、`workspace/plans/<node_key_safe>/<plan_id>/` と `workspace/pipelines/<node_key_safe>/<pipeline_id>/` の既定構造で、`plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` を毎回新規発行する。
- workflow 入力は `spec` 正本と当該実行で生成した前段成果物のみに限定する。
- 本規則に違反した workflow は `fail` とし、当該実行を破棄して再実行する。
