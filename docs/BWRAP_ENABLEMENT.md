# bwrap leaf-sandboxing enablement runbook (one-time live verification)

Procedure to verify the leaf bwrap sandbox under a real `claude -p` run and then flip
`METDSL_CONDUCTOR_BWRAP` to **default-ON**. This is a one-time enablement gate, not a
recurring trial step.

- **Design / rationale (canonical):** `docs/design/deterministic_conductor.md` §Leaf
  sandboxing + §Open risks. Do not restate the design here — this file is only the
  operator procedure and the pass/fail criteria.
- **Why a live run is required:** `record-launch` already builds a per-arid bwrap profile
  and records `sandbox_enforced: true`, but `spawn_leaf` runs leaves unconfined while the
  flag is off, so the recorded invariant is not yet matched by reality. Only `claude
  --version` has been confirmed under the rendered profile; a full `claude -p` run
  (real auth, MCP `build-runtime` spawn, hooks firing, `--session-id` transcript, and —
  the highest risk — the **compile/build toolchain writing `.o`/`.mod`/exe inside the
  leaf's `write_roots`**) has not. This procedure closes that gap.

## 0. Preconditions

1. The host supports unprivileged user namespaces and has `bwrap` on `PATH`
   (`bwrap --version` works as the invoking user). WSL2 / some container hosts disable
   user namespaces — if so, enablement must wait for a host that supports it (the conductor
   will fail-closed there, which is the correct behavior, not a regression).
2. Claude backend preflight already passes (MCP `build-runtime` registered + tool
   permission granted): see `docs/RUNBOOK.md` §0-2. Run the normal preflight first.
3. **Run this standalone.** It is a billed, autonomous `--invoke-llm` orchestration; do
   not run it concurrently with other manual workflow activity or it will pollute the
   workspace-global baseline (the truth is `meta=pass` + `aggregate_verdict`; a polluted
   parallel run can false-fail). Use a clean workspace state.

## 1. Run one node end-to-end under the sandbox

Pick a small leaf component and run through `validate` so every phase — including the
high-risk **Build** — executes under bwrap:

```
! METDSL_CONDUCTOR_BWRAP=1 python3 tools/run_workflow.py \
    spec/component/dynamics/advection_diffusion/dynamics_advdiff_flux_1d_upwind_center2 \
    validate --llm claude
```

`run_workflow.py` does `base_env = dict(os.environ)`, so the shell-exported
`METDSL_CONDUCTOR_BWRAP=1` propagates to the conductor; `spawn_leaf` then wraps every leaf
in `render_bwrap_command`. (The `!` prefix runs it in this session so its output lands in
the conversation.)

## 2. Pass criteria

The run must reach `orchestration_meta.json` `status=pass` with a real
`aggregate_verdict` (not a sandbox/launch error). Concretely confirm all of:

| Check | How to confirm |
|---|---|
| Leaves actually ran sandboxed | each `agents/<arid>/dialogs/child.response.json` has `sandbox_enforced: true` **and** a `sandbox_command` starting with `bwrap`; the leaf produced a real reply (not an immediate launch error) |
| Real auth + `--session-id` transcript worked | `~/.claude/projects/<slug>/<session_id>.jsonl` exists and has assistant turns for each leaf (auth/config-home bind is functional) |
| MCP `build-runtime` spawned in-sandbox | the generate/build/validate leaves recorded `run_linter` / `compile_project` / `run_program` evidence (`mcp_command_log.jsonl` present, `ok:true`) |
| Hooks fired in-sandbox | the run completed without a `*_violation` due to a missing hook decision; gate-friction behavior is unchanged |
| **Build output landed in write_roots (highest risk)** | the **Build phase passed** — `compile_project` wrote `.o`/`.mod` to the per-run object dir and the exe to `binary/<binary_id>/bin/` with no `unauthorized_write_violation` / EROFS. This is the make-or-break check. |

`python3 tools/audit_orchestration.py <orchestration_id>` summarizes per-run cost and
status for a quick read.

## 3. If it fails

- **Build fails with a write/`Read-only file system`/`unauthorized_write_violation`
  error.** The build toolchain wrote outside the bwrap write scope. The fix is to add the
  offending path to the leaf's bwrap write scope: `build_bwrap_profile`
  (`tools/orchestration_runtime.py`) confines writes to the capability `write_roots`
  + `workspace/tmp/<arid>`. Identify the path from the error and ensure the Build phase's
  `write_roots` (or the `OBJDIR`/`BINDIR` overrides `Build` passes to `compile_project`)
  resolve under an authorized root. Re-run step 1 after the profile fix.
- **`SandboxError` / leaf raises before launching.** The host lacks a usable bwrap profile
  or user namespaces. Confirm precondition 0.1; the conductor failing closed here is
  correct — do not work around it by disabling the flag on an unsupported host.
- **Preflight rejects** with `sandbox_not_enforced`: expected only if a profile is missing;
  see `docs/RUNBOOK.md` §0-2 and the design doc.

## 4. After a clean pass — flip the default

Once step 2 passes on a representative node (ideally re-confirm on a node **with a
dependency**, so dependency `write_roots` are exercised too):

1. Change `WorkflowConductor._bwrap_enabled()` (`tools/workflow_conductor.py`) so the
   default is **on** — i.e. bwrap is enforced unless `METDSL_CONDUCTOR_BWRAP` is explicitly
   set to an off value (`off`/`0`/`false`). This makes the recorded
   `sandbox_enforced: true` invariant match reality (the design doc calls
   `METDSL_CONDUCTOR_BWRAP=off` the *temporary* divergence; default-on is the
   contract-honest end state).
2. Update `docs/design/deterministic_conductor.md` §Leaf sandboxing / §Open risks to record
   that the live run passed and the flag now defaults on (move it out of "opt-in" / "open
   risk").
3. Keep `METDSL_CONDUCTOR_BWRAP=off` as the documented escape hatch for an unsupported host
   (with the caveat that it runs unconfined despite the recorded invariant).
4. Run the existing conductor/bwrap unit tests; add a regression that a default
   (env-unset) conductor enforces the sandbox.

> The diff for step 1 is small (invert the default in one method) but **must not** be made
> before step 2 passes live — enabling on a host where Build writes outside `write_roots`
> would fail-close every run.

## 5. Rollback

Set `METDSL_CONDUCTOR_BWRAP=off` (after the default flip) to run unconfined on a host where
the sandbox can't launch. This is a temporary divergence (the leaf runs unconfined while
`record-launch` still records `sandbox_enforced: true`); prefer fixing the host or the
profile over running with the escape hatch long-term.
