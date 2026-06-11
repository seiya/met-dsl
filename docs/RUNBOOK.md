# Runbook（試行を回す最小手順）

この文書は「試行を回すための最小運用手順」を定義する。core workflow は `Spec → Compile → Generate → Build → Validate` の 5 phase 構成。運用知見に応じて更新する。

## 0. 目的
- `spec` の `Controlled Spec`（物理定義）と `tests`（検証プロファイル）から実行と判定を行い、物理妥当性と性能を評価する。
- 失敗の原因を **Spec / Compile / Generate / Build / Validate** のどこにあるか切り分ける。任意フロー `Tune` / `Promote` は core workflow 外で扱う。

## 0-1. 必須 CLI tools

workflow 実行および本 RUNBOOK の修復手順は以下の CLI を前提とする。

| ツール | 用途 |
|---|---|
| `python3` | workflow runtime（`tools/orchestration_runtime.py` 等） |
| `jq` | output_manifest 等 JSON からの shell 変数抽出（`python3 -c` は `forbid_python_inline_write` でブロックされるため代替不可） |
| `git` | `write_scope_baseline` / `git apply`（`guarded-apply-patch` 内部で使用）/ status 検査 |

不在の場合は `tools/run_workflow.py` 起動時点で fail-fast する。

## 1. 入力と artifact（最小）
- 入力: `controlled_spec.md`（物理・アルゴリズム定義）/ `tests.md`（ケース展開・実行条件・判定閾値）/ `deps.yaml`（依存宣言）
- 生成（Compile）: `spec.ir.yaml`（**単一構造 IR**: case / algorithm / impl_defaults / io_contract / dependency セクション統合）
- 生成（Generate）: `model`（物理計算）と `runner`（実行・判定連携）のソース
- 生成（Build）: バイナリ（`binary/<binary_id>/bin/`）
- 出力（Validate）: `diagnostics.json` / `perf.json` / `verdict.json` / `aggregate_verdict.json` / `summary.json` / `semantic_review.json`
- 禁止: `dummy` 出力、`dummy` データ、`dummy` 計算、workflow 進行目的の人工 artifact generation

