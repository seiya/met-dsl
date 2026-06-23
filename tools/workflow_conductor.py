"""Deterministic workflow conductor.

Drives the Compile -> Generate -> Build -> Validate phase/substep loop in plain
Python, calling the orchestration_runtime.py bookkeeping subcommands directly and
spawning each substep BODY as an isolated leaf LLM (``claude -p`` / ``codex exec``).

Motivation (see docs/design/deterministic_conductor.md): the legacy path uses an
LLM "orchestration agent" to drive a deterministic bookkeeping state machine. For
a trivial node the LLM makes essentially no decisions, yet every bookkeeping CLI
output accumulates in its context and is re-read every turn (cache_read grows
O(turns^2)). Moving the deterministic loop into Python removes the parent LLM's
turns, its ~70K static-protocol-doc resident load, and the per-turn accumulation;
the LLM is invoked only as a leaf for the judgement-bearing substeps
(generate/verify/judge) and, on an unclassifiable failure, a one-shot diagnostician.

This module is intentionally self-contained: it reuses the existing
orchestration_runtime.py subcommands (the stable CLI contract) and the
validate_pipeline_semantics validators rather than importing internals, so the
same guards fire as on the LLM path.

Status: M2 happy-path scaffolding. Failure routing (M3) and LLM escalation (M4)
are layered on top of the loop. The request-payload builder is validated against
real, working request.json artifacts in tools/tests/test_workflow_conductor.py.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_yaml(path: Path) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None

# --- phase / substep structure -------------------------------------------------

PHASE_ORDER: tuple[str, ...] = ("compile", "generate", "build", "validate")

# Ordered substeps per phase. Build is a single "step" agent (no substeps),
# represented as [None] so the loop body is uniform.
SUBSTEPS: dict[str, tuple[str | None, ...]] = {
    "compile": ("generate", "verify"),
    "generate": ("generate", "verify"),
    "build": (None,),
    "validate": ("execute", "judge"),
}

# Build is the only phase whose step_result executor is the (child) step agent;
# the substep-aware phases record the orchestration agent as executor.
SUBSTEP_AWARE_PHASES: frozenset[str] = frozenset({"compile", "generate", "validate"})

# Output basenames excluded from a producer substep's "all deliverables written"
# check: audit/process logs whose presence/placement is not a deliverable contract
# (the MCP command log placement in particular varies by build system).
_OPTIONAL_OUTPUT_BASENAMES: frozenset[str] = frozenset({
    "mcp_command_log.jsonl", "stdout.log", "stderr.log",
})


def child_agent_role(step: str) -> str:
    """The agent_role of the leaf child for a phase: build => step, else substep."""
    return "step" if step == "build" else "substep"


def phase_index(step: str) -> int:
    return PHASE_ORDER.index(step)


def phases_through(until_phase: str) -> tuple[str, ...]:
    return PHASE_ORDER[: phase_index(until_phase) + 1]


# --- deterministic failure-routing decision tables -----------------------------
#
# Canonical sources:
#   docs/workflow/phases/phase_03_build.md  (Build failure_category -> retry)
#   docs/workflow/phases/phase_04_validate.md  (Validate.judge failure_class x attribution)
# Kept as data so the conductor (and its unit tests) route deterministically.

# Build failure_category -> (retry_target_phase, repair_strategy)
BUILD_FAILURE_ROUTING: dict[str, tuple[str, str]] = {
    "compile_error": ("generate", "reuse"),
    "link_error": ("generate", "reuse"),
    "make_error": ("generate", "restart"),
    "dependency_violation": ("generate", "restart"),
    "validate_post_build_violation": ("generate", "restart"),
}

# Validate.judge (failure_class, attribution) -> routing action.
# Action is one of:
#   ("generate", strategy) | ("compile", "reopen") | ("validate", "re_execute")
#   ("fail_closed", None)  -> manual intervention (spec attribution)
VALIDATE_JUDGE_ROUTING: dict[tuple[str, str], tuple[str, str | None]] = {
    ("evidence_mismatch", "code"): ("generate", "reuse"),
    ("evidence_mismatch", "ir"): ("compile", "reopen"),
    ("evidence_mismatch", "evidence"): ("validate", "re_execute"),
    ("physics_fail", "code"): ("generate", "reuse"),
    ("physics_fail", "ir"): ("compile", "reopen"),
    ("physics_fail", "spec"): ("fail_closed", None),
    ("runtime_error", "code"): ("generate", "reuse"),
    ("structural_violation", "code"): ("generate", "reuse"),
    ("structural_violation", "ir"): ("compile", "reopen"),
}


# Bound the deterministic retry/reopen loop so a persistently-failing node cannot
# spin forever; matches the operator-observed ceiling of ~3 reopens.
MAX_ATTEMPTS_PER_PHASE = 3


@dataclass(frozen=True)
class RouteDecision:
    """Outcome of classifying a substep/phase result."""

    action: str  # advance | retry | reopen | fail_closed | escalate
    target_phase: str | None = None
    repair_strategy: str | None = None
    reason: str | None = None


class SandboxEnforcementError(RuntimeError):
    """Raised when bwrap enforcement is mandatory but a leaf cannot be sandboxed
    (no usable profile). Surfaced so the conductor terminalizes as `fail_closed` rather
    than a generic conductor error."""


def classify_build_failure(failure_category: str | None) -> RouteDecision:
    if not failure_category:
        return RouteDecision("escalate", reason="build_fail_no_category")
    routed = BUILD_FAILURE_ROUTING.get(failure_category)
    if routed is None:
        return RouteDecision("escalate", reason=f"build_unknown_category:{failure_category}")
    target, strategy = routed
    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                         reason=f"build_{failure_category}")


def classify_validate_judge(failure_class: str | None, attribution: str | None) -> RouteDecision:
    if failure_class == "pass":
        return RouteDecision("advance")
    if not failure_class or not attribution:
        return RouteDecision("escalate", reason="judge_missing_class_or_attribution")
    routed = VALIDATE_JUDGE_ROUTING.get((failure_class, attribution))
    if routed is None:
        return RouteDecision("escalate",
                             reason=f"judge_unrouted:{failure_class}/{attribution}")
    target, strategy = routed
    if target == "fail_closed":
        return RouteDecision("fail_closed", reason=f"judge_{failure_class}_spec")
    if target == "compile":
        return RouteDecision("reopen", target_phase="compile",
                             reason=f"judge_{failure_class}_ir")
    if target == "validate":
        return RouteDecision("retry", target_phase="validate", repair_strategy="re_execute",
                             reason=f"judge_{failure_class}_evidence")
    return RouteDecision("retry", target_phase=target, repair_strategy=strategy,
                         reason=f"judge_{failure_class}_{attribution}")


def classify_verify_severity(issue_severity: str | None, workflow_mode: str) -> RouteDecision:
    """dev-mode verify/judge severity gate: major|critical => fail, minor => retry."""
    sev = (issue_severity or "none").lower()
    if sev in ("none", ""):
        return RouteDecision("advance")
    if workflow_mode == "dev" and sev in ("major", "critical"):
        return RouteDecision("fail_closed", reason=f"dev_verify_{sev}")
    if sev == "minor":
        return RouteDecision("retry", reason="verify_minor")
    return RouteDecision("escalate", reason=f"verify_severity_{sev}")


# --- LLM diagnostician (escalation for unclassifiable failures) -----------------

_DIRECTIVE_SCHEMA = (
    'Output EXACTLY ONE JSON object as the FINAL line, with keys:\n'
    '- "action": "retry" | "reopen" | "fail_closed"\n'
    '- "target_phase": "compile" | "generate" | "build" | "validate" | null\n'
    '- "repair_strategy": "reuse" | "restart" | "re_execute" | null\n'
    '- "reason": short string\n'
    'Routing guidance: code defect -> action=retry target_phase=generate; IR defect -> '
    'action=reopen target_phase=compile; missing/insufficient evidence -> action=retry '
    'target_phase=validate repair_strategy=re_execute; spec defect or genuinely '
    'unrecoverable -> action=fail_closed.'
)


def _diagnosis_prompt(node_key: str, phase: str, failed_arids: list[str],
                      context: dict[str, Any], workflow_mode: str) -> str:
    ctx_json = json.dumps(context, indent=1, ensure_ascii=False)[:6000]
    return (
        "You are a workflow failure diagnostician. Read-only, one shot: reason over "
        "the artifacts below and emit a single routing directive. Do NOT write files "
        "or call tools.\n\n"
        f"node_key: {node_key}\n"
        f"failed phase: {phase}\n"
        f"workflow_mode: {workflow_mode}\n"
        f"failed substep agent_run_ids: {failed_arids}\n\n"
        f"failure artifacts (JSON):\n{ctx_json}\n\n"
        f"{_DIRECTIVE_SCHEMA}\n"
    )


def _last_json_object(text: str) -> Any:
    """Return the last balanced top-level {...} that parses as JSON, or None."""
    best = None
    depth = 0
    start: int | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    best = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    pass
                start = None
    return best


def _parse_directive(stdout: str) -> RouteDecision | None:
    """Parse + validate the diagnostician's JSON directive into a RouteDecision.
    Returns None on any malformed/out-of-vocabulary directive (caller fails closed)."""
    obj = _last_json_object(stdout or "")
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    if action not in ("retry", "reopen", "fail_closed"):
        return None
    target = obj.get("target_phase")
    if target is not None and target not in PHASE_ORDER:
        return None
    strategy = obj.get("repair_strategy")
    if strategy not in (None, "reuse", "restart", "re_execute"):
        strategy = None
    reason = str(obj.get("reason") or "diagnostician")[:120]
    if action == "fail_closed":
        return RouteDecision("fail_closed", reason=reason)
    if action == "reopen":
        if target is None:
            return None
        return RouteDecision("reopen", target_phase=target, reason=reason)
    return RouteDecision("retry", target_phase=target, repair_strategy=strategy, reason=reason)


# --- node_key / path derivation ------------------------------------------------


def node_key_safe(node_key: str) -> str:
    """component/spec_id@1.0.0 -> component__spec_id__1.0.0."""
    kind_rest, _, version = node_key.partition("@")
    kind, _, spec_id = kind_rest.partition("/")
    return f"{kind}__{spec_id}__{version}"


def spec_id_of(node_key: str) -> str:
    kind_rest, _, _ = node_key.partition("@")
    _, _, spec_id = kind_rest.partition("/")
    return spec_id


@dataclass
class NodeRefs:
    """Resolved workspace references for a node + its reserved ids.

    Mutable: a retry/reopen that re-runs a producing phase allocates a fresh
    producer id (ir/source/binary/run) so it never overwrites a prior attempt's
    artifacts; the conductor updates the relevant field in place.
    """

    node_key: str
    spec_path: str  # spec/<kind>/<domain>/<family>/<spec_id>
    ir_id: str
    pipeline_id: str
    source_id: str | None = None
    binary_id: str | None = None
    run_id: str | None = None
    source_binary_id: str | None = None

    @property
    def safe(self) -> str:
        return node_key_safe(self.node_key)

    @property
    def spec_id(self) -> str:
        return spec_id_of(self.node_key)

    @property
    def ir_ref(self) -> str:
        return f"workspace/ir/{self.safe}/{self.ir_id}"

    @property
    def pipeline_ref(self) -> str:
        return f"workspace/pipelines/{self.safe}/{self.pipeline_id}"

    def source_dir(self, source_id: str | None = None) -> str:
        return f"{self.pipeline_ref}/source/{source_id or self.source_id}"

    def binary_dir(self, binary_id: str | None = None) -> str:
        return f"{self.pipeline_ref}/binary/{binary_id or self.binary_id}"

    def run_node_dir(self, run_id: str | None = None) -> str:
        return f"{self.pipeline_ref}/runs/{run_id or self.run_id}/{self.safe}"


# --- launch-request payload builder -------------------------------------------
#
# Reproduces, deterministically, the request payload the LLM orchestration agent
# assembles by following references/launch_prompts.md. Validated field-for-field
# against real working launches/*.request.json (test_workflow_conductor.py).
# NOTE: `launch_prompt_full` is intentionally OMITTED so record-launch renders the
# canonical prompt and returns it as `launch_prompt_text` (launch_prompts.md template).

# Universal child-contract docs every substep must read. docs/AGENT_CONTRACT.md is
# the canonical child-readable contract; docs/ORCHESTRATION.md (orchestrator/conductor
# design spec) is intentionally excluded — no substep reads it. record-launch's
# _workflow_contract_refs_for_launch keeps this aligned on the runtime side.
_DOC_CORE = ("docs/workflow/WORKFLOW_CORE.md", "docs/AGENT_CONTRACT.md")
_PHASE_DOC = {
    "compile": "docs/workflow/phases/phase_01_compile.md",
    "generate": "docs/workflow/phases/phase_02_generate.md",
    "build": "docs/workflow/phases/phase_03_build.md",
    "validate": "docs/workflow/phases/phase_04_validate.md",
}


def _skill_name(step: str, substep: str | None) -> str:
    return f"workflow-{step}" if substep is None else f"workflow-{step}-{substep}"


def build_launch_request(
    refs: NodeRefs,
    *,
    step: str,
    substep: str | None,
    orchestration_id: str,
    orchestration_agent_run_id: str,
    child_agent_run_id: str,
    agent_model: str,
    workflow_mode: str,
    case_ids: tuple[str, ...] = (),
    repair: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Construct the record-launch --request-json payload for one substep.

    case_ids is required for validate.execute (per-case raw/state_snapshots paths).
    repair carries issue_severity/repair_strategy/repair_target_agent_run_id/
    repair_reason on a retry (defaults to the literal "none" the templates use).
    """
    spec = refs.spec_path
    skill = _skill_name(step, substep)
    role = child_agent_role(step)
    rep = {
        "issue_severity": "none",
        "repair_strategy": "none",
        "repair_target_agent_run_id": "none",
        "repair_reason": "none",
    }
    if repair:
        rep.update(repair)

    req: dict[str, Any] = {
        "agent_role": role,
        "node_key": refs.node_key,
        "step": step,
        "orchestration_id": orchestration_id,
        "agent_run_id": child_agent_run_id,
        "parent_agent_run_id": orchestration_agent_run_id,
        "agent_model": agent_model,
        "workflow_mode": workflow_mode,
        "ir_ref": refs.ir_ref,
        "pipeline_ref": refs.pipeline_ref,
        "skill_name": skill,
        "skill_ref": f"skills/{skill}/SKILL.md",
    }
    if substep is not None:
        req["substep"] = substep

    must_read: list[str] = [f"skills/{skill}/SKILL.md", *_DOC_CORE, _PHASE_DOC[step]]

    if step == "compile":
        req["dependency_ref"] = f"{spec}/deps.yaml"
        must_read += [
            f"{refs.ir_ref}/spec.ir.yaml" if substep == "verify" else None,
            f"{spec}/controlled_spec.md",
            f"{spec}/tests.md",
            f"{spec}/deps.yaml",
        ]
        must_read = [m for m in must_read if m]
        req["allowed_output_paths"] = [
            f"{refs.ir_ref}/spec.ir.yaml",
            f"{refs.ir_ref}/ir_meta.json",
        ]
    elif step == "generate":
        req["source_id"] = refs.source_id
        req["dependency_ref"] = refs.ir_ref
        src = refs.source_dir()
        if substep == "generate":
            must_read += [
                _PHASE_DOC["build"],
                "docs/workflow/MCP_COMMAND_LOG_PLACEMENT.md",
                # The runner emits JSON; PERFORMANCE_DIAGNOSTICS §6 pins the safe
                # numeric/descriptor forms that post_generate gates on. Declaring it
                # up front avoids the agent discovering the need mid-run (an extra
                # exploration turn observed in audits).
                "docs/PERFORMANCE_DIAGNOSTICS.md",
                f"{refs.ir_ref}/spec.ir.yaml",
                # controlled_spec.md is intentionally NOT must-read here: phase_02
                # §2-1 forbids Generate.generate from taking controlled_spec.md as
                # input (re-introducing controlled_spec-derived info is a fail), so
                # the requirement composition is read from spec.ir.yaml.algorithm.
                # tests.md stays (used for case_id coverage).
                f"{spec}/tests.md",
            ]
            # lineage.json is authored host-side by the conductor (_write_lineage), not by
            # the leaf — it sits at the pipeline root which must stay non-writable to the
            # sandboxed leaf. So it is NOT in the leaf's allowed_output_paths.
            req["allowed_output_paths"] = [
                f"{src}/src/{refs.spec_id}_model.f90",
                f"{src}/src/{refs.spec_id}_runner.f90",
                f"{src}/src/Makefile",
                f"{src}/src/mcp_command_log.jsonl",
                f"{src}/source_meta.json",
            ]
        else:  # verify
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml",
                f"{src}/source_meta.json",
                f"{spec}/controlled_spec.md",
                f"{spec}/tests.md",
                f"{refs.pipeline_ref}/lineage.json",
            ]
            req["allowed_output_paths"] = [
                f"{src}/src/{refs.spec_id}_model.f90",
                f"{src}/src/{refs.spec_id}_runner.f90",
                f"{src}/src/Makefile",
                f"{src}/src/mcp_command_log.jsonl",
                f"{src}/source_meta.json",
            ]
    elif step == "build":
        req["source_id"] = refs.source_id
        req["binary_id"] = refs.binary_id
        req["dependency_ref"] = refs.pipeline_ref
        must_read += [
            f"{refs.ir_ref}/spec.ir.yaml",
            f"{refs.source_dir()}/source_meta.json",
        ]
        bdir = refs.binary_dir()
        req["allowed_output_paths"] = [
            f"{bdir}/bin/{refs.spec_id}_runner",
            f"{bdir}/binary_meta.json",
            f"{bdir}/mcp_command_log.jsonl",
        ]
    elif step == "validate":
        req["run_id"] = refs.run_id
        req["dependency_ref"] = refs.pipeline_ref
        rundir = refs.run_node_dir()
        if substep == "execute":
            req["source_id"] = refs.source_id
            req["source_binary_id"] = refs.source_binary_id
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml",
                f"{refs.source_dir()}/source_meta.json",
                f"{refs.binary_dir(refs.source_binary_id)}/binary_meta.json",
            ]
            outs = [
                f"{rundir}/mcp_command_log.jsonl",
                f"{rundir}/diagnostics.json",
                f"{rundir}/perf.json",
                f"{rundir}/trial_meta.json",
                f"{rundir}/quality_check.json",
                f"{rundir}/raw/metrics_basis.json",
            ]
            for cid in case_ids:
                outs.append(f"{rundir}/raw/state_snapshots/{cid}.json")
            outs += [
                f"{rundir}/raw/state_snapshots/snapshot_schema.json",
                f"{rundir}/stdout.log",
                f"{rundir}/stderr.log",
                f"{refs.source_dir()}/src/mcp_command_log.jsonl",
            ]
            req["allowed_output_paths"] = outs
        else:  # judge
            must_read += [
                f"{refs.ir_ref}/spec.ir.yaml",
                f"{refs.source_dir()}/source_meta.json",
                f"{refs.binary_dir()}/binary_meta.json",
                f"{spec}/tests.md",
            ]
            req["allowed_output_paths"] = [
                f"{rundir}/semantic_review.json",
                f"{rundir}/verdict.json",
                f"{rundir}/aggregate_verdict.json",
                f"{rundir}/summary.json",
                f"{rundir}/validate_meta.json",
            ]
    else:  # pragma: no cover - guarded by SUBSTEPS keys
        raise ValueError(f"unknown step: {step}")

    req["skill_must_read_refs"] = ",".join(must_read)
    req.update(rep)
    return req


