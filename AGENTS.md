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
- 英数字トークンは、terms・識別子・略語・ファイル名を含む。
- 英数字トークン内部の記号連結（例: `CPU/GPU`,`pass/fail`,`L0-L3`,`case.resolved.yaml`）には空白を入れない。
- インラインコード（バッククォート内）は文字列を変更しない。

## Term 表記ルール
- 本文は日本語で記述するが、terms の canonical source 表記は英語とする。
- terms・artifact 名・role 名・phase 名・classification 名は、`docs/GLOSSARY.md` に定義した英語表記を使用する。
- 日本語訳を併記する場合は初出の説明に限定し、以後は英語表記へ統一する。
- 新規 term を追加する場合は、先に `docs/GLOSSARY.md` へ英語表記で定義し、その後に各文書で使用する。

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
- 略語は初出で定義し、terms は既存文書（`docs/GLOSSARY.md`）と整合させる。
- 日本語本文中でも、terms は日本語訳へ置換せず英語表記を維持する。

## 変更時チェック
- 議論ログ調の表現が残っていない。
- 各節が単独で読める完結文になっている。
- 要件・制約・input/output・判定条件が具体化されている。
- terms・artifact 名・role 名・phase 名・classification 名が `docs/GLOSSARY.md` の英語表記と一致している。

## CLI 仕様参照ルール
- CLI 引数情報の取得経路は、subcommand 頻度と payload 複雑度に応じて使い分ける。詳細表は `CLAUDE.md` の「CLI 仕様の確認規約」節を canonical source とする。
- `tools/orchestration_runtime.py` の頻出 subcommand (`record-launch` / `record-agent-run` / `record-child-return` / `deactivate-child` / `record-reply` / `set-status` / `write-step-result` / `workflow-launch-check` / `reserve-phase-root` / `mark-dependency-readiness` / `guarded-apply-patch` / `run-gate`) は `docs/CLI_REFERENCE.md` (Tier-A) を canonical source とする。
- `tools/orchestration_runtime.py` の稀少 subcommand (`init` / `preflight` / `preflight-status` / `record-timeout` / `read-checkpoint` / `verify-checkpoint-integrity` / `check-step-completed` / `orchestration-read` / `repair-agent-runs`) と、`tools/run_workflow.py` / `tools/validate_pipeline_semantics.py` / `tools/audit_orchestration.py` は `<tool> [<sub>] --help` を canonical source とする。`docs/CLI_REFERENCE_RARE.md` は稀少 subcommand の overview のみ保持する。
- `tools/` 配下の `.py` 実装を `Read` tool / `grep` / `sed` / `cat` 等で直接読む経路は `forbid_tools_direct_read` および `read_manifest_read_guard` の対象として禁止する。`--help` 経由の argparse 出力読取は block されない。

## MCP 実行ルール
- `compile` / `run` / `quality check` は、必ず MCP サーバー経由で実行する。
- 標準サーバーは `mcp_servers/build_runtime_server.py` とし、`detect_build_system` / `compile_project` / `run_program` / `run_quality_checks` / `run_linter` を使用する。
- `compile` で `fortran` / `c` / `cpp` / `mixed` 系を扱う場合、依存関係を扱える標準ビルドツールのみを許可する。既定値は `make` とする。
- `gcc` / `clang` / `gfortran` を直接呼び出す単発ビルドを禁止する。
- `Generate` は `static lint` を MCP `run_linter` で実行する。`run_linter` は `compile` / `compile_project` / `toolchain.build_system` 経由のビルドとは別手順であり、`compile` を標準ビルドツール経由とする原則の対象外として扱う。
- `compile` / `run` 以外で MCP 適用が有効な処理（例: build system 判定、test 実行、check 実行）は、同様に MCP ツールを実装し、直接シェル実行を避ける。

## MCP 設定参照
- MCP クライアント設定は `mcp_servers/mcp_servers.example.json` を参照する。
- 運用詳細は `mcp_servers/README.md` を参照する。

## Project Local Skills 運用ルール
- `skills/` 配下の `SKILL.md` を、ワークフロー phase ごとの実行手順の canonical source として扱う。
- phase と `SKILL` の対応は `docs/AGENT_SKILLS.md` を参照する。
- `generate -> verify -> regenerate` ループを持つ phase では、対応する `generate` 用 `SKILL` と `verify` 用 `SKILL` を分離して適用する。
- `Codex` / `Gemini` / `Claude Code` のいずれでも、作業開始前に対象 phase の `SKILL.md` を読み、定義された input/output contract と判定基準に従う。

## Workflow 文書参照ルール
- workflow 仕様への入口は `docs/WORKFLOW.md` とする。workflow 共通の不変規範、phase sequence、`phase` 別 I/O 契約一覧は `docs/workflow/WORKFLOW_CORE.md` を、各 `phase` の詳細契約は `docs/workflow/phases/` 配下を canonical source とする。
- `orchestration agent` と `step agent` / `substep agent` の階層実行契約は `docs/ORCHESTRATION.md` を canonical source とする。
- phase と `SKILL` の対応、規則の記載先判定、phase 切替規則は `docs/AGENT_SKILLS.md` を canonical source とする。
- workflow 固有の禁止事項、過去 artifact 参照禁止、独立 `agent` 実行証跡要件は、`AGENTS.md` へ再掲せず対応 canonical source を参照する。
- workflow 起動は、ユーザーが `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を実行する方式を canonical entrypoint とする。
- workflow 実行時は、ユーザーが起動した `tools/run_workflow.py` が設定する `METDSL_WORKFLOW_MODE=1` と `METDSL_ORCHESTRATION_ID=<orchestration_id>` を canonical source とする。