## 1-1. artifact layout（運用必須）
- `Compile` は `workspace/ir/<node_key_safe>/<ir_id>/` に `spec.ir.yaml` と `ir_meta.json` を保存する。
- `Generate` / `Build` / `Validate` は `workspace/pipelines/<node_key_safe>/<pipeline_id>/` に保存する。
- 各 `pipeline` には `lineage.json` を必須配置する。
- `source` artifact は `workspace/pipelines/<node_key_safe>/<pipeline_id>/source/<source_id>/` に保存する。
- `binary` artifact は `workspace/pipelines/<node_key_safe>/<pipeline_id>/binary/<binary_id>/` に保存する。
- `Validate` artifact は `workspace/pipelines/<node_key_safe>/<pipeline_id>/runs/<run_id>/<node_key_safe>/` に保存する。
- 判定時は `run_id` 単位で読み込む。`run_id` を跨ぐファイル混在を禁止する。
- 判定時は `node_key` 単位で `verdict` / `aggregate_verdict` / `summary` を分離して読み込む。
- 任意フロー `Promote` の正式版 artifact は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` に保存する（core workflow 外）。`workspace` は試行用途に限定する。

## 1-2. 逸脱防止ゲート（運用必須）
- workflow 共通の不変規範（不正防止、過去 artifact 参照禁止、検証契約導出、`workspace/` ルート制約、`quality check` 判定軸）は `docs/workflow/WORKFLOW_CORE.md` を canonical source とする。
- 全体方針と `spec` 管理要件（`spec_kind` / registry / 命名規則）は `SPEC.md` を canonical source とする。
- workflow 実行は、各 phase（`Compile` / `Generate` / `Validate`）を `LLM` により実行する。`Build` は決定的処理であり MCP `compile_project` 呼び出しで実行する。
- workflow 実行の代替として、複数 phase の処理と artifact generation を一括代行する script を新規生成または実行してはならない。
- 各 phase 開始前に `write_scope_baseline` を取得し、各 phase 完了前に `workspace/` 配下以外の差分を検出する `write_scope` 検査を必須実行する。
- `python` 実行を workflow 経路で使用する場合、`__pycache__` を `workspace/` 配下へ限定する。`PYTHONDONTWRITEBYTECODE=1` または `PYTHONPYCACHEPREFIX=workspace/.pycache/<pipeline_id>/` を必須適用する。
- `write_scope` 検査で `workspace/` 配下以外の差分を検出した場合、当該 phase を `fail` とし、`write_scope_violation.json` を `workspace/` 配下へ記録する。
- `Generate.verify` のデータ依存判定は `spec.ir.yaml.io_contract.semantic_dependency.required_sources` を canonical source とする。
- `Generate.verify` の output contract 判定は `spec.ir.yaml.io_contract.outputs` を canonical source とし、`evidence_ref` と `shape_expr` の整合を必須検査する。
- 出力形式、input/output contract、判定条件の要求定義は `controlled_spec.md` と `tests.md` と `deps.yaml` と `spec.ir.yaml` と `docs/` canonical source から取得し、`tools/` 配下の検証スクリプトを要求定義入力へ使用してはならない。
- 機械的合否を確定する手順の canonical implementation は、`validate_pipeline_semantics.py` 相当 invocation を `python3 tools/orchestration_runtime.py run-gate --gate validate_pipeline_semantics --agent-run-id <agent_run_id> --capability-token <capability_token> --args-json '<json>'` 経由で実行する手順とする。エージェントは当該 `run-gate` 実行を `exit code 0` まで完了する。
- validator invocation は `run-gate` を原則とする。直接実行を許可する場合は read-only 検査かつ gate 非依存検査に限定し、許可対象は `validate_workspace_root.py` と `check_artifact_syntax.py` のみとする。
- 要求定義の不足を検証実装から逆算補完してはならない。不足時は当該 phase を `fail` とする。
- `Validate.judge` は固定スクリプト検査に加えて `LLM` 意味検査を必須実行し、`semantic_review.json` の `decision=pass` を開始条件に含める。
- `Validate.judge` 開始前に、対象 `node_key` の同一 `run_id` 配下へ `run_program` 実行記録と `diagnostics.json` と `perf.json` と `raw` 実行証跡が揃っていることを検証する。未達時は `Validate.judge fail` とする。
- `Compile.verify` 完了前に、`run-gate` で `validate_pipeline_semantics` を `--stage compile --ir-ref workspace/ir/<node_key_safe>/<ir_id>/` 相当引数で実行する。`fail` 時は `ir_meta.json` の `verification_status=pass` を付与してはならない。
- `Generate.verify` 完了前に、`run-gate` で `validate_pipeline_semantics` を `--stage post_generate --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` 相当引数で実行する。検証対象の `source_id` を固定する場合は `--source-id <source_id>` 相当引数を付与する。
- `Build` 完了前に、`run-gate` で `validate_pipeline_semantics` を `--stage post_build --pipeline-root workspace/pipelines/<node_key_safe>/<pipeline_id>/` 相当引数で実行する。
- `Validate.execute` 完了前に、`run-gate` で `validate_pipeline_semantics` を `--stage post_execute` 相当引数で実行する。`--pipeline-root` は繰り返し指定可能とし、`spec.ir.yaml.dependency.all_nodes` が複数 `node` を保持する試行では、`all_nodes` に対応する全 `pipeline_root` を `--pipeline-root` へ展開して実行する。本試行の `run_id` を `args_json` の `run_id`（→ `--run-id`）へ渡し、検証を当該 run へ scope する（`append-only` の pipeline に残る過去 retry の壊れた sibling run で恒久 fail しないため）。`fail` 時は `Validate.execute` を `fail` とし、`Validate.judge` を開始してはならない。
- `Validate.judge` 開始前と完了前に、`run-gate` で `validate_pipeline_semantics` を `--stage pre_judge` 相当引数で実行し、`fail` 時は当該 `pipeline` を `invalid` とする。判定対象 `run_id` を `args_json` の `run_id`（→ `--run-id`）へ渡し、検証を当該 run へ scope する。
- `validate_pipeline_semantics --stage pre_judge` は `--allow-missing-orchestration` と `--allow-missing-llm-review` と併用してはならない。
- `Validate.judge` 開始前の `pre_judge` 相当引数は、対象 `spec.ir.yaml.dependency.all_nodes` に対応する全 `pipeline_root` を `--pipeline-root` へ繰り返し指定して実行する。
- `trial_meta.json` は `generated_by_stage` と `source_source_id` と `source_binary_id` と `source_command_ref` と `source_artifact_hash` を必須記録とし、欠落または不整合時は `fail` とする (`run_id` は trial_meta が配置される `runs/<run_id>/` directory path 自体が encode しているため、別途 `source_run_id` フィールドは記録しない)。
- 本節の検証に違反した試行は当該 phase で停止し、下流 phase 開始条件を満たす目的の人工 artifact generation を禁止する。

### 1-2-1. `validate_pipeline_semantics.py` の補足静的規則（Generate 周辺）
- **`Makefile` オブジェクト規則のターゲット表記**: `spec.ir.yaml.impl_defaults.toolchain.language=fortran` かつ複数 `module` から成る `src/` に対し、`use` 依存から機械導出されるオブジェクト依存検査が走る。検査はターゲット token から `$(NAME)` / `${NAME}` を除去したあとに残る **literal** なベース名（例: `foo.o`）だけを規則として採用する。各 `.o` の前提に必要な `.mod` / `.o` は **literal ターゲット行**（例: `foo.o: bar.o baz.mod`）として列挙する。
- **`runner` の禁止出力名 substring 検査の範囲**: `*_runner.f90` の全文を小文字化したうえで、禁止名の **部分文字列** として検出する。**コメント行を除外しない**。`verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json` をコメントや文字列リテラル内に含めてはならない。
- **各 `pipeline` の `lineage.json`**: `workspace/pipelines/<node_key_safe>/<pipeline_id>/lineage.json` は検査対象の `pipeline` ごとに必須。

## 1-3. エージェント起動規約（運用必須）
- workflow 実行は `orchestration agent` を起点に開始し、`orchestration_id` を必須発行する。
- workflow 開始前に、`step agent` と `substep agent` の独立起動可否を検証する `preflight` を実行し、`pass` でない場合は開始してはならない。
- `backend=codex` の preflight は、`checks.codex_hooks_enabled.pass=true` と `checks.codex_home_writable.pass=true` を同時に満たさなければならない。
- `preflight` は `sandbox_runtime=bwrap` と `sandbox_enforced=true` を必須条件に含める。
- workflow 起動は `python3 tools/run_workflow.py <spec_ref> <until_phase> [--llm <codex|cursor|claude>]` を canonical entrypoint とする。`<until_phase>` は `compile` / `generate` / `build` / `validate` のいずれかを指定する。
- 標準 `substep` を持たない `Build` step は `step agent` を独立起動して実行する。
- `Compile` / `Generate` / `Validate` の各 phase は `orchestration agent` が各 `substep` の `substep agent` を独立起動して実行する。
- 各 `step` / `substep` の実処理を script で代行してはならない。
- `step agent` と `substep agent` は `agent_run_id` ごとに固有 `context_id` を持ち、`context_isolated=true` を必須記録とする。
- `record-launch` は `workspace/orchestrations/<orchestration_id>/output_manifests/<agent_run_id>.json` / `read_manifests/<agent_run_id>.json` / `sandbox_profiles/<agent_run_id>.json` を生成する。
- 各 `step` / `substep` の完了時には `agent.result.json` と `agent.summary.txt` を保存する。
- `orchestration agent` は `spec.ir.yaml.dependency` の `topo_level` と依存充足状態に基づいて起動順序を逐次決定する。
- `orchestration` の実行記録は `workspace/orchestrations/<orchestration_id>/` に保存し、`orchestration_meta.json` と `agent_graph.json` と `agent_runs.jsonl` を必須とする。
- `step_result.json` は `executor_agent_run_id` と `substep_agent_run_ids` を必須記録する。`Build`（標準 substep を持たない phase）の `substep_agent_run_ids` は空配列を許可する。

## 2. 最小ループ
1. **Spec 更新**: `controlled_spec.md` / `tests.md` / `deps.yaml` を修正し、曖昧さ・欠落を解消する。
2. **Compile**: `controlled_spec.md` + `tests.md` + `deps.yaml` + `spec/registry/spec_catalog.yaml` を入力に `spec.ir.yaml` を生成する。
   - `Compile.generate` substep が `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` の 5 セクションを統合保持する単一 IR を生成する。
   - `Compile.verify` substep が構造 invariant（case 被覆性 / algorithm 完全性 / io_contract 整合 / dependency 整合 / impl_defaults 整合）の self-check を行う。
   - `LLM` 利用 phase なので `SPEC.md` の「`LLM` の扱い」を適用する。
3. **階層実行順序の固定**: `spec.ir.yaml.dependency.topo_level` 昇順で実行順序を固定する。親 `node` の `Compile` は直下依存 `node` が `direct dependency ir readiness` を満たすまで開始してはならない。親 `node` の `Generate` 以降は直下依存 `node` が `direct dependency execution readiness` を満たすまで開始してはならない。同一 `topo_level` の独立 `node` も 1 件ずつ逐次実行する。
4. **`node` 単位 workflow 発行**: `orchestration agent` が各 `node_key` ごとに個別 `ir_id` と個別 `pipeline_id` を発行する。
5. **Generate**: 対象 `node` ごとに `LLM` で `model` と `runner` を分離して生成する。
   - `Generate` は `controlled_spec.md` を直接入力にしてはならず、`spec.ir.yaml` を canonical source とする。
   - `Generate.verify` は `spec.ir.yaml` の各セクションに対する G1〜G7 検証項目（`phase_02_generate.md` 参照）を実施する。
   - 依存を持つ `node` は `spec.ir.yaml.dependency.direct_deps` で解決された依存 `node` の公開 `operation` を呼び出す実装を必須とし、同等機能の再実装を禁止する。
6. **Build**: 対象 `node` ごとに `MCP` サーバーの `compile_project` で依存関係を扱える標準ビルドツールを実行する。
   - `Build` 失敗は **必ず `Generate` への retry feedback** となる（決定的処理ゆえコード以外に修正余地がない）。
   - `Build` 自身を内部 retry してはならない。
7. **Validate**: 対象 `node` ごとに `Validate.execute` substep がバイナリを実行し一次証跡を生成、`Validate.judge` substep が判定指標を再計算して `verdict` を確定する。
   - `Validate.execute` は `MCP run_program` で `runner` を実行し、`run_program` 実行コマンドに `spec.ir.yaml.case` を必ず含める。
   - `Validate.execute` は `runs/<run_id>/<node_key_safe>/raw/` へ判定再計算用の一次証跡を保存する。`raw` 構成の必須条件は `spec.ir.yaml.io_contract.raw_requirements.required_evidence` を canonical source とする。
   - `Validate.judge` は `raw` 一次証跡のみを入力として判定指標を再計算し、`diagnostics` と一致しない場合は `Validate.judge fail` とする。固定スクリプト検査に加えて `LLM` 意味検査を実施し、`semantic_review.json` の `decision=pass` を必須条件にする。
   - 依存込み判定は `aggregate_verdict.json` へ出力する。直下依存 `node` が `fail` または `blocked` の場合、上位 `node` は `blocked` として終了する。
8. **強制停止**: 入力不足または前段 artifact 不足で当該 phase を進められない場合、当該 phase を `fail` で停止する。推定補完や人工ファイル生成で進めてはならない。
9. **記録**: `spec_version` / `test_profile_version` / `case_hash` / `git_sha` を保存する。
   - `ir_id` / `pipeline_id` / `source_id` / `binary_id` / `run_id` を保存する。
   - `node_key` / `topo_level` / `dependency_ref` を保存する。
   - `dependency_ref` は phase 別 canonical path を保存する。`Compile` は `spec/.../deps.yaml`、`Generate` 以降は `workspace/...` の phase root（`ir_ref` または `pipeline_ref`）を記録する。
   - `LLM` 利用 phase は各 phase の `<stage>_meta.json` に `attempt_count` / `verification_status` / `last_fail_reason` / `debug_mode` を保存する。
   - `step` / `substep` の `agent_runs.jsonl` は `agent_backend` / `agent_model` / `context_id` / `context_isolated=true` を記録する。
10. **次アクション**: 失敗 classification に応じて戻る場所を決める（次節）。

## 3. 失敗時の戻り先（指針）
| 失敗種別 | 戻り先 |
|---|---|
| `LLM` ステージ実行不能 | input contract または `MCP` 接続定義 |
| Spec 不備（曖昧・欠落・単位不整合） | `Spec` |
| Test 不備（ケース展開・閾値・実行条件の矛盾） | `tests` |
| Dependency 解決 fail（未登録 / 未実装 / 互換性違反） | `deps.yaml` / `spec_catalog.yaml` |
| Dependency block（下層 `node` の `fail`） | 下層 `node` |
| Compile 検証 fail（IR 構造 invariant 違反） | `Compile`（必要に応じて `Spec`） |
| Generate 検証 fail（IR と不整合な実装） | `Generate`（IR が誤りなら `Compile`） |
| Build 失敗（コンパイルエラー） | `Generate`（決定的に Generate 戻り） |
| 物理 fail（実行結果が判定不合格） | `Generate` / `Compile` / `Spec` のいずれか — `judge.findings` で詳細を指定 |
| Validate 判定 fail（一次証跡と diagnostics の乖離） | `Generate`（コード品質問題） |
| `semantic_review.decision=fail`（IR の意図と異なる実装） | `Generate` |
| `semantic_review.decision=fail`（IR 自体が spec の意図と異なる） | `Compile` |
| 依存統合 fail（依存 `operation` 呼び出し欠落） | `Generate` または `Build` |
| 依存 Compile 未完了 | `Orchestration` または下層 `node` |
| 依存 workflow 未実行 | `Orchestration` または下層 `node` |
| 不正生成 fail（`dummy` 出力、人工データ作成） | 当該 phase を破棄し `Spec` / phase input 定義 |
| 再現性崩れ（determinism 破壊） | `Compile` / 実行環境 |

`Spec` への自動 retry は core workflow では行わない。`orchestration agent` が `Spec` 戻りが必要と判定した場合、`fail_closed` で停止し、`failure_analysis.json` に詳細を記録する。

任意フロー:
- 性能未達（B の探索不足） → 任意フロー `Tune` の `impl_defaults` variant 探索を起動する。
- 正式版昇格 → 任意フロー `Promote` で `releases/` へ。

## 3-1. 失敗した workflow の再開（`--resume`）

途中で fail した workflow を、完了済み `step`（compile 済み等）を再利用したまま失敗箇所から再開する canonical 経路は `python3 tools/run_workflow.py --resume` とする。

```bash
# 最新の orchestration を、前回の spec_ref / until_phase / llm のまま再開する
python3 tools/run_workflow.py --resume

