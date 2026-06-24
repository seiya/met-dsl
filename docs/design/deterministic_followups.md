# Deterministic build/execute migration — known follow-up issues

The Build / Validate.execute in-process migration is complete and the original
Codex review findings are fixed (see git history). Two issues remain, both
**orthogonal to the migration** and surfaced only while chasing a fully-green
end-to-end run on `component/dynamics_advection_diffusion_boundary_1d_periodic_copy`.

## 1. reopen + build step_result guard (pre-existing machinery)

When a `Validate` failure routes a **retry to `Generate`** (e.g. an execute
content failure → `classify_failure` → `retry generate restart`, or a judge
`physics_fail`/`code` → Generate), `conduct()` calls `reopen_phase(from_phase=generate)`,
which invalidates the downstream `build`/`validate` checkpoints and step_results.
On the next `build`, `record-launch`'s guard
`_build_step_agents_missing_step_result` (tools/orchestration_runtime.py) fires:

```
record-launch: prior build agent(s) for <node>/build finished without a
step_result (<arids>); write it with `write-step-result` ... before launching
another build
```

i.e. the prior build child agent_runs remain but their step_results were
invalidated by the reopen, and the guard treats them as "finished without a
step_result". This is **pre-existing reopen/retry machinery** (the guard and
reopen predate the deterministic migration); it was simply never exercised
repeatably because a `Validate → Generate` reopen loop was rare. The deterministic
`execute → Generate` routing plus persistent generator nondeterminism (issue 2)
makes it reachable every few cycles.

Fix direction: `reopen_phase` should clear/backfill the prior build child agent
records (or the guard should ignore agents whose step_result was reopen-invalidated)
so a `Validate → Generate → build` reopen loop can re-launch build cleanly.

## 2. Generator Compile/Generate inconsistency (LLM quality)

The root trigger throughout: the LLM intermittently produces artifacts that
violate a cross-phase contract, which the gates correctly reject:

- **BIN naming** (fixed by adaptation): the generated `Makefile` commonly sets
  `BIN = $(SPEC)` (= `<spec_id>`, no `_runner`). Build/Validate.execute now derive
  the binary basename from the Makefile's BIN (`Conductor._resolve_exe_name`), so
  any value works — the rigid `<spec_id>_runner` gate was removed.
- **snapshot_index shape** (open): `Compile` sometimes declares
  `io_contract.raw_requirements.required_evidence.schema.time_shape_expr: [1]`
  while the generated runner writes `snapshot_index` as a scalar (shape `[]`).
  `post_execute` rejects the mismatch (`snapshot_index shape [] does not match
  declared time_shape_expr [1]`), failing `Validate.execute`. In a prior green run
  Compile chose `scalar` and the runner matched. This is generator nondeterminism,
  not a migration defect.

Fix direction: tighten the Compile/Generate prompts (or post_generate checks) so
the runner's `snapshot_index` shape always matches the IR's `time_shape_expr`
(scalar vs `[1]`), the same way the BIN naming was made robust.
