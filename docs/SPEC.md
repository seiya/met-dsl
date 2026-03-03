# 全体仕様: ドキュメント駆動で気象・気候計算向けサブルーチン群と runner を生成する基盤

## 最終ゴール
**ドキュメント的文章（`Controlled Spec` + `tests`）を正本**として、`CPU` / `GPU` などのハードウェア上で **各 `spec` が定義する計算課題を実装するサブルーチン群（`model`）と、入出力・実行・判定連携を担う `runner`** を最適化して生成できる基盤フレームワークを構築し、生成成果物を実用水準で運用する。

## スコープ
### 対象
- 入力: 自然言語中心の `Controlled Spec`（物理・アルゴリズム定義）と `tests`（検証入力・判定プロファイル）
- 出力: 実行コード（`model` + `runner`）、物理診断、性能診断、合否判定（`verdict.json` / `aggregate_verdict.json` / `summary.json`）、依存解決情報（`dependency.resolved.yaml`）
- 運用: `Spec -> Plan -> Generate -> Execute -> Judge -> Tune -> Promote` のループ
- ハードウェア: `CPU` / `GPU` を含む（`Phase 0` は `CPU` 参照、以降 `GPU` へ拡張）
- 実行時入力はユーザーが科学目的に応じて設計でき、`tests` はそのうち検証に使う既定プロファイルを与える。
- `Controlled Spec` と `tests` の正本形式は `Markdown` とし、自然言語で一意に定義できる項目は文章で記述する。
- 出力プログラミング言語に依らず、物理計算を担う `model` と、入出力・判定連携を担う `runner` を分離する。
- `Controlled Spec` に追加必須項目を導入する運用を前提にしない。`Plan` は既存の `controlled_spec.md` と `tests.md` と `deps.yaml` から検証契約を導出する。

### 非対象（現時点）
- bitwise 一致の保証
- 完全自動での科学的妥当性の発見（妥当性の定義は人間が与える）

## 不変原則
1. ドキュメントを正本とする。`Controlled Spec` と `tests` は、ドメイン研究者が単独で解釈でき、かつ判定入力へ決定的に変換できなければならない。
2. 妥当性保証は出口で行う。`LLM` の自由度・非再現性・ハルシネーションを前提として受け入れ、実行結果の妥当性は物理妥当性判定で保証する。bitwise 一致は要求しない。

## 不正防止原則（チート禁止）
1. `tests` 合格または workflow 進行を目的とした `dummy` 出力の生成を禁止する。
2. `diagnostics.json` と `perf.json` は、対象 `runner` の実行結果としてのみ生成してよい。手書き生成、固定値埋め込み、外部での後編集を禁止する。
3. `verdict.json` と `aggregate_verdict.json` は、`tests.md` と実行成果物から導出しなければならない。判定結果の先書き、固定 `pass` 化、根拠なき `xfail` 化を禁止する。
4. ステージ入力が不足している場合、当該ステージは `fail` で停止しなければならない。推測補完、仮定補完、プレースホルダ補完で先へ進めてはならない。
5. ステージ失敗時に、下流ステージ開始条件を満たす目的で成果物ファイルを人工生成してはならない。
6. 本原則への違反は仕様違反とし、当該 `pipeline` は無効とする。
7. 明示的な指定がない場合、既存 workflow 出力（過去 `plan_id` / `pipeline_id` / `generation_id` / `build_id` / `execution_id` の成果物）を参照してはならない。中身の閲覧を禁止し、`spec` 正本と当該実行で生成した前段成果物のみで独立実行しなければならない。
8. `toolchain.language` が `fortran` / `c` / `cpp` / `mixed` 系の場合、`runner` が外部インタプリタ（`python` / `bash` / `sh` / `node`）を起動する構成を禁止する。
9. `Judge` 根拠の再計算に必要な実行証跡（`raw` 成果物）を欠く `diagnostics` / `verdict` を無効とする。
10. workflow 成果物の保存先ルートは `workspace/` のみを許可する。workflow ルート判定は `workspace/` のみを対象とし、`workspace/` 以外のディレクトリを判定対象に含めない。workflow 実行開始前に `workspace/` が存在しない場合は、リポジトリルート直下へ `workspace/` を作成する。
11. 依存 `node` の公開 `operation` を呼び出さず、同等機能を依存元 `node` に再実装して `tests` 合格を狙う実装を禁止する。
12. `toolchain.language=fortran` の場合、`module` 名とソースファイル名を一致させない実装と、汎用ファイル名 `model.f90` への集約を禁止する。
13. `Plan` / `Generate` / `Judge` で必要な検証契約は、`controlled_spec.md` と `tests.md` と `deps.yaml` から導出しなければならない。新しい入力項目の記述をユーザーへ要求してはならない。
14. 検証契約の導出で必須情報が不足する場合は当該ステージを `fail` で停止しなければならない。推測補完で進行してはならない。
15. `raw/metrics_basis.json` へ `diagnostics.json` を複写してはならない。`raw` は `diagnostics` 再計算に必要な一次証跡のみを保持しなければならない。
16. `quality check` は `diagnostics.json` と `verdict.json` の比較を正本とし、`stdout` 差分のみで合否を確定してはならない。
17. `Judge` 開始前と `Judge` 完了前に `python3 tools/validate_pipeline_semantics.py` を実行し、`fail` 時は当該 `pipeline` を `invalid` としなければならない。