# 特定 orchestration を再開する場合
python3 tools/run_workflow.py --resume --orchestration-id <orchestration_id>

# until_phase を延長して再開する場合（lone positional が phase 名なら until_phase 上書き）
python3 tools/run_workflow.py --resume build
```

- `spec_ref` / `until_phase` / `--llm` / `--mode` は省略すると対象 orchestration の既存 artifact（`orchestration_meta.json` / `preflight.json` / `launches/orchestration.start.prompt.txt`）から復元される。明示指定した値が優先される。
- `--orchestration-id` を省略した場合は `workspace/orchestrations/` 内で時系列最新（`orchestration_meta.json#started_at` 順）の orchestration を対象とする。ただし最新が非 terminal status（`running` 等）の場合は、実行中の並行 run へ誤接続して共有 `workspace/tmp/<arid>` を破壊する事故を避けるため `latest_orchestration_not_resumable` で停止する（その run を resume するには `--orchestration-id` を明示する）。
- resume 時に `spec_ref` を明示 override した場合、override 後の `spec_ref` / `source_dependency_ref` は `orchestration_meta.json` へ反映される（次回の implicit resume が stale な旧値へ revert しないようにするため）。
- 内部動作: `--resume` は `orchestration_runtime.py init --resume-from-checkpoint`（= `resume_enabled=true` 設定、`orchestration_agent_run_id` 保持、`phase_state` merge）を実行してから起動する。対象 orchestration が terminal status（`fail` / `fail_closed` / `pass` 等）で終端済みの場合、live status を `running` へ戻す（terminal → 他 status の遷移は `fail` → `fail_closed` を除き runtime が reject するため、reset しないと resume した agent が完了しても `pass` を記録できない）。reset 時、終端時の `reason_code` / `reason_detail` / `blocking_policy_scope` は `resumed_from_*` に退避し、`finished_at` / `detected_at` を除去する（履歴は `failure_analysis.json` と `phase_state_log.jsonl` に残る）。完了済み `step` の skip 判定は orchestration agent が `check-step-completed` で行う（SKILL.md 運用ルール 19）。`verify-checkpoint-integrity` で `stale` 検出された `step` は skip されず再実行される。
- legacy record の自動補修: `init --resume-from-checkpoint` は併せて `repair-agent-runs` を実行し、`agent_model` 必須化 + 自動 backfill 導入（commit `caa10ab`）**以前**に記録された `agent_runs.jsonl` の step/substep 行に欠落した `parent_agent_run_id` / `agent_model` を補完する。これらは append-only かつ duplicate `record-agent-run` も拒否されるため forward では復元できず、`Validate.judge` の `pre_judge` gate を恒久的に fail させ resume を不能にしていた。補修は既存 artifact から authoritative に導出する（`parent_agent_run_id`: substep は `step_result.json#executor_agent_run_id`、step は `orchestration_meta.json#orchestration_agent_run_id` を `agent_graph.json` の child→parent edge と cross-check; `agent_model`: 同 orchestration の uniform な非空値を採用）。既存非空値は上書きせず、補修行に provenance を付与し `record_repairs.jsonl` に監査ログを残す。idempotent。`agent_model` が auto 導出できない場合（sibling に非空値が無い / 複数値が混在）は補修結果が `needs_manual` となり resume はそのまま続行（後段で gate が落ちる）するため、operator が `python3 tools/orchestration_runtime.py repair-agent-runs --repo-root . --orchestration-id <id> --agent-model <model_id>` を明示実行してから再 resume する。
- `Build` 失敗は決定的処理のため Build を内部 retry せず `Generate` へ戻す（本表 §3）。Build step は checkpoint 上「未完了」のままとなり、resume 時に Generate から再実行される。

