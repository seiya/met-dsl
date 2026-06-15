# Tests: 1D advection-diffusion flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_flux_1d_upwind_center2/controlled_spec.md`

## 1. Tested `operation`
- `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`

## 2. Input-defaulting rules
- The normal case uses `a>0`, `nu>=0`, `dx>0`, `dt>0`.
- The abnormal case uses `a<=0`.

## 3. Diagnostics contract
- Require outputting `checks.flux_adv_consistency`, `checks.flux_dif_consistency`, and `checks.input_guard` in `diagnostics.json`.

## 4. Test definitions
- `test_id`: `l0_constant_state_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with a constant-field input, satisfy `flux_dif=0` and `flux_adv=a*u_const`.
- `test_id`: `l0_linear_state_diff_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with a linear-field input, `flux_dif` becomes uniform.
- `test_id`: `l0_invalid_a_xfail`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `xfail`
  - `xfail_condition`: `a<=0`
  - `pass_when`: `verdict.overall == fail and verdict.failed_checks includes 'input_guard'`

## 5. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 6. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