## 運用原則（Spec-First）
1. 物理仕様の変更は**必ず `Controlled Spec` を更新**してから実装へ反映する。
2. 実験条件・判定条件の変更は**`tests` を更新**してテストへ反映する。
3. 実装のみを直接修正して物理仕様を変えることを禁止する（乖離防止）。
4. 判定不能・曖昧な仕様は「仮実装で進める」のではなく、`Spec` へ差し戻して解消する。

## LLM の扱い
- `LLM` はモデル種類を問わない（交換可能）。
- 最終的な品質保証は `Execute` 後の実行診断で行う。判定の正本は `diagnostics.json` / `verdict.json` / `perf.json` とする。
- `LLM` を使う各ステージは、ステージ内部で `generate -> verify -> regenerate` を反復し、検証合格後のみ成果物を確定する。
- ステージ内部の `verify` は、当該ステージの入力契約に対する出力整合性を検査する狭いスコープの判定とする。
- ステージ内部の `verify` は主に構造・契約・トレーサビリティの整合確認を目的とし、物理妥当性の最終保証を代替しない。
- ステージ内部の `verify` 合格は必要条件であり、十分条件ではない。
- `verifier` は `generator` と独立したコンテキスト（別セッションまたは別エージェント）での実行を可能な限り優先する。
- 実行環境の制約で独立コンテキストを確保できない場合は、同一コンテキスト実行を許容し、制約理由をメタデータへ記録する。
- 失敗試行の中間成果物は標準運用で永続保存せず、最終合格成果物のみを保存する。
- `LLM` 利用ステージは、ステージメタデータの出力を必須とする。正本は各ステージの `<stage>_meta.json` とし、コード生成ステージでは `generate_meta.json` とする。
- `LLM` 利用ステージのメタデータ必須項目は、`attempt_count`、`verification_status`、`last_fail_reason`、`context_isolated`、`debug_mode` とする。`context_isolated=false` の場合は `constraint_reason` を必須とする。
- `debug_mode` の既定値は `false` とする。`debug_mode=false` では失敗試行の中間成果物を保存しない。
- `debug_mode=true` の場合のみ失敗試行の成果物保存を許可する。保存時は各ステージのメタデータに `retained_failed_attempts`（保存件数）と保存先を記録する。

## アーキテクチャ方針（現時点）
- **物理アルゴリズム（A）**: 物理結果に影響する選択。`case.resolved.yaml` で決定し、決定的である必要がある。
- **実行アルゴリズム（B）**: 計算過程や性能に影響する選択。`impl.resolved.yaml` で表現し、探索可能とする。
- この分離により「物理再現性」と「性能探索」を両立する。

## 大規模 `spec` 運用設計
### 目的
- 問題別 `spec` を維持しつつ、再利用可能な物理演算を `component` 単位で正本化する。
- 粒度の過分割を防ぎ、交換可能な単位だけを `spec` として管理する。
- 生成対象が増加しても、`spec` の識別子・依存関係・生成物の追跡可能性を維持する。

### 適用範囲
- `spec` の階層構造（`problem` / `component` / `profile`）
- `spec`・`component`・`operation` の命名規則
- `spec` 間依存と再利用規則
- 既存 `component` / `operation` の登録台帳