## 4. 運用の最小チェックリスト
- `Controlled Spec` に未定義項目がない。
- `spec.ir.yaml` が `case` / `algorithm` / `impl_defaults` / `io_contract` / `dependency` の 5 セクションを保持し、`Compile.verify` の V1〜V5 invariant を満たす。
- `spec.ir.yaml.io_contract.outputs` の `evidence_ref` が `raw` 実体に解決できる。
- `spec.ir.yaml.io_contract.test_evidence_requirements` が `tests.md` の全 `test_id` を過不足なく保持している。
- `spec.ir.yaml.io_contract.raw_requirements.required_evidence` で `artifact=state_snapshots` を必須宣言する場合、`schema.variables[].name` と `schema.variables[].shape_expr` と `schema.time_variable` と `schema.time_shape_expr` が定義されている。
- 各 phase で `write_scope_baseline` を取得し、完了前に差分比較を実施している。
- `write_scope` 検査で `workspace/` 配下以外の差分が検出されていない。
- `python` 実行時の `__pycache__` 出力先が `workspace/` 配下に限定されている。
- `Generate.verify` が G1〜G7 の各検証項目を実施している。
- `Generate.verify` が `runner` の raw evidence 出力設計と `spec.ir.yaml.io_contract.raw_requirements.required_evidence` / `test_evidence_requirements` を照合し、`Validate.judge` 再計算に必要な per-test evidence を静的に確認している。
- `raw` の必須構成が `spec.ir.yaml.io_contract.raw_requirements.required_evidence` と一致している。
- `LLM` 利用 phase のメタデータで `verification_status` が `pass` である。
- `debug_mode=false` の試行で失敗試行 artifact が保存されていない。
- `diagnostics` / `perf` / `verdict` が揃って出る。
- `aggregate_verdict` と `summary.dependency_summary` が `spec.ir.yaml.dependency` と整合する。
- `spec.ir.yaml.dependency.all_nodes` の `node_key` 集合と `workspace/ir` / `workspace/pipelines` の `node` 集合が一致する。
- `workspace/orchestrations/<orchestration_id>/` に `orchestration_meta.json` / `agent_graph.json` / `agent_runs.jsonl` が存在する。
- `step_result.json` が `workspace/orchestrations/<orchestration_id>/steps/<node_key_safe>/<step>/<agent_run_id>/` に存在する。
- 各 `step` と各 `substep` が独立 `agent_run_id` を持ち、`parent_agent_run_id` で親子関係を追跡できる。
- 各 `step` と各 `substep` の `context_id` が重複せず、全件で `context_isolated=true` が記録されている。
- `workspace/orchestrations/<orchestration_id>/preflight.json` が `can_launch_step_agents=true` と `can_launch_substep_agents=true` と `sandbox_enforced=true` を満たしている。
- 各 `node_key` の個別 `ir_id` と個別 `pipeline_id` が発行されている。
- 実行証跡から、`script` 一括実行ではなく `orchestration -> step` または `orchestration -> substep` の独立 `agent` 実行であることを確認できる。
- 明示的な指定がない試行で、既存 workflow 出力の参照または閲覧が実施されていない。
- `lineage.json` が `node` 単位で分離され、単一 `lineage` に複数 `node_key` が混在していない。
- `Validate.judge` 入力は同一 `run_id` の `run_program` 実行記録と `diagnostics` / `perf` に限定されている。
- 依存を持つ `node` が `spec.ir.yaml.dependency` で解決された依存 `operation` を呼び出している。
- 依存 `operation` と同等機能を依存元 `node` へ再実装していない。
- 上位 `node` の `source/<source_id>/src/` に依存 `node` 実装本体が複製・再配置・再定義されていない。
- `spec.ir.yaml.impl_defaults.toolchain.language=fortran` の依存 `component` を持つ `node` で `use <spec_id>_model` と `call <spec_id>__*` が実装されている。
- `trial_meta.json` の `generated_by_stage` / `source_source_id` / `source_binary_id` / `source_command_ref` / `source_artifact_hash` が欠落していない (`run_id` は `runs/<run_id>/` directory path 自体が encode するため別フィールドにしない)。
- `trial_meta.json` の `source_command_ref` が参照する `run_program` 実行コマンドに `spec.ir.yaml.case` が含まれている。
- `blocked` で終了した `node` に `aggregate_verdict.json` / `summary.json` / `trial_meta.json` が存在し、`blocked_reason` が記録されている。
- `runner` が `python` / `bash` / `sh` / `node` など外部インタプリタを起動していない。
- `runner` が `verdict.json` / `aggregate_verdict.json` / `summary.json` / `trial_meta.json` を書き込んでいない。
- `runs/<run_id>/<node_key_safe>/raw/` が存在し、`Validate.judge` 再計算に必要なファイルが揃っている。
- `raw/metrics_basis.json` が `diagnostics.json` の複写ではなく、一次証跡から構成されている。
- `run-gate` による `validate_workspace_root` 実行が `PASS` を返している。
- `run-gate` による `validate_pipeline_semantics --stage pre_judge` 相当実行が `PASS` を返している。
- `semantic_review.json` が存在し、`decision=pass` である。
- 異なる `node_key` の `source/<source_id>/src` が不正に完全一致していない。
- `copy_based_artifact_reuse` が未検出である。
- `write_scope_violation.json` が未生成である。

