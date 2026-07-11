# Controlled Spec: 2D shallow water problem (problem spec)

## 0. Meta information
- `spec_id`: `shallow_water2d`
- `spec_version`: `0.4.0`
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
The spatial discretization is **well-balanced** by the first-order hydrostatic reconstruction of Audusse et al. (2004). The interface states supplied to the flux `component` and the discrete bottom-topography source term are built from the same reconstructed depths.

The reconstruction is first-order (`p0`): the in-cell reconstruction stays piecewise constant and only the interface depth is redefined. It satisfies the `profile` constraint `reconstruction: p0` and the flux `component`'s fixed `p0` order. The reconstruction order is never switched at runtime.

The analytic gradient $\partial z_b/\partial x$, $\partial z_b/\partial y$ is not an input of the discretization. The bottom-topography source term is the discrete difference form of step 4.

`z_b` is time-invariant. This `problem` `node` evaluates it once, at setup, on the halo-extended grid with its periodic image, so that the interface reconstruction of step 2 is defined at the domain seam. The boundary `component` maps `U` only; its input/output contract is unchanged and `z_b` is not passed to it.

The update step is fixed to the following order.
1. Update the ghost region of `U` with `dynamics_shallow_water_boundary_2d_periodic_copy__apply`.
2. Reconstruct the interface states hydrostatically. At the `x`-interface $(i+1/2,j)$, define
$$
z_{i+1/2,j}=\max\left(z_{b,i,j},\;z_{b,i+1,j}\right)
$$
$$
h^{*}_{i+1/2,j,L}=\max\left(0,\;h_{i,j}+z_{b,i,j}-z_{i+1/2,j}\right),\quad
h^{*}_{i+1/2,j,R}=\max\left(0,\;h_{i+1,j}+z_{b,i+1,j}-z_{i+1/2,j}\right)
$$
and form the interface states with the cell velocities
$$
U^{*}_{i+1/2,j,L}=\left[h^{*}_{i+1/2,j,L},\;h^{*}_{i+1/2,j,L}\,u_{i,j},\;h^{*}_{i+1/2,j,L}\,v_{i,j}\right]^T
$$
$$
U^{*}_{i+1/2,j,R}=\left[h^{*}_{i+1/2,j,R},\;h^{*}_{i+1/2,j,R}\,u_{i+1,j},\;h^{*}_{i+1/2,j,R}\,v_{i+1,j}\right]^T
$$
The `y`-interface $(i,j+1/2)$ is reconstructed the same way, with $z_{i,j+1/2}=\max(z_{b,i,j},z_{b,i,j+1})$ giving $h^{*}_{i,j+1/2,B}$ from cell $(i,j)$ and $h^{*}_{i,j+1/2,T}$ from cell $(i,j+1)$, and producing the bottom state $U^{*}_{i,j+1/2,B}$ and the top state $U^{*}_{i,j+1/2,T}$.
3. Compute the interface flux with `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`, passing the reconstructed states of step 2 as its `U_L` / `U_R` / `U_B` / `U_T` inputs. The flux `component`'s input/output contract is unchanged: it consumes the interface states the caller supplies.
4. Execute the `SSPRK2` update with `dynamics_shallow_water_time_update_2d_ssprk2__advance`, supplying the interface-flux difference field
$$
L_{flux}=-\frac{F^{*}_{i+1/2,j}-F^{*}_{i-1/2,j}}{\Delta x}
-\frac{G^{*}_{i,j+1/2}-G^{*}_{i,j-1/2}}{\Delta y}
$$
and the reconstruction-consistent bottom-topography source term $S_b=[0,S^{hu}_{i,j},S^{hv}_{i,j}]^T$ with
$$
S^{hu}_{i,j}=\frac{g}{2\,\Delta x}\left(\left(h^{*}_{i+1/2,j,L}\right)^2-\left(h^{*}_{i-1/2,j,R}\right)^2\right),\quad
S^{hv}_{i,j}=\frac{g}{2\,\Delta y}\left(\left(h^{*}_{i,j+1/2,B}\right)^2-\left(h^{*}_{i,j-1/2,T}\right)^2\right)
$$
Here $h^{*}_{i+1/2,j,L}$ and $h^{*}_{i-1/2,j,R}$ are the two reconstructed depths of cell $(i,j)$ itself, taken at its right and left `x`-interfaces; $h^{*}_{i,j+1/2,B}$ and $h^{*}_{i,j-1/2,T}$ are the same for its top and bottom `y`-interfaces. `z_b` remains accepted but inert at the `dynamics_shallow_water_time_update_2d_ssprk2__advance` boundary: the bottom topography is consumed by the reconstruction of step 2 and by $S_b$, both of which belong to this `problem` `node`, and the time-update `component`'s input/output contract is unchanged.

