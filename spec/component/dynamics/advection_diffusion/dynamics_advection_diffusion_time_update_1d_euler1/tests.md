# Tests: 1D forward Euler update (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_time_update_1d_euler1_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_time_update_1d_euler1`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_time_update_1d_euler1/controlled_spec.md`

## 1. Tested `operation`
- `dynamics_advection_diffusion_time_update_1d_euler1__advance`

## 2. Input-defaulting rules
- The normal case uses `dx>0`, `dt>0`.
- The abnormal case uses `dt<=0`.

## 3. Diagnostics contract
- Require outputting `checks.zero_gradient_invariance`, `checks.formula_consistency`, and `checks.input_guard` in `diagnostics.json`.

## 4. Test definitions
- `test_id`: `l0_zero_gradient_invariance_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `pass`
  - `judgment`: with a uniform-field input, satisfy `u^{n+1}=u^n`.
- `test_id`: `l0_single_step_formula_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `pass`
  - `judgment`: the computation result for a known input matches the update expression.
- `test_id`: `l0_invalid_dt_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_time_update_1d_euler1__advance`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `dt<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 6. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