## hook ブロック時の修復チートシート {#hook-recovery}

workflow 実行中に hook がブロックした場合、`reason` と `audit_detail.policy` から原因を特定し、以下の表に従って次のアクションを取ること。

| policy | ブロックされた操作の例 | 取るべき次の 1 アクション |
|---|---|---|
| `auto_read_expected_block` | Claude Code harness が `.claude/settings.json` / `.cursor/mcp.json` / `mcp_servers/README.md` / `mcp_servers/mcp_servers.example.json` / `mcp_servers/tools/` 配下のファイル（harness が実際に読むのは `*.json`）を startup 直後に auto-read した（orchestration agent では加えて `MEMORY.md` / `README.md` / `TODO.md` / `CLAUDE.md` / `~/.claude/projects/.../memory/MEMORY.md`） | **無視してよい**。harness の決定論的 startup 動作であり benign noise。再試行や追加 Read を試みないこと。許容範囲の詳細は `skills/workflow-orchestration/references/startup_contract.md` 運用ルール 3 のブロック (A)/(B) を参照 |
| `read_manifest_read_guard` | 許可 root 外のファイルを `Read` した | `read_manifests/<agent_run_id>.json` の `allowed_read_roots` を確認し、必要なら `run-gate --gate orchestration_read` 経由で読む。`launches/<arid>.parent_return_token` は **`Read` tool で読まず** `"$(cat <path>)"` 形式で `record-child-return --return-token` に渡す（active_child window 中の Read は child arid の manifest で評価され block される）。CLI 仕様の確認は `docs/CLI_REFERENCE.md` を参照し、`tools/orchestration_runtime.py` を直接読まないこと |
| `output_manifest_write_guard` | `/tmp`・`/dev/shm`・manifest 外 path への書き込み | `output_manifests/<agent_run_id>.json` の `allowed_tmp_root` (= `workspace/tmp/<agent_run_id>/`) 配下を **literal path** で直接指定する (例: `cat > workspace/tmp/<agent_run_id>/x.patch <<EOF`)。`export TMPDIR=...` / `jq -er ...` / `printenv` の bootstrap Bash は Claude Code session sandbox の approval 要求で workflow が停止するため使用禁止 (`skills/workflow-orchestration/references/startup_contract.md` の tmp area 利用契約 参照)。hook は write 対象 path のみを判定し `$TMPDIR` env を参照しない |
| `enforce_guarded_apply_patch` | `Edit`/`Write`/`apply_patch` で `.json`/`.txt` を書こうとした | `python3 tools/orchestration_runtime.py guarded-apply-patch --repo-root . --orchestration-id <oid> --actor-role <role> --agent-run-id <id> --paths-json '["<path>"]' --patch-file workspace/tmp/<agent_run_id>/x.patch --capability-token <token>` に切り替える（`<agent_run_id>` は literal 置換）。`spec.ir.yaml` などの `.yaml` は `Edit`/`Write` を直接使う（guarded-apply-patch は `.json`/`.txt` 専用） |
| `forbid_python_inline_write` | `python3 -c` / `python3 - <<EOF` を実行した | **書き込み意図**: `.json`/`.txt` は `guarded-apply-patch`、その他は `Edit`/`Write` tool を使う。**UUID 生成意図**: `python3 tools/new_agent_run_id.py` を使う。**JSON 読み取り意図**: `Read` tool で直接読む |
| `forbid_tools_direct_read` | `grep`・`cat`・`sed` で `tools/` 配下を読もうとした | `tools/` の実装は参照禁止。仕様は `docs/`・`spec/`・`skill_must_read_refs` を参照する |
| `rule_source_violation` | 他 agent の capability・gate 結果・他 phase の SKILL.md を読んだ | gate 失敗内容は `2>workspace/tmp/<agent_run_id>/last_gate_stderr.txt` で stderr をキャプチャして取得する（`<agent_run_id>` は literal 置換） |
| `forbid_git_reset_hard` | `git reset --hard` を実行しようとした | `git restore <file>` または `git checkout <file>` で個別ファイルを戻す |
| `capability_invalid_empty_write_roots` | `write_roots=[]` の capability で書き込もうとした | `record-launch` の `--request-json` に `allowed_output_paths` が正しく設定されているか確認する |

