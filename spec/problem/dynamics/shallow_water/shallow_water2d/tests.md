# Tests: 2 次元 shallow water equation（検証入力・判定条件）

## 0. メタ情報
- `status`: `draft`
- `test_profile_id`: `shallow_water2d_baseline`
- `test_profile_version`: `0.2.0`
- `spec_ref.spec_kind`: `problem`
- `spec_ref.spec_id`: `shallow_water2d`
- `spec_ref.spec_version`: `0.3.0`
- `spec_ref.controlled_spec_path`: `spec/problem/dynamics/shallow_water/shallow_water2d/controlled_spec.md`

## 1. 目的
本スイートは、底面地形を含む 2 次元 shallow water equation の離散実装について、保存性、静水不変性、地形強制下の安定性、理論解一致、平行移動同値性、`CFL` ガードを検証する。保存性判定は全ケースで質量を対象とし、運動量は `topography_profile=flat` のケースで対象とする。判定対象は `L0` から `L3` とし、期待失敗（`xfail`）を含む。

## 2. 入力既定化
### 2-1. 基本定数
- 領域長 `L_x=L_y=1.0` とする。
- 代表水位 `H_0=1.0` とする。
- 重力加速度は Controlled Spec の固定値 `g=9.81` を使用する。
- 微小振幅は `eta0=1.0e-3` とする。
- 線形重力波の参照位相速度は $c_0=\sqrt{gH_0}$ とする。

### 2-2. 底面地形プロファイル
底面地形は `topography_profile` で次の 2 種類を許可する。
- `williamson_tc5_cone`: Controlled Spec の円錐地形を使用する。
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
  地形パラメタは `x_c=3L_x/4`、`y_c=2L_y/3`、`r_0=min(L_x,L_y)/6`、`h_s=0.2H_0` とする。
- `flat`: 全セル `z_b=0` とする。

### 2-3. 初期条件プロファイル
- `linear_wave_x` は次で定義する。
  $$
  \eta(x,y,0)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}-\mathrm{shift\_x\_fraction}\right)\right)
  $$
  $$
  h(x,y,0)=\eta(x,y,0)-z_b(x,y)
  $$
  $$
  u(x,y,0)=\frac{\eta_0 c_0}{H_0}\sin\left(2\pi\left(\frac{x}{L_x}-\mathrm{shift\_x\_fraction}\right)\right),\quad v(x,y,0)=0
  $$
  $$
  hu=h\,u,\quad hv=h\,v
  $$
- `oblique_mode` は次で定義する。
  $$
  \eta(x,y,0)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}+\frac{y}{L_y}-\mathrm{shift\_x\_fraction}-\mathrm{shift\_y\_fraction}\right)\right)
  $$
  $$
  h(x,y,0)=\eta(x,y,0)-z_b(x,y),\quad hu=0,\quad hv=0
  $$
- `lake_at_rest` は次で定義する。
  $$
  \eta(x,y,0)=H_0,\quad h(x,y,0)=H_0-z_b(x,y),\quad hu=0,\quad hv=0
  $$

### 2-4. 理論解（適用条件付き）
`topography_profile=flat` かつ `initial_profile=linear_wave_x` の場合のみ、線形化 shallow water equation の参照解を使用する。参照解は次とする。
$$
h_{ref}(x,t)=H_0+\eta_0\sin\left(2\pi\left(\frac{x}{L_x}-\frac{c_0 t}{L_x}-\mathrm{shift\_x\_fraction}\right)\right)
$$
`h` 以外の変数に対する理論一致判定は本スイートで要求しない。

## 3. 実行制御の既定値
- $t_{start}=0.0$、$t_{end}=0.2$ とする。
- `dt` は次の手順で決定する。
  1. $\lambda_0=\max_{i,j}\left((|u_{i,j}|+\sqrt{gh_{i,j}})/dx+(|v_{i,j}|+\sqrt{gh_{i,j}})/dy\right)$ を初期時刻で評価する。
  2. $\mathrm{dt\_raw}=\mathrm{dt\_scale}\cdot \mathrm{cfl\_target}/\lambda_0$ とする。
  3. $\mathrm{n\_step}=\lceil (t_{end}-t_{start})/\mathrm{dt\_raw}\rceil$ とする。
  4. $dt=(t_{end}-t_{start})/\mathrm{n\_step}$ とする。
