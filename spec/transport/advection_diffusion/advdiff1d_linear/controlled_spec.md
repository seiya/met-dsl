# Controlled Spec: 1次元線形移流拡散方程式（物理・アルゴリズム定義）

## 0. メタ情報
- `spec_id`: `advdiff1d_linear`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`

## 1. 問題定義
対象は1次元線形移流拡散方程式
\[
\frac{\partial u}{\partial t} + \frac{\partial (a u)}{\partial x}
= \frac{\partial}{\partial x}\left(\nu \frac{\partial u}{\partial x}\right)
\]
である。方程式は保存形として扱う。未知変数はスカラー場 \(u(x,t)\) のみとし、係数 \(a,\nu\) は定数とする。外力項は本Specでは持たない。

## 2. 変数と座標の定義
座標系は1次元Cartesian座標とし、座標名は `x`、単位は `m` とする。状態変数は `u` で、意味は受動スカラー濃度、配置はセル中心、単位は無次元（`1`）とする。

## 3. 領域・格子・境界条件の型定義
領域は1次元区間 \([0,L)\) とする。格子は一様セル中心格子を使い、格子幅は `dx=L/nx` で定義する。`L` と `nx` は実行時入力（runtime inputs）であり、検証では `physical_tests` が既定プロファイルを与える。

境界条件アルゴリズムは周期境界で固定する。ghost幅は1とし、境界写像は `u[-1]=u[nx-1]`, `u[nx]=u[0]` とする。

## 4. 物理アルゴリズム（A）
空間離散化は、移流項に一次風上差分（`a>0` 前提）、拡散項に二次中心差分を用いる。時間積分は前進オイラー法（明示1段）で固定する。

更新式は次で固定する。
\[
u_i^{n+1}
= u_i^n
- C \left(u_i^n - u_{i-1}^n\right)
+ D \left(u_{i+1}^n - 2u_i^n + u_{i-1}^n\right)
\]
\[
C = a \frac{\Delta t}{\Delta x},\quad
D = \nu \frac{\Delta t}{\Delta x^2}
\]

実装に必要な安定指標は `cfl_combined = C + 2D` とする。安定判定の閾値や運用ルールは検証プロファイルごとに定義する。

## 5. モデルパラメタと実行時入力契約
このSpecで固定する物理定数は `a=1.0 m/s`, `nu=1.0e-2 m2/s` とする。`a<=0` は許可しない。

実行時入力（runtime inputs）は次とする。
- `L`, `nx`（領域長と分割数）
- `initial_condition`（初期値の関数形または離散場）
- `t_start`, `t_end`
- `dt_rule`（`dt` 直接指定または `cfl` 由来規則）
- `output_schedule`（保存時刻）

この入力契約を満たす限り、ユーザーは科学目的に応じて実行時入力を自由に設計できる。`physical_tests` は妥当性検証で使う既定入力プロファイルを提供する。

## 6. 禁止事項
非周期境界を禁止する。limiter/clip/filter の追加を禁止する。実行時の離散化自動切替（空間次数や時間積分法の切替）を禁止する。未定義パラメタの暗黙補完を禁止し、検出時は必ずエラー終了する。

## 7. トレーサビリティ
参照根拠は LeVeque (2002) を一次資料とする。`case.resolved.yaml` には、少なくとも `physics.equation`, `physics.discretization`, `physics.boundary`, `inputs_contract` がこのSpecから一意に復元できる形で写像されなければならない。

実行メタデータの必須項目は `spec_id`, `spec_version`, `case_id`, `case_hash`, `impl_hash`, `git_sha` とする。

妥当性検証で使う既定の入力・判定条件は `spec/transport/advection_diffusion/advdiff1d_linear/physical_tests/advdiff1d_linear_physical_tests.yaml` を参照する。

## 8. AD準備情報
`ad_readiness.enabled=true` とする。状態更新は `u_next=F(u_now, params, forcing)` の形で表現できることを要件とする。演算子は「移流フラックス」「拡散フラックス」「周期境界処理」「時間更新」に分離する。

非滑らか・離散演算として `min`（`dt` 規則で使用される場合）、`ceil`（ステップ数決定で使用される場合）、周期インデックスwrapを明示する。`ceil` は制御フロー整数演算として扱い、勾配対象外とする。識別可能パラメタは `a`（`m/s`）, `nu`（`m2/s`）で、どちらもこのSpecでは固定値である。