## unauthorized_write_violation の dismiss {#dismiss-violation-recovery}

`record-agent-run` が `terminal run has unauthorized write paths: ...` で fail する場合、以下の手順で operator が良性 violation を承認（dismiss）して再試行できる。

**典型的な発生原因（良性）**

- `tools/__pycache__/*.pyc` — git-ignored Python bytecode（Fix 1b の snapshot ignore でほぼ消えるが既存 pyc が残っている場合の保険）
- MCP server が生成した audit log が `manifest_integrity_protected_logs` に含まれていなかった

**回復手順**

1. violation の unauthorized_paths を確認する。
   ```
   cat workspace/orchestrations/<orch_id>/violations/<arid>.unauthorized_write_violation.json
   ```
2. 良性パスのみを dismiss する（`--paths` は violation の `unauthorized_paths` の部分集合として照合される）。
   ```bash
   python3 tools/orchestration_runtime.py dismiss-violation \
     --repo-root . \
     --orchestration-id <orch_id> \
     --agent-run-id <arid> \
     --dismiss-reason "tools/__pycache__ は gitignore 済み Python bytecode であり無害" \
     --operator-token "$(cat ~/.met-dsl/operator_tokens/<orch_id>.txt)" \
     --paths tools/__pycache__/orchestration_runtime.cpython-313.pyc
   ```
   operator token は orchestration init 時に `~/.met-dsl/operator_tokens/<orch_id>.txt` へ自動生成される。`workspace/` 配下ではないため agent からは読み取れず、operator のみが参照できる。