- $\mathrm{cfl\_target}=0.45$ とする。
- 停止条件は $n=\mathrm{n\_step}$ とする。
- 出力時刻は $0.0,0.05,0.10,0.15,0.20$ とする。

## 4. ケース展開規則
### 4-1. family 定義
`family` ごとの `sweep` と固定値を以下で定義する。

| family_id | purpose | sweep | fixed |
| --- | --- | --- | --- |
| `swe2d_ref` | refinement_for_mass_and_positivity_with_topography | $nx=ny\in\{32,64,128\}$ | `topography_profile=williamson_tc5_cone`, `initial_profile=linear_wave_x`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_lake` | lake_at_rest_invariance_with_topography | $nx=ny=64$ | `topography_profile=williamson_tc5_cone`, `initial_profile=lake_at_rest`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_resp` | topography_forced_response_stability | $nx=ny=64$ | `topography_profile=williamson_tc5_cone`, `initial_profile=oblique_mode`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_flat_ref` | refinement_for_linear_wave_analytic | $nx=ny\in\{32,64,128\}$ | `topography_profile=flat`, `initial_profile=linear_wave_x`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_flat_sym` | translation_equivariance_without_topography | $nx=ny=64$ | `topography_profile=flat`, `initial_profile=oblique_mode`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0`, `dt_scale=1.0` |
| `swe2d_guard` | expected_failure_for_cfl | $nx=ny=32,\ \mathrm{dt\_scale}=2.40$ | `topography_profile=flat`, `initial_profile=linear_wave_x`, `shift_x_fraction=0.0`, `shift_y_fraction=0.0` |

### 4-2. `case_id` 生成規則
- テンプレートは `{family}_n{nx:03d}_sx{sx_pct:03d}_sy{sy_pct:03d}_dts{dts_pct:03d}` とする。
- `sx_pct` は $\mathrm{round}(100\cdot \mathrm{shift\_x\_fraction})$ で決定する。
- `sy_pct` は $\mathrm{round}(100\cdot \mathrm{shift\_y\_fraction})$ で決定する。
- `dts_pct` は $\mathrm{round}(100\cdot \mathrm{dt\_scale})$ で決定する。
- 展開順序は `family`, `nx`, `shift_x_fraction`, `shift_y_fraction`, `dt_scale` の順で固定する。

### 4-3. 明示上書きケース
- `case_id` は `swe2d_ref_n064_sx000_sy000_dts100_tend500` を追加する。
- `base_case_id` は `swe2d_ref_n064_sx000_sy000_dts100` を参照し、`t_end` を `5.0` のみに上書きする。
- `case_id` は `swe2d_flat_sym_n064_sx025_sy012_dts100` を追加する。
- `base_case_id` は `swe2d_flat_sym_n064_sx000_sy000_dts100` を参照し、`shift_x_fraction=0.25` と `shift_y_fraction=0.125` のみ上書きする。

## 5. 診断成果物と契約
### 5-1. 成果物
- 診断出力ファイルは `diagnostics.json` とする。
- 判定出力ファイルは `verdict.json` とする。

### 5-2. 必須診断項目
`diagnostics.json` は次のフィールドを必須とする。
- `cfl.max`
- `conserved.mass.initial`
- `conserved.mass.final`
- `conserved.momentum_x.initial`
- `conserved.momentum_x.final`
- `conserved.momentum_y.initial`
- `conserved.momentum_y.final`
- `extrema.h.min`
- `errors.analytic_h.l2_rel_tend`
- `errors.symmetry_h_l2_rel`
- `invariants.lake_rest.max_velocity`
- `invariants.lake_rest.max_surface_deviation`

### 5-3. `N/A` 規則
- 診断項目が計算不能または非適用の場合は、出力値を `null` とし、`reason_na` を必須とする。
- `errors.analytic_h.l2_rel_tend` は `topography_profile=flat` かつ `initial_profile=linear_wave_x` 以外で `N/A` とする。
- `errors.symmetry_h_l2_rel` は平行移動ペア評価以外で `N/A` とする。
- `momx_drift_rel` と `momy_drift_rel` は `topography_profile=flat` 以外で `N/A` とする。
- `invariants.lake_rest.*` は `initial_profile=lake_at_rest` 以外で `N/A` とする。

### 5-4. 指標の定義
質量ドリフト相対値を次で定義する。
$$
\mathrm{mass\_drift\_rel}=
\frac{|M_{end}-M_0|}{\max(|M_0|,1e{-14})},
\quad
M=\sum_{i,j} h_{i,j}\,dx\,dy
$$

運動量ドリフト相対値を次で定義する。
$$
\mathrm{momx\_drift\_rel}=
\frac{|P^x_{end}-P^x_0|}{\max(M_0\,c_0,1e{-14})},
\quad
P^x=\sum_{i,j} (hu)_{i,j}\,dx\,dy
$$
$$
\mathrm{momy\_drift\_rel}=
\frac{|P^y_{end}-P^y_0|}{\max(M_0\,c_0,1e{-14})},
\quad
P^y=\sum_{i,j} (hv)_{i,j}\,dx\,dy
$$
ここで $M_0$ は初期質量とする。

理論比較誤差（`flat + linear_wave_x` のみ）を次で定義する。
$$
\mathrm{analytic\_h\_l2\_rel}=
\frac{\|h_{num}(t_{end})-h_{ref}(t_{end})\|_2}{\|h_{ref}(t_{end})\|_2}
$$

平行移動同値性誤差を次で定義する。
$$
\mathrm{symmetry\_h\_l2\_rel}=
\frac{\|h_{shifted}(t_{end})-\mathrm{shift}(h_{ref}(t_{end}),+\Delta x,+\Delta y)\|_2}{\|h_{ref}(t_{end})\|_2}
$$
ここで $\Delta x=\mathrm{shift\_x\_fraction}\cdot L_x$、$\Delta y=\mathrm{shift\_y\_fraction}\cdot L_y$ とする。

静水不変性の流速指標を次で定義する。
$$
\mathrm{lake\_rest\_max\_velocity}=
\max_{i,j}\left(\sqrt{u_{i,j}^2+v_{i,j}^2}\right)
$$

静水不変性の水面偏差指標を次で定義する。
$$
\mathrm{lake\_rest\_max\_surface\_deviation}=
\max_{i,j}\left|\eta_{i,j}(t_{end})-\eta_{i,j}(0)\right|,
\quad \eta=h+z_b
$$

## 6. 既定閾値
- $\mathrm{cfl.max} \le 1.0$
- $h_{min} \ge 5.0e{-2}$
- $\mathrm{mass\_drift\_rel} \le 1.0e{-10}$
- $\mathrm{momx\_drift\_rel} \le 1.0e{-10}$
- $\mathrm{momy\_drift\_rel} \le 1.0e{-10}$
- `analytic_h_l2_rel` は `nx=32` で $\le 2.2e{-1}$、`nx=64` で $\le 1.2e{-1}$、`nx=128` で $\le 6.5e{-2}$ とする。
- `convergence_order` は $p=\log(e_{coarse}/e_{fine})/\log(2)$ を用い、`n32_to_n64` と `n64_to_n128` の双方で $\ge 0.80$ を要求する。
- `lake_rest.max_velocity \le 1.0e{-12}`
- `lake_rest.max_surface_deviation \le 1.0e{-12}`
- `symmetry_h_l2_rel \le 2.0e{-11}`

## 7. テスト定義
### 7-1. `l1_refinement_mass_and_positivity`
- `level`: `L1`
- `objective`: `williamson_tc5_cone + linear_wave_x` で refinement に対する質量保存と正値性を確認する。
- 対象ケース:
  - `swe2d_ref_n032_sx000_sy000_dts100`
  - `swe2d_ref_n064_sx000_sy000_dts100`
  - `swe2d_ref_n128_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- 判定条件:
  - `CFL` 判定は適用する。評価式は `cfl.max`、閾値は $\le 1.0$ とする。
  - 深さ正値判定は適用する。評価式は `extrema.h.min`、閾値は $\ge 5.0e{-2}$ とする。
  - 質量保存判定は適用する。評価式は `mass_drift_rel`、閾値は $\le 1.0e{-10}$ とする。
  - 運動量保存判定は適用しない。非適用根拠は「底面地形源項が運動量を交換するため」とする。
  - 理論比較判定は適用しない。非適用根拠は「地形ありケースのため」とする。
  - 平行移動同値性判定は適用しない。非適用根拠は「ペアケースを実行しないため」とする。
  - 静水不変性判定は適用しない。非適用根拠は「初期条件が `lake_at_rest` ではないため」とする。

