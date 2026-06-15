# Physical-validity judgment (most important requirement)

## Purpose
The pass/fail judgment of this framework is done by **physical validity**, not by bitwise agreement.
The judgment defined in this document constitutes the basis that forms the relevant `node`'s `self_verdict`.

## Basic rules
- The judgeable items differ per physics problem, but **always include the definable items in the verification scope**.
- Do not silently omit an item that cannot be defined; clearly state **N/A and the reason** in `diagnostics.json` / `verdict.json`.

## Required checks (when definable)
1. **CFL condition check**
- Evaluate the `CFL` number from the space-time discretization and the wave speed (or advection speed).
- Record the maximum `CFL`, the threshold, and whether there is a violation.
2. **Discrete conservation (all conserved quantities)**
- Verify the drift of quantities that should be conserved (mass, momentum, energy, etc.).
- State the error metric and tolerance per conserved quantity, and judge pass/fail individually.
3. **Discrete symmetry**
- Verify the symmetries that the initial conditions, boundary conditions, and equations have (reflection, rotation, translation, etc.).
- Define the symmetry-error norm and tolerance.
4. **Comparison with theory (linear problems, etc.)**
- Compare the theoretical amplification rate and the numerical amplification rate (and the phase error as needed).
- Record the error metric and judgment result for the representative mode group.

## Operations Rules
- For each test case, define "applicability, evaluation expression (or evaluation procedure), tolerance" in advance.
- In an `LLM`-using stage, confirm in the in-stage `verify` that the evaluation expressions and thresholds match `tests`.
- The `verifier` prioritizes, as far as possible, execution in a context independent of the `generator` (a separate session or a separate agent).
- When an isolated context cannot be secured due to execution-environment constraints, same-context execution is permitted, and the constraint reason is recorded in the corresponding `<stage>_meta.json` (`ir_meta.json` for `Compile`, `source_meta.json` for `Generate`, `validate_meta.json` for `Validate`).
- Whether failed attempts may be saved follows the `debug_mode` rule in `SPEC.md`.
- `verdict.json` has both the partial judgments of each item and the overall judgment, and makes the basis traceable.
- The overall judgment including dependencies is done in `aggregate_verdict.json`, and the judgment expressions of this document are not referenced directly.
