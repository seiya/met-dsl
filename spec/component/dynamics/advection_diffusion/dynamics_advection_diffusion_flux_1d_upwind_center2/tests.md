# Tests: 1D advection-diffusion flux (L0)

## 0. Meta information
- `test_profile_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2_l0`
- `test_profile_version`: `0.1.0`
- `status`: `draft`
- `spec_ref.spec_kind`: `component`
- `spec_ref.spec_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2`
- `spec_ref.spec_version`: `0.1.0`
- `spec_ref.controlled_spec_path`: `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_flux_1d_upwind_center2/controlled_spec.md`

## 1. Test purpose
This suite verifies the published `operation` `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux` at `L0`: the advective/diffusive flux consistency for a constant field, the uniformity of the diffusive flux for a linear field, and the input guard for an invalid advection velocity (`a<=0`).

## 2. Input-defaulting rules
- The normal case uses `a>0`, `nu>=0`, `dx>0`, `dt>0`.
- The abnormal case uses `a<=0`.

## 3. Execution-control rules
`N/A`: this `component` exposes a single pointwise `operation` and defines no time-stepping or iteration. Execution control is the responsibility of the time-update `component` and the `problem` runner.

## 4. Case-expansion rules
`N/A`: the `L0` suite uses fixed single-stencil inputs and defines no `case` sweep. Case expansion is defined at the `problem` level.

## 5. Diagnostics contract
- Require outputting `checks.flux_adv_consistency`, `checks.flux_dif_consistency`, and `checks.input_guard` in `diagnostics.json`.

## 6. Test definitions
- `test_id`: `l0_constant_state_flux_pass`
  - `level`: `L0`
  - `operation_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`
  - `expected_outcome`: `pass`
  - `judgment`: with a constant-field input, satisfy `flux_dif=0` and `flux_adv=a*u_const`, each within an absolute tolerance of `1e-12` (component-wise max deviation `<= 1e-12`).
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

## 7. Pass/fail aggregation rules
- `per_test.pass_rule`: `pass` when the judgment expression is satisfied.
- `per_test.xfail_rule`: `xfail` when `xfail_condition` is true and `pass_when` is satisfied.
- `suite.pass_rule`: `pass` when all `test_id` are `pass` or `xfail`.

## 8. Traceability
- Record `test_profile_id` and `test_profile_version` in `trial_meta.json`.
