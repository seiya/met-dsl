# Controlled Spec: 1D forward Euler update (component spec)

## 0. Meta information
- `spec_id`: `dynamics_advection_diffusion_time_update_1d_euler1`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Responsibility and scope
This `component` is responsible for executing the time update of the 1D advection-diffusion problem.

## 2. input/output contract
The input is `u^n(i)`, `a`, `nu`, `dx`, `dt`, and the boundary-applied neighboring cell values. The output is `u^{n+1}(i)`.

## 3. Operation definition
The published `operation` is `dynamics_advection_diffusion_time_update_1d_euler1__advance`. The update expression is
$$
u_i^{n+1}
= u_i^n
- C\left(u_i^n-u_{i-1}^n\right)
+ D\left(u_{i+1}^n-2u_i^n+u_{i-1}^n\right)
$$
$$
C=a\frac{\Delta t}{\Delta x},\quad D=\nu\frac{\Delta t}{\Delta x^2}
$$

## 4. Failure conditions and constraints
Treat `dx<=0` and `dt<=0` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_advection_diffusion_time_update_1d_euler1__advance`.

## 6. Prohibitions
Forbid automatic switching of the time-integration method.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/advection_diffusion/dynamics_advection_diffusion_time_update_1d_euler1/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. `ceil` (when used in the `dt` rule) is made explicit as a non-differentiable operation.
