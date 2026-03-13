# 用語集・記号・レベル定義

この文書は、他のドキュメントが参照する用語を 1 か所に集約し、単独で読んでも意味が通るようにする。

## 1. 成果物（Artifacts）
- **controlled_spec.md**: 物理・数値アルゴリズム定義の正本。生成器が `model`（実装本体）を作るために参照する。
- **problem spec**: `spec_kind=problem` の `controlled_spec.md`。統合シナリオ、実行時入力契約、依存 `component` / 採用 `profile` を定義する。
- **component spec**: `spec_kind=component` の `controlled_spec.md`。再利用可能な物理演算の入出力契約と公開 `operation` を定義する。
- **profile spec**: `spec_kind=profile` の `controlled_spec.md`。`component` の選択規則、既定値、拘束条件を定義する。
- **tests.md**: 検証プロファイル（入力インスタンス、ケース展開、判定条件）の正本。`problem` / `component` / `profile` の全 `spec_kind` で使用し、テストランナーは必要箇所を決定的に解釈して参照する。
- **spec_catalog.yaml**: `spec` の台帳。`spec_kind`、`domain`、`family`、`spec_id`、配置先（`controlled_spec_path`、`tests_path` など）、状態、`official_releases`（正式版実装の登録情報）を保持する。
- **component_catalog.yaml**: 再利用 `component` / `operation` の台帳。保存先は `releases/registry/component_catalog.yaml` とし、責務、公開 `API`、互換性、実装状態を保持する。
- **deps.yaml**: 各 `spec` が要求する依存宣言。`component_id` / `profile_id` と `version constraint` を定義する。
- **case.yaml**: 人間が書く（または将来 `Spec` から生成する）テストケース定義。`sweep` / `refinement` などを含み得る。
- **case.resolved.yaml**: テストランナーが生成する「決定済み」入力。`sweep` 展開、物理アルゴリズム（後述）と数値条件を決定したもの。`runner`（例: `simulate`）がこれを読む。出力契約を保持してはならない。
- **impl.resolved.yaml**: 実装計画（`Implementation Plan`）。計算過程（並列化、メモリ配置、融合、ブロッキング等）に関する可変パラメタを決定したもの。性能チューニングの探索対象になり得る。
- **dependency.resolved.yaml**: 依存解決結果の正本。`node_key`、`direct_deps`、`transitive_deps`、`topo_level` を保持する。
- **derived_contract.json**: `Plan verify` が導出する検証契約。`io_contract.inputs` / `io_contract.outputs` と `semantic_dependency.required_sources` と `raw_requirements.required_evidence` と `test_evidence_requirements` を保持し、`Generate verify` と `Execute` / `Judge` の判定正本として使用する。
- **expected_node_set**: `deps.yaml` と `spec_catalog.yaml` から再構成した期待 `node` 集合。`dependency.resolved.yaml` の網羅検証に使用する。
- **node workflow**: 単一 `node_key` を対象にした `Plan -> Generate -> Build -> Execute -> Judge` の 1 系列実行。
- **orchestration agent**: `workflow` 全体の進行制御を担当する統括エージェント。`step` / `substep` 起動、依存順序管理、状態集約を担当し、工程成果物を直接生成しない。`substep` を持つ工程では `substep agent` を直接管理し、`step_result.json` を集約する。
- **step agent**: 単一 `node` の単一 `step` を担当するエージェント。標準 `substep` を持たない工程の成果物生成と検証を担当する。
- **substep agent**: 単一 `substep` を担当するエージェント。入力契約に従って成果物を生成し、`orchestration agent` へ返却する。
- **node_key_safe**: `node_key` の保存用表記。推奨形式は `<spec_kind>__<spec_id>__<spec_version>`。
- **orchestration_id**: 1 回の `workflow` 実行全体を識別する `ID`。`workspace/orchestrations/<orchestration_id>/` の保存キーとして使用する。
- **plan_id**: `node` 単位で `case.resolved.yaml` と `impl.resolved.yaml` と `dependency.resolved.yaml` の組を識別する `ID`。推奨形式は `<node_key_safe>_<case_hash12>_<impl_hash12>`。
- **pipeline_id**: `node` 単位の `Generate -> Build -> Execute` 系列を識別する `ID`。推奨形式は `<plan_id>_<utc_ts>_<seq3>`。
- **generation_id / build_id / execution_id**: 各段階の試行を識別する `ID`。
- **agent_run_id**: `step agent` / `substep agent` / `orchestration agent` の 1 回の実行を識別する `ID`。`parent_agent_run_id` と組で親子関係を表す。
- **issue_severity**: 子 `agent` 成果物の問題重大度。`minor` / `major` / `critical` の 3 値を使用する。
- **repair_strategy**: 子 `agent` への再投入方針。`reuse` は同一 `agent_session_id` 継続修正、`restart` は新規 `agent_session_id` 再起動を表す。
- **repair_target_agent_run_id**: 再投入判断の対象にした直前 `agent_run` を示す参照 `ID`。
- **node_key**: 実行 / 判定対象 `node` の識別子。形式は `<spec_kind>/<spec_id>@<spec_version>` とする。
- **topo_level**: 依存 `DAG` におけるトポロジカル階層。小さい値ほど下層 `node` を表す。
- **release_id**: 各 `spec` の正式版実装を識別する `ID`。推奨形式は `<spec_version>_<utc_ts>_<seq3>`。
- **target_architecture**: 正式版成果物を分離するアーキテクチャ識別子。例: `x86_64`,`aarch64`,`nvidia_sm80`。
- **release artifact root**: 正式版成果物の保存ルート。`releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` を正本とする。
- **official_releases**: `spec_catalog.yaml` に保持する正式版実装の登録配列。`target_architecture`、`toolchain_language`、`target_backend`、`source_pipeline_id`、`source_generation_id`、`source_build_id`、`source_execution_id`、`artifact_root`、`promoted_at`、`status` を持つ。
- **lineage.json**: `spec_ref`、`plan_ref`、`pipeline_id`、各段階 `ID` の関係を記録する来歴ファイル。
- **orchestration_meta.json**: `orchestration` 実行メタデータ。`orchestration_id`、対象 `spec_ref`、`dependency_ref`、開始時刻、実行状態を記録する。
- **agent_graph.json**: `orchestration` における `agent` 親子関係。`parent_agent_run_id` と `child_agent_run_id` と `relation_type` を記録する。
- **context_id**: `LLM` 実行コンテキスト識別子。`step agent` / `substep agent` ごとに固有値を持ち、同一 `orchestration_id` 内で重複を禁止する。
- **context_isolated**: `step agent` / `substep agent` が独立コンテキストで実行されたことを示す真偽値。`true` を必須とする。
- **agent_runs.jsonl**: `agent` 実行イベントの時系列ログ。`agent_run_id`、`parent_agent_run_id`、`agent_role`、`status`、`started_at`、`finished_at`、`agent_backend`、`agent_model`、`context_id`、`context_isolated` を記録する。再投入時は `launch_request_ref` 先へ `issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `repair_reason` を記録する。
- **step_result.json**: 工程集約結果。`status`、`required_outputs`、`failed_substeps`、`executor_agent_run_id`、`substep_agent_run_ids` を記録する。`substep` を持つ工程では `executor_agent_run_id` は `orchestration agent_run_id`、標準 `substep` を持たない工程では `step agent_run_id` とする。`substep_agent_run_ids` は `substep` を持たない工程で空配列を許可する。再投入を実施した工程は `retry_decisions` を追加し、`issue_severity` と `repair_strategy` と `repair_target_agent_run_id` と `new_agent_run_id` と `repair_reason` を保持する。
- **model**: 物理計算を実行する計算コンポーネント / ライブラリ。入力状態から次状態を計算する責務を持つ。
- **runner（例: `simulate`）**: 実行エントリポイント。入力読込・`model` 呼び出し・`diagnostics` / `perf` 出力を担当する。
- **`<stage>_meta.json`**: `LLM` 利用ステージの実行メタデータ。`attempt_count`、`verification_status`、`last_fail_reason`、`context_isolated`、`debug_mode` を保持する。`context_isolated=false` では `constraint_reason` を必須とする。`debug_mode=true` で失敗試行を保存した場合は `retained_failed_attempts` と保存先を保持する。
- **generate_meta.json**: コード生成ステージの `<stage>_meta.json`。
- **verifier（in-stage）**: `LLM` ステージ内部で実行される整合チェック担当。成果物のみを入力に取り、`generate -> verify -> regenerate` ループで合否を返す。
- **diagnostics.json**: `runner` が出す物理・数値診断（保存量、誤差、`CFL` など）。合否は含めない。
- **perf.json**: `runner` が出す性能診断（最低限 `walltime_sec`、`throughput_cells_per_sec`、`parallelism`）。合否は含めない。
- **verdict.json**: 当該 `node` の合否判定（`self_verdict`）と根拠。
- **aggregate_verdict.json**: 当該 `node` と推移依存 `node` を含む集約合否判定。
- **summary.json**: `run` 全体の集計。`self_summary` と `dependency_summary` を必須保持する。
- **dependency_summary**: 依存集約件数。`total`、`pass`、`xfail`、`fail`、`blocked` を保持する。
- **dependency workflow 網羅チェック**: `dependency.resolved.yaml` の `node_key` 集合と `workspace/plans` / `workspace/pipelines` の `node` 集合が 1 対 1 で一致することを確認する検証。
- **blocked_reason**: `node` が `blocked` で終了した直接理由。依存 `node` の `fail` / `blocked` を識別可能に記録する。
- **blocking_direct_deps**: `blocked` を引き起こした直下依存 `node_key` の配列。
- **stdout.log / stderr.log**: 実行ログ（必ず保存し、後追いデバッグ可能にする）。
- **attempts/**: `debug_mode=true` のときにのみ作成される失敗試行保存ディレクトリ。標準運用（`debug_mode=false`）では作成しない。
- **dummy 出力**: workflow 進行または `tests` 合格を目的に、実行根拠なしで人工生成した成果物。`diagnostics` / `perf` / `verdict` / `aggregate_verdict` を含む。
- **dummy 計算**: 物理計算を実行せず、固定値や定型文字列のみで計算結果を代替する実装。
- **fail-fast 停止**: 工程入力不足または契約不一致を検知した時点で当該工程を `fail` で停止し、推測補完や人工生成で継続しない運用規則。
- **pipeline semantic validation**: `python3 tools/validate_pipeline_semantics.py` による内容検証ゲート。`raw` 一次証跡、`trial_meta` 追跡整合、`quality check` 比較正本、固定値生成パターン、`copy_based_artifact_reuse` を機械検証する。
- **raw snapshot schema**: `problem` `node` の `raw/state_snapshots/snapshot_schema.json` に保存する項目定義。`variables[].name` と `variables[].shape_expr` と `time_variable` と `time_shape_expr` により、各問題設定で判定再計算に使用する状態量と時刻情報を表す。

補足:
- `perf.json` は `diagnostics.json` とは分離して出力する（同居しない）。
- `verifier` は `generator` と独立したコンテキストでの実行を可能な限り優先する。
- 実行環境の制約で独立コンテキストを確保できない場合は、同一コンテキスト実行を許容し、各ステージの `<stage>_meta.json` に制約理由を記録する。
- 失敗試行の中間成果物は標準運用で保存しない。保存は `debug_mode=true` の場合のみ許可する。

## 2. テストレベル（L0-L3）の意味
`L0-L3` は「テストの粒度と目的」を表す分類であり、実装の層番号ではない。

- **L0: 部品テスト（Unit / Operator / Guard）**
- **L1: 解析解・収束傾向テスト（Analytic / MMS / Refinement）**
- **L2: 保存則・制約テスト（Invariants / Constraints）**
- **L3: ロバスト性・同値性テスト（Robustness / Equivalence）**
- 同値性に「性能回帰（`performance regression`）」も含める（物理合格の上で性能を比較する）。

## 3. 期待失敗（Guard / XFAIL）
- 正しく実装されていれば「失敗する」べきテスト。
- 期待失敗条件を満たした場合は `PASS` と判定する。

## 3-1. 依存ブロック（Blocked）
- 直下依存 `node` の `fail` または依存未解決により、上位 `node` の判定を開始できない状態。
- `blocked` は `aggregate_verdict` と `dependency_summary` に必須記録する。
- `blocked` が発生した上位 `node` の workflow 実行結果は `fail` とする。

## 4. 物理的に妥当な一致（Physical Validity）
bitwise 一致は要求しない。以下の性質で一致を判定する。
- 保存則ドリフトが許容内
- 制約（非負性、過大なオーバーシュート）が許容内
- 解析解や参照解に対する誤差が許容内
- `refinement` で誤差が改善
- 将来: 統計・スペクトル・アンサンブル指標

## 5. アルゴリズムの 2 分類（重要）
本プロジェクトでは「アルゴリズム」を 2 種類に分ける。

### A) 物理アルゴリズム（Physics-affecting）
- 物理結果（精度・安定性）に影響する選択。
- 例: 空間離散化（中央 2 次、一次風上、`WENO` 等）、時間積分、フィルタ、拡散、物理過程の近似、境界条件の数値実装。
- **`case.resolved.yaml` で決定し、決定的である必要がある**（同じ `case` なら同じ物理解が期待される）。

### B) 実行アルゴリズム（Execution-only / Performance-affecting）
- 物理結果（理想的には）を変えず、計算過程（性能、メモリ、並列効率）に影響する選択。
- 例: ループ順序、タイル / ブロッキング、配列レイアウト、融合 / 分割、ベクトル化、`GPU` カーネル分割、非同期、数値的に等価な式変形、通信重ね合わせ。
- **`impl.resolved.yaml` で表現し、探索（自動チューニング）の対象にできる**。

注意:
- 実行アルゴリズムでも丸め誤差の差は起こり得る。許容は「物理的妥当性一致」で吸収する。

## 6. 決定性（Determinism）の意味
- 決定性は「物理結果の再現性」を保証するために必要である。
- ただし、物理結果を保証する決定性は主に **物理アルゴリズム（A）** と入力条件の決定に関わる。
- **実行アルゴリズム（B）は必ずしも固定しない**。性能チューニングでは B を意図的に変えて探索する。

## 7. run_id
- 1 回のテスト実行に付与する識別子。
- 推奨: `YYYYMMDD_HHMMSS_<gitsha>_<target>`

## 8. MCP（Model Context Protocol）
- ツール実行を標準化するためのプロトコル。
- 本プロジェクトでは `compile` / `run` / `quality check` を `MCP` サーバー経由で実行する。
- `fortran` / `c` / `cpp` / `mixed` 系の `compile` は、依存関係を扱える標準ビルドツール（既定値 `make`）を介して実行する。

## 9. 自動微分（AD: Automatic Differentiation）
- 離散実装された計算グラフに対して導関数（`JVP` / `VJP` / `gradient`）を機械的に求める手法。
- 本プロジェクトでは将来対応を前提とし、現段階では「`AD` を阻害しない仕様・実装構造」を要求する。
- 非微分演算（例: `clip`、`limiter`、分岐）を含む場合は、仕様上で扱いを明示する。

## 10. `spec` 分類語彙（`spec_kind` / `domain` / `family`）
- **spec_kind**: `spec` の種別。`problem` / `component` / `profile` の 3 値のみを許可する。
- **domain**: 物理モデルの上位分類。`spec` 配置と `component_id` 接頭辞の一貫性を保つための固定語彙。例: `dynamics`, `microphysics`, `radiation`, `land_surface`。
- **family**: `domain` 内の分類単位。`problem` では方程式群、`component` では再利用演算群、`profile` では選択規則群を表す。
- **component**: `component spec` が定義する再利用可能な物理演算単位。方程式系または離散化責務で分割する。例: `advection_flux`, `time_integrator`, `boundary_periodic`。
- **operation**: `component` が公開する呼び出し単位。言語固有の関数・手続き・メソッドなどの実体を抽象化した語彙。
- **適用規則**: `spec` の配置は `spec/<spec_kind>/<domain>/<family>/<spec_id>/...` とする。`component_id` 推奨形式 `<domain>_<family>_<operator>_<dim>d_<scheme>` の先頭 2 要素は `domain` と `family` に一致させる。`operation_id` は `<component_id>__<action>` 形式を用いる。