# --- runtime CLI + leaf spawn primitives --------------------------------------


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class Conductor:
    """Holds invariant context and the primitive operations of the loop."""

    repo_root: Path
    orchestration_id: str
    orchestration_agent_run_id: str
    backend: str
    env: dict[str, str]
    agent_model: str = "claude-opus-4-8"
    workflow_mode: str = "dev"
    # The resolved backend command (may be a wrapper with flags, e.g. from
    # --llm-command); empty falls back to the bare backend name.
    llm_command: str = ""

    def emit(self, event: str, **fields: Any) -> None:
        """Write one JSONL info event to stdout (the conductor runs in-process
        under run_workflow.py, so these join its node-level event stream)."""
        payload = {
            "status": "info",
            "event": event,
            "orchestration_id": self.orchestration_id,
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def runtime(self, args: list[str]) -> dict[str, Any]:
        """Call an orchestration_runtime.py subcommand; return parsed JSON stdout."""
        proc = subprocess.run(
            ["python3", "tools/orchestration_runtime.py", *args],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}"
            raise RuntimeError(f"runtime {args[0]} failed: {detail}")
        out = proc.stdout.strip()
        return json.loads(out) if out else {}

    def new_agent_run_id(self) -> str:
        proc = subprocess.run(
            ["python3", "tools/new_agent_run_id.py"],
            cwd=self.repo_root, env=self.env, text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"new_agent_run_id failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _reuse_resume_enabled(self) -> bool:
        """Opt-in (default off) for minor-fix reuse session resume (claude only).
        Off by default until verified by a live integration run; toggled via env
        `METDSL_CONDUCTOR_REUSE_RESUME`."""
        return str(self.env.get("METDSL_CONDUCTOR_REUSE_RESUME", "")).strip().lower() in {
            "1", "true", "yes",
        }

    def leaf_command(
        self,
        prompt_text: str,
        *,
        session_id: str | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        """Headless command to run one substep body as an isolated leaf agent.
        Honors a custom llm_command (wrapper + flags) so the conductor launches the
        same executable/model as the configured backend, not a hard-coded binary.

        For the claude backend, `session_id` pins the leaf's Claude Code session id
        to its `agent_run_id` (so the per-arid transcript is addressable and a later
        repair can `--resume` it). `resume_session_id` (claude only) resumes a prior
        leaf's session for context inheritance on a minor-fix `repair_strategy=reuse`,
        forked into the new session so the prior transcript is not mutated. Guards key
        on the active_child marker (= the new arid), not the session, so the resumed
        repair is still evaluated against its own manifest."""
        base = shlex.split(self.llm_command) if self.llm_command.strip() else [self.backend]
        if self.backend == "claude":
            # `-p` runs non-interactively; the committed .claude/settings.json supplies
            # MCP build-runtime registration + permission grants (see preflight gate).
            flags: list[str] = []
            if resume_session_id:
                flags += ["--resume", resume_session_id, "--fork-session"]
            if session_id:
                flags += ["--session-id", session_id]
            return [*base, *flags, "-p", prompt_text]
        if self.backend == "codex":
            return [*base, "exec", prompt_text]
        raise ValueError(f"unsupported backend for leaf spawn: {self.backend}")

    def _bwrap_enabled(self) -> bool:
        """bwrap leaf sandboxing is unconditionally MANDATORY (Phase-2; Linux+bwrap
        only). The FS-diff write-authorization model (`_validate_actual_write_paths`
        authorizes a leaf write purely by write_roots containment) is only sound while
        bwrap actually confines each leaf to its write_roots, so there is no opt-out: a
        host that cannot sandbox the leaf fails closed at launch rather than running
        unconfined (an unconfined leaf + FS-diff would authorize writes anywhere). The
        method is retained as a single seam for the call sites; it always returns True."""
        return True

    def _sandbox_profile_for(self, child_arid: str) -> dict[str, Any] | None:
        """The bwrap profile record-launch wrote for this child, or None."""
        path = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                / "sandbox_profiles" / f"{child_arid}.json")
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return doc if isinstance(doc, dict) else None

    def _readonly_sandbox_profile(self) -> dict[str, Any]:
        """A read-only bwrap profile for a leaf with no record-launch (the failure
        diagnostician): repo read-only, no write_roots, tmp-only scratch + backend
        auth/session home + the hooks/audit bookkeeping dirs. Raises
        SandboxEnforcementError if the host cannot build the profile (so the caller can
        fail closed instead of crashing or launching unconfined)."""
        from tools.orchestration_runtime import build_readonly_bwrap_profile
        base = shlex.split(self.llm_command) if self.llm_command.strip() else [self.backend]
        backend_command = base[0] if base else self.backend
        try:
            return build_readonly_bwrap_profile(
                repo_root=self.repo_root,
                orchestration_id=self.orchestration_id,
                agent_run_id=self.orchestration_agent_run_id,
                backend_command=backend_command,
                backend_type=self.backend,
            )
        except (ValueError, OSError) as exc:
            raise SandboxEnforcementError(
                f"read-only diagnostician sandbox profile unavailable: {exc}") from exc

    def spawn_leaf(
        self,
        prompt_text: str,
        child_env: dict[str, str],
        *,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        child_arid: str | None = None,
        profile: dict[str, Any] | None = None,
    ) -> ProcResult:
        argv = self.leaf_command(
            prompt_text, session_id=session_id, resume_session_id=resume_session_id)
        # Wrap the leaf in the bwrap sandbox that record-launch already built (repo
        # read-only; writes confined to the child's write_roots + workspace/tmp).
        # record-launch records sandbox_enforced=True for every backend, so applying it
        # here makes that record true (the conductor leaf is otherwise unconfined).
        # Applies to both claude and codex — both get a profile at launch. A caller may
        # pass an explicit `profile` for a leaf that has no record-launch profile keyed
        # by child_arid (the read-only diagnostician; see escalate()).
        if self._bwrap_enabled():
            # Fail closed: enforcement is mandatory and record-launch records
            # sandbox_enforced=true, so ANY leaf without a usable profile — a missing/
            # invalid one (older orchestration resumed, corrupted/deleted file) or a
            # caller that supplies neither an explicit profile nor a child_arid — must
            # NOT silently fall back to an unconfined launch.
            if profile is None:
                profile = self._sandbox_profile_for(child_arid) if child_arid else None
            if profile is None:
                raise SandboxEnforcementError(
                    "bwrap enforcement is mandatory but no usable sandbox profile is "
                    f"available for this leaf (child_arid={child_arid!r}); refusing to "
                    "launch unconfined (fail-closed)")
            from tools.orchestration_runtime import render_bwrap_command
            try:
                argv = render_bwrap_command(profile=profile, command_argv=argv)
            except ValueError as exc:
                # A structurally invalid/corrupted profile (missing repo_root/tmp_dir,
                # bad file pin, …) must also fail closed as a sandbox error, not bubble
                # up as a generic conductor error.
                raise SandboxEnforcementError(
                    f"sandbox profile for {child_arid} is invalid: {exc}") from exc
        proc = subprocess.run(
            argv, cwd=self.repo_root, env=child_env, text=True, capture_output=True, check=False,
        )
        return ProcResult(proc.returncode, proc.stdout, proc.stderr)

    def read_parent_return_token(self, child_arid: str) -> str:
        path = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                / "launches" / f"{child_arid}.parent_return_token")
        return path.read_text(encoding="utf-8").strip()

    # -- bookkeeping subcommand wrappers --------------------------------------

    def _oid_args(self) -> list[str]:
        return ["--repo-root", ".", "--orchestration-id", self.orchestration_id]

    def record_launch(self, child_arid: str, request: dict[str, Any]) -> dict[str, Any]:
        response = {
            "agent_run_id": child_arid,
            "agent_session_id": child_arid,
            "started_at": _iso_now(),
            "backend": self.backend,
        }
        return self.runtime([
            "record-launch", *self._oid_args(),
            "--parent-agent-run-id", self.orchestration_agent_run_id,
            "--child-agent-run-id", child_arid,
            "--request-json", json.dumps(request),
            "--response-json", json.dumps(response),
        ])

    def finalize_child(self, child_arid: str, return_token: str, reply_text: str,
                       agent_run_json: dict[str, Any]) -> dict[str, Any]:
        return self.runtime([
            "finalize-child", *self._oid_args(),
            "--agent-run-id", child_arid,
            "--return-token", return_token,
            "--reply-text", reply_text,
            "--agent-run-json", json.dumps(agent_run_json),
        ])

    def _write_lineage(self, refs: NodeRefs) -> None:
        """Author/refresh the pipeline `lineage.json` host-side (runtime-owned).

        `lineage.json` lives at the pipeline root, which must stay non-writable to the
        sandboxed leaf (the root contains the future source/binary/runs areas, and the
        Edit/Write tools' atomic temp-sibling+rename would need the whole root writable).
        So the conductor — which runs unconfined and already holds every id — writes it,
        matching `docs/WORKSPACE_LAYOUT.md` ("added by each phase ... runtime"). Called at
        each pipeline phase start after the producer id is reserved; idempotent, it
        accumulates the stage ids (source_id at generate, +binary_id at build, +run_id at
        validate). `direct_dependency_status` maps each direct dependency to "ready" — the
        conductor only reaches here once `workflow_launch_check` confirmed readiness."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml") or {}
        dep = ir.get("dependency") if isinstance(ir, dict) else None
        direct_deps = dep.get("direct_deps") if isinstance(dep, dict) else None
        status: dict[str, str] = {}
        for d in direct_deps or []:
            nk = d.get("node_key") if isinstance(d, dict) else d
            if isinstance(nk, str) and nk.strip():
                status[nk.strip()] = "ready"
        lineage = {
            "node_key": refs.node_key,
            "spec_ref": refs.spec_path,
            "ir_ref": refs.ir_ref,
            "dependency_ref": refs.ir_ref,
            "pipeline_id": refs.pipeline_id,
            "source_id": refs.source_id,
            "binary_id": refs.binary_id,
            "run_id": refs.run_id,
            "direct_dependency_status": status,
        }
        path = self.repo_root / refs.pipeline_ref / "lineage.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(lineage, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def write_step_result(self, node_key: str, step: str, executor_arid: str,
                          result: dict[str, Any]) -> dict[str, Any]:
        return self.runtime([
            "write-step-result", *self._oid_args(),
            "--node-key", node_key, "--step", step,
            "--agent-run-id", executor_arid,
            "--result-json", json.dumps(result),
        ])

    def check_step_completed(self, node_key: str, step: str) -> dict[str, Any] | None:
        out = self.runtime([
            "check-step-completed", *self._oid_args(),
            "--node-key", node_key, "--step", step,
        ])
        return out if isinstance(out, dict) and out.get("integrity") == "ok" else None

    def workflow_launch_check(self, node_key: str, step: str, require_child_agent: str) -> dict[str, Any]:
        out = self.runtime([
            "workflow-launch-check", *self._oid_args(),
            "--node-key", node_key, "--step", step,
            "--require-child-agent", require_child_agent,
            "--backend", self.backend,
        ])
        if out.get("status") != "pass":
            raise RuntimeError(
                f"workflow-launch-check blocked {step}: {out.get('reason_code')} {out.get('reason_detail')}")
        return out

    def reserve_root(self, node_key: str, step: str, reserved_id: str, by_arid: str) -> dict[str, Any]:
        return self.runtime([
            "reserve-phase-root", *self._oid_args(),
            "--node-key", node_key, "--step", step,
            "--reserved-id", reserved_id,
            "--reserved-by-agent-run-id", by_arid,
        ])

    def set_status(self, status: str, reason_code: str | None = None,
                   reason_detail: str | None = None) -> dict[str, Any]:
        args = ["set-status", *self._oid_args(), "--status", status]
        if reason_code:
            args += ["--reason-code", reason_code]
        if reason_detail:
            args += ["--reason-detail", reason_detail]
        return self.runtime(args)

    def reopen_phase(self, node_key: str, from_phase: str, trigger_arid: str,
                     reason: str) -> dict[str, Any]:
        return self.runtime([
            "reopen-phase", *self._oid_args(),
            "--node-key", node_key, "--from-phase", from_phase,
            "--trigger-agent-run-id", trigger_arid, "--reason", reason,
        ])

    # -- substep outcome (deterministic, reads canonical artifacts) -----------

    def _child_env(self, child_arid: str) -> dict[str, str]:
        env = dict(self.env)
        env["METDSL_ORCHESTRATION_ID"] = self.orchestration_id
        env["TMPDIR"] = str(self.repo_root / "workspace" / "tmp" / child_arid)
        return env

    def read_case_ids(self, refs: NodeRefs) -> tuple[str, ...]:
        """Per-case ids from the compiled IR (for validate.execute output paths)."""
        ir = _read_yaml(self.repo_root / refs.ir_ref / "spec.ir.yaml")
        if ir is None:
            return ()
        case = ir.get("case") if isinstance(ir, dict) else None
        tcs = case.get("test_case_set") if isinstance(case, dict) else None
        if not isinstance(tcs, list):
            return ()
        return tuple(
            sorted(c["case_id"] for c in tcs if isinstance(c, dict) and c.get("case_id"))
        )

    def determine_substep_status(self, refs: NodeRefs, phase: str, substep: str | None,
                                 allowed_output_paths: list[str],
                                 min_mtime: float = 0.0) -> tuple[str, list[str]]:
        """Deterministically classify a substep from the artifacts it produced.

        verify/judge read the canonical status field; the producing substeps
        (generate/execute/build) pass when their deliverables exist AND were
        (re)written during this attempt (mtime >= min_mtime), so a retry/reopen
        that reuses an artifact directory cannot pass on a prior attempt's stale
        outputs. The downstream verify/judge then certifies the content.
        """
        output_refs = [p for p in allowed_output_paths if (self.repo_root / p).exists()]
        if phase == "compile" and substep == "verify":
            meta = _read_json(self.repo_root / refs.ir_ref / "ir_meta.json") or {}
            status = "pass" if meta.get("verification_status") == "pass" else "fail"
        elif phase == "generate" and substep == "verify":
            meta = _read_json(self.repo_root / refs.source_dir() / "source_meta.json") or {}
            status = "pass" if meta.get("verification_status") == "pass" else "fail"
        elif phase == "validate" and substep == "judge":
            agg = _read_json(self.repo_root / refs.run_node_dir() / "aggregate_verdict.json") or {}
            status = "pass" if str(agg.get("aggregate_verdict") or agg.get("overall")) in ("pass", "xfail") else "fail"
        else:
            # producing substep (compile.generate / generate.generate / build /
            # validate.execute): it passes only when it wrote ALL of its
            # DELIVERABLE outputs — not the phase required_outputs (for validate
            # those are the judge's), and not the audit/process logs whose
            # placement varies by build system (e.g. a Make build's
            # mcp_command_log lands under source/<src>/src, not binary/<bin>) —
            # AND each was authored in this attempt (mtime >= min_mtime) so a retry
            # never passes on stale files. The downstream verify/judge certifies it.
            required = [p for p in allowed_output_paths
                        if Path(p).name not in _OPTIONAL_OUTPUT_BASENAMES]
            present = [p for p in required if (self.repo_root / p).exists()]
            all_written = len(present) == len(required)
            fresh = all((self.repo_root / p).stat().st_mtime >= min_mtime for p in present)
            status = "pass" if all_written and fresh else "fail"
        return status, output_refs

    def _agent_run_json(self, refs: NodeRefs, phase: str, substep: str | None,
                        child_arid: str, status: str,
                        output_refs: list[str],
                        result_summary: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_run_id": child_arid,
            "agent_role": child_agent_role(phase),
            "agent_backend": self.backend,
            "status": status,
            "started_at": _iso_now(),
            "finished_at": _iso_now(),
            "agent_session_id": child_arid,
            "context_id": child_arid,
            "context_isolated": True,
            "node_key": refs.node_key,
            # record_agent_run does NOT infer step/substep from the launch request
            # (it backfills only parent_agent_run_id / agent_model); the pre_judge
            # substep-linkage check needs them, so supply them here.
            "step": phase,
        }
        if substep is not None:
            payload["substep"] = substep
        if status == "pass":
            payload["output_refs"] = output_refs
        elif result_summary and result_summary.strip():
            # A failed substep carries no output_refs, so _validate_agent_summary_text
            # requires a summary/reason token; surface the leaf failure reason here so
            # finalize-child produces a valid agent.summary.txt instead of crashing.
            payload["result_summary"] = result_summary.strip()
        return payload

    def run_substep(self, refs: NodeRefs, phase: str, substep: str | None,
                    repair: dict[str, str] | None = None) -> SubstepOutcome:
        child_arid = self.new_agent_run_id()
        request = build_launch_request(
            refs, step=phase, substep=substep,
            orchestration_id=self.orchestration_id,
            orchestration_agent_run_id=self.orchestration_agent_run_id,
            child_agent_run_id=child_arid,
            agent_model=self.agent_model, workflow_mode=self.workflow_mode,
            case_ids=self.read_case_ids(refs) if phase == "validate" else (),
            repair=repair,
        )
        rec = self.record_launch(child_arid, request)
        # Minor-fix reuse (claude only, opt-in): resume the producer leaf's session so
        # the repair inherits its context (and design intent) instead of cold-starting.
        # restart stays cold (no resume) to avoid anchoring on the defective reasoning.
        # The producer's session id == its agent_run_id (pinned via --session-id at its
        # own launch), so it is addressable by repair_target_agent_run_id.
        resume_session_id: str | None = None
        if (
            self.backend == "claude"
            and self._reuse_resume_enabled()
            and repair is not None
            and repair.get("repair_strategy") == "reuse"
        ):
            target = str(repair.get("repair_target_agent_run_id") or "").strip()
            if target and target != "none":
                resume_session_id = target
        # Capture the launch instant so a producer substep only passes on outputs
        # (re)written during this child window, not stale files from a prior attempt.
        launched_at = time.time()
        proc = self.spawn_leaf(
            rec["launch_prompt_text"], self._child_env(child_arid),
            session_id=child_arid, resume_session_id=resume_session_id,
            child_arid=child_arid)
        # Persist the leaf's verbatim stdout/stderr durably (every run, pass or
        # fail) so the LLM's actual response — including an infra failure message
        # such as a token-limit abort — is never lost. These conductor-side writes
        # land in the child's bookkeeping dir (not its allowed_output_paths) and are
        # not hook-guarded, so they don't trip the output-manifest guard.
        self._persist_leaf_output(child_arid, proc)
        token = self.read_parent_return_token(child_arid)
        status, output_refs = self.determine_substep_status(
            refs, phase, substep, request["allowed_output_paths"], min_mtime=launched_at)
        # A nonzero leaf exit (crash / transport failure) fails the substep even if
        # the expected artifacts happen to exist (e.g. stale outputs from a prior
        # attempt) — the process return code gates artifact-based success.
        # EVERY non-pass status must carry a result_summary: a failed payload has no
        # output_refs, so without one _validate_agent_summary_text rejects the
        # auto-generated agent.summary.txt and finalize-child crashes. A nonzero exit
        # uses the leaf's stderr tail; a returncode-0 content failure (verify/judge
        # fail, missing deliverable) uses a generic tag — the detailed diagnostics
        # live in the canonical artifacts (ir_meta/verdict.json) that classify_failure
        # reads for routing.
        result_summary: str | None = None
        if proc.returncode != 0:
            status = "fail"
            result_summary = self._leaf_failure_summary(proc)
        elif status != "pass":
            result_summary = f"substep_fail: {phase}" + (f".{substep}" if substep else "")
        reply = f"status: {status}\noutput_refs: {len(output_refs)}\nleaf rc={proc.returncode}"
        if result_summary:
            reply += f"\nresult_summary: {result_summary}"
        self.finalize_child(
            child_arid, token, reply,
            self._agent_run_json(refs, phase, substep, child_arid, status,
                                 output_refs, result_summary))
        return SubstepOutcome(child_arid, status, output_refs, proc.returncode)

    def _persist_leaf_output(self, child_arid: str, proc: ProcResult,
                             prefix: str = "leaf") -> None:
        """Write the leaf process stdout/stderr to the child's dialogs dir."""
        dialogs = (self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
                   / "agents" / child_arid / "dialogs")
        dialogs.mkdir(parents=True, exist_ok=True)
        (dialogs / f"{prefix}.stdout.log").write_text(proc.stdout or "", encoding="utf-8")
        (dialogs / f"{prefix}.stderr.log").write_text(proc.stderr or "", encoding="utf-8")

    @staticmethod
    def _leaf_failure_summary(proc: ProcResult) -> str:
        """A terse one-line reason from a failed leaf's output tail (stderr first,
        else stdout), bounded so the reply/summary stays well under the budget."""
        tail = (proc.stderr.strip() or proc.stdout.strip() or "")[-400:]
        tail = " ".join(tail.split())
        return f"leaf_exit={proc.returncode}; {tail}".strip()

    # -- phase + conduct ------------------------------------------------------

    def _completed_producer_arid(self, node_key: str, phase: str,
                                 executor_arid: str | None) -> str | None:
        """The producing substep arid of an already-completed phase, read from its
        checkpointed step_result (recovers a repair target when resume skips it)."""
        if not executor_arid:
            return None
        sr = _read_json(
            self.repo_root / "workspace" / "orchestrations" / self.orchestration_id
            / "steps" / node_key_safe(node_key) / phase / executor_arid / "step_result.json")
        if not isinstance(sr, dict):
            return None
        subs = sr.get("substep_agent_run_ids")
        if isinstance(subs, list) and subs:
            return subs[0]
        return sr.get("executor_agent_run_id")

    def _ensure_fresh_producer_id(self, refs: NodeRefs, phase: str) -> None:
        """If a producing phase's output already exists (a prior attempt or a
        cross-phase reopen re-run), allocate a fresh producer id so the re-run
        writes to a new location instead of overwriting prior artifacts (which also
        trips create-form guarded writes). No-op on the first run of a phase."""
        date = _today()
        if phase == "compile":
            if (self.repo_root / refs.ir_ref).exists():
                safe, slug = node_key_safe(refs.node_key), _slug_of(refs.spec_id)
                seq = _next_seq(self.repo_root / "workspace" / "ir" / safe, f"{slug}_{date}")
                refs.ir_id = f"{slug}_{date}_{seq}"
                self.reserve_root(refs.node_key, "compile", refs.ir_id,
                                  self.orchestration_agent_run_id)
        elif phase == "generate":
            if (self.repo_root / refs.source_dir()).exists():
                seq = _next_seq(self.repo_root / refs.pipeline_ref / "source", f"src_{date}")
                refs.source_id = f"src_{date}_{seq}"
        elif phase == "build":
            if (self.repo_root / refs.binary_dir()).exists():
                seq = _next_seq(self.repo_root / refs.pipeline_ref / "binary", f"bin_{date}")
                refs.binary_id = f"bin_{date}_{seq}"
                refs.source_binary_id = refs.binary_id
        elif phase == "validate":
            if (self.repo_root / refs.pipeline_ref / "runs" / str(refs.run_id)).exists():
                seq = _next_seq(self.repo_root / refs.pipeline_ref / "runs", f"run_{date}")
                refs.run_id = f"run_{date}_{seq}"

    def _repair_payload(self, decision: RouteDecision, target_arid: str | None) -> dict[str, str]:
        return {
            "issue_severity": "major",
            "repair_strategy": decision.repair_strategy or "restart",
            "repair_target_agent_run_id": target_arid or "none",
            "repair_reason": decision.reason or "route_repair",
        }

    def run_phase(self, refs: NodeRefs, phase: str,
                  repair: dict[str, str] | None = None) -> PhaseOutcome:
        """Run one phase as a single attempt and write one terminal step_result.
        On a substep failure the phase's routing decision (cross-phase reopen,
        fail_closed, or escalate) is returned for conduct() to act on; in-place
        retry is intentionally not done here (its retry_decisions / effective-pass
        bookkeeping is error-prone) — a same-phase decision terminalizes via conduct.
        """
        node_key = refs.node_key
        if not hasattr(self, "_producer_arid"):
            self._producer_arid: dict[str, str] = {}
        completed = self.check_step_completed(node_key, phase)
        if completed is not None:
            # A resumed run skips this phase, but a later cross-phase repair may
            # still target its producer (repair_strategy=reuse). Recover the
            # producing substep arid from the checkpointed step_result so the
            # repair child has a prior run to diff against.
            producer = self._completed_producer_arid(
                node_key, phase, completed.get("agent_run_id"))
            if producer:
                self._producer_arid[phase] = producer
            return PhaseOutcome(phase, "pass", decision=RouteDecision("advance"),
                                skipped=True)
        self.workflow_launch_check(node_key, phase, child_agent_role(phase))
        self._ensure_fresh_producer_id(refs, phase)
        # Author/refresh the pipeline lineage.json host-side BEFORE the substeps run:
        # generate.verify's post_generate gate requires it, and the sandboxed leaf cannot
        # write it (pipeline-root file; see _write_lineage). Pipeline phases only —
        # compile writes under workspace/ir/, not the pipeline root.
        if phase in ("generate", "build", "validate"):
            self._write_lineage(refs)

        outcomes: list[SubstepOutcome] = []
        for i, substep in enumerate(SUBSTEPS[phase]):
            oc = self.run_substep(refs, phase, substep, repair=repair if i == 0 else None)
            outcomes.append(oc)
            if oc.status != "pass":
                break
        self._producer_arid[phase] = outcomes[0].agent_run_id

        if phase in SUBSTEP_AWARE_PHASES:
            executor = self.orchestration_agent_run_id
            substep_arids = [oc.agent_run_id for oc in outcomes]
        else:  # build: the single step child is the executor
            executor = outcomes[0].agent_run_id
            substep_arids = []
        failed = [oc.agent_run_id for oc in outcomes if oc.status != "pass"]
        status = "pass" if not failed and len(outcomes) == len(SUBSTEPS[phase]) else "fail"

        result: dict[str, Any] = {
            "status": status,
            "required_outputs": phase_required_outputs(refs, phase),
            "executor_agent_run_id": executor,
            "substep_agent_run_ids": substep_arids,
            "failed_substeps": failed,
            "retry_decisions": None,
            "validation_stage": PHASE_VALIDATION_STAGE[phase],
        }
        # Every terminal Validate step_result (pass OR fail) must carry a
        # launch_request_ref so the pre_phase_complete judge hook can resolve the
        # execution dir — use the last substep that ran (judge on pass; execute on
        # an execute-failure where the judge never ran).
        if phase == "validate" and outcomes:
            result["launch_request_ref"] = (
                f"workspace/orchestrations/{self.orchestration_id}"
                f"/launches/{outcomes[-1].agent_run_id}.request.json")
        self.write_step_result(node_key, phase, executor, result)
        if status == "pass":
            decision = RouteDecision("advance")
        else:
            # A nonzero leaf exit is an infra/transport failure (token limit, OOM,
            # transport) the decision tables cannot classify, and a diagnostician
            # leaf would likely hit the same limit — route straight to fail_closed
            # with the captured reason so the operator can --resume.
            transport = next((oc for oc in outcomes if oc.leaf_returncode != 0), None)
            if transport is not None:
                decision = RouteDecision(
                    "fail_closed",
                    reason=f"leaf_transport_error: leaf_exit={transport.leaf_returncode}")
            else:
                decision = self.classify_failure(refs, phase, outcomes)
        return PhaseOutcome(phase, status, substep_arids, failed, decision)

    def _gather_failure_context(self, refs: NodeRefs, phase: str) -> dict[str, Any]:
        """Collect the canonical status artifacts of a failed phase so the
        diagnostician reasons over their CONTENT (no filesystem read by the leaf,
        sidestepping the read-manifest guard for an unregistered reasoning agent)."""
        candidates = {
            "verdict.json": f"{refs.run_node_dir()}/verdict.json",
            "semantic_review.json": f"{refs.run_node_dir()}/semantic_review.json",
            "aggregate_verdict.json": f"{refs.run_node_dir()}/aggregate_verdict.json",
            "binary_meta.json": f"{refs.binary_dir()}/binary_meta.json",
            "ir_meta.json": f"{refs.ir_ref}/ir_meta.json",
            "source_meta.json": f"{refs.source_dir()}/source_meta.json",
        }
        ctx: dict[str, Any] = {}
        for name, rel in candidates.items():
            data = _read_json(self.repo_root / rel)
            if data is not None:
                ctx[name] = data
        return ctx

    def escalate(self, refs: NodeRefs, phase: str, outcome: PhaseOutcome) -> RouteDecision:
        """One-shot LLM diagnostician for a failure the decision tables cannot
        classify. Embeds the failure-artifact content in the prompt, spawns a
        read-only reasoning leaf, and parses its final JSON routing directive.
        An unparsable/invalid directive is conservatively terminal (fail_closed)."""
        context = self._gather_failure_context(refs, phase)
        prompt = _diagnosis_prompt(refs.node_key, phase, outcome.failed_substeps,
                                   context, self.workflow_mode)
        try:
            # The diagnostician has no record-launch profile (no child_arid); under
            # bwrap-enforced mode build a dedicated read-only profile (repo ro, no
            # write_roots) so it runs sandboxed instead of fail-closing. A read-only
            # leaf has nothing to attribute, so the FS-diff is trivially empty.
            profile = self._readonly_sandbox_profile() if self._bwrap_enabled() else None
            proc = self.spawn_leaf(
                prompt, self._child_env(self.orchestration_agent_run_id), profile=profile)
        except (SandboxEnforcementError, OSError) as exc:
            # The host cannot launch the sandboxed read-only diagnostician — either the
            # profile is unbuildable (SandboxEnforcementError) or the bwrap/backend
            # binary is missing (OSError/FileNotFoundError from subprocess.run, e.g. if
            # the startup preflight was bypassed). The diagnostician is a best-effort
            # recovery leaf, so treat an un-launchable diagnosis as conservatively
            # terminal — same posture as an unparsable directive — rather than crashing
            # the conductor or launching unconfined.
            self.emit("diagnose_launch_failed", phase=phase, error=str(exc)[:200])
            return RouteDecision("fail_closed", reason=f"{phase}_diagnose_sandbox_unavailable")
        self._persist_leaf_output(self.orchestration_agent_run_id, proc,
                                  prefix=f"diagnose.{phase}")
        decision = _parse_directive(proc.stdout)
        if decision is None:
            return RouteDecision("fail_closed", reason=f"{phase}_diagnose_unparsable")
        return decision

    def classify_failure(self, refs: NodeRefs, phase: str,
                         outcomes: list[SubstepOutcome]) -> RouteDecision:
        """Map a failed phase to a routing decision (M3 wires the full tables)."""
        if phase == "build":
            meta = _read_json(self.repo_root / refs.binary_dir() / "binary_meta.json") or {}
            return classify_build_failure(meta.get("failure_category"))
        if phase == "validate":
            verdict = _read_json(self.repo_root / refs.run_node_dir() / "verdict.json") or {}
            review = _read_json(self.repo_root / refs.run_node_dir() / "semantic_review.json") or {}
            findings = review.get("findings") or []
            attribution = findings[0].get("attribution") if findings and isinstance(findings[0], dict) else None
            return classify_validate_judge(verdict.get("failure_class"), attribution)
        # compile / generate: verify severity gate. A failed phase with no
        # recorded severity (e.g. the producing substep itself failed) is
        # unclassifiable -> escalate to the diagnostician rather than guessing.
        meta_path = (refs.ir_ref + "/ir_meta.json") if phase == "compile" else (refs.source_dir() + "/source_meta.json")
        meta = _read_json(self.repo_root / meta_path) or {}
        sev = meta.get("last_fail_severity") or meta.get("issue_severity")
        if not sev or sev == "none":
            return RouteDecision("escalate", reason=f"{phase}_fail_unclassified")
        return classify_verify_severity(sev, self.workflow_mode)

    def conduct(self, refs: NodeRefs, until_phase: str) -> str:
        """Drive the phases, acting on each phase's cross-phase routing decision:
        reopen an upstream (already-passed) phase, fail_closed, or escalate. The
        per-phase attempt budget bounds the reopen loop."""
        phases = phases_through(until_phase)
        attempts: dict[str, int] = {p: 0 for p in phases}
        pending_repair: dict[str, dict[str, str]] = {}
        idx = 0
        while idx < len(phases):
            phase = phases[idx]
            self.emit("phase_start", node_key=refs.node_key, phase=phase,
                      attempt=attempts[phase] + 1)
            phase_started = time.monotonic()
            try:
                outcome = self.run_phase(refs, phase, repair=pending_repair.pop(phase, None))
            except SandboxEnforcementError as exc:
                # bwrap-enforced mode + a leaf with no usable profile: terminalize as
                # fail_closed (the sandbox-enforcement failure path) rather than letting
                # it bubble to run_workflow's generic conductor_error/fail handler.
                self.set_status("fail_closed", reason_code="sandbox_enforcement_violation",
                                reason_detail=str(exc)[:200])
                return "fail_closed"
            if outcome.skipped:
                # Already checkpointed complete (resume): no body ran, so an
                # elapsed time would be misleading — report it as skipped instead.
                self.emit("phase_complete", node_key=refs.node_key, phase=phase,
                          result="skipped")
            else:
                self.emit("phase_complete", node_key=refs.node_key, phase=phase,
                          result=outcome.status,
                          elapsed_seconds=round(time.monotonic() - phase_started, 2))
            if outcome.status == "pass":
                idx += 1
                continue

            decision = outcome.decision or RouteDecision("escalate", reason="no_decision")
            if decision.action == "escalate":
                decision = self.escalate(refs, phase, outcome)
            if decision.action == "fail_closed":
                reason = decision.reason or ""
                # Map to an allowlisted FAIL_CLOSED_REASON_CODES value (the runtime
                # rejects any other code for fail_closed); the specific routing reason is
                # preserved in reason_detail.
                if reason.startswith("leaf_transport_error"):
                    reason_code = "leaf_transport_error"
                elif "sandbox" in reason:
                    # diagnostician could not be sandboxed under mandatory bwrap
                    reason_code = "sandbox_enforcement_violation"
                else:
                    reason_code = "conductor_phase_fail_closed"
                self.set_status("fail_closed", reason_code=reason_code,
                                reason_detail=reason[:200])
                return "fail_closed"

            target = decision.target_phase or phase
            if target not in phases:
                self.set_status("fail", reason_code=f"{phase}_fail",
                                reason_detail=f"route_target_out_of_scope:{target}")
                return "fail"
            attempts[target] += 1
            if attempts[target] > MAX_ATTEMPTS_PER_PHASE:
                self.set_status("fail_closed", reason_code="retry_budget_exhausted",
                                reason_detail=f"{target} exceeded {MAX_ATTEMPTS_PER_PHASE}")
                return "fail_closed"

            target_idx = phases.index(target)
            if target_idx >= idx:
                # same/downstream target reaching conduct means run_phase already
                # exhausted its in-place retries -> terminal.
                self.set_status("fail", reason_code=f"{phase}_fail",
                                reason_detail=(decision.reason or "")[:200])
                return "fail"

            # upstream target is checkpointed pass -> reopen it (and downstream).
            trigger = outcome.failed_substeps[-1] if outcome.failed_substeps else None
            if trigger is None:
                self.set_status("fail", reason_code=f"{phase}_fail",
                                reason_detail="reopen_no_trigger")
                return "fail"
            self.reopen_phase(refs.node_key, from_phase=target, trigger_arid=trigger,
                              reason=decision.reason or f"{phase}_reopen")
            if decision.repair_strategy and decision.repair_strategy not in ("none", None):
                pending_repair[target] = self._repair_payload(
                    decision, self._producer_arid.get(target, "none"))
            idx = target_idx
        self.set_status("pass")
        return "pass"


# --- phase deliverables (step_result.required_outputs) -------------------------
#
# validation_stage and required_outputs per phase, grounded in real step_result.json
# (see test fixtures). required_outputs is the phase deliverable subset, NOT the full
# union of substep allowed_output_paths.

PHASE_VALIDATION_STAGE: dict[str, str] = {
    "compile": "compile",
    "generate": "post_generate",
    "build": "post_build",
    "validate": "pre_judge",
}


def phase_required_outputs(refs: NodeRefs, phase: str) -> list[str]:
    if phase == "compile":
        return [f"{refs.ir_ref}/spec.ir.yaml", f"{refs.ir_ref}/ir_meta.json"]
    if phase == "generate":
        src = refs.source_dir()
        # lineage.json is authored host-side by the conductor (_write_lineage), not a leaf
        # output_ref, so it is NOT a step required_output (which must be covered by the
        # producer leaf's output_refs). post_generate still verifies it independently.
        return [
            f"{src}/src/{refs.spec_id}_model.f90",
            f"{src}/src/{refs.spec_id}_runner.f90",
            f"{src}/src/Makefile",
            f"{src}/source_meta.json",
        ]
    if phase == "build":
        bdir = refs.binary_dir()
        return [
            f"{bdir}/bin/{refs.spec_id}_runner",
            f"{bdir}/binary_meta.json",
            f"{refs.source_dir()}/src/mcp_command_log.jsonl",
        ]
    if phase == "validate":
        rundir = refs.run_node_dir()
        return [
            f"{rundir}/aggregate_verdict.json",
            f"{rundir}/verdict.json",
            f"{rundir}/summary.json",
            f"{rundir}/semantic_review.json",
            f"{rundir}/validate_meta.json",
        ]
    raise ValueError(f"unknown phase: {phase}")


# --- loop outcome types --------------------------------------------------------


@dataclass
class SubstepOutcome:
    agent_run_id: str
    status: str
    output_refs: list[str]
    # The leaf process exit code. Nonzero is an infra/transport failure (token
    # limit, OOM, transport) — not a content failure the decision tables can
    # classify — so run_phase routes it straight to fail_closed.
    leaf_returncode: int = 0


@dataclass
class PhaseOutcome:
    phase: str
    status: str
    substep_arids: list[str] = field(default_factory=list)
    failed_substeps: list[str] = field(default_factory=list)
    decision: RouteDecision | None = None
    # True when a --resume short-circuited the phase because it was already
    # checkpointed complete (no body re-run). Lets conduct() avoid reporting a
    # misleading ~0.0s elapsed time for a phase that did not actually execute.
    skipped: bool = False


# --- node resolution + id allocation + entrypoint ------------------------------


def _slug_of(spec_id: str) -> str:
    """Deterministic id slug: lower-case spec_id, non-alnum runs -> '-'. Matches
    the reserve-phase-root canonical-format regex (the specific value is free)."""
    return re.sub(r"[^a-z0-9]+", "-", spec_id.lower()).strip("-") or "node"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _next_seq(parent: Path, prefix: str) -> str:
    """Next 3-digit sequence for <prefix>_<NNN> directories under parent."""
    mx = 0
    if parent.is_dir():
        pat = re.compile(re.escape(prefix) + r"_(\d{3})$")
        for d in parent.iterdir():
            m = pat.match(d.name)
            if m:
                mx = max(mx, int(m.group(1)))
    return f"{mx + 1:03d}"


_SPEC_REF_FILE_NAMES = frozenset({"controlled_spec.md", "tests.md", "deps.yaml"})


def resolve_node(repo_root: Path, spec_ref: str) -> tuple[str, str]:
    """Resolve (node_key, spec_path) from spec_ref via spec_catalog.yaml.

    Accepts the same spec_ref forms as run_workflow: a spec directory OR a
    file-style ref (controlled_spec.md / tests.md / deps.yaml) under it — the
    latter is normalized to its parent directory before the catalog lookup.
    """
    ref = Path(spec_ref.strip().rstrip("/"))
    spec_dir = ref.parent if ref.name in _SPEC_REF_FILE_NAMES else ref
    spec_id = spec_dir.name
    catalog = _read_yaml(repo_root / "spec" / "registry" / "spec_catalog.yaml") or {}
    for entry in catalog.get("specs") or []:
        if isinstance(entry, dict) and entry.get("spec_id") == spec_id:
            kind = entry["spec_kind"]
            version = entry["spec_version"]
            spec_path = str(Path(entry["controlled_spec_path"]).parent)
            return f"{kind}/{spec_id}@{version}", spec_path
    raise ValueError(
        f"spec_id not found in spec_catalog.yaml: {spec_id} (from spec_ref {spec_ref})")


def resume_node_refs(conductor: "Conductor", node_key: str, spec_path: str) -> NodeRefs:
    """Reconstruct NodeRefs from the RESUMED ORCHESTRATION's own records (NOT the
    global-latest workspace dirs, which could belong to a different/newer run).
    ir_id/pipeline_id come from this orchestration's reservations; source/binary/run
    come from its checkpoint's completed-step outputs (fresh ids are allocated for a
    producing phase that has not run yet, which the resumed run then creates)."""
    safe = node_key_safe(node_key)
    orch_dir = (conductor.repo_root / "workspace" / "orchestrations"
                / conductor.orchestration_id)
    res_dir = orch_dir / "reservations" / safe
    ir_id = (_read_json(res_dir / "compile.json") or {}).get("reserved_ir_id")
    pipeline_id = (_read_json(res_dir / "generate.json") or {}).get("reserved_ir_id")
    if not ir_id or not pipeline_id:
        raise ValueError(
            f"conductor resume: missing ir/pipeline reservation for {node_key} in "
            f"{conductor.orchestration_id}")

    source_id = binary_id = run_id = None
    checkpoint = _read_json(orch_dir / "orchestration_checkpoint.json") or {}
    for entry in checkpoint.get("completed_steps", []):
        if not isinstance(entry, dict) or entry.get("node_key") != node_key:
            continue
        for ref in entry.get("output_refs", []):
            if not isinstance(ref, str):
                continue
            if "/source/" in ref:
                source_id = ref.split("/source/")[1].split("/")[0]
            if "/binary/" in ref:
                binary_id = ref.split("/binary/")[1].split("/")[0]
            if "/runs/" in ref:
                run_id = ref.split("/runs/")[1].split("/")[0]
    date = _today()
    source_id = source_id or f"src_{date}_001"
    binary_id = binary_id or f"bin_{date}_001"
    run_id = run_id or f"run_{date}_001"
    return NodeRefs(
        node_key=node_key, spec_path=spec_path,
        ir_id=ir_id, pipeline_id=pipeline_id,
        source_id=source_id, binary_id=binary_id, run_id=run_id,
        source_binary_id=binary_id,
    )


def prepare_node(conductor: "Conductor", node_key: str, spec_path: str) -> NodeRefs:
    """Allocate canonical ids (ir/pipeline/source/binary/run) and reserve the
    ir_id + pipeline_id roots before the Compile phase runs."""
    safe = node_key_safe(node_key)
    slug = _slug_of(spec_id_of(node_key))
    date = _today()
    ir_id = f"{slug}_{date}_{_next_seq(conductor.repo_root / 'workspace' / 'ir' / safe, f'{slug}_{date}')}"
    pipeline_id = (
        f"{slug}_{date}_"
        f"{_next_seq(conductor.repo_root / 'workspace' / 'pipelines' / safe, f'{slug}_{date}')}"
    )
    refs = NodeRefs(
        node_key=node_key, spec_path=spec_path,
        ir_id=ir_id, pipeline_id=pipeline_id,
        source_id=f"src_{date}_001", binary_id=f"bin_{date}_001",
        run_id=f"run_{date}_001", source_binary_id=f"bin_{date}_001",
    )
    by = conductor.orchestration_agent_run_id
    conductor.reserve_root(node_key, "compile", ir_id, by)
    conductor.reserve_root(node_key, "generate", pipeline_id, by)
    return refs


def run_conductor(*, repo_root: Path | str, orchestration_id: str,
                  orchestration_agent_run_id: str, spec_ref: str,
                  source_dependency_ref: str, until_phase: str, backend: str,
                  agent_model: str, workflow_mode: str, env: dict[str, str],
                  llm_command: str = "", resume: bool = False) -> str:
    """Conductor entrypoint used by run_workflow.py (the only orchestration driver).
    Resolves the node, allocates+reserves ids (or, on resume, reuses the checkpointed
    ids), and runs the deterministic phase loop. Returns the terminal orchestration
    status (pass | fail | fail_closed)."""
    root = Path(repo_root)
    node_key, spec_path = resolve_node(root, spec_ref)
    conductor = Conductor(
        repo_root=root, orchestration_id=orchestration_id,
        orchestration_agent_run_id=orchestration_agent_run_id,
        backend=backend, env=env,
        agent_model=agent_model or "claude-opus-4-8", workflow_mode=workflow_mode,
        llm_command=llm_command,
    )
    refs = (resume_node_refs(conductor, node_key, spec_path) if resume
            else prepare_node(conductor, node_key, spec_path))
    return conductor.conduct(refs, until_phase.lower())

