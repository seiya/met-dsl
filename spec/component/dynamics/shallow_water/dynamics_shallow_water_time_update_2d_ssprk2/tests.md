# Tests: 2D `SSPRK2` update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_shallow_water_time_update_2d_ssprk2_l0`
- `test_profile_version`: `0.2.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_ref.spec_version`: `0.2.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_ssprk2/controlled_spec.md`

## 1. Tested `operation`
- `dynamics_shallow_water_time_update_2d_ssprk2__advance`

## 2. Input-defaulting rules
- The normal case uses `dt>0`, `dx>0`, `dy>0`, and includes both `S_b=0` and `S_b!=0` in the evaluation target.
- The abnormal case uses `dt<=0`.

## 3. Diagnostics contract
- Require outputting `checks.zero_rhs_invariance`, `checks.stage_weight_consistency`, and `checks.input_guard` in `diagnostics.json`.

## 4. Test definitions
- `test_id`: `l0_zero_rhs_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with an `L(U)=0` input, satisfy `U^{n+1}=U^n`.
- `test_id`: `l0_stage_weight_consistency_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `pass`
  - `judgment`: the weights of the 2-stage composition are applied as `1/2,1/2`.
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_shallow_water_time_update_2d_ssprk2__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 6. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
