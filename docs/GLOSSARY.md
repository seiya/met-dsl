# 用語集・記号・レベル定義

この文書は、他のドキュメントが参照する用語を 1 か所に集約し、単独で読んでも意味が通るようにする。

## 1. 成果物（Artifacts）
- **controlled_spec.md**: 物理・数値アルゴリズム定義の正本。生成器が model（実装本体）を作るために参照する。
- **physical_tests.yaml**: 妥当性検証用プロファイル（入力インスタンス、ケース展開、判定条件）の正本。テストランナーが参照する。
- **spec_catalog.yaml**: `spec` の台帳。`spec_id`、配置先、状態、移行要否を保持する。
- **component_catalog.yaml**: 再利用 `component` / `operation` の台帳。責務、公開 API、互換性、実装状態を保持する。
- **deps.yaml**: 各 `spec` が要求する `component` 依存宣言。`component_id` と `version constraint` を定義する。
- **case.yaml**: 人間が書く（または将来 Spec から生成する）テストケース定義。 sweep/refinement などを含み得る。
- **case.resolved.yaml**: テストランナーが生成する“決定済み”入力。 sweep 展開、物理アルゴリズム（後述）と数値条件を決定したもの。 runner（例: `simulate`）がこれを読む。
- **impl.resolved.yaml**: 実装計画（Implementation Plan）。計算過程（並列化、メモリ配置、融合、ブロッキング等）に関する“可変”パラメタを決定したもの。性能チューニングの探索対象になり得る。
- **plan_id**: `case.resolved.yaml` と `impl.resolved.yaml` の組を識別する ID。推奨形式は `<spec_id>_<case_hash12>_<impl_hash12>`。
- **pipeline_id**: Generate→Build→Execute の 1 系列を識別する ID。推奨形式は `<plan_id>_<utc_ts>_<seq3>`。
- **generation_id / build_id / execution_id**: 各段階の試行を識別する ID。
- **lineage.json**: `spec_ref`、`plan_ref`、`pipeline_id`、各段階 ID の関係を記録する来歴ファイル。
- **model**: 物理計算を実行する計算コンポーネント/ライブラリ。入力状態から次状態を計算する責務を持つ。
- **runner（例: `simulate`）**: 実行エントリポイント。入力読込・ model 呼び出し・ diagnostics/perf 出力を担当する。
- **diagnostics.json**: runner が出す物理・数値診断（保存量、誤差、CFL など）。合否は含めない。
- **perf.json**: runner が出す性能診断（最低限 `walltime_sec`、`throughput_cells_per_sec`、`parallelism`）。合否は含めない。
- **verdict.json**: テストランナーが出す合否判定と根拠。
- **summary.json**: run 全体の集計（pass/fail/skipped、失敗分類別件数など）。
- **stdout.log / stderr.log**: 実行ログ（必ず保存し、後追いデバッグ可能にする）。

補足:
- `perf.json` は `diagnostics.json` とは分離して出力する（同居しない）。

## 2. テストレベル（L0-L3）の意味
L0-L3 は「テストの粒度と目的」を表す分類であり、実装の層番号ではない。

- **L0: 部品テスト（Unit / Operator / Guard）**
- **L1: 解析解・収束傾向テスト（Analytic / MMS / Refinement）**
- **L2: 保存則・制約テスト（Invariants / Constraints）**
- **L3: ロバスト性・同値性テスト（Robustness / Equivalence）**
- 同値性に「性能回帰（performance regression）」も含める（物理合格の上で性能を比較する）。

## 3. 期待失敗（Guard / XFAIL）
- 正しく実装されていれば“失敗する”べきテスト。
- 期待失敗条件を満たした場合は PASS と判定する。

## 4. 物理的に妥当な一致（Physical Validity）
bitwise 一致は要求しない。以下の性質で一致を判定する。
- 保存則ドリフトが許容内
- 制約（非負性、過大なオーバーシュート）が許容内
- 解析解や参照解に対する誤差が許容内
- refinement で誤差が改善
- 将来: 統計・スペクトル・アンサンブル指標

## 5. アルゴリズムの 2 分類（重要）
本プロジェクトでは「アルゴリズム」を 2 種類に分ける。

### A) 物理アルゴリズム（Physics-affecting）
- 物理結果（精度・安定性）に影響する選択。
- 例: 空間離散化（中央 2 次、一次風上、WENO 等）、時間積分、フィルタ、拡散、物理過程の近似、境界条件の数値実装。
- **case.resolved.yaml で決定し、決定的である必要がある**（同じ case なら同じ物理解が期待される）。

### B) 実行アルゴリズム（Execution-only / Performance-affecting）
- 物理結果（理想的には）を変えず、計算過程（性能、メモリ、並列効率）に影響する選択。
- 例: ループ順序、タイル/ブロッキング、配列レイアウト、融合/分割、ベクトル化、GPU カーネル分割、非同期、数値的に等価な式変形、通信重ね合わせ。
- **impl.resolved.yaml で表現し、探索（自動チューニング）の対象にできる**。

注意:
- 実行アルゴリズムでも丸め誤差の差は起こり得る。許容は「物理的妥当性一致」で吸収する。

## 6. 決定性（Determinism）の意味
- 決定性は「物理結果の再現性」を保証するために必要。
- ただし、物理結果を保証する決定性は主に **物理アルゴリズム（A）** と入力条件の決定に関わる。
- **実行アルゴリズム（B）は必ずしも固定しない**。性能チューニングでは B を意図的に変えて探索する。

## 7. run_id
- 1 回のテスト実行に付与する識別子。
- 推奨: `YYYYMMDD_HHMMSS_<gitsha>_<target>`

## 8. 自動微分（AD: Automatic Differentiation）
- 離散実装された計算グラフに対して導関数（JVP/VJP,gradient）を機械的に求める手法。
- 本プロジェクトでは将来対応を前提とし、現段階では「AD を阻害しない仕様・実装構造」を要求する。
- 非微分演算（例: clip,limiter, 分岐）を含む場合は、仕様上で扱いを明示する。

## 9. `spec` 分類語彙（`domain` / `component`）
- **domain**: 物理モデルの上位分類。`spec` 配置と `component_id` 接頭辞の一貫性を保つための固定語彙である。例: `transport`, `dynamics`, `microphysics`, `radiation`, `land_surface`。
- **component**: `domain` 内の機能単位。方程式系または離散化責務で分割する。例: `advection_diffusion`, `compressible_core`, `bulk_cloud`。
- **operation**: `component` が公開する呼び出し単位。言語固有の関数・手続き・メソッドなどの実体を抽象化した語彙である。
- **適用規則**: `spec` の配置は `spec/<domain>/<component>/<spec_id>/...` とし、`component_id` 推奨形式 `<domain>_<component>_<operator>_<dim>d_<scheme>` の先頭 2 要素と一致させる。`operation_id` は `<component_id>__<action>` 形式を用いる。