3. 同一 `agent_run_id` で `record-agent-run` を再実行する。検出された unauthorized_paths が `dismissed_paths` の部分集合（= dismissed_paths が unauthorized_paths を包含）であれば terminal validation を通過する。違反パスを一部しか dismiss していない場合は再実行が再度 fail するため、未 dismiss の違反パスが残っていないか確認すること。

**注意**

- dismiss-violation は operator の明示的承認を記録するための safety gate であり、自動化スクリプトで呼ばないこと。
- 新規パスを後から追加 dismiss する場合は同じコマンドを再実行する（`dismissed_paths` が上書きされる）。
- dismiss 済み violation に対し、後続の再検出が **dismiss 対象外の新規 unauthorized path** を含む場合、violation file は再生成され terminal validation は再 fail する。このとき従前の operator 承認は失われず、`prior_dismissals[]`（`dismissed_at` / `dismiss_reason` / `dismissed_paths` / `superseded_at`）として履歴に保全される（監査証跡の連続性確保）。
- Fix 1a（`PYTHONDONTWRITEBYTECODE=1` 環境変数）が適用されていれば `.pyc` violation 自体が発生しないため、通常この手順は不要。

## duplicate agent_run_id recovery {#duplicate-agent_run_id-recovery}

`record-agent-run` を **同一 `agent_run_id`** で 2 回 invoke すると `ValueError: duplicate agent_run_id: <id>` を raise する。idempotent ではない hard error として設計されており、同じ `agent_run_id` を後から更新／upsert する経路は無い。