The discretization holds the following invariants.
- **Wet domain.** $h^{*}>0$ at every interface, at every step of the run. The domain of validity of this `problem spec` is the non-drying regime; drying and wetting are out of scope.
- **Well-balanced (lake at rest).** When $\eta=h+z_b$ is uniform and $u=v=0$, the two reconstructed depths at each interface are equal and the flux difference cancels $S_b$. `h`, `hu`, and `hv` are unchanged up to round-off.
- **Flat reduction.** When `topography_profile=flat`, $h^{*}=h$ and $S_b=0$, and the discretization is identical to the plain `p0` scheme.
- **Mass conservation.** The mass component of $S_b$ is zero and the interface mass flux is single-valued, so $\sum_{i,j}h_{i,j}$ is conserved up to round-off.

The $\max(0,\cdot)$ of step 2 is the definitional positivity guard of the reconstruction, and it does not activate while the wet-domain invariant holds. An interface at which the reconstruction yields $h^{*}\le 0$ is a violation of that invariant: it is a runtime error, and the run stops with an error before the value reaches the flux `component`, whose contract treats `h<=0` as an error. It must not be handled by clipping or flooring the state, by substituting a positive replacement depth, or by continuing with a zero-depth interface flux. Section 6 states the initial condition that establishes the invariant at $t=0$.

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

In the initial state, `h>0` and `h+z_b>0` in all cells are required. The hydrostatic reconstruction of section 5 additionally requires the initial free surface of every cell to lie above the bottom topography of its 4 face neighbours,
$$
\eta_{i,j}(0)>\max\left(z_{b,i,j},\;z_{b,i\pm1,j},\;z_{b,i,j\pm1}\right)
$$
so that the reconstructed interface depth satisfies $h^{*}>0$ in the initial state. This condition is stronger than `h+z_b>0`, which constrains the cell's own bottom only. It establishes the wet-domain invariant of section 5 at $t=0$; the invariant is required to hold at every step thereafter, and its violation during the run is a runtime error as defined in section 5. An undefined parameter is an error without implicit completion.

## 7. Prohibitions
Forbid non-periodic boundary, automatic switching of `topography_profile`, the introduction of a bottom-topography function or parameter other than the allowed values, the introduction of a forcing term other than the bottom-topography source term, and the runtime automatic switching of the discretization scheme. Forbid `clip` / `limiter` / `filter` on `h`. The $\max(0,\cdot)$ of the hydrostatic reconstruction of section 5 is applied to the reconstructed interface depth $h^{*}$, a derived quantity of the reconstruction of step 2, and never to the cell-centered state `h`; it is not a `clip` on `h`.

## 8. Traceability
`case.resolved.yaml` requires recording the resolution result of `spec_kind`, `spec_id`, `spec_version`, `component_id@version`, and `profile_id@version`.

The reference basis is Williamson et al. (1992, JCP, DOI:10.1016/S0021-9991(05)80016-6), Audusse et al. (2004, SIAM J. Sci. Comput. 25(6), DOI:10.1137/S1064827503431090), LeVeque (2002), and Toro (2009).

## 9. tests reference
The corresponding `tests.md` is `spec/problem/dynamics/shallow_water/shallow_water2d/tests.md`, with `test_profile_version` of `0.2.0`.

## 10. AD preparation information
`ad_readiness.enabled` is `true`. The state update is expressed in the form $U_{next}=F(U_{now}, params)$, and `max`, `abs`, `ceil`, and the periodic-index wrap are made explicit as non-differentiable operations.
