# Tests: 1 次元 前進 Euler 更新（L0）

## 0. メタ情報
- `test_profile_id`: `dynamics_advection_diffusion_time_update_1d_euler1_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_time_update_1d_euler1`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_time_update_1d_euler1/controlled_spec.md`

## 1. テスト対象 `operation`
- `dynamics_advection_diffusion_time_update_1d_euler1__advance`

## 2. 入力既定化規則
- 正常系は `dx>0`, `dt>0` を使用する。
- 異常系は `dt<=0` を使用する。

## 3. 診断契約
- `diagnostics.json` に `checks.zero_gradient_invariance`, `checks.formula_consistency`, `checks.input_guard` を必須出力とする。

## 4. テスト定義
- `test_id`: `l0_zero_gradient_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `pass`
  - `判定`: 一様場入力で `u^{n+1}=u^n` を満たす。
- `test_id`: `l0_single_step_formula_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `pass`
  - `判定`: 既知入力に対する計算結果が更新式と一致する。
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. 合否集約規則
- `per_test.pass_rule`: 判定式を満たす場合に `pass` とする。
- `per_test.xfail_rule`: `xfail_condition` が真で `pass_when` を満たす場合に `xfail` とする。
- `suite.pass_rule`: 全 `test_id` が `pass` または `xfail` の場合に `pass` とする。

## 6. トレーサビリティ
- `test_profile_id` と `test_profile_version` を `trial_meta.json` に記録する。
