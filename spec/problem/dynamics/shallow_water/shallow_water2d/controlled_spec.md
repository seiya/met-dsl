# Controlled Spec: 2D shallow water problem (problem spec)

## 0. Meta information
- `spec_id`: `shallow_water2d`
- `spec_version`: `0.3.0`
- `status`: `controlled_draft`
- `spec_kind`: `problem`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Problem definition
The target is the conservative form of the 2D shallow water equation
$$
\frac{\partial h}{\partial t}
+ \frac{\partial (hu)}{\partial x}
+ \frac{\partial (hv)}{\partial y}
= 0
$$
$$
\frac{\partial (hu)}{\partial t}
+ \frac{\partial }{\partial x}\left(hu^2 + \frac{1}{2} g h^2\right)
+ \frac{\partial (huv)}{\partial y}
= -gh\frac{\partial z_b}{\partial x}
$$
$$
\frac{\partial (hv)}{\partial t}
+ \frac{\partial (huv)}{\partial x}
+ \frac{\partial }{\partial y}\left(hv^2 + \frac{1}{2} g h^2\right)
= -gh\frac{\partial z_b}{\partial y}
$$
The forcing term handles only the source term due to the bottom-topography gradient.

## 2. Definition of variables and coordinates
The coordinate system is 2D Cartesian coordinates, the coordinate names are `x`,`y`, and the unit is `m`.
- `h`: water depth, cell-centered placement, unit `m`
- `hu`: `x`-direction momentum, cell-centered placement, unit `m2/s`
- `hv`: `y`-direction momentum, cell-centered placement, unit `m2/s`
- `z_b`: bottom topography, cell-centered placement, unit `m`, time-invariant
- `eta`: free-surface elevation, `eta=h+z_b`, unit `m`

The derived variables are `u=hu/h`, `v=hv/h`, and `c=sqrt(g*h)`. `h<=0` is invalid input.

## 3. Type definition of domain and boundary conditions
The domain is the orthogonal periodic domain $[0,L_x)\times[0,L_y)$. The grid is a uniform cell-centered finite-volume grid, and $dx=L_x/nx$, $dy=L_y/ny$.

The boundary condition is fixed to periodic boundary on all boundaries. The default input used for verification is defined in `tests.md`.

## 4. Dependent `component` and adopted `profile`
This `problem spec` references the following `component`.
- `dynamics_shallow_water_flux_2d_rusanov_p0`
- `dynamics_shallow_water_boundary_2d_periodic_copy`
- `dynamics_shallow_water_time_update_2d_ssprk2`

The adopted `profile` is `dynamics_shallow_water_profile_2d_rusanov_p0_ssprk2`.

## 5. Integration algorithm
The update step is fixed to the following order.
1. Update the ghost region with `dynamics_shallow_water_boundary_2d_periodic_copy__apply`.
2. Compute the interface flux with `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`.
3. Execute the `SSPRK2` update with `dynamics_shallow_water_time_update_2d_ssprk2__advance`, using the flux difference and the bottom-topography source term $S_b=[0,-gh\,\partial_x z_b,-gh\,\partial_y z_b]^T$.

The stability index is defined as
$$
\mathrm{cfl}=\Delta t\cdot\max_{i,j}\left(\frac{|u_{i,j}|+c_{i,j}}{\Delta x}+\frac{|v_{i,j}|+c_{i,j}}{\Delta y}\right)
$$
For the threshold, refer to the judgment conditions of `tests.md`.

## 6. Model parameters and the runtime input contract
The physical constants are `g=9.81 m/s2` and `H_0=1.0 m`. The bottom-topography profile is specified by `topography_profile`, and only the 2 values `williamson_tc5_cone` and `flat` are allowed. When `topography_profile=williamson_tc5_cone`, the bottom topography `z_b` follows Williamson et al. (1992) and is fixed to an isolated cone-mountain shape as follows.
$$
d_x(x)=\min\left(|x-x_c|,L_x-|x-x_c|\right),\quad
d_y(y)=\min\left(|y-y_c|,L_y-|y-y_c|\right)
$$
$$
r(x,y)=\sqrt{d_x(x)^2+d_y(y)^2}
$$
$$
z_b(x,y)=h_s\max\left(0,1-\frac{r(x,y)}{r_0}\right)
$$
The default parameters are `x_c=3L_x/4`, `y_c=2L_y/3`, `r_0=min(L_x,L_y)/6`, and `h_s=0.2H_0`. The center position and radius are the value of Williamson et al. (1992) `Test Case 5` `(\lambda_c,\theta_c,R_0)=(3\pi/2,\pi/6,\pi/9)` mapped to the periodic orthogonal coordinates. After discretization, `z_b` must satisfy `z_b>0` in at least 1 cell. When `topography_profile=flat`, `z_b=0` in all cells.

The runtime input requires the following.
- `L_x`, `L_y`, `nx`, `ny`
- `topography_profile`
- `initial_condition` (`h`, `hu`, `hv`)
- `t_start`, `t_end`
- `dt_rule`
- `output_schedule`

In the initial state, `h>0` and `h+z_b>0` in all cells are required. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Forbid non-periodic boundary, automatic switching of `topography_profile`, the introduction of a bottom-topography function or parameter other than the allowed values, the introduction of a forcing term other than the bottom-topography source term, and the runtime automatic switching of the discretization scheme. Forbid `clip` / `limiter` / `filter` on `h`.

## 8. Traceability
`case.resolved.yaml` requires recording the resolution result of `spec_kind`, `spec_id`, `spec_version`, `component_id@version`, and `profile_id@version`.

The reference basis is Williamson et al. (1992, JCP, DOI:10.1016/S0021-9991(05)80016-6), LeVeque (2002), and Toro (2009).

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/shallow_water/shallow_water2d/tests.md`, with `test_profile_version` of `0.2.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `true`. The state update is expressed in the form $U_{next}=F(U_{now}, params)$, and `max`, `abs`, `ceil`, and the periodic-index wrap are made explicit as non-differentiable operations.