### 7-2. `l1_refinement_linear_wave`
- `level`: `L1`
- `objective`: `flat + linear_wave_x` で refinement に伴う理論解誤差低下を確認する。
- 対象ケース:
  - `swe2d_flat_ref_n032_sx000_sy000_dts100`
  - `swe2d_flat_ref_n064_sx000_sy000_dts100`
  - `swe2d_flat_ref_n128_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- 判定条件:
  - `CFL` 判定は適用する。評価式は `cfl.max`、閾値は $\le 1.0$ とする。
  - 深さ正値判定は適用する。評価式は `extrema.h.min`、閾値は $\ge 5.0e{-2}$ とする。
  - 質量保存判定は適用する。評価式は `mass_drift_rel`、閾値は $\le 1.0e{-10}$ とする。
  - 運動量保存判定は適用する。評価式は `momx_drift_rel` と `momy_drift_rel`、閾値は双方 $\le 1.0e{-10}$ とする。
  - 理論比較判定は適用する。`analytic_h_l2_rel` はケース別閾値を適用し、`convergence_order` は双方で $\ge 0.80$ を要求する。
  - 平行移動同値性判定は適用しない。非適用根拠は「ペアケースを実行しないため」とする。
  - 静水不変性判定は適用しない。非適用根拠は「初期条件が `lake_at_rest` ではないため」とする。

### 7-3. `l2_lake_at_rest_invariance`
- `level`: `L2`
- `objective`: `williamson_tc5_cone + lake_at_rest` で流速と自由水面が不変であることを確認する。
- 対象ケース:
  - `swe2d_lake_n064_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- 判定条件:
  - `CFL` 判定は適用する。評価式は `cfl.max`、閾値は $\le 1.0$ とする。
  - 深さ正値判定は適用する。評価式は `extrema.h.min`、閾値は $\ge 5.0e{-2}$ とする。
  - 質量保存判定は適用する。評価式は `mass_drift_rel`、閾値は $\le 1.0e{-10}$ とする。
  - 運動量保存判定は適用しない。非適用根拠は「底面地形源項が運動量を交換するため」とする。
  - 静水不変性判定は適用する。`lake_rest.max_velocity \le 1.0e{-12}` と `lake_rest.max_surface_deviation \le 1.0e{-12}` を要求する。
  - 理論比較判定は適用しない。非適用根拠は「地形あり静水ケースのため」とする。
  - 平行移動同値性判定は適用しない。非適用根拠は「単一ケース検証のため」とする。

