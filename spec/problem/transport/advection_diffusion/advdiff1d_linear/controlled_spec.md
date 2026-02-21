# Controlled Spec: 1 次元 線形 移流拡散 問題（problem spec）

## 0. メタ情報
- `spec_id`: `advdiff1d_linear`
- `spec_version`: `0.3.0`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `transport`
- `family`: `advection_diffusion`

## 1. 問題定義
対象は 1 次元 線形 移流拡散 方程式
$$
\frac{\partial u}{\partial t} + \frac{\partial (a u)}{\partial x}
= \frac{\partial}{\partial x}\left(\nu \frac{\partial u}{\partial x}\right)
$$
である。方程式は 保存形 として扱う。未知変数は スカラー場 $u(x,t)$ とする。外力項は扱わない。

## 2. 変数と座標の定義
座標系は 1 次元 Cartesian 座標とし、座標名は `x`、単位は `m` とする。状態変数は `u` とし、意味は 受動スカラー 濃度、配置は セル中心、単位は 無次元 `1` とする。

## 3. 領域と境界条件の型定義
領域は 区間 $[0,L)$ とする。格子は 一様 セル中心 格子とし、格子幅は $dx=L/nx$ で定義する。`L` と `nx` は 実行時入力 とする。

境界条件は 周期境界 に固定する。検証に用いる 既定入力 は `tests.md` に定義し、利用者入力の全体を固定しない。

## 4. 依存 `component` と採用 `profile`
本 `problem spec` は次の `component` を参照する。
- `transport_advection_diffusion_flux_1d_upwind_center2`
- `transport_advection_diffusion_boundary_1d_periodic_copy`
- `transport_advection_diffusion_time_update_1d_euler1`

採用 `profile` は `transport_advection_diffusion_profile_1d_upwind_center2_euler1` とする。

## 5. 統合アルゴリズム
更新ステップは次の順序に固定する。
1. `transport_advection_diffusion_boundary_1d_periodic_copy__apply` で ghost 領域を更新する。
2. `transport_advection_diffusion_flux_1d_upwind_center2__compute_flux` で移流・拡散 フラックスを計算する。
3. `transport_advection_diffusion_time_update_1d_euler1__advance` で前進 Euler 更新を実行する。

安定指標は
$$
\text{cfl_combined}=C+2D,\quad C=a\frac{\Delta t}{\Delta x},\quad D=\nu\frac{\Delta t}{\Delta x^2}
$$
と定義する。閾値は `tests.md` の判定条件を参照する。

## 6. モデルパラメタと実行時入力契約
物理定数は `a=1.0 m/s`、`nu=1.0e-2 m2/s` とする。

実行時入力は次を必須とする。
- `L`, `nx`
- `initial_condition`
- `t_start`, `t_end`
- `dt_rule`
- `output_schedule`

`a<=0` は許可しない。未定義パラメタは 暗黙補完せず エラーとする。

## 7. 禁止事項
非周期境界を禁止する。`limiter` / `clip` / `filter` の追加を禁止する。離散化スキームの実行時 自動切替を禁止する。

## 8. トレーサビリティ
`case.resolved.yaml` には `spec_kind`, `spec_id`, `spec_version`, `component_id@version`, `profile_id@version` の解決結果を必須記録とする。

参照根拠は LeVeque（2002）とする。検証プロファイルは `spec/problem/transport/advection_diffusion/advdiff1d_linear/tests.md` を参照する。

## 9. AD 準備情報
`ad_readiness.enabled` は `true` とする。状態更新は $u_{next}=F(u_{now}, params)$ の形で表現し、微分対象外演算として `ceil` と 周期インデックス wrap を明示する。
