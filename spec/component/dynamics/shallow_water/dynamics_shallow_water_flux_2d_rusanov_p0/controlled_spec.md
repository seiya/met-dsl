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
The input is the left/right states and bottom/top states of `U=[h,hu,hv]^T`, and the gravitational acceleration `g`. The output is `F*` and `G*`.

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

## 8. AD preparation information
`ad_readiness.enabled` is `true`. `max` and `abs` are made explicit as non-differentiable operations.

## 9. tests reference
Place the corresponding `tests.md` in the same directory, with `test_profile_version` of `0.1.0`.