### 7-4. `l2_long_run_mass_conservation`
- `level`: `L2`
- `objective`: `williamson_tc5_cone + linear_wave_x` の長時間積分で質量と深さ正値が維持されることを確認する。
- 対象ケース:
  - `swe2d_ref_n064_sx000_sy000_dts100_tend500`
- `expected_outcome`: `pass`
- 判定条件:
  - `CFL` 判定は適用する。評価式は `cfl.max`、閾値は $\le 1.0$ とする。
  - 深さ正値判定は適用する。評価式は `extrema.h.min`、閾値は $\ge 5.0e{-2}$ とする。
  - 質量保存判定は適用する。評価式は `mass_drift_rel`、閾値は $\le 5.0e{-10}$ とする。
  - 運動量保存判定は適用しない。非適用根拠は「底面地形源項が運動量を交換するため」とする。
  - 理論比較判定は適用しない。非適用根拠は「地形あり長時間ケースのため」とする。
  - 平行移動同値性判定は適用しない。非適用根拠は「ペアケースを実行しないため」とする。
  - 静水不変性判定は適用しない。非適用根拠は「初期条件が `lake_at_rest` ではないため」とする。

### 7-5. `l3_topography_forced_response_stability`
- `level`: `L3`
- `objective`: `williamson_tc5_cone + oblique_mode` で地形強制下の数値安定性を確認する。
- 対象ケース:
  - `swe2d_resp_n064_sx000_sy000_dts100`
