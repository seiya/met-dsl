# Controlled Spec: 2 次元 shallow water 問題（problem spec）

## 0. メタ情報
- `spec_id`: `shallow_water2d`
- `spec_version`: `0.3.0`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. 問題定義
対象は 2 次元 shallow water equation の保存形
$$
\frac{\partial h}{\partial t}
+ \frac{\partial (hu)}{\partial x}
+ \frac{\partial (hv)}{\partial y}
= 0
$$
$$
\frac{\partial (hu)}{\partial t}
+ \frac{\partial }{\partial x}\left(hu^2 + \frac{1}{2} g h^2\right)
+ \frac{\partial (huv)}{\partial y}
= -gh\frac{\partial z_b}{\partial x}
$$
$$
\frac{\partial (hv)}{\partial t}
+ \frac{\partial (huv)}{\partial x}
+ \frac{\partial }{\partial y}\left(hv^2 + \frac{1}{2} g h^2\right)
= -gh\frac{\partial z_b}{\partial y}
$$
である。外力項は底面地形勾配による源項のみを扱う。

## 2. 変数と座標の定義
座標系は 2 次元 Cartesian 座標とし、座標名は `x`,`y`、単位は `m` とする。
- `h`: 水深、セル中心配置、単位 `m`
- `hu`: `x` 方向 運動量、セル中心配置、単位 `m2/s`
- `hv`: `y` 方向 運動量、セル中心配置、単位 `m2/s`
- `z_b`: 底面地形、セル中心配置、単位 `m`、時間不変
- `eta`: 自由水面高度、`eta=h+z_b`、単位 `m`

導出変数は `u=hu/h`、`v=hv/h`、`c=sqrt(g*h)` とする。`h<=0` は入力不正とする。

## 3. 領域と境界条件の型定義
領域は $[0,L_x)\times[0,L_y)$ の直交周期領域とする。格子は 一様 セル中心 有限体積 格子とし、$dx=L_x/nx$、$dy=L_y/ny$ とする。

境界条件は全境界で 周期境界 に固定する。検証に用いる 既定入力 は `tests.md` に定義する。

## 4. 依存 `component` と採用 `profile`
本 `problem spec` は次の `component` を参照する。
- `dynamics_shallow_water_flux_2d_rusanov_p0`
- `dynamics_shallow_water_boundary_2d_periodic_copy`
- `dynamics_shallow_water_time_update_2d_ssprk2`

採用 `profile` は `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2` とする。

## 5. 統合アルゴリズム
更新ステップは次の順序に固定する。
1. `dynamics_shallow_water_boundary_2d_periodic_copy__apply` で ghost 領域を更新する。
2. `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux` で界面フラックスを計算する。
3. `dynamics_shallow_water_time_update_2d_ssprk2__advance` で、フラックス差分と底面地形源項 $S_b=[0,-gh\,\partial_x z_b,-gh\,\partial_y z_b]^T$ を用いた `SSPRK2` 更新を実行する。

安定指標は
$$
\mathrm{cfl}=\Delta t\cdot\max_{i,j}\left(\frac{|u_{i,j}|+c_{i,j}}{\Delta x}+\frac{|v_{i,j}|+c_{i,j}}{\Delta y}\right)
$$
と定義する。閾値は `tests.md` の判定条件を参照する。

## 6. モデルパラメタと実行時 input contract
物理定数は `g=9.81 m/s2`、`H_0=1.0 m` とする。底面地形プロファイルは `topography_profile` で指定し、`williamson_tc5_cone` と `flat` の 2 値のみを許可する。`topography_profile=williamson_tc5_cone` の場合、底面地形 `z_b` は Williamson et al.（1992）に倣い、孤立円錐山形で次に固定する。
$$
d_x(x)=\min\left(|x-x_c|,L_x-|x-x_c|\right),\quad
d_y(y)=\min\left(|y-y_c|,L_y-|y-y_c|\right)
$$
$$
r(x,y)=\sqrt{d_x(x)^2+d_y(y)^2}
$$
$$
z_b(x,y)=h_s\max\left(0,1-\frac{r(x,y)}{r_0}\right)
$$
既定パラメタは `x_c=3L_x/4`、`y_c=2L_y/3`、`r_0=min(L_x,L_y)/6`、`h_s=0.2H_0` とする。中心位置と半径は Williamson et al.（1992）`Test Case 5` の `(\lambda_c,\theta_c,R_0)=(3\pi/2,\pi/6,\pi/9)` を周期直交座標へ写像した値とする。離散化後の `z_b` は少なくとも 1 セルで `z_b>0` を満たさなければならない。`topography_profile=flat` の場合は全セルで `z_b=0` とする。

実行時入力は次を必須とする。
- `L_x`, `L_y`, `nx`, `ny`
- `topography_profile`
- `initial_condition`（`h`, `hu`, `hv`）
- `t_start`, `t_end`
- `dt_rule`
- `output_schedule`

初期状態で 全セル `h>0` と `h+z_b>0` を必須とする。未定義パラメタは暗黙補完せず エラーとする。

## 7. 禁止事項
非周期境界、`topography_profile` の自動切替、許可値以外の底面地形関数またはパラメタ導入、底面地形源項以外の外力項導入、離散化スキームの実行時 自動切替を禁止する。`h` への `clip` / `limiter` / `filter` を禁止する。

## 8. トレーサビリティ
`case.resolved.yaml` には `spec_kind`, `spec_id`, `spec_version`, `component_id@version`, `profile_id@version` の解決結果を必須記録とする。

参照根拠は Williamson et al.（1992, JCP, DOI:10.1016/S0021-9991(05)80016-6）と LeVeque（2002）と Toro（2009）とする。検証プロファイルは `spec/problem/dynamics/shallow_water/shallow_water2d/tests.md` を参照する。

## 9. AD 準備情報
`ad_readiness.enabled` は `true` とする。状態更新は $U_{next}=F(U_{now}, params)$ の形で表現し、微分対象外演算として `max`、`abs`、`ceil`、周期インデックス wrap を明示する。
