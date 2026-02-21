# Tests: 1 次元 周期境界 写像（L0）

## 0. メタ情報
- `test_profile_id`: `transport_advection_diffusion_boundary_1d_periodic_copy_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `transport_advection_diffusion_boundary_1d_periodic_copy`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/transport/advection_diffusion/transport_advection_diffusion_boundary_1d_periodic_copy/controlled_spec.md`

## 1. テスト対象 `operation`
- `transport_advection_diffusion_boundary_1d_periodic_copy__apply`

## 2. 入力既定化規則
- 正常系は `nx>=2`, `ng=1` を使用する。
- 異常系は `nx<2` を使用する。

## 3. 診断契約
- `diagnostics.json` に `checks.left_wrap`, `checks.right_wrap`, `checks.input_guard` を必須出力とする。

## 4. テスト定義
- `test_id`: `l0_periodic_left_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `transport_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `判定`: 適用後に `u_{-1}=u_{nx-1}` を満たす。
- `test_id`: `l0_periodic_right_wrap_pass`
  - `level`: `L0`
  - `operation_id`: `transport_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `pass`
  - `判定`: 適用後に `u_{nx}=u_0` を満たす。
- `test_id`: `l0_invalid_nx_xfail`
  - `level`: `L0`
  - `operation_id`: `transport_advection_diffusion_boundary_1d_periodic_copy__apply`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `nx<2`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. 合否集約規則
- `per_test.pass_rule`: 判定式を満たす場合に `pass` とする。
- `per_test.xfail_rule`: `xfail_condition` が真で `pass_when` を満たす場合に `xfail` とする。
- `suite.pass_rule`: 全 `test_id` が `pass` または `xfail` の場合に `pass` とする。

## 6. トレーサビリティ
- `test_profile_id` と `test_profile_version` を `trial_meta.json` に記録する。
