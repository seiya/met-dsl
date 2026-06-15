# Controlled Spec: 2D `SSPRK2` update (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_version`: `0.2.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible for executing the time integration of the shallow water problem with `SSPRK2`.

## 2. input/output contract
The input is `U^n`, the interface-flux difference, the bottom-topography source term `S_b`, `dt`, `dx`, and `dy`. The output is `U^{n+1}`.

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_time_update_2d_ssprk2__advance`. Here let $L_{flux}(U)$ be the interface-flux difference and $S_b(U,z_b)$ be the bottom-topography source term. The update is defined by
$$
U^{(1)}=U^n+\Delta t\left(L_{flux}(U^n)+S_b(U^n,z_b)\right)
$$
$$
U^{n+1}=\frac{1}{2}U^n+\frac{1}{2}\left(U^{(1)}+\Delta t\left(L_{flux}(U^{(1)})+S_b(U^{(1)},z_b)\right)\right)
$$

## 4. Failure conditions and constraints
Treat `dt<=0`, `dx<=0`, and `dy<=0` as invalid input and an error.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_time_update_2d_ssprk2__advance`.

## 6. Prohibitions
Forbid automatic switching of the time-integration method.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. AD preparation information
`ad_readiness.enabled` is `true`. `ceil` (when used in the `dt` rule) is made explicit as a non-differentiable operation.

## 9. tests reference
Place the corresponding `tests.md` in the same directory, with `test_profile_version` of `0.2.0`.
