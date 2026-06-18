# Controlled Spec: 1D advection-diffusion flux (component spec)

## 0. Meta information
- `spec_id`: `dynamics_advection_diffusion_flux_1d_upwind_center2`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Responsibility and scope
This `component` is responsible for computing the interface flux of the 1D advection-diffusion problem. It does not handle the state update itself.

## 2. input/output contract
The input is `u(i)`, `a`, `nu`, `dx`, and `dt`. The output is `flux_adv(i+1/2)` and `flux_dif(i+1/2)`. `u` is assumed to be cell-centered values.

## 3. Operation definition
The published `operation` is `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`. The advection flux is defined by first-order upwind, and the diffusion flux by second-order central.
$$
F^{adv}_{i+1/2}=a\,u_i\quad(a>0)
$$
$$
F^{dif}_{i+1/2}=-\nu\frac{u_{i+1}-u_i}{dx}
$$

## 4. Failure conditions and constraints
Treat `a<=0`, `dx<=0`, and `dt<=0` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`. On a `major` compatibility break, separate the `spec_id`.

## 6. Prohibitions
The discretization order must not be changed automatically. Forbid implicit completion of undefined input.

## 7. Traceability
This `operation_id` requires registration in `component_catalog.yaml`. `case.resolved.yaml` requires recording the adopted `component_id@version`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_flux_1d_upwind_center2/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. It includes no non-differentiable operations.
