# Tests: 1 次元 移流拡散 フラックス（L0）

## 0. メタ情報
- `test_profile_id`: `transport_advection_diffusion_flux_1d_upwind_center2_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `transport_advection_diffusion_flux_1d_upwind_center2`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/transport/advection_diffusion/transport_advection_diffusion_flux_1d_upwind_center2/controlled_spec.md`

## 1. テスト対象 `operation`
- `transport_advection_diffusion_flux_1d_upwind_center2__compute_flux`

## 2. 入力既定化規則
- 正常系は `a>0`, `nu>=0`, `dx>0`, `dt>0` を使用する。
- 異常系は `a<=0` を使用する。

## 3. 診断契約
- `diagnostics.json` に `checks.flux_adv_consistency`, `checks.flux_dif_consistency`, `checks.input_guard` を必須出力とする。

## 4. テスト定義
- `test_id`: `l0_constant_state_flux_pass`
  - `level`: `L0`
  - `operation_id`: `transport_advection_diffusion_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `判定`: 定数場入力で `flux_dif=0` かつ `flux_adv=a*u_const` を満たす。
- `test_id`: `l0_linear_state_diff_flux_pass`
  - `level`: `L0`
  - `operation_id`: `transport_advection_diffusion_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `判定`: 線形場入力で `flux_dif` が一様となる。
- `test_id`: `l0_invalid_a_xfail`
  - `level`: `L0`
  - `operation_id`: `transport_advection_diffusion_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `a<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. 合否集約規則
- `per_test.pass_rule`: 判定式を満たす場合に `pass` とする。
- `per_test.xfail_rule`: `xfail_condition` が真で `pass_when` を満たす場合に `xfail` とする。
- `suite.pass_rule`: 全 `test_id` が `pass` または `xfail` の場合に `pass` とする。

## 6. トレーサビリティ
- `test_profile_id` と `test_profile_version` を `trial_meta.json` に記録する。
