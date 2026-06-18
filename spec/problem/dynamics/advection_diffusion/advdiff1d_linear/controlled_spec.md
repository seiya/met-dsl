# Controlled Spec: 1D linear advection-diffusion problem (problem spec)

## 0. Meta information
- `spec_id`: `advdiff1d_linear`
- `spec_version`: `0.3.0`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `advection_diffusion`

## 1. Problem definition
The target is the 1D linear advection-diffusion equation
$$
\frac{\partial u}{\partial t} + \frac{\partial (a u)}{\partial x}
= \frac{\partial}{\partial x}\left(\nu \frac{\partial u}{\partial x}\right)
$$
The equation is treated in conservative form. The unknown variable is the scalar field $u(x,t)$. No forcing term is handled.

## 2. Definition of variables and coordinates
The coordinate system is 1D Cartesian coordinates, the coordinate name is `x`, and the unit is `m`. The state variable is `u`, its meaning is the passive-scalar concentration, its placement is the cell center, and its unit is the dimensionless `1`.

## 3. Type definition of domain and boundary conditions
The domain is the interval $[0,L)$. The grid is a uniform cell-centered grid, and the grid width is defined by $dx=L/nx$. `L` and `nx` are runtime input.

The boundary condition is fixed to periodic boundary. The default input used for verification is defined in `tests.md`, and the whole of the user input is not fixed.

## 4. Dependent `component` and adopted `profile`
This `problem spec` references the following `component`.
- `dynamics_advection_diffusion_flux_1d_upwind_center2`
- `dynamics_advection_diffusion_boundary_1d_periodic_copy`
- `dynamics_advection_diffusion_time_update_1d_euler1`

The adopted `profile` is `dynamics_advection_diffusion_profile_1d_upwind_center2_euler1`.

## 5. Integration algorithm
The update step is fixed to the following order.
1. Update the ghost region with `dynamics_advection_diffusion_boundary_1d_periodic_copy__apply`.
2. Compute the advection/diffusion flux with `dynamics_advection_diffusion_flux_1d_upwind_center2__compute_flux`.
3. Execute the forward Euler update with `dynamics_advection_diffusion_time_update_1d_euler1__advance`.

The stability index is defined as
$$
\text{cfl_combined}=C+2D,\quad C=a\frac{\Delta t}{\Delta x},\quad D=\nu\frac{\Delta t}{\Delta x^2}
$$
For the threshold, refer to the judgment conditions of `tests.md`.

## 6. Model parameters and the runtime input contract
The physical constants are `a=1.0 m/s` and `nu=1.0e-2 m2/s`.

The runtime input requires the following.
- `L`, `nx`
- `initial_condition`
- `t_start`, `t_end`
- `dt_rule`
- `output_schedule`

`a<=0` is not allowed. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Forbid non-periodic boundary. Forbid the addition of `limiter` / `clip` / `filter`. Forbid the runtime automatic switching of the discretization scheme.

## 8. Traceability
`case.resolved.yaml` requires recording the resolution result of `spec_kind`, `spec_id`, `spec_version`, `component_id@version`, and `profile_id@version`.

The reference basis is LeVeque (2002).

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/advection_diffusion/advdiff1d_linear/tests.md`, with `test_profile_version` of `0.1.1`.

## 10. AD preparation information
`ad_readiness.enabled` is `true`. The state update is expressed in the form $u_{next}=F(u_{now}, params)$, and `ceil` and the periodic-index wrap are made explicit as non-differentiable operations.