- `expected_outcome`: `pass`
- 判定条件:
  - `CFL` 判定は適用する。評価式は `cfl.max`、閾値は $\le 1.0$ とする。
  - 深さ正値判定は適用する。評価式は `extrema.h.min`、閾値は $\ge 5.0e{-2}$ とする。
  - 質量保存判定は適用する。評価式は `mass_drift_rel`、閾値は $\le 1.0e{-10}$ とする。
  - 運動量保存判定は適用しない。非適用根拠は「底面地形源項が運動量を交換するため」とする。
  - 理論比較判定は適用しない。非適用根拠は「地形ありケースで理論解を使用しないため」とする。
  - 平行移動同値性判定は適用しない。非適用根拠は「地形ありケースでは本テストで同値性を要求しないため」とする。
  - 静水不変性判定は適用しない。非適用根拠は「初期条件が `lake_at_rest` ではないため」とする。

### 7-6. `l3_translation_equivariance`
- `level`: `L3`
- `objective`: `flat + oblique_mode` 初期条件の平行移動に対して数値解が同値であることを確認する。
- ペアケース:
  - `reference` は `swe2d_flat_sym_n064_sx000_sy000_dts100`
  - `shifted` は `swe2d_flat_sym_n064_sx025_sy012_dts100`
- `expected_outcome`: `pass`
- 判定条件:
  - `CFL` 判定は適用する。評価式は `cfl.max`、閾値は $\le 1.0$ とする。
  - 深さ正値判定は適用する。評価式は `extrema.h.min`、閾値は $\ge 5.0e{-2}$ とする。
  - 質量保存判定は適用する。評価式は `mass_drift_rel`、閾値は $\le 1.0e{-10}$ とする。
  - 運動量保存判定は適用する。評価式は `momx_drift_rel` と `momy_drift_rel`、閾値は双方 $\le 1.0e{-10}$ とする。
  - 平行移動同値性判定は適用する。評価式は `symmetry_h_l2_rel`、閾値は $\le 2.0e{-11}$ とする。
  - 理論比較判定は適用しない。非適用根拠は「`oblique_mode` は理論解一致判定の対象外のため」とする。
  - 静水不変性判定は適用しない。非適用根拠は「初期条件が `lake_at_rest` ではないため」とする。

### 7-7. `l0_cfl_guard_xfail`
- `level`: `L0`
- `objective`: `CFL` 違反ケースを検出できることを確認する（期待失敗）。
- 対象ケース:
  - `swe2d_guard_n032_sx000_sy000_dts240`
- `expected_outcome`: `xfail`
- `xfail_condition`: `cfl.max > 1.0`
- `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'cfl'`
- 判定条件:
  - `CFL` 判定は適用する。評価式は `cfl.max`、閾値は $\le 1.0$ とする。
  - 深さ正値判定は適用する。評価式は `extrema.h.min`、閾値は `informational_only` とする。
  - 質量保存判定は適用しない。非適用根拠は「ガードテストの目的は安定条件違反検知のみ」とする。
  - 運動量保存判定は適用しない。非適用根拠は「ガードテストの目的は安定条件違反検知のみ」とする。
  - 理論比較判定は適用しない。非適用根拠は「不安定条件では理論一致判定より先に実行継続可否を評価するため」とする。
  - 平行移動同値性判定は適用しない。非適用根拠は「ペアケースを実行しないため」とする。
  - 静水不変性判定は適用しない。非適用根拠は「初期条件が `lake_at_rest` ではないため」とする。

## 8. verdict 集約規則
- `per_test.pass_rule`: 適用対象の check がすべて `pass` の場合に `pass` とする。
- `per_test.xfail_rule`: `expected_outcome == xfail` かつ `xfail_condition` が真で、`pass_when` を満たす場合に `xfail` とする。
- `suite.pass_rule`: 全 `test_id` が `pass_rule` または `xfail_rule` を満たす場合に `pass` とする。

## 9. 出力要件とトレーサビリティ
- `verdict.json` は各 check の `status`, `metric_value`, `threshold`, `applicable=false` の場合の `reason_na` を必須とする。
- `summary.json` は `pass`, `fail`, `xfail`, `skipped` の件数を必須とする。
- 本文書の `test_profile_id` と `test_profile_version` は `case.resolved.yaml` と `trial_meta.json` に記録しなければならない。
- 本文書の判定条件は `verdict.json` の評価根拠へ写像できなければならない。
