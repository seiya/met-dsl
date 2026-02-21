# Tests: 2 次元 周期境界 写像（L0）

## 0. メタ情報
- `test_profile_id`: `dynamics_shallow_water_boundary_2d_periodic_copy_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_boundary_2d_periodic_copy`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_boundary_2d_periodic_copy/controlled_spec.md`

## 1. テスト対象 `operation`
- `dynamics_shallow_water_boundary_2d_periodic_copy__apply`

## 2. 入力既定化規則
- 正常系は `nx>=2`, `ny>=2`, `ng=1` を使用する。
- 異常系は `ny<2` を使用する。

## 3. 診断契約
- `diagnostics.json` に `checks.x_wrap`, `checks.y_wrap`, `checks.input_guard` を必須出力とする。

## 4. テスト定義
- `test_id`: `l0_periodic_x_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `判定`: `x` 方向 ghost セルが周期写像に一致する。
- `test_id`: `l0_periodic_y_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `判定`: `y` 方向 ghost セルが周期写像に一致する。
- `test_id`: `l0_invalid_ny_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_boundary_2d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `ny<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. 合否集約規則
- `per_test.pass_rule`: 判定式を満たす場合に `pass` とする。
- `per_test.xfail_rule`: `xfail_condition` が真で `pass_when` を満たす場合に `xfail` とする。
- `suite.pass_rule`: 全 `test_id` が `pass` または `xfail` の場合に `pass` とする。

## 6. トレーサビリティ
- `test_profile_id` と `test_profile_version` を `trial_meta.json` に記録する。
