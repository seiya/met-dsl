# Controlled Spec: 2D `SSPRK2` update (component spec)

## 0. Meta information
- `spec_id`: `dynamics_shallow_water_time_update_2d_ssprk2`
- `spec_version`: `0.3.0`
- `status`: `controlled_draft`
- `spec_kind`: `component`
- `domain`: `dynamics`
- `family`: `shallow_water`

## 1. Responsibility and scope
This `component` is responsible for executing the time integration of the shallow water problem with `SSPRK2`.

## 2. input/output contract
The inputs are `U^n`, the interface-flux difference field `L_flux`, the bottom-topography source term field `S_b`, the bathymetry field `z_b`, `dt`, `dx`, and `dy`. The output is `U^{n+1}`.

`L_flux` and `S_b` are supplied as **fixed fields** by the caller; this component does **not** recompute them internally. `z_b` is **accepted at the boundary** for interface stability but is **inert at L0**: the output does not depend on it (it is reserved for higher-fidelity source-term coupling).

## 3. Operation definition
The published `operation` is `dynamics_shallow_water_time_update_2d_ssprk2__advance`. In the general continuous form, let $L_{flux}(U)$ be the interface-flux difference and $S_b(U,z_b)$ be the bottom-topography source term, and the update is
$$
U^{(1)}=U^n+\Delta t\left(L_{flux}(U^n)+S_b(U^n,z_b)\right)
$$
$$
U^{n+1}=\frac{1}{2}U^n+\frac{1}{2}\left(U^{(1)}+\Delta t\left(L_{flux}(U^{(1)})+S_b(U^{(1)},z_b)\right)\right)
$$

### 3.1 L0 realization (profile `dynamics_shallow_water_time_update_2d_ssprk2_l0`, this version)
At L0, `L_flux` and `S_b` are provided as fixed input fields and are **not** functions recomputed at the stage state. Each stage right-hand side is computed as
$$
rhs = L_{flux} + S_b
$$
from the supplied fields. The stage-RHS operation (`ssprk2_stage_rhs`) consumes **only** `L_flux` and `S_b` — it does **not** consume `U` or `z_b`. Both stages therefore use the identical RHS, and the two-stage SSPRK2 composition with weights $\tfrac12,\tfrac12$ reduces to the closed form
$$
U^{n+1} = U^n + \Delta t\,(L_{flux} + S_b).
$$
Per-stage re-evaluation of $L_{flux}(U)$ / $S_b(U,z_b)$ and `z_b` source coupling are **out of scope at L0** and deferred to a higher-fidelity profile.

Implementations **MUST NOT** introduce arithmetic no-ops (e.g. `0*U`, `0*z_b`) to reference unused inputs. The accepted-but-inert input (`z_b`) is kept as a live `intent(in)` dummy and referenced through a benign name binding (e.g. an `associate (unused_z_b => z_b); end associate` block) so it neither participates in the computation nor triggers a compiler unused-argument warning. Do **not** add an `! allow(...)` lint pragma for `z_b`: the project's Fortitude lint has no unused-argument/variable rule, so such a comment matches no diagnostic and is itself rejected (`FORT001` unknown-rule / `FORT002` unused-allow-comment). The `! allow(...)` idiom is reserved for real diagnostics such as the `implicit none`/F2008 `C003` conflict.

## 4. Failure conditions and constraints
Treat `dt<=0`, `dx<=0`, and `dy<=0` as invalid input and an error.

### 4.1 Invariants (L0)
- **zero-rhs invariance:** when `L_flux=0` and `S_b=0`, `U^{n+1}` equals `U^n`.
- **stage-weight consistency:** the two-stage composition applies weights `1/2` and `1/2` to `U^n` and the second-stage state.
- **frozen-field exactness:** with `L_flux` and `S_b` supplied as fixed fields, `U^{n+1} = U^n + dt*(L_flux + S_b)` (both stages use the identical RHS).
- **z_b invariance:** with `L_flux`, `S_b`, and `U^n` held fixed, varying `z_b` does not change `U^{n+1}` (`z_b` is inert at L0).
- **input guard:** when `dt<=0` or `dx<=0` or `dy<=0`, `guard_pass` is false and the update is rejected as invalid input.

## 5. Public API and compatibility
The only published `operation_id` is `dynamics_shallow_water_time_update_2d_ssprk2__advance`.

## 6. Prohibitions
Forbid automatic switching of the time-integration method.

## 7. Traceability
Require recording the adoption result in `component_catalog.yaml` and `case.resolved.yaml`.

## 8. tests reference
The corresponding `tests.md` is `spec/component/dynamics/shallow_water/dynamics_shallow_water_time_update_2d_ssprk2/tests.md`, with `test_profile_version` of `0.3.0`.

## 9. AD preparation information
`ad_readiness.enabled` is `true`. `ceil` (when used in the `dt` rule) is made explicit as a non-differentiable operation.
