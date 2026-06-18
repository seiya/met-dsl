# Controlled Spec: 2D shallow water Rusanov flux (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_flux_2d_rusanov_p0`
- `spec_version`: `0.1.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible for the interface-flux computation of the shallow water equation. Reconstruction is fixed to first-order `p0`.

## 2. input/output contract
The input variables are the left/right states `U_L`, `U_R` across an `x`-interface and the bottom/top states `U_B`, `U_T` across a `y`-interface, each the conserved-variable vector `U=[h, hu, hv]^T`, together with the gravitational acceleration `g`. The output variables are the numerical flux `F*` across the `x`-interface and `G*` across the `y`-interface, each a length-3 vector ordered as `[h, hu, hv]` and aligned with `U`.

Array placement: all inputs and outputs are interface-located values. The caller supplies the reconstructed left/right and bottom/top states at each interface, and receives the flux at the same interface. The operation is pointwise per interface; vectorized application over a 2D grid is the caller's responsibility.

Units: `h` is in `$\mathrm{m}$`, `hu` and `hv` are in `$\mathrm{m^2\,s^{-1}}$`, `g` is in `$\mathrm{m\,s^{-2}}$`, `F*` and `G*` carry the corresponding flux units (`$\mathrm{m^2\,s^{-1}}$` for the `h` component and `$\mathrm{m^3\,s^{-2}}$` for the `hu` / `hv` components).

Dimensions: each state and flux is a 3-component vector. The component is 2D in the sense that it produces the `x`-direction flux `F*` and the `y`-direction flux `G*` from the respective interface states.

Boundary handling: out of scope. This `component` does not apply boundary conditions and assumes the caller provides valid interface states; boundary treatment is the responsibility of the boundary `component`.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`. The Rusanov flux is defined by
$$
F^{*}(U_L,U_R)=\frac{1}{2}\left(F(U_L)+F(U_R)\right)-\frac{1}{2}a_x\left(U_R-U_L\right)
$$
$$
G^{*}(U_B,U_T)=\frac{1}{2}\left(G(U_B)+G(U_T)\right)-\frac{1}{2}a_y\left(U_T-U_B\right)
$$
and the wave speed is
$$
a_x=\max(|u_L|+c_L,|u_R|+c_R),\quad a_y=\max(|v_B|+c_B,|v_T|+c_T),\quad c=\sqrt{gh}
$$

## 4. Failure conditions and constraints
Treat `h<=0` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_flux_2d_rusanov_p0__compute_flux`.

## 6. Prohibitions
Forbid automatic switching of the reconstruction order and the implicit application of a limiter.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_flux_2d_rusanov_p0/tests.md`, with `test_profile_version` of `0.1.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. `max` and `abs` are made explicit as non-differentiable operations.