**典型的な発生原因**

- 既に `agent_runs.jsonl` へ append 済みの child agent_run について retry を試みた
- orchestration agent 自身の entry を terminal で再 append しようとした（orchestration の終端は `set-status` 経由が canonical で、`record-agent-run` を 2 回目に呼ぶ経路は存在しない）

**回復手順**

1. `python3 tools/new_agent_run_id.py` で新しい `agent_run_id` を採番する。
2. `python3 tools/orchestration_runtime.py reserve-phase-root --orchestration-id <oid> --agent-run-id <new_arid> --node-key <node_key> --step <step>` で `ir_id` / `pipeline_id` を新規予約する（旧 `agent_run_id` が予約済みの場合は予約の reuse 可否を operator に確認）。
3. `record-launch` → `Agent` tool 起動 → `record-child-return` → `deactivate-child` → `record-reply` → `record-agent-run` の正規 sequence を新 `agent_run_id` で再実行する（CLAUDE.md の手順 1–9 を参照）。
4. orchestration 自体を終端させる場合は `set-status --status fail_closed --reason-code <code> --reason-detail <detail>` を呼ぶ。`agent_runs.jsonl` の orchestration 行を更新しないこと。

詳細な CLI 規約は [docs/CLI_REFERENCE.md#record-agent-run](CLI_REFERENCE.md#record-agent-run) を canonical source とする。

## Substep timeout 復旧 {#substep-timeout-recovery}

子 Agent tool が API stream idle timeout で途中切断された場合、orchestration agent は `record-timeout` を呼んで終端 entry を確定させる。**ad-hoc な script を `workspace/tmp/` に書いてはならない**。

**前提**: `record-timeout` を呼ぶ前に必ず以下の順で実行すること。

1. `record-child-return --agent-run-id <arid> --return-token <token>`: orchestration agent が Agent tool の return を実際に観測した証跡を記録。
2. `deactivate-child --child-run-id <arid>`: ack を確認 + token 一致を再検証したうえで active marker を解除。
3. `record-timeout --agent-run-id <arid> --reason ...`: 終端 entry を記録。

```bash
# return-token は $(cat ...) をインライン引数として渡す。VAR=$(cat ...) の 2-step
# shell var 形式は先頭 `VAR=` が `Bash(python3 ...)` allowlist 一致を壊し session
# approval を要求するため使用しない。
python3 tools/orchestration_runtime.py record-child-return \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --agent-run-id <child_agent_run_id> \
  --return-token "$(cat workspace/orchestrations/<orchestration_id>/launches/<child_agent_run_id>.parent_return_token)"

python3 tools/orchestration_runtime.py deactivate-child \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --child-run-id <child_agent_run_id>

python3 tools/orchestration_runtime.py record-timeout \
  --repo-root . \
  --orchestration-id <orchestration_id> \
  --agent-run-id <child_agent_run_id> \
  --reason "API stream idle timeout after 600s"
```

呼び出し後、orchestration agent は `set-status --status fail_closed --reason-code <code> --reason-detail <detail>` を続けて呼び、orchestration 自体を終端させる。

### Wedged child の escape hatch

Agent tool プロセスが return を一切観測できない異常状態で `record-child-return` が書けない場合に限り、`record-timeout --force-reason "<operator override 内容>"` で marker check を bypass できる。通常フローを優先し、最終手段として使用すること。