### 要件
1. `spec` は `spec_kind` を必須とし、値は `problem` / `component` / `profile` のいずれかのみを許可する。
2. `spec` 配置は階層構造を必須とする。最小構成を次に固定する。

```text
spec/
  registry/
    spec_catalog.yaml
  problem/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
  component/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
  profile/
    <domain>/
      <family>/
        <spec_id>/
          controlled_spec.md
          tests.md
          deps.yaml
releases/
  registry/
    component_catalog.yaml
  <spec_kind>/
    <domain>/
      <family>/
        <spec_id>/
          <target_architecture>/
            <toolchain_language>/
              <release_id>/
```

3. `domain` と `family` の定義は `GLOSSARY.md` の「`spec` 分類語彙」を参照する。
4. 正式版成果物は `spec` 配下に配置してはならない。保存先は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` を必須とする。
5. `spec_id` はリポジトリ内で一意とし、形式は `^[a-z][a-z0-9_]{2,63}$` を必須とする。
6. `tests.md` は各 `spec` に 1 ファイルのみ配置を許可する。配置先は `spec/<spec_kind>/<domain>/<family>/<spec_id>/tests.md` とする。
7. `component_id` は `^[a-z][a-z0-9_]{2,63}$` を必須とし、推奨形式を `<domain>_<family>_<operator>_<dim>d_<scheme>` とする。
8. `operation_id` は `component_id` を接頭辞に持つ形式（`<component_id>__<action>`）を必須とする。
9. 生成コードの公開名は互換性管理を必須とする。`major` 互換が破壊される変更は別名に分離する。
10. すべての `spec` は `deps.yaml` で依存を宣言し、直接パス参照（相対 `import`）を禁止する。
11. `problem spec` は依存 `component` と採用 `profile` を宣言しなければならない。解決結果は `case.resolved.yaml` に固定し、実行時の再解決を禁止する。
12. 依存解決は `component_id` + `version constraint` と `profile_id` + `version constraint` で行う。未登録依存、未実装依存、互換性違反依存をエラーとする。
13. `releases/registry/component_catalog.yaml` は `component` 単位の責務・公開 `operation`・互換性情報・実装状態を保持する。
14. 未実装 `component` / `operation` を参照する `problem spec` は生成工程へ進めず、依存解決エラーとする。
15. 各 `tests.md` は `L0` テストを少なくとも 1 件定義しなければならない。`L1` 以上の要否は `spec_kind` ではなく検証目的で決定する。
16. 粒度判定は次を必須基準とする。
- 差し替え可能性: 独立に差し替える意思決定がある境界のみ `component spec` 化する。
- 契約独立性: 入出力契約・前提条件・失敗条件を単独記述できる単位のみ分割する。
- 検証独立性: `tests.md` に独立した合否条件を定義できる単位のみ分割する。
- 内部限定関数: 外部公開しない内部 helper 群は独立 `spec` 化を禁止する。
17. `Plan` は `dependency.resolved.yaml` を必須出力とし、`node_key`、`direct_deps`、`transitive_deps`、`topo_level` を必須記録する。
18. 実行順序は `dependency.resolved.yaml` の `topo_level` 昇順に固定する。親 `node` は直下依存 `node` がすべて `pass` または `xfail` になるまで workflow を開始してはならない。
19. 判定成果物は `verdict.json`（当該 `node` の `self_verdict`）と `aggregate_verdict.json`（依存を含む集約判定）を必須とする。
20. `summary.json` は `self_summary` と `dependency_summary` を必須保持し、`dependency_summary` には `total`、`pass`、`xfail`、`fail`、`blocked` を必須記録する。
21. `problem` の `Promote` は `self_verdict=pass` に加え、推移依存を含む `aggregate_verdict.overall=pass` を必須条件とする。
22. `dependency.resolved.yaml` の各 `node_key` について、個別 workflow を完了させる。直下依存が充足する `node` は `Plan -> Generate -> Build -> Execute -> Judge` を完了し、直下依存が不充足の `node` は `blocked` 終端成果物を生成して完了とする。
23. `node` 単位 workflow は、個別の `plan_id` と個別の `pipeline_id` を必須発行する。
24. 同一 `topo_level` の独立 `node` は並列実行してよい。
25. 直下依存 `node` の `aggregate_verdict` に `fail` または `blocked` がある場合、上位 `node` は `self_verdict` を実施せず `aggregate_verdict=blocked` で終了し、workflow 実行結果を `fail` とする。
26. `Generate` は `model` / `runner` の実装要件を満たせない場合、`verification_status=fail` とし、`Generate` を停止しなければならない。`dummy` 実装での代替を禁止する。
27. `Build` または `Execute` が失敗した場合、`diagnostics.json` / `perf.json` の代替生成を禁止し、当該 `node` を `fail` としなければならない。
28. `Judge` は必須診断項目が不足した場合、`N/A` 許容規則で明示された項目を除き `fail` としなければならない。欠落項目を推定値で補完してはならない。
29. `trial_meta.json` は、各成果物の生成手段を追跡可能に記録しなければならない。少なくとも `generated_by_stage` と `source_execution_id` を必須とする。
30. workflow の進行可否は、前段成果物の実在と契約適合で判定しなければならない。手動での進行フラグ改変を禁止する。
31. `dependency.resolved.yaml` は、起点 `node` と推移依存 `node` の閉包を過不足なく 1 回ずつ保持しなければならない。`node_key` の重複と欠落を禁止する。
32. 依存解決検証は、`deps.yaml` と `spec_catalog.yaml` から再構成した `expected_node_set` と `dependency.resolved.yaml` の `node_key` 集合一致を `pass` 条件とする。
33. workflow 完了条件は、`dependency.resolved.yaml` の全 `node_key` に対して `workspace/plans/<node_key_safe>/<plan_id>/` と `workspace/pipelines/<node_key_safe>/<pipeline_id>/` が存在し、`lineage.json` の `node_key` と `dependency_ref` が一致することとする。
34. `blocked` で終了する `node` でも、`aggregate_verdict.json` と `summary.json` と `trial_meta.json` を必須出力とする。`blocked_reason` と `blocking_direct_deps` を必須記録し、`self_verdict` は `not_evaluated` を明示する。
35. 依存 `DAG` を含む workflow 実行中は、対象依存 `DAG` の `workspace/plans` と `workspace/pipelines` 配下成果物を削除してはならない。途中削除による証跡欠落を禁止する。
36. 同一 `topo_level` の並列実行では、独立 `node` の処理を相互中断してはならない。ある `node` が `fail` しても同一 `topo_level` の独立 `node` を完了させ、`topo_level` 完了後に次レベルの開始可否を判定しなければならない。
37. `spec_kind` を問わない workflow 実行は、各ステージ（`Plan` / `Generate` / `Build` / `Execute` / `Judge`）を `LLM` により実行しなければならない。専用実行スクリプトを実行前提にしてはならない。手動 `copy`、手動 `json` 生成、手動 `id` 差し替えでステージ成果物を生成してはならない。
38. `spec_kind` を問わない workflow 実行では、`dependency.resolved.yaml` の全 `node_key` に対して個別 `lineage.json` を必須生成し、各 `lineage.json` に単一 `node_key` を記録しなければならない。単一 `pipeline` に複数 `node_key` の workflow を集約してはならない。
39. `Judge` 開始条件は、対象 `node_key` の `execution_id` 配下に `run_program` の実行記録と `diagnostics.json` と `perf.json` が存在し、同一 `execution_id` の成果物として追跡可能であることとする。未達の場合は `Judge fail` とする。
40. `trial_meta.json` は、`generated_by_stage`、`source_execution_id`、`source_command_ref`、`source_artifact_hash` を必須記録しなければならない。値が欠落または不整合の場合は workflow を `fail` とする。
41. `Generate verify` は、`model` に数値状態更新を伴う物理計算本体が存在することを必須検証とする。固定文字列出力のみ、固定 `JSON` 出力のみ、定数テーブル写像のみの実装を `verification_status=fail` としなければならない。
42. `Judge` は、`verdict.json` と `aggregate_verdict.json` の生成根拠として同一 `execution_id` の実行成果物参照を必須とする。異なる `execution_id` または外部履歴成果物の流用を禁止する。
43. 同一入力で発行された異なる `pipeline_id` 間で、`id` 系メタデータのみが差分で診断・判定本文が完全一致する場合、`copy_based_artifact_reuse` として検出し、当該 `pipeline` を `invalid` としなければならない。
44. `dependency` を含む workflow の完了宣言は、`dependency workflow` 網羅チェックと `trial_meta` 完整性チェックと `copy_based_artifact_reuse` 非検出を同時に満たす場合のみ許可する。
45. `Generate verify` は、`runner` からの外部インタプリタ起動と、物理更新・判定対象演算を持たない `model` 実装を検出した場合、`verification_status=fail` としなければならない。
46. `Execute` は、`Judge` 再計算に必要な実行証跡を `execution_id/<node_key>/raw/` に保存しなければならない。欠落時は `Execute fail` とする。
47. `Judge` は、`raw` 実行証跡から判定指標を再計算し、`diagnostics.json` との整合を検証しなければならない。再計算不能または不整合時は `Judge fail` とする。
48. 異なる `node_key` で生成された `src` のコードハッシュが一致した場合、共通ライブラリとして明示されたファイルを除き `copy_based_artifact_reuse` として `invalid` にしなければならない。
49. `spec_kind` を問わない workflow は、リポジトリ管理下の `spec` 正本と当該試行で生成した成果物のみを入力として、`LLM` により実行しなければならない。`/tmp` など管理外パスの補助スクリプト実行を禁止する。
50. `Plan` / `Generate` / `Build` / `Execute` / `Judge` の成果物保存先は、リポジトリ相対 `workspace/` 起点のパスを必須とする。`workspace/` 以外のルートを検出した場合は `pipeline invalid` とする。
51. `lineage.json` と `trial_meta.json` の成果物参照パスは `workspace/` 起点で記録しなければならない。絶対パスまたは `workspace/` 外のパス記録を禁止する。
52. 依存を持つ `node` の `Generate` は、`dependency.resolved.yaml` の `direct_deps` で解決された依存 `node` の公開 `operation` 呼び出しを必須とする。
53. 依存 `operation` と同等機能を依存元 `node` の `model` / `runner` に再実装してはならない。検出時は `Generate verify fail` とする。
54. 依存先が `profile` で公開 `operation` を持たない場合、依存元 `problem` は `profile` の選択結果と拘束条件を参照する実装痕跡を必須記録とする。欠落時は `Generate verify fail` とする。
55. `toolchain.language=fortran` の `module` 名と公開 `subroutine` 名は、`spec_id` 由来接頭辞を含む一意名を必須とする。
56. `toolchain.language=fortran` のソースファイル名は、定義 `module` 名と一致する `<module_name>.f90` を必須とする。
57. `Build` は、依存を持つ `node` の依存 `operation` 解決先が `dependency.resolved.yaml` と一致することを必須検証とし、不一致時は `Build fail` とする。
58. `Plan verify` は、`controlled_spec.md` と `tests.md` と `deps.yaml` から導出した検証契約を `plan` 成果物へ保存しなければならない。推奨ファイル名は `derived_contract.json` とする。
59. `Generate verify` は、対象 `node` の検証契約で要求された依存 `operation` と出力指標のデータ依存を検証しなければならない。制御構造の形式（時空間ループの有無など）を固定要件にしてはならない。
60. `Generate verify` は、`model` 出力と無関係な定数出力、固定 `JSON` 出力、解析式の直接代入による `diagnostics` 生成を検出した場合に `fail` としなければならない。
61. `Execute` は、`Judge` が独立再計算に使用する一次証跡を `raw/` 配下へ保存しなければならない。一次証跡には状態スナップショット、ケース別指標計算に必要な中間データ、実行トレースを含める。
62. `Judge` は、`raw` 一次証跡から独立経路で判定指標を再計算しなければならない。`diagnostics.json` を再計算入力へ流用してはならない。
63. `Judge` は、再計算値と `diagnostics` が一致しない場合、または `raw` 一次証跡のみで再計算できない場合に `fail` としなければならない。
64. `problem` `node` の `raw/state_snapshots/` は、`snapshot_schema.json` で `Judge` 再計算に使用する状態量名配列（`state_variables`）と時刻情報キー（`time_variable`）を宣言しなければならない。少なくとも 1 つ以上の状態ファイルへ当該項目を保持し、ダミー文字列のみの状態ファイルを禁止する。
65. `quality_check.json` は `checks.verdict_available=true` と `checks.diagnostics_match=true` と `checks.verdict_match=true` を同時に満たさなければならない。いずれかが `false` または欠落の場合は `Execute fail` とする。
66. `Generate verify` は、`model` が `case_id` 分岐と固定数値代入のみで判定指標を構成する実装を検出した場合、`verification_status=fail` としなければならない。

### 設計方針
- `problem spec` は統合シナリオを定義する。目的は、複数 `component` を組み合わせた整合性と回帰判定である。
- `component spec` は再利用可能な物理演算の契約を定義する。目的は、交換可能性と `API` 安定性の担保である。
- `profile spec` は `component` 選択規則とパラメタ拘束を定義する。目的は、スキーム選択の差分管理である。
- `spec` 横断で共有する演算（境界処理、フラックス、時間積分、診断）は `component` として独立管理する。
- `component` の `API` 変更は `major` を更新し、旧 `major` を同時運用可能にする。

### 運用ルール
- 新規 `spec` 追加時は `spec/registry/spec_catalog.yaml` への登録を必須とする。
- `spec_catalog.yaml` の各エントリは `spec_kind`、`domain`、`family`、`spec_id`、`spec_version`、`status`、`controlled_spec_path`、`tests_path` を必須とする。
- `problem spec` で再利用境界が変わる場合は `releases/registry/component_catalog.yaml` を同時更新する。
- `component` の実装状態が `spec_defined_not_implemented` の間は、依存先 `problem spec` を `status=draft` 扱いに固定する。
- `workspace` は試行成果物の作業領域とし、正式版成果物の正本に使ってはならない。
- 明示的な指定がない場合、`workspace` の既存 workflow 出力を入力参照または内容確認に使用してはならない。workflow は毎回独立に実行し、必要成果物を当該実行で生成する。
- `dependency.resolved.yaml` は `plan_id` の正本に含め、`case.resolved.yaml` / `impl.resolved.yaml` と同時に `immutable` 管理する。
- `plans` と `pipelines` は `node` 単位で分離し、`workspace/plans/<node_key_safe>/<plan_id>/` と `workspace/pipelines/<node_key_safe>/<pipeline_id>/` を正本とする。
- `spec_kind` を問わない workflow 実行では、依存 `DAG` を展開した全 `node` の workflow 完了を必須とする。
- `spec_kind` を問わない workflow 実行は、各ステージを `LLM` により実行する。専用実行スクリプトの実行を前提にしてはならない。手動ファイル操作での workflow 代替を禁止する。
- ユーザーからプログラミング言語の明示指定がない場合、`target.class=cpu` では `fortran`、`target.class=gpu` では `cuda_fortran` を必ず採用し、`impl.resolved.yaml` へ補完後の値を明示記録する。
- `toolchain.language` の既定値からの逸脱は、ユーザーがプログラミング言語を明示指定した場合にのみ許可する。
- 採用する実装は `releases/<spec_kind>/<domain>/<family>/<spec_id>/<target_architecture>/<toolchain_language>/<release_id>/` へ昇格保存する。
- 昇格時は `spec_catalog.yaml` の対象 `spec_id` に `official_releases` を追加し、`release_id` / `target_architecture` / `toolchain_language` / `target_backend` / `source_pipeline_id` / `source_generation_id` / `source_build_id` / `source_execution_id` / `artifact_root` / `promoted_at` / `status` を必須記録する。
- `official_releases` の `status=active` は各 `spec_id` の `target_architecture + toolchain_language` ごとに 1 件のみ許可し、切替は新規 `release` 追加と旧 `release` の `deprecated` 化で管理する。
- 互換移行期間では、旧配置 `spec/<domain>/<family>/<spec_id>/...` を `problem spec` とみなし、`spec_kind=problem` として台帳に登録する。
- 互換移行期間中も、新規追加 `spec` は新配置 `spec/<spec_kind>/<domain>/<family>/<spec_id>/...` を必須とする。

### 判定基準
- `spec` の `CI` は `spec_kind` / `spec_id` / `component_id` / `operation_id` の形式検証を `pass` しなければならない。
- `CI` は各 `spec` の `tests.md` 存在検証と `L0` テスト存在検証を `pass` しなければならない。
- `CI` は `tests.md` の `spec_ref` と `controlled_spec.md` の整合性検証を `pass` しなければならない。
- `problem spec` の `CI` は `component` 依存と `profile` 依存の未登録・未実装・互換性違反を `fail` としなければならない。
- `CI` は `dependency.resolved.yaml` の `DAG` が非循環であることを `pass` しなければならない。
- `CI` は `aggregate_verdict.json` の依存集合と `dependency.resolved.yaml` の推移依存集合が一致することを `pass` しなければならない。
- 生成実行ログ（`trial_meta.json`）には `spec_kind`、`spec_id`、`spec_version`、`component_id@api_version`、`profile_id@version` の解決結果を必須記録とする。
- `component_catalog.yaml` の公開 `operation` と生成コードの公開シンボルは 1 対 1 で対応しなければならない。
- `CI` は `trial_meta.json` の `generated_by_stage`、`source_execution_id`、`source_command_ref`、`source_artifact_hash` の存在と整合性を `pass` しなければならない。
- `CI` は `spec_kind` を問わない workflow 実行で `dependency.resolved.yaml` の全 `node_key` に対する個別 `lineage.json` の存在を `pass` しなければならない。
- `CI` は `Generate verify` で固定値出力のみの `model` 実装を `fail` しなければならない。
- `CI` は `Generate verify` で `runner` の外部インタプリタ起動を検出した場合に `fail` しなければならない。
- `CI` は `execution_id/<node_key>/raw/` の必須実行証跡が欠落した場合に `fail` しなければならない。
- `CI` は `Judge` 再計算値と `diagnostics` の不整合を検出した場合に `fail` しなければならない。
- `CI` は `derived_contract.json` の欠落、または `controlled_spec.md` と `tests.md` と `deps.yaml` からの再導出結果との不整合を検出した場合に `fail` しなければならない。
- `CI` は `raw/metrics_basis.json` と `diagnostics.json` の内容完全一致を検出した場合に `fail` しなければならない。
- `CI` は `quality check` の比較対象が `diagnostics.json` と `verdict.json` でない場合に `fail` しなければならない。
- `CI` は、依存を持つ `node` が `dependency.resolved.yaml` で解決された依存 `operation` を呼び出していることを `pass` 条件にしなければならない。
- `CI` は、依存 `operation` と同等機能の再実装を検出した場合に `fail` しなければならない。
- `CI` は、`toolchain.language=fortran` の成果物で `module` 名とソースファイル名の不一致を検出した場合に `fail` しなければならない。
- `CI` は、`toolchain.language=fortran` の成果物で公開シンボル名に `spec_id` 由来接頭辞がない場合に `fail` しなければならない。
- `CI` は workflow 成果物の保存先ルートが `workspace/` 以外の場合に `fail` しなければならない。
- `CI` は `python3 tools/validate_workspace_root.py` を実行し、終了コードが 0 でない場合に `fail` しなければならない。
- `CI` は `python3 tools/validate_pipeline_semantics.py` を実行し、終了コードが 0 でない場合に `fail` しなければならない。
- `CI` は `lineage.json` と `trial_meta.json` の成果物参照パスが `workspace/` 起点でない場合に `fail` しなければならない。
- `CI` は `copy_based_artifact_reuse` 検出時に当該 `pipeline` を `invalid` として `fail` しなければならない。

## 成功条件（最小）
- `Controlled Spec` から、各 `spec` が定義する計算課題を実装するサブルーチン群（`model`）と `runner` の変換が再現可能である。
- `runner` が `model` を呼び出す構成が維持され、物理更新ロジックが二重実装されない。
- 物理妥当性判定により合否が決定的に再現される。
- 依存を含む集約判定（`aggregate_verdict`）と依存集約サマリー（`dependency_summary`）が決定的に再現される。
- 依存 `DAG` の下層 `node` 失敗が上位 `node` の `blocked` と workflow `fail` に決定的に伝播する。
- `impl`（B）の探索で、物理合格を維持しつつ性能が改善できる。

## 参照
- `CONTROLLED_SPEC.md`
- `TESTS.md`
- `PHYSICAL_VALIDATION.md`
- `GLOSSARY.md`
- `WORKFLOW.md`
