# Glossary / Notation / Level Definitions

この文書は、他のドキュメントが参照する term を 1 か所に集約し、単独で読んでも意味が通るようにする。

## 1. Artifacts
- **controlled_spec.md**: 物理・数値アルゴリズム定義の canonical source。生成器が `model`（実装本体）を作るために参照する。
- **problem spec**: `spec_kind=problem` の `controlled_spec.md`。統合シナリオ、実行時 input contract、依存 `component` / 採用 `profile` を定義する。
- **component spec**: `spec_kind=component` の `controlled_spec.md`。再利用可能な物理演算の input/output contract と公開 `operation` を定義する。
- **profile spec**: `spec_kind=profile` の `controlled_spec.md`。`component` の選択規則、既定値、拘束条件を定義する。
- **tests.md**: 検証プロファイル（入力インスタンス、ケース展開、判定条件）の canonical source。`problem` / `component` / `profile` の全 `spec_kind` で使用し、テストランナーは必要箇所を決定的に解釈して参照する。
- **spec_catalog.yaml**: `spec` の registry。`spec_kind`、`domain`、`family`、`spec_id`、配置先（`controlled_spec_path`、`tests_path` など）、状態、`official_releases`（正式版実装の登録情報）を保持する。
- **component_catalog.yaml**: 再利用 `component` / `operation` の registry。保存先は `releases/registry/component_catalog.yaml` とし、責務、公開 `API`、互換性、実装状態を保持する。
- **deps.yaml**: 各 `spec` が要求する依存宣言。`component_id` / `profile_id` と `version constraint` を定義する。
- **case.yaml**: 人間が書く（または将来 `Spec` から生成する）テストケース定義。`sweep` / `refinement` などを含み得る。
- **case.resolved.yaml**: テストランナーが生成する「決定済み」入力。`sweep` 展開と実行時入力の決定値を保持する。`runner`（例: `simulate`）がこれを読む。演算構成、output contract、検証契約を保持してはならない。
- **impl.resolved.yaml**: 実装計画（`Implementation Plan`）。計算過程（並列化、メモリ配置、融合、ブロッキング等）に関する可変パラメタを決定したもの。性能チューニングの探索対象になり得る。
- **dependency.resolved.yaml**: 依存解決結果の canonical source。`node_key`、`direct_deps`、`transitive_deps`、`topo_level` を保持する。
- **direct dependency plan readiness**: 対象 `node` の直下依存 `node` について、対応する `plan_id` が発行済みであり、`plan_meta.json.verification_status=pass` を満たす状態。この条件を満たさない上位 `node` は `Plan` を開始してはならない。
- **direct dependency execution readiness**: 対象 `node` の直下依存 `node` について、対応する `plan_id` と `pipeline_id` が発行済みであり、最新 `aggregate_verdict` が `pass` または `xfail` である状態。この条件を満たさない上位 `node` は `Generate` 以降を開始してはならない。
- **algorithm.resolved.yaml**: `Plan` が導出する `YAML` mapping 形式の生成契約。`problem` の統合順序、`component operation` の呼び出し順序、条件分岐、反復、列処理、派生量定義、更新対象、`invariants` を保持し、`Generate` の canonical source 入力として使用する。`ordering` は `step_id` 列または `before` / `after` object 列を許可し、`iteration_contract` と `splitting_policy` は object とする。
- **derived_contract.json**: `Plan verify` が導出する検証契約。`io_contract.inputs` / `io_contract.outputs` と `semantic_dependency.required_sources` と `raw_requirements.required_evidence` と `test_evidence_requirements` を保持し、`Generate verify` と `Execute` / `Judge` の判定 canonical source として使用する。`semantic_dependency.required_sources` の canonical form は文字列配列とする。`test_evidence_requirements.required_raw_variables` は宣言済み `state_snapshots` 変数名または `time_variable` のみを許可する。生成契約を保持してはならない。
- **expected_node_set**: `deps.yaml` と `spec_catalog.yaml` から再構成した期待 `node` 集合。`dependency.resolved.yaml` の網羅検証に使用する。
- **node workflow**: 単一 `node_key` を対象にした `Plan -> Generate -> Build -> Execute -> Judge` の 1 系列実行。
- **orchestration agent**: `workflow` 全体の進行制御を担当する統括エージェント。`step` / `substep` 起動、依存順序管理、状態集約を担当し、phase artifactsを直接生成しない。`substep` を持つ phase では `substep agent` を直接管理し、`step_result.json` を集約する。
- **step agent**: 単一 `node` の単一 `step` を担当するエージェント。標準 `substep` を持たない phase の artifact generation と検証を担当する。
- **substep agent**: 単一 `substep` を担当するエージェント。input contract に従って artifact を生成し、`orchestration agent` へ返却する。
- **node_key_safe**: `node_key` の保存用表記。推奨形式は `<spec_kind>__<spec_id>__<spec_version>`。
- **orchestration_id**: 1 回の `workflow` 実行全体を識別する `ID`。`workspace/orchestrations/<orchestration_id>/` の保存キーとして使用する。
- **plan_id**: `node` 単位で `case.resolved.yaml` と `algorithm.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の組を識別する `ID`。推奨形式は `<slug>_<date>_<seq3>`。
- **pipeline_id**: `node` 単位の `Generate -> Build -> Execute` 系列を識別する `ID`。推奨形式は `<slug>_<date>_<seq3>`。
- **generation_id / build_id / execution_id**: 各段階の試行を識別する `ID`。推奨形式は `<prefix>_<date>_<seq3>`（`prefix` は `gen` / `build` / `exec`）。
- **agent_run_id**: `step agent` / `substep agent` / `orchestration agent` の 1 回の実行を識別する `ID`。`parent_agent_run_id` と組で親子関係を表す。
- **issue_severity**: 子 `agent` artifact の問題重大度。`minor` / `major` / `critical` の 3 値を使用する。
- **repair_strategy**: 子 `agent` への再投入方針。`reuse` は同一 `agent_session_id` 継続修正、`restart` は新規 `agent_session_id` 再起動を表す。
- **repair_target_agent_run_id**: 再投入判断の対象にした直前 `agent_run` を示す参照 `ID`。
- **node_key**: 実行 / 判定対象 `node` の識別子。形式は `<spec_kind>/<spec_id>@<spec_version>` とする。
- **topo_level**: 依存 `DAG` におけるトポロジカル階層。小さい値ほど下層 `node` を表す。
- **release_id**: 各 `spec` の正式版実装を識別する `ID`。推奨形式は `<spec_version>_<utc_ts>_<seq3>`。
- **target_architecture**: 正式版 artifact を分離するアーキテクチャ識別子。例: `x86_64`,`aarch64`,`nvidia_sm80`。
- **release artifact root**: 正式版 artifact の保存ルート。`releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` を canonical source とする。
- **official_releases**: `spec_catalog.yaml` に保持する正式版実装の登録配列。`target_architecture`、`toolchain_language`、`target_backend`、`source_pipeline_id`、`source_generation_id`、`source_build_id`、`source_execution_id`、`artifact_root`、`promoted_at`、`status` を持つ。
- **lineage.json**: `spec_ref`、`plan_ref`、`pipeline_id`、各段階 `ID` の関係を記録する来歴ファイル。
- **orchestration_meta.json**: `orchestration` 実行メタデータ。`orchestration_id`、対象 `spec_ref`、`dependency_ref`、開始時刻、実行状態を記録する。チェックポイント再開を許可する場合は `resume_enabled`（真偽）と `resumed_at`（任意）を追加する。
- **orchestration_checkpoint.json**: `pass` 完了済み `step` と `output_refs` の SHA-256 を保持する orchestration 証跡。`write-step-result` が `status=pass` で完了したときに `tools/orchestration_runtime.py` により更新される。手動編集を禁止する。
- **resume_enabled**: `orchestration_meta.json` の真偽フィールド。`true` の orchestration のみ `orchestration_checkpoint.json` をスキップ判定の入力として使用してよい。
- **skipped_by_checkpoint**: `agent_runs.jsonl` の `agent_role` 値のひとつ。チェックポイント整合性が確認でき当該 `step` を起動しなかったことを記録する。
- **agent_graph.json**: `orchestration` における `agent` 親子関係。`parent_agent_run_id` と `child_agent_run_id` と `relation_type` を記録する。
- **context_id**: `LLM` 実行コンテキスト識別子。`step agent` / `substep agent` ごとに固有値を持ち、同一 `orchestration_id` 内で重複を禁止する。
- **context_isolated**: `step agent` / `substep agent` が独立コンテキストで実行されたことを示す真偽値。`true` を必須とする。
- **agent_runs.jsonl**: `agent` 実行イベントの時系列ログ。`agent_run_id`、`parent_agent_run_id`、`agent_role`、`status`、`started_at`、`finished_at`、`agent_backend`、`agent_model`、`context_id`、`context_isolated`、`launch_request_ref`、`launch_response_ref`、`launch_prompt_ref`、`launch_reply_ref`、`agent_result_ref`、`agent_summary_ref` を記録する。再投入時は `launch_request_ref` 先へ `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を記録する。
- **step_result.json**: phase 集約結果。`status`、`required_outputs`、`failed_substeps`、`executor_agent_run_id`、`substep_agent_run_ids` を記録する。`substep` を持つ phase では `executor_agent_run_id` は `orchestration agent_run_id`、標準 `substep` を持たない phase では `step agent_run_id` とする。`substep_agent_run_ids` は `substep` を持たない phase で空配列を許可する。再投入を実施した phase は `retry_decisions` を追加し、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `new_agent_run_id` と `repair_reason` を保持する。
- **model**: 物理計算を実行する計算コンポーネント / ライブラリ。入力状態から次状態を計算する責務を持つ。
- **runner（例: `simulate`）**: 実行エントリポイント。入力読込・`model` 呼び出し・`diagnostics` / `perf` 出力を担当する。
- **`<stage>_meta.json`**: `LLM` 利用ステージの実行メタデータ。`attempt_count`、`verification_status`、`last_fail_reason`、`context_isolated`、`debug_mode` を保持する。`context_isolated=false` では `constraint_reason` を必須とする。`debug_mode=true` で失敗試行を保存した場合は `retained_failed_attempts` と保存先を保持する。
- **generate_meta.json**: コード生成ステージの `<stage>_meta.json`。
- **verifier（in-stage）**: `LLM` ステージ内部で実行される整合チェック担当。artifact のみを入力に取り、`generate -> verify -> regenerate` ループで合否を返す。
- **diagnostics.json**: `runner` が出す物理・数値診断（保存量、誤差、`CFL` など）。合否は含めない。
- **perf.json**: `runner` が出す性能診断（最低限 `walltime_sec`、`throughput_cells_per_sec`、`parallelism`）。合否は含めない。
- **verdict.json**: 当該 `node` の合否判定（`self_verdict`）と根拠。
- **aggregate_verdict.json**: 当該 `node` と推移依存 `node` を含む集約合否判定。
- **summary.json**: `run` 全体の集計。`self_summary` と `dependency_summary` を必須保持する。
- **dependency_summary**: 依存集約件数。`total`、`pass`、`xfail`、`fail`、`blocked` を保持する。
- **dependency workflow coverage check**: `dependency.resolved.yaml` の `node_key` 集合と `workspace/plans` / `workspace/pipelines` の `node` 集合が 1 対 1 で一致することを確認する検証。
- **dependency implementation encapsulation**: 依存 `node` の実装本体を依存元 `node` の `generate/<generation_id>/src/` 配下へ複製・再配置・再定義しない境界規則。依存元 `node` は依存 `node` の公開 `operation` 呼び出し、共有 `library`、または `profile` 参照のみを保持できる。
- **blocked_reason**: `node` が `blocked` で終了した直接理由。依存 `node` の `fail` / `blocked` を識別可能に記録する。
- **blocking_direct_deps**: `blocked` を引き起こした直下依存 `node_key` の配列。
- **stdout.log / stderr.log**: 実行ログ（必ず保存し、後追いデバッグ可能にする）。
- **attempts/**: `debug_mode=true` のときにのみ作成される失敗試行保存ディレクトリ。標準運用（`debug_mode=false`）では作成しない。
- **dummy output**: workflow 進行または `tests` 合格を目的に、実行根拠なしで人工生成した artifact。`diagnostics` / `perf` / `verdict` / `aggregate_verdict` を含む。
- **dummy computation**: 物理計算を実行せず、固定値や定型文字列のみで計算結果を代替する実装。
- **fail-fast stop**: phase input 不足または契約不一致を検知した時点で当該 phase を `fail` で停止し、推測補完や人工生成で継続しない運用規則。
- **pipeline semantic validation**: `python3 tools/validate_pipeline_semantics.py` の `--stage` invocation による内容検証ゲートである。`--stage plan` / `post_generate` / `post_build` は execution artifact 無しで該当段階の契約と生成物を検証し、`post_execute` / `pre_judge` / 省略時（`full`）は `raw` 一次証跡、`trial_meta` 追跡整合、`quality check` 比較 canonical source、固定値生成パターン、`copy_based_artifact_reuse` 等を機械検証する。
- **static lint**: `Generate` ステージで MCP `run_linter` により実行するソース静的解析である。`Build` の `compile_project` や `toolchain.build_system` 経由のビルドとは別手順とする。`quality check`（`run_quality_checks`）とは別物である。
- **lint_command_ref**: `generate_meta.json` が保持する `static lint` の MCP 証跡である。`verification_status=pass` の場合に必須とし、`run_linter` キー配下に `command_id` と `command_log_ref` と `preset` を持つ object 配列を記録する。
- **metrics basis**: `raw/metrics_basis.json` に保存する per-test evidence index。`test_evidence_requirements` の全 `test_id` を保持し、各 `test_id` の Judge 再計算に必要な `required_raw_variables` を raw 値または raw 参照として保持する。suite 全体 summary や `diagnostics.json` の複写で代替してはならない。
- **raw snapshot schema**: `problem` `node` の `raw/state_snapshots/snapshot_schema.json` に保存する項目定義。`variables[].name` と `variables[].shape_expr` と `time_variable` と `time_shape_expr` により、各問題設定で判定再計算に使用する状態量と時刻情報を表す。
- **algorithm contract**: `algorithm.resolved.yaml` が保持する演算構成 `IR`。`execution_mode` と `steps[]` と `ordering` と `control_condition` と `iteration_contract` と `update_semantics` と `temporaries` と `derived_field_rules` と `invariants` と `splitting_policy` を必須語彙とする。

補足:
- `perf.json` は `diagnostics.json` とは分離して出力する（同居しない）。
- `verifier` は `generator` と独立したコンテキストでの実行を可能な限り優先する。
- 実行環境の制約で独立コンテキストを確保できない場合は、同一コンテキスト実行を許容し、各ステージの `<stage>_meta.json` に制約理由を記録する。
- 失敗試行の中間 artifact は標準運用で保存しない。保存は `debug_mode=true` の場合のみ許可する。

## 2. Test Levels (L0-L3)
`L0-L3` は「テストの粒度と目的」を表す classification であり、実装の層番号ではない。

- **L0: 部品テスト（Unit / Operator / Guard）**
- **L1: 解析解・収束傾向テスト（Analytic / MMS / Refinement）**
- **L2: 保存則・制約テスト（Invariants / Constraints）**
- **L3: ロバスト性・同値性テスト（Robustness / Equivalence）**
- 同値性に「性能回帰（`performance regression`）」も含める（物理合格の上で性能を比較する）。

## 3. Expected Failure (Guard / XFAIL)
- 正しく実装されていれば「失敗する」べきテスト。
- expected failure条件を満たした場合は `PASS` と判定する。

## 3-1. Dependency Block (Blocked)
- 直下依存 `node` の `fail` または依存未解決により、上位 `node` の判定を開始できない状態。
- `blocked` は `aggregate_verdict` と `dependency_summary` に必須記録する。
- `blocked` が発生した上位 `node` の workflow execution result は `fail` とする。

## 4. Physical Validity
bitwise 一致は要求しない。以下の性質で一致を判定する。
- 保存則ドリフトが許容内
- 制約（非負性、過大なオーバーシュート）が許容内
- 解析解や参照解に対する誤差が許容内
- `refinement` で誤差が改善
- 将来: 統計・スペクトル・アンサンブル指標

## 5. Algorithm Classes
本プロジェクトでは「アルゴリズム」を 2 種類に分ける。

### A) 物理アルゴリズム（Physics-affecting）
- 物理結果（精度・安定性）に影響する選択。
- 例: 空間離散化（中央 2 次、一次風上、`WENO` 等）、時間積分、フィルタ、拡散、物理過程の近似、境界条件の数値実装。
- **`case.resolved.yaml` と `algorithm.resolved.yaml` で決定し、決定的である必要がある**（同じ `case` と同じ `algorithm` なら同じ物理解が期待される）。

### B) 実行アルゴリズム（Execution-only / Performance-affecting）
- 物理結果（理想的には）を変えず、計算過程（性能、メモリ、並列効率）に影響する選択。
- 例: ループ順序、タイル / ブロッキング、配列レイアウト、融合 / 分割、ベクトル化、`GPU` カーネル分割、非同期、数値的に等価な式変形、通信重ね合わせ。
- **`impl.resolved.yaml` で表現し、探索（自動チューニング）の対象にできる**。

注意:
- 実行アルゴリズムでも丸め誤差の差は起こり得る。許容は「物理的妥当性一致」で吸収する。

## 6. Determinism
- determinismは「物理結果の再現性」を保証するために必要である。
- ただし、物理結果を保証するdeterminismは主に **物理アルゴリズム（A）** と入力条件の決定に関わる。
- **実行アルゴリズム（B）は必ずしも固定しない**。性能チューニングでは B を意図的に変えて探索する。

## 7. run_id
- 1 回のテスト実行に付与する識別子。
- 推奨: `YYYYMMDD_HHMMSS_<gitsha>_<target>`

## 8. MCP（Model Context Protocol）
- ツール実行を標準化するためのプロトコル。
- 本プロジェクトでは `compile` / `run` / `quality check` を `MCP` サーバー経由で実行する。
- `fortran` / `c` / `cpp` / `mixed` 系の `compile` は、依存関係を扱える標準ビルドツール（既定値 `make`）を介して実行する。

## 9. Automatic Differentiation (AD)
- 離散実装された計算グラフに対して導関数（`JVP` / `VJP` / `gradient`）を機械的に求める手法。
- 本プロジェクトでは将来対応を前提とし、現段階では「`AD` を阻害しない仕様・実装構造」を要求する。
- 非微分演算（例: `clip`、`limiter`、分岐）を含む場合は、仕様上で扱いを明示する。

## 10. `spec` Classification Vocabulary (`spec_kind` / `domain` / `family`)
- **spec_kind**: `spec` の種別。`problem` / `component` / `profile` の 3 値のみを許可する。
- **domain**: 物理モデルの上位 classification。`spec` 配置と `component_id` 接頭辞の一貫性を保つための固定語彙。例: `dynamics`, `microphysics`, `radiation`, `land_surface`。
- **family**: `domain` 内の classification 単位。`problem` では方程式群、`component` では再利用演算群、`profile` では選択規則群を表す。
- **component**: `component spec` が定義する再利用可能な物理演算単位。方程式系または離散化責務で分割する。例: `advection_flux`, `time_integrator`, `boundary_periodic`。
- **operation**: `component` が公開する呼び出し単位。言語固有の関数・手続き・メソッドなどの実体を抽象化した語彙。
- **適用規則**: `spec` の配置は `spec/<spec_kind>/<domain>/<family>/<spec_id>/...` とする。`component_id` 推奨形式 `<domain>_<family>_<operator>_<dim>d_<scheme>` の先頭 2 要素は `domain` と `family` に一致させる。`operation_id` は `<component_id>__<action>` 形式を用いる。
