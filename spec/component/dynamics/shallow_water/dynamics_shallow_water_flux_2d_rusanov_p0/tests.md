# Tests: 2 次元 shallow water Rusanov フラックス（L0）

## 0. メタ情報
- `test_profile_id`: `dynamics_shallow_water_flux_2d_rusanov_p0_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov_p0/controlled_spec.md`

## 1. テスト対象 `operation`
- `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`

## 2. 入力既定化規則
- 正常系は `h>0` を満たす左右状態・上下状態を使用する。
- 異常系は `h<=0` を含む状態を使用する。

## 3. 診断契約
- `diagnostics.json` に `checks.equal_state_consistency`, `checks.wave_speed_nonnegative`, `checks.input_guard` を必須出力とする。

## 4. テスト定義
- `test_id`: `l0_equal_state_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `判定`: `U_L=U_R` で `F*=F(U_L)` を満たす。
- `test_id`: `l0_wave_speed_nonnegative_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `pass`
  - `判定`: 計算された波速 `a_x`,`a_y` が非負である。
- `test_id`: `l0_invalid_dry_state_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `h<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. 合否集約規則
- `per_test.pass_rule`: 判定式を満たす場合に `pass` とする。
- `per_test.xfail_rule`: `xfail_condition` が真で `pass_when` を満たす場合に `xfail` とする。
- `suite.pass_rule`: 全 `test_id` が `pass` または `xfail` の場合に `pass` とする。

## 6. トレーサビリティ
- `test_profile_id` と `test_profile_version` を `trial_meta.json` に記録する。
